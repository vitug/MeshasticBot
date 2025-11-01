import json
import logging
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub
import time
import telebot
from telebot import types
import threading
import os
from collections import OrderedDict

# Настройка логирования в файл
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mesh_bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

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

# Константы для автопереподключения
RECONNECT_INTERVAL = 10
CONNECTION_CHECK_INTERVAL = 5


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
        self.default_channel = None
        self.config_mtime = 0
        self.last_node_scan = 0
        self.node_scan_interval = 30
        self.messages_dir = 'messages_logs'
        
        # Флаги для автопереподключения
        self.is_connected = False
        self.last_reconnect_attempt = 0
        self.last_connection_check = 0
        self.reconnect_in_progress = False
        self.manual_disconnect = False  # ✅ НОВЫЙ ФЛАГ: ручное отключение пользователем
        
        self._load_config()
        self._init_messages_dir()

        # Инициализация компонентов
        self._init_meshtastic()
        self._init_telegram()
        self._setup_subscriptions()

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
            self.default_channel = self.config.get('default_channel')
            self.config_mtime = os.path.getmtime('config.json')
            
            if not self.telegram_token:
                logger.warning("Telegram токен не найден, интеграция отключена")
                self.telegram_chat_id = None
            elif not self.telegram_chat_id:
                logger.info("Telegram chat_id не указан в config.json. Бот будет ждать первого сообщения.")
            
            logger.info(f"Конфигурация загружена: IP={self.ip}, Port={self.port}, Keywords={self.keywords}, "
                        f"Private nodes={self.private_node_names}, General suffix='{self.general_suffix}', "
                        f"Private suffix='{self.private_suffix}', Telegram: {'enabled' if self.telegram_token else 'disabled'}")
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

    def _log_message_to_file(self, message_type, short_name, original_text, rssi='unknown', snr='unknown', hop_count=None, is_private=False, to_id=None, is_outgoing=False, is_bot_reply=False):
        """
        Сервисный метод: запись сообщения в соответствующий файл.
        
        Args:
            message_type: 'general', 'private', 'private_group'
            short_name: имя отправителя/получателя
            original_text: текст сообщения
            rssi: уровень сигнала
            snr: отношение сигнал/шум
            hop_count: количество хопов
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
            if hop_count and hop_count > 0:
                hops_info = f" ({hop_count} hops)"
                signal_info = hops_info if not signal_info else signal_info + hops_info
        
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
            
            new_telegram_token = new_config.get('telegram_token')
            new_telegram_chat_id = str(new_config.get('telegram_chat_id', '')) if new_config.get('telegram_chat_id') else None
            
            if new_telegram_token != self.telegram_token or new_telegram_chat_id != self.telegram_chat_id:
                logger.warning("Изменены Telegram настройки (токен/chat_id). Перезапустите приложение для применения.")
                self.telegram_token = new_telegram_token
                self.telegram_chat_id = new_telegram_chat_id
            
            self.config_mtime = os.path.getmtime('config.json')
            logger.info("Конфигурация перезагружена успешно (keywords, suffixes, private_nodes обновлены)")
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
                            logger.debug(f"Сканирование: обновлён {short_name} -> {node_id}")
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

    def _get_signal_reply(self, short_name, rssi, snr, suffix):
        """Сервисный метод: формирование ответа с сигналом (RSSI/SNR)."""
        return f"{short_name} SNR: {snr}, RSSI: {rssi} {suffix}"

    def _get_hops_reply(self, short_name, hop_count, suffix):
        """Сервисный метод: формирование ответа с количеством хопов."""
        return f"{short_name} {hop_count} hops {suffix}"

    def _get_direct_reply(self, short_name, snr, rssi, suffix):
        """Сервисный метод: формирование ответа для прямого приема (сигнал)."""
        return f"{short_name} SNR: {snr}, RSSI: {rssi} {suffix}"

    def _check_connection(self):
        """Сервисный метод: проверка состояния соединения с Meshtastic."""
        if not self.interface:
            return False
        
        try:
            _ = self.interface.nodesByNum
            return True
        except Exception as e:
            logger.warning(f"Проверка соединения не удалась: {e}")
            return False

    def _mark_disconnected(self):
        """Сервисный метод: пометить соединение как разорванное."""
        if self.is_connected:
            self.is_connected = False
            logger.warning("⚠️ Соединение с Meshtastic потеряно")
            # ✅ Не выводим сообщение о попытке переподключения, если было ручное отключение
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
                            None,  # short_name не нужен для исходящих
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
            self.is_connected = True
            logger.info(f"✓ Подключение к {ip}:{port} успешно!")
            print(f"✓ Подключение к {ip}:{port} успешно!")
            return True
        except Exception as e:
            logger.error(f"Ошибка подключения к Meshtastic {ip}:{port}: {e}", exc_info=True)
            self.interface = None
            self.is_connected = False
            return False

    def _attempt_reconnect(self):
        """Сервисный метод: попытка переподключения к Meshtastic."""
        # ✅ ИСПРАВЛЕНИЕ: не переподключаться, если было ручное отключение
        if self.manual_disconnect:
            logger.debug("Автопереподключение отключено (manual_disconnect=True)")
            return
            
        if self.reconnect_in_progress:
            return
        
        now = time.time()
        if now - self.last_reconnect_attempt < RECONNECT_INTERVAL:
            return
        
        self.reconnect_in_progress = True
        self.last_reconnect_attempt = now
        
        logger.info(f"🔄 Попытка переподключения к {self.ip}:{self.port}...")
        print(f"🔄 Попытка переподключения к {self.ip}:{self.port}...")
        
        success = self._connect_meshtastic(self.ip, self.port)
        
        if success:
            logger.info("✓ Переподключение успешно!")
            print("✓ Переподключение к Meshtastic успешно!")
            self._scan_nodes()
        else:
            logger.warning(f"✗ Переподключение не удалось. Следующая попытка через {RECONNECT_INTERVAL} сек")
        
        self.reconnect_in_progress = False

    # ==================== МЕТОДЫ ДЛЯ TELEGRAM-БОТА ====================
    
    def _init_telegram(self):
        """Инициализация Telegram бота."""
        if self.telegram_token:
            try:
                self.bot = telebot.TeleBot(self.telegram_token)
                self._setup_telegram_handlers()
                logger.info("Telegram бот инициализирован")
            except Exception as e:
                logger.error(f"Ошибка инициализации Telegram бота: {e}")
                self.bot = None
        else:
            self.bot = None

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

        @self.bot.message_handler(func=lambda message: True)
        def handle_telegram_message(message):
            self._handle_telegram_message(message)

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
            
            status_text = f"""📊 Статус Meshtastic бота:
            
