# Meshtastic Telegram Bot

Этот проект представляет собой Telegram-бота для интеграции с сетью Meshtastic. Бот позволяет отправлять и получать текстовые сообщения между Telegram и Meshtastic-устройствами, автоматически отвечать на ключевые слова (с информацией о сигнале RSSI/SNR или количестве хопов), логировать сообщения в файлы и поддерживать приватные/групповые чаты. Поддерживается автопереподключение, разбивка длинных сообщений на части и сканирование нод для маппинга имен/ID.

- **Фильтрация автоответов по хопам**: Для общего канала можно настроить интервал хопов (hop_filter_interval: [min, max] в config.json), в пределах которого автоответы на ключевые слова не отправляются в Meshtastic (чтобы избежать спама), но уведомление с меткой [FILTERED] пересылается в Telegram.
- **Настраиваемый таймаут Telegram polling**: Параметр telegram_timeout (в секундах) в config.json позволяет задавать интервал опроса Telegram API для баланса между отзывчивостью и нагрузкой (по умолчанию 60s).
- **Поддержка reply-цепочек**: Ответы в Telegram автоматически привязываются к родительскому сообщению (если это reply в Meshtastic), создавая удобные цепочки обсуждений.
- **Разбивка длинных сообщений**: Сообщения длиннее ~200 байт автоматически разбиваются на части с нумерацией [1/3], [2/3] и т.д., чтобы соответствовать лимитам Meshtastic.
- **Детализированное логирование**: Логи сообщений теперь включают префиксы [IN] (входящее), [OUT] (исходящее), [BOT] (автоответ), информацию о via-нодах, сырые значения hop_start/hop_limit, а также направление для приватных сообщений.
- **Динамическая перезагрузка config**: Бот автоматически обнаруживает изменения в config.json (каждые 10s) и перезагружает ключевые параметры (keywords, suffixes, private_nodes, hop_filter_interval, telegram_timeout) без перезапуска.
- **Параметр node_long_name**: Fallback-имя для long_name для установки имени ноды при смене пресета (настраивается в config.json, по умолчанию 'Node').

This project is a Telegram bot for integrating with the Meshtastic network. The bot enables sending and receiving text messages between Telegram and Meshtastic devices, automatic replies to keywords (with signal info like RSSI/SNR or hop count), logging messages to files, and handling private/group chats. It supports auto-reconnection, splitting long messages into parts, and node scanning for name/ID mapping.

## Для пользователей (User Instructions)

### Быстрый запуск

1. Клонируйте репозиторий:
    
    text
    
    ```
    git clone https://github.com/vitug/MeshasticBot.git
    cd MeshasticBot
    ```
    
2. Установите зависимости:
    
    text
    
    ```
    pip install -r requirements.txt
    ```
    
