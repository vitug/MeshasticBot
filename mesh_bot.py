import json
import logging
import socket  # Для типов сетевых ошибок
from meshtastic.tcp_interface import TCPInterface
from meshtastic.protobuf import config_pb2, channel_pb2
from pubsub import pub
import time
import telebot
from telebot import types
import threading
import os
from collections import OrderedDict
import re
import requests

# Настройка логирования в файл
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mesh_bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

try:
    from sdnotify import SystemdNotifier
    HAS_SYSTEMD = True
except ImportError:
    SystemdNotifier = None
    HAS_SYSTEMD = False
    logger.warning("Модуль sdnotify не найден — watchdog пинги отключены")
    
# Подавляем DEBUG-логи от библиотек
logging.getLogger('meshtastic').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('telebot').setLevel(logging.WARNING)

# Глобальный маппинг для reply с потокобезопасностью
msg_mapping = OrderedDict()
msg_mapping_lock = threading.Lock()
MAX_MAPPING_SIZE = 1000

# Константы для разбивки сообщений
MAX_BYTES_PER_MESSAGE = 200
MESSAGE_SPLIT_DELAY = 1.5

# Константы для автопереподключения (улучшено)
RECONNECT_INTERVAL = 30  # Увеличено для снижения спама
CONNECTION_CHECK_INTERVAL = 10  # Чуть чаще проверять