Подключение: {status}
Автопереподключение: {auto_reconnect}
Адрес: {self.ip}:{self.port}
Известных нод: {nodes_count}
Приватных нод: {len(self.private_node_names)}
Ключевых слов: {len(self.keywords)}
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

            # ✅ ИСПРАВЛЕНИЕ: сбрасываем флаг ручного отключения при команде /connect
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

            # ✅ ИСПРАВЛЕНИЕ: устанавливаем флаг ручного отключения
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
            if node_name not in self.private_node_names:
                self.bot.reply_to(message, f"Нода '{node_name}' не в списке private_node_names. Доступные: {', '.join(self.private_node_names)}")
                return

            node_id = self.node_map.get(node_name)
            if not node_id:
                self.bot.reply_to(message, f"ID ноды '{node_name}' не найден. Подождите обновления node_map.")
                return

            text = parts[2]
            send_kwargs = {}
            
            # Отправка с разбивкой на части И записью в файл
            success, total_parts = self._send_multipart_to_meshtastic(text, send_kwargs, node_id, log_to_file=True)
            
            if success:
                if total_parts > 1:
                    self.bot.reply_to(message, f"✓ Личное сообщение отправлено ноде '{node_name}' в {total_parts} частях!")
                else:
                    self.bot.reply_to(message, f"✓ Личное сообщение отправлено ноде '{node_name}'!")
            else:
                self.bot.reply_to(message, f"✗ Ошибка отправки сообщения ноде '{node_name}'.")
                
        except Exception as e:
            logger.error(f"Ошибка обработки /pm: {e}")
            self.bot.reply_to(message, f"Ошибка: {e}")

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

            send_kwargs = {'replyId': meshtastic_reply_id} if meshtastic_reply_id else {}
            if self.default_channel:
                send_kwargs['channel'] = self.default_channel

            text_bytes = self._calculate_text_bytes(text)
            logger.info(f"Размер сообщения: {text_bytes} байт")
            
            # Отправка с разбивкой на части И записью в файл
            success, total_parts = self._send_multipart_to_meshtastic(text, send_kwargs, dest_node_id, log_to_file=True)

            if success:
                if dest_node_id:
                    if total_parts > 1:
                        self.bot.reply_to(message, f"✓ Сообщение отправлено в личку ноде {dest_node_id} в {total_parts} частях!")
                    else:
                        self.bot.reply_to(message, f"✓ Сообщение отправлено в личку ноде {dest_node_id}!")
                else:
                    if total_parts > 1:
                        self.bot.reply_to(message, f"✓ Сообщение отправлено в общий канал в {total_parts} частях!")
                    else:
                        self.bot.reply_to(message, "✓ Сообщение отправлено в общий канал!")
            else:
                self.bot.reply_to(message, f"✗ Ошибка отправки сообщения (отправлено {total_parts} частей).")
                
        except Exception as e:
            logger.error(f"Ошибка обработки Telegram сообщения: {e}", exc_info=True)
            try:
                self.bot.reply_to(message, f"Ошибка: {e}")
            except:
                pass

    def _forward_to_telegram(self, meshtastic_msg_id, short_name, original_text, node_id, is_private, rssi, snr, hop_count, reply_id=None):
        """Метод для пересылки сообщения из Meshtastic в Telegram с поддержкой reply."""
        if not self.bot or not self.telegram_chat_id:
            return
        
        try:
            # ✅ Поиск родительского сообщения в Telegram, если есть reply_id
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
            if hop_count is not None and hop_count > 0:
                signal_info = f" ({hop_count} hops)"
            elif rssi != 'unknown' and snr != 'unknown':
                signal_info = f" (SNR: {snr}, RSSI: {rssi})"
            if signal_info:
                telegram_text += signal_info

            # ✅ Отправка с reply_to_message_id если есть родительское сообщение
            sent_msg = self.bot.send_message(
                self.telegram_chat_id, 
                telegram_text,
                reply_to_message_id=telegram_parent_id if telegram_parent_id else None
            )
            
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
                    logger.info("Запуск Telegram polling...")
                    self.bot.polling(none_stop=True, interval=0, timeout=20)
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
        """Настройка подписки на события Meshtastic."""
        if self.interface:
            pub.subscribe(self._on_receive, "meshtastic.receive")
            logger.info("Подписка на события meshtastic.receive установлена")

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

            send_kwargs = self._get_send_kwargs(reply_id, channel_name)

            # Запись ВХОДЯЩЕГО сообщения в файл
            if is_broadcast:
                self._log_message_to_file('general', short_name, original_text, rssi, snr, hop_count, is_outgoing=False)
            else:
                self._log_message_to_file('private', short_name, original_text, rssi, snr, hop_count, to_id=to_id, is_outgoing=False)

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
                    reply_id  # Передаем reply_id!
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
                    meshtastic_msg_id
                )
            else:
                logger.debug(f"Ключевые слова не найдены в: '{original_text}'")
                
        except UnicodeDecodeError as e:
            logger.error(f"Ошибка декодирования текста: {e}")
        except Exception as e:
            logger.error(f"Ошибка обработки пакета: {e}", exc_info=True)

    def _handle_auto_reply(self, is_private, short_name, node_id, send_kwargs, rssi, snr, packet, channel_name, original_text, meshtastic_msg_id):
        """Метод для обработки автоматического ответа на ключевые слова."""
        if not self.interface or not self.is_connected:
            logger.warning("Нет активного подключения к Meshtastic, пропускаем автоответ")
            return

        if is_private and short_name.lower() not in self.private_node_names:
            logger.debug(f"Нода {short_name} не в списке private_node_names, пропускаем автоответ")
            return

        # ✅ ИСПРАВЛЕНИЕ: создаем новый auto_reply_kwargs с replyId = meshtastic_msg_id
        auto_reply_kwargs = {}
        if meshtastic_msg_id:
            auto_reply_kwargs['replyId'] = meshtastic_msg_id  # ID текущего сообщения!
        if channel_name:
            auto_reply_kwargs['channel'] = channel_name

        reply = None
        
        if is_private:
            reply = self._get_signal_reply(short_name, rssi, snr, self.private_suffix)
            logger.debug(f"Сигнал для private: RSSI={rssi}, SNR={snr}")
            send_type = self._send_to_meshtastic(reply, auto_reply_kwargs, node_id)  # ✅ используем auto_reply_kwargs
            if send_type:
                logger.info(f"Отправлен ответ в личный канал: {reply} ({send_type}) -> {node_id}")
                # Запись АВТООТВЕТА в файл (private)
                self._log_message_to_file('private', short_name, reply, to_id=node_id, is_bot_reply=True)
        else:
            hop_start = packet.get('hopStart')
            hop_limit = packet.get('hopLimit')
            if hop_start is not None and hop_limit is not None:
                hop_count = hop_start - hop_limit
                if hop_count > 0:
                    reply = self._get_hops_reply(short_name, hop_count, self.general_suffix)
                    logger.debug(f"Хопы для broadcast: {hop_count}")
                else:
                    reply = self._get_direct_reply(short_name, snr, rssi, self.general_suffix)
                    logger.debug(f"Прямой broadcast: сигнал RSSI={rssi}, SNR={snr}")
            else:
                reply = self._get_direct_reply(short_name, snr, rssi, self.general_suffix)
                logger.warning(f"Хопы не определены, fallback на сигнал")
            
            send_type = self._send_to_meshtastic(reply, auto_reply_kwargs)  # ✅ используем auto_reply_kwargs
            if send_type:
                logger.info(f"Отправлен ответ: {reply} (broadcast)")
                # Запись АВТООТВЕТА в файл (general)
                self._log_message_to_file('general', short_name, reply, is_bot_reply=True)
        
        if reply:
            # ✅ передаем meshtastic_msg_id (ID исходного сообщения)
            self._forward_auto_reply_to_telegram(
                short_name, 
                original_text, 
                reply, 
                meshtastic_msg_id,  # это правильный ID!
                is_private
            )

    # ==================== ОСНОВНОЙ ЗАПУСК ====================
    
    def run(self):
        """Запуск бота: Telegram polling в фоне + основной цикл с автопереподключением."""
        print(f"🚀 Запуск Meshtastic Telegram Bot...")
        print(f"📡 Адрес Meshtastic: {self.ip}:{self.port}")
        print(f"🤖 Telegram: {'включен' if self.bot else 'отключен'}")

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

                # ✅ ИСПРАВЛЕНИЕ: проверка соединения и автопереподключение только если не было ручного отключения
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

                time.sleep(1)
                
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