3. Создайте файл config.json в корне проекта (пример ниже, с новыми параметрами):
    
    text
    
    ```
    {
        "ip": "192.168.1.100",
        "port": 4403,
        "keywords": ["ping", "test"],
        "private_node_names": ["node1", "node2"],
        "general_suffix": " OK",
        "private_suffix": " private OK",
        "telegram_token": "YOUR_TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "YOUR_CHAT_ID",
        "default_channel": "LongFast",
        "node_long_name": "MyNode",
        "telegram_timeout": 60,
        "hop_filter_interval": [1, 3]
    }
    ```
    
    - Получите telegram_token от [@BotFather](https://t.me/BotFather).
    - telegram_chat_id можно указать позже (бот сохранит его автоматически после первого сообщения от пользователя).
    
4. Запустите бота:
    
    text
    
    ```
    python mesh_bot.py
    ```
    
5. В Telegram:
    
    - Отправляйте сообщения боту — они уйдут в Meshtastic (broadcast по умолчанию). Длинные сообщения разбиваются автоматически.
    - /pm <node_name> <text> — отправка приватного сообщения указанной ноде (node_name из списка private_node_names в config.json, нода добавляется в список автоматически при отправке первого сообщения).
    - /connect <ip> <port> — подключение к другому устройству Meshtastic (обновит IP и порт в config.json для автопереподключения).
    - /disconnect — отключение от текущей ноды Meshtastic (остановит переподключение до следующей команды /connect).
    - /status — статус соединения с Meshtastic (показывает подключение, время последней проверки и флаги).
    - /set_preset [<preset> <slot>] — установка пресета LoRa для глобальной конфигурации (влияет на все ноды).
        - Без параметров: показывает справку со списком доступных пресетов (например, LongFast, MediumSlow, ShortFast и т.д.).
        - С параметрами: /set_preset <preset_name> <slot> (slot: 0–7). Примеры:
            - /set_preset LongFast 0 — устанавливает пресет LongFast в слот 0.
            - /set_preset MediumSlow 1 — устанавливает MediumSlow в слот 1.
        - Доступные пресеты: LongFast, MediumSlow, ShortFast, VeryLongSlow, Test (полный список в справке команды).
    - /help — помощь по всем командам (с примерами и описаниями).
    - Ответы на сообщения в Telegram автоматически создают reply-цепочки, если исходное сообщение было reply в Meshtastic.

Сообщения из Meshtastic пересылаются в Telegram с префиксами (например, [NodeName] Text (SNR: 20, RSSI: -70)). Автоответы на ключевые слова отправляются автоматически, с фильтрацией по хопам для broadcast (уведомления о фильтре в Telegram).

Логи сообщений сохраняются в папке messages_logs/ (general_messages.txt, private_messages.txt, private_group_messages.txt) с деталями о сигнале, хопах и via-нодах (при наличии в пакете).

### Автоответы на ключевые слова

- Если сообщение relayed (hop_count > 0) — ответ с хопами; иначе — с RSSI/SNR.
- Суффиксы добавляются из config (general_suffix/private_suffix).

## Для администраторов (Admin Installation as Systemd Service)

Для запуска как системного сервиса на Linux (например, Raspberry Pi):

1. Активируйте виртуальное окружение (если используете):
    
    text
    
    ```
    source meshtastic_env/bin/activate
    ```
    
2. Установите зависимости:
    
    text
    
    ```
    pip install -r requirements.txt
    ```
    
3. Создайте systemd-сервис:
    
    text
    
    ```
    sudo nano /etc/systemd/system/mesh-bot.service
    ```
    
    Вставьте содержимое:
    
    text
    
    ```
    [Unit]
    Description=Meshtastic Mesh Bot
    After=network.target
    
    [Service]
    Type=simple
    User=user  # Замените на вашего пользователя
    WorkingDirectory=/home/user/src/meshtastic  # Путь к проекту
    ExecStart=/bin/bash -c 'source meshtastic_env/bin/activate && python mesh_bot.py'  # Если без env, уберите source
    Restart=always
    RestartSec=10
    WatchdogSec=300
    NotifyAccess=main
    StandardOutput=journal
    StandardError=journal
    
    [Install]
    WantedBy=multi-user.target
    ```
    
    Сервис поддерживает systemd watchdog (пинги каждые 60s) для мониторинга и автоматического перезапуска при зависаниях.
    
4. Команды управления сервисом:
    
    text
    
    ```
    sudo systemctl daemon-reload
    sudo systemctl enable mesh-bot  # Автозапуск при загрузке
    sudo systemctl start mesh-bot
    sudo systemctl status mesh-bot
    journalctl -u mesh-bot -f  # Просмотр логов в реальном времени
    ```
    
    Для остановки/перезапуска: sudo systemctl stop/restart mesh-bot.
    

Убедитесь, что config.json настроен правильно. Сервис будет автоматически переподключаться при разрыве связи (проверка каждые 10s, переподключение каждые 30s). Изменения в config.json применяются динамически без рестарта.

## Лицензия

MIT License. Подробности в LICENSE.