class MeshTelegramBot:
    """
    Основной класс для бота Meshtastic с интеграцией Telegram.
    Разделяет логику на сервисные методы, Telegram-обработчики и Meshtastic-обработчики.
    """

    def __init__(self):
        self.interface = None
        self.bot = None
        self.config = None
        self.keywords = []
        self.private_node_names = []
        self.node_map = {}
        self.general_suffix = ''
        self.private_suffix = ''
        self.telegram_token = None
        self.telegram_chat_id = None
        self.telegram_timeout = 60  # Таймаут по умолчанию 60 секунд
        self.default_channel = None
        self.node_long_name = 'Node'  # Храним long_name в классе (fallback)
        self.config_mtime = 0
        self.last_node_scan = 0
        self.node_scan_interval = 30
        self.messages_dir = 'messages_logs'
        self.pending_messages = {}  # Хранение ожидающих подтверждения сообщений {chat_id: dict}
        
        # Флаги для автопереподключения
        self.is_connected = False
        self.last_reconnect_attempt = time.time()  # Фикс: Инициализируем текущим временем, чтобы избежать overflow на старте
        self.last_connection_check = 0
        self.reconnect_in_progress = False
        self.manual_disconnect = False
        
        # Параметры фильтрации автоответов по хопам для общего канала
        self.hop_filter_min = None
        self.hop_filter_max = None
        
        self._load_config()
        self._init_messages_dir()

        # Инициализация компонентов
        self._init_meshtastic()
        self._init_telegram()
        self._setup_subscriptions()
        
        # Watchdog для systemd
        self.last_watchdog_ping = time.time()
        self.watchdog_interval = 60
        self.notifier = SystemdNotifier() if HAS_SYSTEMD else None
        self.has_systemd = HAS_SYSTEMD       

    # ==================== СЕРВИСНЫЕ МЕТОДЫ ====================
    
    def _load_config(self):
        """Сервисный метод: загрузка конфигурации из config.json."""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            self.ip = self.config['ip']
            self.port = self.config['port']
            self.keywords = [kw.lower() for kw in self.config['keywords']]
            self.private_node_names = [name.lower() for name in self.config.get('private_node_names', [])]
            self.general_suffix = self.config.get('general_suffix', '')
            self.private_suffix = self.config.get('private_suffix', '')
            self.telegram_token = self.config.get('telegram_token')
            self.telegram_chat_id = str(self.config.get('telegram_chat_id', '')) if self.config.get('telegram_chat_id') else None
            self.telegram_timeout = self.config.get('telegram_timeout', 60)  # Загружаем таймаут, по умолчанию 60
            self.default_channel = self.config.get('default_channel')
            self.node_long_name = self.config.get('node_long_name', 'Node')  # Загружаем long_name из config
            hop_interval = self.config.get('hop_filter_interval', [None, None])
            self.hop_filter_min = hop_interval[0] if isinstance(hop_interval, list) and len(hop_interval) >= 2 else None
            self.hop_filter_max = hop_interval[1] if isinstance(hop_interval, list) and len(hop_interval) >= 2 else None
            self.config_mtime = os.path.getmtime('config.json')
            
            if not self.telegram_token:
                logger.warning("Telegram токен не найден, интеграция отключена")
                self.telegram_chat_id = None
            elif not self.telegram_chat_id:
                logger.info("Telegram chat_id не указан в config.json. Бот будет ждать первого сообщения.")
            
            logger.info(f"Конфигурация загружена: IP={self.ip}, Port={self.port}, Keywords={self.keywords}, "
                        f"Private nodes={self.private_node_names}, General suffix='{self.general_suffix}', "
                        f"Private suffix='{self.private_suffix}', Node long_name='{self.node_long_name}', "
                        f"Hop filter: [{self.hop_filter_min}, {self.hop_filter_max}], "
                        f"Telegram: {'enabled' if self.telegram_token else 'disabled'}, Timeout={self.telegram_timeout}s")
        except FileNotFoundError:
            logger.error("Файл config.json не найден!")
            exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга config.json: {e}")
            exit(1)
        except KeyError as e:
            logger.error(f"Отсутствует ключ в config.json: {e}")
            exit(1)

    def _init_messages_dir(self):
        """Сервисный метод: создание папки для логов сообщений."""
        os.makedirs(self.messages_dir, exist_ok=True)
        logger.info(f"Папка для логов сообщений: {self.messages_dir}")

    def _log_message_to_file(self, message_type, short_name, original_text, rssi='unknown', snr='unknown', hop_count=None, hop_start=None, hop_limit=None, via_short_name=None, is_private=False, to_id=None, is_outgoing=False, is_bot_reply=False):
        """
        Сервисный метод: запись сообщения в соответствующий файл.
        
        Args:
            message_type: 'general', 'private', 'private_group'
            short_name: имя отправителя/получателя
            original_text: текст сообщения
            rssi: уровень сигнала
            snr: отношение сигнал/шум
            hop_count: количество хопов (вычисленное)
            hop_start: исходный лимит хопов
            hop_limit: оставшийся лимит хопов
            via_short_name: короткое имя via-ноды (промежуточной)
            is_private: приватное ли сообщение
            to_id: ID получателя (для исходящих)
            is_outgoing: исходящее ли сообщение (из Telegram)
            is_bot_reply: автоответ ли это
        """
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        
        # Формируем префикс для типа сообщения
        if is_bot_reply:
            prefix = "[BOT]"
        elif is_outgoing:
            prefix = "[OUT]"
        else:
            prefix = "[IN]"
        
        # Информация о сигнале (только для входящих)
        signal_info = ""
        if not is_outgoing and not is_bot_reply:
            if snr != 'unknown' and rssi != 'unknown':
                signal_info = f" (SNR: {snr}, RSSI: {rssi})"
            if via_short_name:
                via_info = f" via {via_short_name}"
                signal_info = via_info if not signal_info else signal_info + via_info
            if hop_count and hop_count > 0:
                hops_info = f" ({hop_count} hops)"
                signal_info = hops_info if not signal_info else signal_info + hops_info
            if hop_start is not None and hop_limit is not None:
                hops_raw_info = f" (hop_start: {hop_start}, hop_limit: {hop_limit})"
                signal_info = hops_raw_info if not signal_info else signal_info + hops_raw_info
        
        # Информация о получателе (для исходящих и автоответов)
        if is_outgoing or is_bot_reply:
            if to_id and to_id != 0xffffffff:
                direction_info = f" -> {to_id}"
            elif short_name and is_bot_reply:
                direction_info = f" -> {short_name}"
            else:
                direction_info = " -> broadcast"
        else:
            # Для входящих сообщений
            if to_id and to_id != 0xffffffff:
                direction_info = f" -> {to_id}"
            else:
                direction_info = ""
        
        # Формируем строку лога
        if is_outgoing or is_bot_reply:
            log_line = f"{timestamp} {prefix}{direction_info}{signal_info}: {original_text}\n"
        else:
            log_line = f"{timestamp} {prefix} [{short_name}]{direction_info}{signal_info}: {original_text}\n"
        
        # Определяем файл для записи
        if message_type == 'general':
            filename = os.path.join(self.messages_dir, 'general_messages.txt')
        elif message_type == 'private':
            filename = os.path.join(self.messages_dir, 'private_messages.txt')
        else:
            filename = os.path.join(self.messages_dir, 'private_group_messages.txt')
        
        try:
            with open(filename, 'a', encoding='utf-8') as f:
                f.write(log_line)
            logger.debug(f"Сообщение записано в {filename}: {log_line.strip()}")
        except Exception as e:
            logger.error(f"Ошибка записи в файл {filename}: {e}")

    def _reload_config(self):
        """Сервисный метод: перезагрузка конфигурации при изменении файла."""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                new_config = json.load(f)
            
            self.keywords = [kw.lower() for kw in new_config['keywords']]
            self.private_node_names = [name.lower() for name in new_config.get('private_node_names', [])]
            self.general_suffix = new_config.get('general_suffix', '')
            self.private_suffix = new_config.get('private_suffix', '')
            self.default_channel = new_config.get('default_channel')
            self.node_long_name = new_config.get('node_long_name', 'Node')  # Перезагружаем long_name
            self.telegram_timeout = new_config.get('telegram_timeout', 60)  # Перезагружаем таймаут
            hop_interval = new_config.get('hop_filter_interval', [None, None])
            self.hop_filter_min = hop_interval[0] if isinstance(hop_interval, list) and len(hop_interval) >= 2 else None
            self.hop_filter_max = hop_interval[1] if isinstance(hop_interval, list) and len(hop_interval) >= 2 else None
            
            new_telegram_token = new_config.get('telegram_token')
            new_telegram_chat_id = str(new_config.get('telegram_chat_id', '')) if new_config.get('telegram_chat_id') else None
            
            if new_telegram_token != self.telegram_token or new_telegram_chat_id != self.telegram_chat_id:
                logger.warning("Изменены Telegram настройки (токен/chat_id). Перезапустите приложение для применения.")
                self.telegram_token = new_telegram_token
                self.telegram_chat_id = new_telegram_chat_id
            
            self.config_mtime = os.path.getmtime('config.json')
            logger.info("Конфигурация перезагружена успешно (keywords, suffixes, private_nodes, node_long_name, telegram_timeout, hop_filter_interval обновлены)")
        except Exception as e:
            logger.error(f"Ошибка перезагрузки config.json: {e}")

    def _update_config_and_save(self, ip=None, port=None):
        """Сервисный метод: обновление и сохранение IP/Port в config.json."""
        if ip is not None:
            self.config['ip'] = ip
            self.ip = ip
        if port is not None:
            self.config['port'] = port
            self.port = port
        try:
            with open('config.json', 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logger.info(f"Config обновлён: IP={self.ip}, Port={self.port}")
        except Exception as e:
            logger.error(f"Ошибка сохранения config: {e}")

    def _save_chat_id_to_config(self, chat_id):
        """Сервисный метод: сохранение chat_id в config.json после первого сообщения."""
        if self.config is not None:
            self.config['telegram_chat_id'] = str(chat_id)
            try:
                with open('config.json', 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                logger.info(f"chat_id {chat_id} сохранён в config.json")
                print(f"chat_id {chat_id} сохранён в config.json. Перезапустите бота для полной активации.")
                self.telegram_chat_id = str(chat_id)
                self.config_mtime = os.path.getmtime('config.json')
            except Exception as e:
                logger.error(f"Ошибка сохранения chat_id в config: {e}")
                print(f"chat_id {chat_id} определён, но не сохранён в config. Добавьте вручную: 'telegram_chat_id': '{chat_id}'")

    def _save_node_long_name_to_config(self, new_name):
        """Сервисный метод: сохранение node_long_name в config.json."""
        if self.config is not None:
            self.config['node_long_name'] = new_name
            self.node_long_name = new_name
            try:
                with open('config.json', 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                logger.info(f"Node long_name '{new_name}' сохранён в config.json")
            except Exception as e:
                logger.error(f"Ошибка сохранения node_long_name в config: {e}")

    def _calculate_text_bytes(self, text):
        """Сервисный метод: подсчёт байт в тексте."""
        byte_count = 0
        for char in text:
            if ord(char) < 128:
                byte_count += 1
            else:
                byte_count += 2
        return byte_count

    def _split_text_by_bytes(self, text, max_bytes=MAX_BYTES_PER_MESSAGE):
        """Сервисный метод: разбивка текста на части с учётом байтового размера."""
        if not text:
            return []
        
        total_bytes = self._calculate_text_bytes(text)
        if total_bytes <= max_bytes:
            return [text]
        
        words = text.split()
        parts = []
        current_part = []
        current_bytes = 0
        
        marker_reserve = 10
        effective_max = max_bytes - marker_reserve
        
        for word in words:
            word_bytes = self._calculate_text_bytes(word)
            space_bytes = 1 if current_part else 0
            
            if current_bytes + word_bytes + space_bytes <= effective_max:
                current_part.append(word)
                current_bytes += word_bytes + space_bytes
            else:
                if current_part:
                    parts.append(' '.join(current_part))
                    current_part = [word]
                    current_bytes = word_bytes
                else:
                    char_part = []
                    char_bytes = 0
                    for char in word:
                        char_byte = 1 if ord(char) < 128 else 2
                        if char_bytes + char_byte <= effective_max:
                            char_part.append(char)
                            char_bytes += char_byte
                        else:
                            if char_part:
                                parts.append(''.join(char_part))
                            char_part = [char]
                            char_bytes = char_byte
                    if char_part:
                        current_part = [''.join(char_part)]
                        current_bytes = self._calculate_text_bytes(current_part[0])
        
        if current_part:
            parts.append(' '.join(current_part))
        
        if len(parts) > 1:
            total_parts = len(parts)
            marked_parts = []
            for i, part in enumerate(parts, 1):
                marked_part = f"{part} [{i}/{total_parts}]"
                marked_parts.append(marked_part)
            return marked_parts
        
        return parts

    def _get_node_info(self, from_num, interface):
        """Сервисный метод: получение информации о ноде отправителя."""
        node = interface.nodesByNum.get(from_num)
        if node:
            short_name = node.get('user', {}).get('shortName', 'Unknown')
            node_id = node.get('user', {}).get('id')
        else:
            short_name = 'Unknown'
            node_id = None
            logger.warning(f"Инфо ноды {from_num} не найдена")
        
        logger.debug(f"Инфо ноды {from_num}: short_name={short_name}, node_id={node_id}")
        return short_name, node_id

    def _scan_nodes(self):
        """Сервисный метод: сканирование всех известных нод и обновление node_map."""
        if not self.interface or not self.is_connected:
            return
        
        try:
            updated = False
            for num, node in self.interface.nodesByNum.items():
                if 'user' in node:
                    short_name = node.get('user', {}).get('shortName', '').lower()
                    node_id = node.get('user', {}).get('id')
                    if short_name and node_id:
                        if short_name not in self.node_map or self.node_map[short_name] != node_id:
                            self.node_map[short_name] = node_id
                            updated = True
            if updated:
                logger.info(f"Node_map обновлён: {len(self.node_map)} нод")
        except Exception as e:
            logger.error(f"Ошибка сканирования нод: {e}")
            self._mark_disconnected()

    def _get_channel_name(self, packet):
        """Сервисный метод: получение имени канала из пакета."""
        channel_info = packet.get('decoded', {}).get('channel', {})
        channel_name = channel_info.get('name', None) if isinstance(channel_info, dict) else None
        logger.debug(f"Канал: name={channel_name}")
        return channel_name

    def _is_broadcast(self, to_id):
        """Сервисный метод: проверка, является ли сообщение broadcast."""
        is_broadcast = (to_id == 0xffffffff)
        is_private = not is_broadcast
        logger.debug(f"Сообщение: {'broadcast (general)' if is_broadcast else 'unicast (private)'}")
        return is_broadcast, is_private

    def _get_send_kwargs(self, reply_id, channel_name):
        """Сервисный метод: базовые kwargs для sendText."""
        send_kwargs = {'replyId': reply_id} if reply_id else {}
        if channel_name:
            send_kwargs['channel'] = channel_name
        return send_kwargs

    def _get_signal_reply(self, short_name, rssi, snr, suffix, via_short_name=None):
        """Сервисный метод: формирование ответа с сигналом (RSSI/SNR)."""
        reply = f"{short_name} SNR: {snr}, RSSI: {rssi}"
        if via_short_name:
            reply += f" via {via_short_name}"
        reply += f" {suffix}"
        return reply

    def _get_hops_reply(self, short_name, hop_count, suffix, via_short_name=None):
        """Сервисный метод: формирование ответа с количеством хопов."""
        reply = f"{short_name} {hop_count} hops"
        if via_short_name:
            reply += f" via {via_short_name}"
        reply += f" {suffix}"
        return reply

    def _get_direct_reply(self, short_name, snr, rssi, suffix):
        """Сервисный метод: формирование ответа для прямого приема (сигнал)."""
        return f"{short_name} SNR: {snr}, RSSI: {rssi} {suffix}"

    def _check_connection(self):
        """Сервисный метод: проверка состояния соединения с Meshtastic (улучшено)."""
        if not self.interface:
            return False
        
        try:
            # Пробуем heartbeat для провокации ошибки, если сокет мёртв
            self.interface.sendHeartbeat()
            _ = self.interface.nodesByNum # Дополнительная проверка
            return True
        except (socket.error, BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning(f"Сетевая ошибка в check_connection: {e} (тип: {type(e).__name__})")
            return False
        except Exception as e:
            logger.warning(f"Проверка соединения не удалась: {e}")
            return False

    def _mark_disconnected(self):
        """Сервисный метод: пометить соединение как разорванное."""
        if self.is_connected:
            self.is_connected = False
            logger.warning("⚠️ Соединение с Meshtastic потеряно")
            # Не выводим сообщение о попытке переподключения, если было ручное отключение
            if not self.manual_disconnect:
                print("⚠️ Соединение с Meshtastic потеряно. Попытка переподключения...")

    def _send_to_meshtastic(self, text, send_kwargs, node_id=None):
        """Сервисный метод: отправка текста в Meshtastic (unicast или broadcast)."""
        if not self.interface or not self.is_connected:
            logger.error("Нет подключения к Meshtastic для отправки сообщения")
            return None
        
        try:
            if node_id:
                kwargs = {**send_kwargs, 'destinationId': node_id}
                self.interface.sendText(text, **kwargs)
                send_type = "unicast"
            else:
                self.interface.sendText(text, **send_kwargs)
                send_type = "broadcast"
            logger.info(f"Отправлен текст в Meshtastic: '{text}' ({send_type}) -> {node_id or 'broadcast'}")
            return send_type
        except Exception as e:
            logger.error(f"Ошибка отправки в Meshtastic: {e}")
            self._mark_disconnected()
            return None

    def _send_multipart_to_meshtastic(self, text, send_kwargs, node_id=None, log_to_file=False):
        """
        Сервисный метод: отправка текста в Meshtastic с разбивкой на части.
        
        Args:
            text: исходный текст
            send_kwargs: kwargs для sendText
            node_id: ID ноды для unicast (None для broadcast)
            log_to_file: записывать ли в файл (для сообщений из Telegram)
            
        Returns:
            tuple: (успешно, количество частей)
        """
        parts = self._split_text_by_bytes(text, MAX_BYTES_PER_MESSAGE)
        
        if not parts:
            logger.warning("Текст пуст после разбивки")
            return False, 0
        
        total_parts = len(parts)
        logger.info(f"Текст разбит на {total_parts} частей (max {MAX_BYTES_PER_MESSAGE} байт каждая)")
        
        success_count = 0
        for i, part in enumerate(parts):
            part_bytes = self._calculate_text_bytes(part)
            logger.debug(f"Отправка части {i+1}/{total_parts}: {part_bytes} байт, текст: '{part[:50]}...'")
            
            if i == 0:
                current_kwargs = send_kwargs.copy()
            else:
                current_kwargs = {k: v for k, v in send_kwargs.items() if k != 'replyId'}
                if 'channel' in send_kwargs:
                    current_kwargs['channel'] = send_kwargs['channel']
            
            send_type = self._send_to_meshtastic(part, current_kwargs, node_id)
            
            if send_type:
                success_count += 1
                
                # Логируем каждую часть в файл, если требуется
                if log_to_file:
                    if node_id:
                        # Личное сообщение
                        self._log_message_to_file(
                            'private',
                            None, # short_name не нужен для исходящих
                            part,
                            to_id=node_id,
                            is_outgoing=True
                        )
                    else:
                        # Общее сообщение
                        self._log_message_to_file(
                            'general',
                            None,
                            part,
                            is_outgoing=True
                        )
                
                if i < total_parts - 1:
                    logger.debug(f"Задержка {MESSAGE_SPLIT_DELAY} сек перед следующей частью")
                    time.sleep(MESSAGE_SPLIT_DELAY)
            else:
                logger.error(f"Ошибка отправки части {i+1}/{total_parts}")
                break
        
        return success_count == total_parts, total_parts

    def _find_reply_info(self, telegram_parent_id):
        """Сервисный метод: поиск meshtastic_reply_id, node_id и is_private по telegram_id из маппинга."""
        with msg_mapping_lock:
            for mid, info in msg_mapping.items():
                if info['telegram_msg_id'] == telegram_parent_id:
                    return mid, info['node_id'], info['is_private']
        return None, None, False

    def _get_current_node_info(self):
        """Вспомогательный метод: получение текущего long_name, modem_preset и channel_num."""
        if not self.interface or not self.is_connected:
            return self.node_long_name, "Не подключено", "Не подключено" # Fallback на config для long_name
        try:
            # long_name всегда из config
            long_name = self.node_long_name
            logger.debug(f"Node long_name loaded from config: '{long_name}'")
            # Получаем modem_preset (LoRa config)
            local_node = self.interface.localNode
            if local_node and local_node.localConfig and local_node.localConfig.lora:
                modem_preset = local_node.localConfig.lora.modem_preset
                # Маппинг enum на имя пресета
                preset_map = {
                    config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST: "Long Fast",
                    config_pb2.Config.LoRaConfig.ModemPreset.LONG_SLOW: "Long Slow",
                    config_pb2.Config.LoRaConfig.ModemPreset.VERY_LONG_SLOW: "Very Long Slow",
                    config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW: "Medium Slow",
                    config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST: "Medium Fast",
                    config_pb2.Config.LoRaConfig.ModemPreset.SHORT_SLOW: "Short Slow",
                    config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST: "Short Fast",
                    config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE: "Long Moderate",
                    config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO: "Short Turbo",
                }
                preset_name = preset_map.get(modem_preset, f"Unknown ({modem_preset})")
                
                # Получаем channel_num (ранее freq_slot)
                channel_num = local_node.localConfig.lora.channel_num
                channel_num_name = f"{channel_num}" if channel_num is not None else "Не загружено"
            else:
                preset_name = "Не загружено (запросите /set_preset для загрузки)"
                channel_num_name = "Не загружено"
            return long_name, preset_name, channel_num_name
        except Exception as e:
            logger.error(f"Ошибка получения node info: {e}")
            return self.node_long_name, "Ошибка", "Ошибка" # Fallback на config

    def _update_node_name_with_preset(self, preset_abbr, slot):
        """
        Обновление longName ноды с добавлением пресета.
        ShortName не изменяется (остается как есть, обычно 4 символа).
        Базовое имя берём из config.
        
        Args:
            preset_abbr: сокращение пресета (LF, MS, SF, VLS, LS)
            slot: номер слота
            
        Returns:
            tuple: (success, old_name, new_name)
        """
        try:
            # Получаем локальную ноду (для setOwner)
            local_node = self.interface.localNode
            if not local_node:
                logger.warning("localNode не доступен, пропускаем обновление имени")
                return False, None, None
            # Базовое имя из config (вместо чтения из Meshtastic)
            current_long_name = self.node_long_name
            logger.debug(f"Текущее longName из config: '{current_long_name}', shortName не изменяется")
            
            # Паттерн для поиска старого пресета в скобках в конце имени
            # Ищет паттерн типа (LF0), (MS1), (VLS2) и т.д. в конце строки
            preset_pattern = r'\s*\([A-Z]{1,3}\d\)\s*$'
            
            # Удаляем старый пресет из longName, если есть
            base_long_name = re.sub(preset_pattern, '', current_long_name).strip()
            
            # Формируем новое longName с пресетом
            new_preset_tag = f"({preset_abbr}{slot})"
            new_long_name = f"{base_long_name} {new_preset_tag}"
            
            # Ограничение: longName до 40 символов
            if len(new_long_name) > 40:
                # Обрезаем базовое имя
                max_base_len = 40 - len(new_preset_tag) - 1 # -1 для пробела
                base_long_name = base_long_name[:max_base_len]
                new_long_name = f"{base_long_name} {new_preset_tag}"
            
            logger.info(f"Обновление longName ноды: '{current_long_name}' -> '{new_long_name}' (из config + суффикс)")
            
            # Сохраняем обновлённое имя в config
            self._save_node_long_name_to_config(new_long_name)
            
            # Обновляем через setOwner (отправляет admin-сообщение, shortName НЕ трогаем)
            local_node.setOwner(long_name=new_long_name)
            logger.info(f"LongName ноды обновлено через setOwner: {new_long_name}")
            return True, current_long_name, new_long_name
            
        except Exception as e:
            logger.error(f"Ошибка обновления longName ноды: {e}", exc_info=True)
            return False, None, None

    def _save_private_node_to_config(self, node_name):
        """Сервисный метод: добавление ноды в private_node_names в config.json."""
        if self.config is not None:
            node_name_lower = node_name.lower()
            
            # Проверяем, нет ли уже в списке
            if node_name_lower in self.private_node_names:
                logger.debug(f"Нода '{node_name_lower}' уже в private_node_names")
                return False
            
            # Добавляем в память
            self.private_node_names.append(node_name_lower)
            
            # Обновляем config
            if 'private_node_names' not in self.config:
                self.config['private_node_names'] = []
            
            self.config['private_node_names'].append(node_name_lower)
            
            try:
                with open('config.json', 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4, ensure_ascii=False)
                logger.info(f"Нода '{node_name_lower}' добавлена в private_node_names и сохранена в config.json")
                self.config_mtime = os.path.getmtime('config.json')
                return True
            except Exception as e:
                logger.error(f"Ошибка сохранения private_node_names в config: {e}")
                # Откатываем изменения в памяти при ошибке
                self.private_node_names.remove(node_name_lower)
                if node_name_lower in self.config.get('private_node_names', []):
                    self.config['private_node_names'].remove(node_name_lower)
                return False
        return False
        
    def _disconnect_meshtastic(self):
        """Сервисный метод: отключение от Meshtastic."""
        if self.interface:
            try:
                pub.unsubscribe(self._on_receive, "meshtastic.receive")
                self.interface.close()
                self.interface = None
                self.is_connected = False
                logger.info("Отключение от Meshtastic выполнено")
            except Exception as e:
                logger.error(f"Ошибка при отключении от Meshtastic: {e}")
                self.interface = None
                self.is_connected = False
        else:
            logger.info("Уже отключено от Meshtastic")

    def _connect_meshtastic(self, ip, port):
        """Сервисный метод: подключение к Meshtastic."""
        try:
            self._disconnect_meshtastic()
            logger.info(f"Подключение к Meshtastic: {ip}:{port}")
            self.interface = TCPInterface(hostname=ip, portNumber=port, debugOut=None)
            self._setup_subscriptions()
            
            # Пауза для инициализации и автоматической загрузки нод
            time.sleep(2)
            
            self.is_connected = True
            logger.info(f"✓ Подключение к {ip}:{port} успешно!")
            print(f"✓ Подключение к {ip}:{port} успешно!")
            
            # Ожидание автоматической загрузки нод (до 30 сек max)
            wait_start = time.time()
            while time.time() - wait_start < 30:
                if self.interface.nodesByNum: # Если хотя бы одна нода загружена
                    logger.info(f"Ноды загружены автоматически: {len(self.interface.nodesByNum)} шт.")
                    self._scan_nodes() # Обновляем node_map сразу
                    break
                logger.debug("Ожидание автоматической загрузки нод...")
                time.sleep(2)
            else:
                logger.warning("Таймаут ожидания загрузки нод. Автоответ на private может не работать сразу.")
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка подключения к Meshtastic {ip}:{port}: {e}", exc_info=True)
            self.interface = None
            self.is_connected = False
            return False

    def _attempt_reconnect(self):
        """Сервисный метод: попытка переподключения к Meshtastic (с экспоненциальным backoff, фикс overflow)."""
        # не переподключаться, если было ручное отключение
        if self.manual_disconnect:
            logger.debug("Автопереподключение отключено (manual_disconnect=True)")
            return
            
        if self.reconnect_in_progress:
            return
        
        now = time.time()
        # Фикс overflow: int(exponent), cap на 0 и max 10 (2^10=1024 сек ~17 мин)
        exponent = max(0, int((now - self.last_reconnect_attempt) // 60))
        backoff = min(1 << min(exponent, 10), 300) # Битовый сдвиг для int-математики (без pow)
        logger.debug(f"Backoff calc: exponent={exponent}, backoff={backoff}s")
        
        if now - self.last_reconnect_attempt < backoff:
            logger.debug(f"Слишком рано для reconnect (нужно подождать {backoff}s)")
            return
        
        self.reconnect_in_progress = True
        self.last_reconnect_attempt = now
        
        logger.info(f"🔄 Попытка переподключения к {self.ip}:{self.port} (backoff: {backoff}s)...")
        print(f"🔄 Попытка переподключения к {self.ip}:{self.port}...")
        
        success = self._connect_meshtastic(self.ip, self.port)
        
        if success:
            logger.info("✓ Переподключение успешно!")
            print("✓ Переподключение к Meshtastic успешно!")
            self._scan_nodes()
        else:
            logger.warning(f"✗ Переподключение не удалось. Следующая попытка через {backoff}s")
        
        self.reconnect_in_progress = False

    def _on_disconnect(self, interface=None):
        """Обработчик потери соединения (если pubsub поддерживает)."""
        logger.warning("Событие потери соединения от meshtastic")
        self._mark_disconnected()
        if not self.manual_disconnect:
            self._attempt_reconnect()

    def _handle_meshtastic_error(self, error):
        """Ловит внутренние ошибки meshtastic (включая BrokenPipe)."""
        logger.error(f"Ошибка от meshtastic: {error}")
        if isinstance(error, (BrokenPipeError, ConnectionResetError, OSError)):
            self._mark_disconnected()
            if not self.manual_disconnect:
                self._attempt_reconnect()

    # ==================== МЕТОДЫ ДЛЯ TELEGRAM-БОТА ====================
    
    def _init_telegram(self):
        """Инициализация Telegram бота."""
        if self.telegram_token:
            try:
                # Set global timeout for all API calls (send_message, etc.) via apihelper
                from telebot import apihelper
                apihelper.REMOTE_TIMEOUT = self.telegram_timeout # Use your config value (default 60s)
                logger.info(f"Telegram API timeout установлен: {self.telegram_timeout}s (via REMOTE_TIMEOUT)")
                              
                self.bot = telebot.TeleBot(self.telegram_token)
                self._setup_telegram_handlers()
                logger.info("Telegram бот инициализирован")
            except Exception as e:
                logger.error(f"Ошибка инициализации Telegram бота: {e}")
                self.bot = None
        else:
            self.bot = None

    def _send_with_retry(self, *args, max_retries=3, **kwargs):
        """Сервисный метод: отправка в Telegram с retry при timeout."""
        for attempt in range(max_retries):
            try:
                return self.bot.send_message(*args, **kwargs)
            except (requests.exceptions.ReadTimeout, telebot.apihelper.ApiException) as e:
                logger.warning(f"Timeout/API error on attempt {attempt + 1}: {str(e)}")
                if "timeout" in str(e).lower() and attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5 # Exponential backoff: 5s, 10s, 15s
                    logger.warning(f"Timeout на попытке {attempt + 1}/{max_retries}. Ждём {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    raise
        return None
    
    def _answer_callback_with_retry(self, callback_query_id, max_retries=3):
        """Сервисный метод: ответ на callback query с retry при timeout."""
        for attempt in range(max_retries):
            try:
                self.bot.answer_callback_query(callback_query_id)
                return True
            except (requests.exceptions.ReadTimeout, telebot.apihelper.ApiException) as e:
                logger.warning(f"Timeout/API error answering callback {callback_query_id} on attempt {attempt + 1}: {str(e)}")
                if "timeout" in str(e).lower() and attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5  # Exponential backoff: 5s, 10s, 15s
                    logger.warning(f"Timeout на попытке {attempt + 1}/{max_retries}. Ждём {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to answer callback {callback_query_id} after {max_retries} retries")
                    return False
        return False
   
    def _setup_telegram_handlers(self):
        """Настройка обработчиков для Telegram бота."""
        @self.bot.message_handler(commands=['connect'])
        def handle_connect(message):
            self._handle_connect_command(message)
        @self.bot.message_handler(commands=['disconnect'])
        def handle_disconnect(message):
            self._handle_disconnect_command(message)
        @self.bot.message_handler(commands=['pm'])
        def handle_pm(message):
            self._handle_pm_command(message)
        @self.bot.message_handler(commands=['status'])
        def handle_status(message):
            self._handle_status_command(message)
        @self.bot.message_handler(commands=['set_preset'])
        def handle_set_preset(message):
            self._handle_set_preset_command(message)
        @self.bot.message_handler(func=lambda message: True)
        def handle_telegram_message(message):
            self._handle_telegram_message(message)
        @self.bot.callback_query_handler(func=lambda call: call.data and call.data.startswith(('confirm_send_', 'cancel_send_')))
        def handle_confirmation(call):
            self._handle_confirmation(call)

    def _handle_confirmation(self, call):
        """Обработчик подтверждения отправки сообщения."""
        try:
            # Добавляем обработку исключений для answer_callback_query
            if not self._answer_callback_with_retry(call.id):
                logger.warning(f"Продолжаем обработку без ответа на callback {call.id}")
            
            data = call.data
            chat_id = str(call.message.chat.id)
            if data.startswith('cancel_send_'):
                orig_msg_id = int(data.split('_', 3)[2])
                pending = self.pending_messages.get(chat_id)
                if pending and pending['msg_id'] == orig_msg_id:
                    self.bot.edit_message_text("❌ Отправка отменена.", chat_id, call.message.message_id)
                    del self.pending_messages[chat_id]
                else:
                    # Попытка ответить на callback, даже если сессия истекла
                    self._answer_callback_with_retry(call.id, max_retries=1)
                return
            if data.startswith('confirm_send_'):
                orig_msg_id = int(data.split('_', 3)[2])
                pending = self.pending_messages.get(chat_id)
                if not pending or pending['msg_id'] != orig_msg_id:
                    self._answer_callback_with_retry(call.id, max_retries=1)
                    return
                text = pending['text']
                dest_node_id = pending['dest']
                meshtastic_reply_id = pending['reply_id']
                node_name = pending.get('node_name')
                if not self.interface or not self.is_connected:
                    self.bot.edit_message_text("🔴 Не подключено к Meshtastic. Сообщение не отправлено.", chat_id, call.message.message_id)
                    del self.pending_messages[chat_id]
                    return
                send_kwargs = {'replyId': meshtastic_reply_id} if meshtastic_reply_id else {}
                if self.default_channel:
                    send_kwargs['channel'] = self.default_channel
                success, total_parts = self._send_multipart_to_meshtastic(text, send_kwargs, dest_node_id, log_to_file=True)
                if success:
                    if dest_node_id:
                        target = f"личку ноде {node_name or dest_node_id}"
                        parts_text = f" в {total_parts} частях" if total_parts > 1 else ""
                        self.bot.edit_message_text(f"✓ Сообщение отправлено в {target}{parts_text}!", chat_id, call.message.message_id)
                    else:
                        target = "общий канал"
                        parts_text = f" в {total_parts} частях" if total_parts > 1 else ""
                        self.bot.edit_message_text(f"✓ Сообщение отправлено в {target}{parts_text}!", chat_id, call.message.message_id)
                else:
                    self.bot.edit_message_text(f"✗ Ошибка отправки (отправлено {total_parts} частей).", chat_id, call.message.message_id)
                del self.pending_messages[chat_id]
        except Exception as e:
            logger.error(f"Ошибка обработки подтверждения: {e}", exc_info=True)

    def _handle_status_command(self, message):
        """Обработчик команды /status - показывает состояние подключения."""
        try:
            chat_id = str(message.chat.id)
            if self.telegram_chat_id and chat_id != self.telegram_chat_id:
                self.bot.reply_to(message, "Доступ запрещён для этого чата.")
                return
            status = "🟢 Подключено" if self.is_connected else "🔴 Отключено"
            # ✅ Показываем режим автопереподключения
            auto_reconnect = "❌ Отключено (ручное отключение)" if self.manual_disconnect else "✅ Включено"
            nodes_count = len(self.node_map)
            
            # Получаем текущее имя ноды, пресет и channel_num
            long_name, preset_name, channel_num_name = self._get_current_node_info()
            
            hop_filter_text = f"[{self.hop_filter_min}, {self.hop_filter_max}]" if self.hop_filter_min is not None and self.hop_filter_max is not None else "отключена"
            
            status_text = f"""📊 Статус Meshtastic бота:
            
Подключение: {status}
Автопереподключение: {auto_reconnect}
Адрес: {self.ip}:{self.port}
Известных нод: {nodes_count}
Приватных нод: {len(self.private_node_names)}
Ключевых слов: {len(self.keywords)}
Фильтр автоответов (hops): {hop_filter_text}
Имя ноды (длинное): {long_name}
LoRa пресет: {preset_name}
Слот частоты: {channel_num_name}
Telegram timeout: {self.telegram_timeout}s
            """
            
            self.bot.reply_to(message, status_text)
        except Exception as e:
            logger.error(f"Ошибка обработки /status: {e}")
            self.bot.reply_to(message, f"Ошибка: {e}")

    def _handle_connect_command(self, message):
        """Обработчик команды /connect [ip:port]."""
        try:
            chat_id = str(message.chat.id)
            if self.telegram_chat_id and chat_id != self.telegram_chat_id:
                self.bot.reply_to(message, "Доступ запрещён для этого чата.")
                return
            parts = message.text.split()
            if len(parts) == 1:
                ip = self.ip
                port = self.port
                addr_str = f"{ip}:{port}"
            elif len(parts) == 2:
                addr = parts[1]
                if ':' not in addr:
                    self.bot.reply_to(message, "Неверный формат: укажите IP:PORT")
                    return
                ip, port_str = addr.split(':', 1)
                try:
                    port = int(port_str)
                except ValueError:
                    self.bot.reply_to(message, "Порт должен быть числом.")
                    return
                self._update_config_and_save(ip, port)
                addr_str = addr
            else:
                self.bot.reply_to(message, "Использование: /connect [ip:port]")
                return
            # сбрасываем флаг ручного отключения при команде /connect
            self.manual_disconnect = False
            logger.info("Сброшен флаг manual_disconnect (пользователь вызвал /connect)")
            
            success = self._connect_meshtastic(ip, port)
            if success:
                self.bot.reply_to(message, f"✓ Подключено к {addr_str} успешно!\nАвтопереподключение включено.")
            else:
                self.bot.reply_to(message, f"✗ Ошибка подключения к {addr_str}. Проверьте логи.\nАвтопереподключение включено.")
        except Exception as e:
            logger.error(f"Ошибка обработки /connect: {e}")
            self.bot.reply_to(message, f"Ошибка: {e}")

    def _handle_disconnect_command(self, message):
        """Обработчик команды /disconnect."""
        try:
            chat_id = str(message.chat.id)
            if self.telegram_chat_id and chat_id != self.telegram_chat_id:
                self.bot.reply_to(message, "Доступ запрещён для этого чата.")
                return
            # устанавливаем флаг ручного отключения
            self.manual_disconnect = True
            logger.info("Установлен флаг manual_disconnect (пользователь вызвал /disconnect)")
            
            self._disconnect_meshtastic()
            self.bot.reply_to(message, "✓ Отключено от Meshtastic.\nАвтопереподключение отключено.\nИспользуйте /connect для повторного подключения.")
        except Exception as e:
            logger.error(f"Ошибка обработки /disconnect: {e}")
            self.bot.reply_to(message, f"Ошибка: {e}")

    def _handle_pm_command(self, message):
        """Обработчик команды /pm <node_name> <text>."""
        try:
            chat_id = str(message.chat.id)
            if self.telegram_chat_id and chat_id != self.telegram_chat_id:
                self.bot.reply_to(message, "Доступ запрещён для этого чата.")
                return
            if not self.interface or not self.is_connected:
                self.bot.reply_to(message, "🔴 Не подключено к Meshtastic. Используйте /connect")
                return
            parts = message.text.split(maxsplit=2)
            if len(parts) < 3:
                self.bot.reply_to(message, "Использование: /pm <node_name> <text>")
                return
            node_name = parts[1].lower()
            text = parts[2]
            
            # Автоматическое добавление ноды в private_node_names
            was_added = False
            if node_name not in self.private_node_names:
                logger.info(f"Нода '{node_name}' не в списке private_node_names, добавляем автоматически...")
                was_added = self._save_private_node_to_config(node_name)
            # Проверяем наличие node_id в node_map
            node_id = self.node_map.get(node_name)
            if not node_id:
                # Даём подсказку, если нода неизвестна
                available_nodes = [name for name in self.node_map.keys()]
                hint = f"Известные ноды: {', '.join(available_nodes)}" if available_nodes else "Нет известных нод. Подождите обновления node_map."
                self.bot.reply_to(message, f"❌ ID ноды '{node_name}' не найден в сети.\n{hint}")
                return
            # Подтверждение отправки
            confirm_text = f"Вы уверены, что хотите отправить это приватное сообщение ноде '{node_name}' ({node_id})?\n\n{text}"
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Да, отправить в личку", callback_data=f"confirm_send_{message.message_id}"),
                types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_send_{message.message_id}")
            )
            self.bot.reply_to(message, confirm_text, reply_markup=markup, parse_mode='Markdown')
            # Сохраняем в pending
            self.pending_messages[chat_id] = {
                'text': text,
                'dest': node_id,
                'reply_id': None,
                'msg_id': message.message_id,
                'node_name': node_name
            }
            logger.info(f"Ожидание подтверждения для /pm: {text} -> {node_name} ({node_id})")
                  
        except Exception as e:
            logger.error(f"Ошибка обработки /pm: {e}")
            self.bot.reply_to(message, f"Ошибка: {e}")

    def _handle_set_preset_command(self, message):
        """Обработчик команды /set_preset <preset_name> <slot> для переключения LoRa-пресета (глобально)."""
        try:
            chat_id = str(message.chat.id)
            if self.telegram_chat_id and chat_id != self.telegram_chat_id:
                self.bot.reply_to(message, "❌ Доступ запрещён для этого чата.")
                return
            if not self.interface or not self.is_connected:
                self.bot.reply_to(message, "🔴 Не подключено к Meshtastic. Используйте /connect.")
                return
            parts = message.text.split()
            if len(parts) != 3:
                help_text = """❌ Использование: /set_preset <preset> <slot>
Примеры:
• /set_preset longfast 0
• /set_preset mediumslow 1
Доступные пресеты (глобальные настройки LoRa):
• longfast (LF) - дальняя связь, быстрая скорость
• longslow (LS) - дальняя связь, медленная скорость
• longmoderate (LM) - дальняя связь, умеренная скорость
• verylongslow (VLS) - очень дальняя связь, медленная скорость
• mediumslow (MS) - средняя дальность, медленная скорость
• mediumfast (MF) - средняя дальность, быстрая скорость
• shortslow (SS) - короткая дальность, медленная скорость
• shortturbo (ST) - короткая дальность, турбо скорость
• shortfast (SF) - короткая дальность, быстрая скорость
Слоты частоты: 0 или 1
⚠️ Пресет применяется глобально ко всем каналам. LongName обновляется с тегом.
После установки: перезагрузите устройство или переподключите бота."""
                self.bot.reply_to(message, help_text)
                return
            preset_name = parts[1].lower()
            
            try:
                slot = int(parts[2])
            except ValueError:
                self.bot.reply_to(message, "❌ Слот должен быть числом (0-7).")
                return
            if slot < 0 or slot > 7:
                self.bot.reply_to(message, "❌ Слот должен быть от 0 до 7.")
                return
            # Маппинг пресетов на enum И сокращения
            preset_info = {
                'longfast': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST, 'abbr': 'LF', 'display': 'Long Fast'},
                'longslow': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.LONG_SLOW, 'abbr': 'LS', 'display': 'Long Slow'},
                'verylongslow': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.VERY_LONG_SLOW, 'abbr': 'VLS', 'display': 'Very Long Slow'},
                'mediumslow': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW, 'abbr': 'MS', 'display': 'Medium Slow'},
                'mediumfast': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST, 'abbr': 'MF', 'display': 'Medium Fast'},
                'shortslow': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.SHORT_SLOW, 'abbr': 'SS', 'display': 'Short Slow'},
                'shortfast': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST, 'abbr': 'SF', 'display': 'Short Fast'},
                'longmoderate': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.LONG_MODERATE, 'abbr': 'LM', 'display': 'Long Moderate'},
                'shortturbo': {'enum': config_pb2.Config.LoRaConfig.ModemPreset.SHORT_TURBO, 'abbr': 'ST', 'display': 'Short Turbo'},
            }
            if preset_name not in preset_info:
                self.bot.reply_to(message, "❌ Неизвестный пресет. Используйте /set_preset без параметров для справки.")
                return
            modem_config = preset_info[preset_name]['enum']
            preset_abbr = preset_info[preset_name]['abbr']
            preset_display_name = preset_info[preset_name]['display']
            # Установка глобального пресета LoRa
            lora_write_success = False
            old_preset = None
            old_slot = None
            try:
                local_node = self.interface.localNode
                local_config = local_node.localConfig
                if not local_config.lora:
                    raise Exception("localConfig.lora не загружен")
                
                old_preset = local_config.lora.modem_preset
                old_slot = local_config.lora.channel_num
                logger.info(f"Старый пресет: {old_preset}, старый slot: {old_slot}")
                
                local_config.lora.modem_preset = modem_config
                logger.info(f"Новый пресет установлен локально: {modem_config}")
                
                # Установка channel_num в LoRa config (ранее freq_slot)
                local_config.lora.channel_num = slot # 0-7 для frequency hopping/offset
                logger.info(f"Channel num установлен локально: {slot}")
                
                lora_write_success = local_node.writeConfig("lora")
                if not lora_write_success:
                    logger.warning("writeConfig('lora') failed, but config is set locally. Reboot may be needed.")
                else:
                    logger.info(f"Глобальный пресет и channel_num успешно записаны: preset={preset_name} (modem_preset={modem_config}, старый={old_preset}), slot={slot} (старый={old_slot})")
                
            except Exception as e:
                logger.error(f"Ошибка установки глобального пресета: {e}")
                lora_write_success = False
            # Обновляем longName ноды с пресетом (shortName не трогаем)
            name_success, old_name, new_name = self._update_node_name_with_preset(preset_abbr, slot)
            
            # new_name теперь из config (после _save_node_long_name_to_config)
            # Формируем ответ в зависимости от успеха
            preset_status = "✅ успешно записан на устройство" if lora_write_success else "⚠️ установлен локально, но запись на устройство не удалась"
            name_status = f"📝 LongName обновлено: {old_name} → {new_name} (сохранено в config)" if name_success else "⚠️ Не удалось обновить longName автоматически"
            
            slot_status = f"📡 Channel num: {slot} ({'успешно записан' if lora_write_success else 'установлен локально'})"
            
            response_text = f"""**{preset_status}**: Глобальный пресет '{preset_display_name}'!
{name_status}
{slot_status}
ℹ️ ShortName остается без изменений
🔄 Слот {slot}
⚠️ Для применения изменений:
1. **Перезагрузите устройство** (обязательно для LoRa пресетов)
2. Переподключите бота (/disconnect + /connect)
3. Проверьте в Meshtastic app (Settings > Radio Configuration > LoRa)
Если запись не удалась, попробуйте вручную в приложении."""
            
            self.bot.reply_to(message, response_text, parse_mode='Markdown')
                
        except Exception as e:
            logger.error(f"Ошибка обработки /set_preset: {e}", exc_info=True)
            self.bot.reply_to(message, f"✗ Ошибка: {str(e)}")

    def _handle_telegram_message(self, message):
        """Обработчик сообщений из Telegram: отправка в Meshtastic."""
        try:
            chat_id = str(message.chat.id)
            
            if not self.telegram_chat_id:
                self._save_chat_id_to_config(chat_id)
                self.bot.reply_to(message, f"Привет! Ваш chat_id: {chat_id}. Теперь бот активен для этого чата.")
                return
            if message.text and message.text.startswith('/'):
                return
            if chat_id != self.telegram_chat_id:
                logger.debug(f"Сообщение из другого чата {chat_id}, игнорируем")
                self.bot.reply_to(message, "Этот бот настроен для другого чата.")
                return
            text = message.text
            logger.info(f"Получено сообщение из Telegram: '{text}' от {message.from_user.username} (msg_id: {message.message_id})")
            if not self.interface or not self.is_connected:
                self.bot.reply_to(message, "🔴 Не подключено к Meshtastic. Используйте /connect")
                return
            meshtastic_reply_id = None
            dest_node_id = None
            is_private_reply = False
            
            if message.reply_to_message:
                telegram_parent_id = message.reply_to_message.message_id
                meshtastic_reply_id, dest_node_id, is_private_reply = self._find_reply_info(telegram_parent_id)
                if meshtastic_reply_id:
                    logger.debug(f"Reply в Meshtastic: {meshtastic_reply_id}, private: {is_private_reply}, dest: {dest_node_id}")
            # Определяем node_name для подтверждения
            node_name = None
            if dest_node_id:
                # Ищем имя по ID
                for name, nid in self.node_map.items():
                    if nid == dest_node_id:
                        node_name = name
                        break
                if not node_name:
                    node_name = str(dest_node_id)
            # Подтверждение отправки
            if dest_node_id and node_name:
                confirm_text = f"Вы уверены, что хотите отправить это приватное сообщение ноде '{node_name}'?\n\n{text}"
            else:
                confirm_text = f"Вы уверены, что хотите отправить это сообщение в общий чат?\n\n{text}"
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("✅ Да, отправить", callback_data=f"confirm_send_{message.message_id}"),
                types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_send_{message.message_id}")
            )
            self.bot.reply_to(message, confirm_text, reply_markup=markup, parse_mode='Markdown')
            # Сохраняем в pending
            self.pending_messages[chat_id] = {
                'text': text,
                'dest': dest_node_id,
                'reply_id': meshtastic_reply_id,
                'msg_id': message.message_id,
                'node_name': node_name
            }
            logger.info(f"Ожидание подтверждения для сообщения: {text} {'-> ' + str(dest_node_id) if dest_node_id else '(general)'}")
                
        except Exception as e:
            logger.error(f"Ошибка обработки Telegram сообщения: {e}", exc_info=True)
            try:
                self.bot.reply_to(message, f"Ошибка: {e}")
            except:
                pass

    def _forward_to_telegram(self, meshtastic_msg_id, short_name, original_text, node_id, is_private, rssi, snr, hop_count, via_short_name=None, reply_id=None):
        """Метод для пересылки сообщения из Meshtastic в Telegram с поддержкой reply."""
        if not self.bot or not self.telegram_chat_id:
            return
        
        try:
            # Поиск родительского сообщения в Telegram, если есть reply_id
            telegram_parent_id = None
            if reply_id:
                with msg_mapping_lock:
                    parent_info = msg_mapping.get(reply_id, {})
                    telegram_parent_id = parent_info.get('telegram_msg_id')
                    if telegram_parent_id:
                        logger.debug(f"Найдено родительское сообщение в Telegram: {telegram_parent_id} для reply_id: {reply_id}")
                    else:
                        logger.debug(f"Родительское сообщение не найдено в маппинге для reply_id: {reply_id}")
            
            prefix = f"[PRIVATE from {short_name}] " if is_private else f"[{short_name}] "
            telegram_text = prefix + original_text

            signal_info = ""
            if via_short_name:
                signal_info = f" via {via_short_name}"
            if hop_count is not None and hop_count > 0:
                hops_info = f" ({hop_count} hops)"
                signal_info = hops_info if not signal_info else signal_info + hops_info
            elif rssi != 'unknown' and snr != 'unknown':
                snr_rssi_info = f" (SNR: {snr}, RSSI: {rssi})"
                signal_info = snr_rssi_info if not signal_info else signal_info + snr_rssi_info
            if signal_info:
                telegram_text += signal_info

            # Отправка с reply_to_message_id если есть родительское сообщение
            sent_msg = self._send_with_retry(
                self.telegram_chat_id, 
                telegram_text,
                reply_to_message_id=telegram_parent_id if telegram_parent_id else None
            )
            if not sent_msg:
                logger.error("Failed to send to Telegram after retries")
                return
            
            # Сохранение маппинга для будущих reply
            if meshtastic_msg_id:
                with msg_mapping_lock:
                    if len(msg_mapping) >= MAX_MAPPING_SIZE:
                        msg_mapping.popitem(last=False)
                    
                    msg_mapping[meshtastic_msg_id] = {
                        'telegram_msg_id': sent_msg.message_id,
                        'node_id': node_id if is_private else None,
                        'is_private': is_private
                    }
            
            reply_info = f", reply_to: {telegram_parent_id}" if telegram_parent_id else ""
            logger.info(f"Переслано в Telegram: {telegram_text} (msg_id: {sent_msg.message_id}, meshtastic_id: {meshtastic_msg_id}, private: {is_private}{reply_info})")
            
        except telebot.apihelper.ApiException as e:
            logger.error(f"Telegram API ошибка при пересылке: {e}")
        except Exception as e:
            logger.error(f"Ошибка пересылки в Telegram: {e}", exc_info=True)

    def _forward_auto_reply_to_telegram(self, short_name, original_text, reply_text, original_message_id, is_private):
        """Метод для пересылки автоответа в Telegram."""
        if not self.bot or not self.telegram_chat_id:
            return
        
        try:
            telegram_parent_id = None
            with msg_mapping_lock:
                # Ищем по ID исходного сообщения
                telegram_parent_id = msg_mapping.get(original_message_id, {}).get('telegram_msg_id')
            
            telegram_reply_to = telegram_parent_id if telegram_parent_id else None

            prefix = "[BOT Auto-reply (private)] " if is_private else "[BOT Auto-reply] "
            auto_reply_text = f"{prefix}to {short_name}: [{original_text[:50]}...] → {reply_text}"

            sent_msg = self.bot.send_message(
                self.telegram_chat_id, 
                auto_reply_text, 
                reply_to_message_id=telegram_reply_to
            )
            
            logger.info(f"Переслан автоответ в Telegram: {auto_reply_text} (reply_to: {telegram_reply_to}, private: {is_private})")
            
        except telebot.apihelper.ApiException as e:
            logger.error(f"Telegram API ошибка при пересылке автоответа: {e}")
        except Exception as e:
            logger.error(f"Ошибка пересылки автоответа в Telegram: {e}", exc_info=True)

    def _start_telegram_polling(self):
        """Запуск polling для Telegram бота в фоне с автоперезапуском."""
        if self.bot:
            while True:
                try:
                    logger.info(f"Запуск Telegram polling с таймаутом {self.telegram_timeout}s...")
                    self.bot.polling(none_stop=True, interval=0, timeout=self.telegram_timeout)
                except Exception as e:
                    logger.error(f"Ошибка Telegram polling: {e}", exc_info=True)
                    time.sleep(5)

    # ==================== МЕТОДЫ ДЛЯ MESHTASTIC ====================
    
    def _init_meshtastic(self):
        """Инициализация Meshtastic интерфейса (без exit при ошибке)."""
        try:
            logger.info(f"Инициализация TCPInterface")
            success = self._connect_meshtastic(self.ip, self.port)
            if success:
                print(f"✓ Подключение к {self.ip}:{self.port} успешно!")
            else:
                print(f"✗ Ошибка подключения к {self.ip}:{self.port}. Будет выполнено автопереподключение.")
        except Exception as e:
            logger.error(f"Ошибка инициализации Meshtastic: {e}", exc_info=True)
            print(f"✗ Ошибка подключения: {e}. Будет выполнено автопереподключение.")

    def _setup_subscriptions(self):
        """Настройка подписки на события Meshtastic (улучшено)."""
        if self.interface:
            pub.subscribe(self._on_receive, "meshtastic.receive")
            # Подписка на события потери соединения и ошибок (если поддерживается)
            pub.subscribe(self._on_disconnect, "meshtastic.connection-lost")
            pub.subscribe(self._handle_meshtastic_error, "meshtastic.error")
            logger.info("Подписка на события meshtastic установлена")

    def _on_receive(self, packet, interface):
        """Основной обработчик входящих сообщений из Meshtastic."""
        if 'decoded' not in packet or packet.get('decoded', {}).get('portnum') != 'TEXT_MESSAGE_APP':
            return

        try:
            original_text = packet['decoded']['payload'].decode('utf-8', errors='ignore')
            text_lower = original_text.lower()
            logger.info(f"Обработано текстовое сообщение: '{original_text}' от {packet['from']}")

            words = text_lower.split()
            has_keywords = any(kw in words for kw in self.keywords)

            from_num = packet['from']
            to_id = packet['to']
            
            meshtastic_msg_id = packet.get('id')
            reply_id = packet.get('decoded', {}).get('replyId')
            
            logger.debug(f"Meshtastic msg ID: {meshtastic_msg_id}, Reply ID: {reply_id}")

            short_name, node_id = self._get_node_info(from_num, interface)
            
            if node_id and short_name:
                self.node_map[short_name.lower()] = node_id
                logger.debug(f"Обновлён node_map: {short_name.lower()} -> {node_id}")

            channel_name = self._get_channel_name(packet)
            is_broadcast, is_private = self._is_broadcast(to_id)

            rssi = packet.get('rxRssi', 'unknown')
            snr = packet.get('rxSnr', 'unknown')

            hop_start = packet.get('hopStart')
            hop_limit = packet.get('hopLimit')
            hop_count = None
            if hop_start is not None and hop_limit is not None:
                hop_count = hop_start - hop_limit
                logger.debug(f"Вычислено hop_count: {hop_count}")

            via_id = packet.get('via')
            via_short_name = None
            if via_id:
                via_short_name, _ = self._get_node_info(via_id, interface)
                logger.debug(f"VIA нода {via_id}: short_name={via_short_name}")

            send_kwargs = self._get_send_kwargs(reply_id, channel_name)

            # Запись ВХОДЯЩЕГО сообщения в файл
            if is_broadcast:
                self._log_message_to_file('general', short_name, original_text, rssi, snr, hop_count, hop_start, hop_limit, via_short_name, is_outgoing=False)
            else:
                self._log_message_to_file('private', short_name, original_text, rssi, snr, hop_count, hop_start, hop_limit, via_short_name, to_id=to_id, is_outgoing=False)

            forward_to_telegram = False
            if is_broadcast:
                forward_to_telegram = True
            elif is_private and short_name.lower() in self.private_node_names:
                forward_to_telegram = True

            if forward_to_telegram:
                # ✅ Передаем reply_id для создания reply-цепочки в Telegram
                self._forward_to_telegram(
                    meshtastic_msg_id,
                    short_name, 
                    original_text, 
                    node_id, 
                    is_private, 
                    rssi, 
                    snr, 
                    hop_count,
                    via_short_name,
                    reply_id
                )

            if has_keywords:
                self._handle_auto_reply(
                    is_private, 
                    short_name, 
                    node_id, 
                    send_kwargs, 
                    rssi, 
                    snr, 
                    packet, 
                    channel_name, 
                    original_text, 
                    meshtastic_msg_id,
                    via_short_name
                )
            else:
                logger.debug(f"Ключевые слова не найдены в: '{original_text}'")
                
        except UnicodeDecodeError as e:
            logger.error(f"Ошибка декодирования текста: {e}")
        except Exception as e:
            logger.error(f"Ошибка обработки пакета: {e}", exc_info=True)

    def _handle_auto_reply(self, is_private, short_name, node_id, send_kwargs, rssi, snr, packet, channel_name, original_text, meshtastic_msg_id, via_short_name=None):
        """Метод для обработки автоматического ответа на ключевые слова."""
        if not self.interface or not self.is_connected:
            logger.warning("Нет активного подключения к Meshtastic, пропускаем автоответ")
            return

        if is_private and short_name.lower() not in self.private_node_names:
            logger.debug(f"Нода {short_name} не в списке private_node_names, пропускаем автоответ")
            return

        auto_reply_kwargs = {}
        if meshtastic_msg_id:
            auto_reply_kwargs['replyId'] = meshtastic_msg_id
        if channel_name:
            auto_reply_kwargs['channel'] = channel_name

        # Вычисляем hop_count
        hop_start = packet.get('hopStart')
        hop_limit = packet.get('hopLimit')
        hop_count = None
        if hop_start is not None and hop_limit is not None:
            hop_count = hop_start - hop_limit

        reply = None
        suffix = self.private_suffix if is_private else self.general_suffix
        
        if hop_count is not None and hop_count > 0:
            reply = self._get_hops_reply(short_name, hop_count, suffix, via_short_name)
            logger.debug(f"Хопы для {'private' if is_private else 'broadcast'}: {hop_count}")
        else:
            reply = self._get_signal_reply(short_name, rssi, snr, suffix, via_short_name)
            logger.debug(f"Прямой {'private' if is_private else 'broadcast'}: сигнал RSSI={rssi}, SNR={snr}")
        
        if reply:
            # Фильтрация автоответов для общего канала по интервалу хопов
            if not is_private and hop_count is not None and self.hop_filter_min is not None and self.hop_filter_max is not None:
                if self.hop_filter_min <= hop_count <= self.hop_filter_max:
                    logger.info(f"Автоответ пропущен для общего канала: hop_count={hop_count} в интервале [{self.hop_filter_min}, {self.hop_filter_max}]")
                    # Пересылаем уведомление в Telegram, но не отправляем в Meshtastic
                    self._forward_auto_reply_to_telegram(
                        short_name, 
                        original_text, 
                        f"[FILTERED] {reply}", 
                        meshtastic_msg_id,
                        is_private
                    )
                    return
            
            # Добавляем паузу перед отправкой автоответа
            time.sleep(1)
            logger.debug(f"Пауза 1 сек перед отправкой автоответа: '{reply[:30]}...'")        
            
            if is_private:
                send_type = self._send_to_meshtastic(reply, auto_reply_kwargs, node_id)
                if send_type:
                    logger.info(f"Отправлен ответ в личный канал: {reply} ({send_type}) -> {node_id}")
                    # Запись АВТООТВЕТА в файл (private)
                    self._log_message_to_file('private', short_name, reply, to_id=node_id, is_bot_reply=True)
            else:
                send_type = self._send_to_meshtastic(reply, auto_reply_kwargs)
                if send_type:
                    logger.info(f"Отправлен ответ: {reply} (broadcast)")
                    # Запись АВТООТВЕТА в файл (general)
                    self._log_message_to_file('general', short_name, reply, is_bot_reply=True)
            
            # передаем meshtastic_msg_id (ID исходного сообщения)
            self._forward_auto_reply_to_telegram(
                short_name, 
                original_text, 
                reply, 
                meshtastic_msg_id,
                is_private
            )

    # ==================== ОСНОВНОЙ ЗАПУСК ====================
    
    def run(self):
        """Запуск бота: Telegram polling в фоне + основной цикл с автопереподключением."""
        logger.info(f"🚀 Запуск Meshtastic Telegram Bot...")
        logger.info(f"📡 Адрес Meshtastic: {self.ip}:{self.port}")
        logger.info(f"🤖 Telegram: {'включен' if self.bot else 'отключен'} (timeout: {self.telegram_timeout}s)")
        logger.info(f"📝 Node long_name из config: {self.node_long_name}")
        hop_filter_text = f"[{self.hop_filter_min}, {self.hop_filter_max}]" if self.hop_filter_min is not None and self.hop_filter_max is not None else "отключена"
        logger.info(f"🔧 Фильтр автоответов (hops): {hop_filter_text}")

        if self.bot:
            telegram_thread = threading.Thread(target=self._start_telegram_polling, daemon=True)
            telegram_thread.start()
            logger.info("Telegram polling запущен в фоне")

        last_config_check = 0
        config_check_interval = 10
               
        try:
            logger.info("Запуск основного цикла с автопереподключением...")
            print("✓ Основной цикл запущен. Нажмите Ctrl+C для остановки.\n")
            
            while True:
                now = time.time()
                
                try:
                    # Проверка изменений config.json
                    if now - last_config_check >= config_check_interval:
                        try:
                            current_mtime = os.path.getmtime('config.json')
                            if current_mtime > self.config_mtime:
                                logger.info(f"Обнаружено изменение config.json, перезагружаем...")
                                self._reload_config()
                        except Exception as e:
                            logger.error(f"Ошибка проверки config.json: {e}")
                        last_config_check = now

                    # проверка соединения и автопереподключение только если не было ручного отключения
                    if not self.manual_disconnect and now - self.last_connection_check >= CONNECTION_CHECK_INTERVAL:
                        if not self._check_connection():
                            if self.is_connected:
                                self._mark_disconnected()
                            self._attempt_reconnect()
                        self.last_connection_check = now

                    # Периодическое сканирование нод
                    if now - self.last_node_scan >= self.node_scan_interval and self.interface and self.is_connected:
                        self._scan_nodes()
                        self.last_node_scan = now

                    # Пинг watchdog
                    if self.notifier and now - self.last_watchdog_ping >= self.watchdog_interval:
                        try:
                            self.notifier.notify("WATCHDOG=1")
                            self.last_watchdog_ping = now
                            logger.debug("Watchdog ping отправлен")
                        except Exception as wd_e:
                            logger.error(f"Ошибка watchdog пинга: {wd_e}")
                                      
                    time.sleep(1)
                except Exception as loop_e:
                    logger.error(f"Ошибка в main loop: {loop_e}", exc_info=True)
                    self._mark_disconnected()
                    if not self.manual_disconnect:
                        self._attempt_reconnect()
                        
        except KeyboardInterrupt:
            logger.info("Получен сигнал прерывания (Ctrl+C)")
            print("\n⏹️  Остановка приложения...")
        except Exception as e:
            logger.error(f"Неожиданная ошибка в основном цикле: {e}", exc_info=True)
        finally:
            self._cleanup()

    def _cleanup(self):
        """Сервисный метод: очистка ресурсов при завершении."""
        try:
            self._disconnect_meshtastic()
            if self.bot:
                self.bot.stop_polling()
                logger.info("Telegram бот остановлен")
            print("✓ Приложение остановлено")
        except Exception as e:
            logger.error(f"Ошибка при закрытии: {e}")


# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================

if __name__ == "__main__":
    bot = MeshTelegramBot()
    bot.run()