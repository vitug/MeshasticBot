# MeshTelegramBot

Этот проект представляет собой Telegram-бота для интеграции с сетью Meshtastic. Бот позволяет отправлять и получать текстовые сообщения между Telegram и Meshtastic-устройствами, автоматически отвечать на ключевые слова (с информацией о сигнале RSSI/SNR для приватных сообщений или количестве хопов/сигнале для broadcast), логировать сообщения в файлы и поддерживать приватные/групповые чаты в Telegram. Поддерживается автопереподключение к Meshtastic, разбивка длинных сообщений на части (с нумерацией), сканирование нод для маппинга имен/ID, перезагрузка конфигурации на лету (кроме Telegram-токена/chat_id) и watchdog для systemd. Есть возможность удаленно  менять пресет для устройства Meshtastic.

This project is a Telegram bot for integrating with the Meshtastic network. The bot enables sending and receiving text messages between Telegram and Meshtastic devices, automatic replies to keywords (with signal info like RSSI/SNR for private or hop count/signal for broadcast), logging messages to files, and handling private/group chats in Telegram. It supports auto-reconnection to Meshtastic, splitting long messages into parts (with numbering), node scanning for name/ID mapping, on-the-fly config reload (except Telegram token/chat_id), and systemd watchdog. You can change remotely preset for Meshtastic device.

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
    
3. Создайте файл config.json в корне проекта (пример ниже):
    
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
        "node_long_name": "Node",
        "telegram_timeout": 60
    }
    ```
    
    - Получите telegram_token от [@BotFather](https://t.me/BotFather).
    - telegram_chat_id можно указать позже (бот сохранит его автоматически после первого сообщения от пользователя).
    - node_long_name: отображаемое имя вашего устройства в логах и ответах (fallback: "Node").
    - telegram_timeout: таймаут polling для Telegram (по умолчанию 60 секунд, влияет на отзывчивость).
4. Запустите бота:
    
    text
    
    ```
    python mesh_bot.py
    ```
    
5. В Telegram:
    
    - Отправляйте сообщения боту — они уйдут в Meshtastic (broadcast по умолчанию, с разбивкой на части если >200 байт).
    - /pm <node_name> <text> — приватное сообщение (node_name из private_node_names).
    - /connect <ip> <port> — подключение к другому устройству Meshtastic (обновит config.json).
    - /disconnect — отключение от ноды Meshtastic
    - /status — статус соединения с Meshtastic.
    - /set_preset — установка режима работы LoRa.    - /help — помощь по командам.

Сообщения из Meshtastic пересылаются в Telegram с префиксами (например, [NodeName] Text (SNR: 20, RSSI: -70) или [PRIVATE from NodeName] Text). Автоответы на ключевые слова: для broadcast — количество хопов (если relayed) или сигнал (RSSI/SNR); для приватных — сигнал (RSSI/SNR). Reply-цепочки сохраняются в Telegram и Meshtastic.

Логи сообщений сохраняются в папке messages_logs/ (general_messages.txt для broadcast, private_messages.txt для приватных). Основные логи бота — в mesh_bot.log. Автопереподключение к Meshtastic каждые 30 секунд при потере связи.

### Автоответы на ключевые слова

- **Broadcast (общий канал)**: Если сообщение relayed (hop_count > 0) — ответ с хопами; иначе — с RSSI/SNR.
- **Приватное**: Всегда с RSSI/SNR.
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
    WorkingDirectory=/home/user/MeshasticBot  # Путь к проекту (адаптируйте)
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
    

Убедитесь, что config.json настроен правильно. Сервис будет автоматически переподключаться при разрыве связи с Meshtastic и отправлять watchdog-пины (требует sdnotify). Изменения в config.json (кроме Telegram-токена/chat_id) применяются на лету.

## Возможные проблемы и отладка

- **Нет подключения к Meshtastic**: Проверьте IP/порт в config.json, используйте /connect или /status.
- **Telegram не отвечает**: Увеличьте telegram_timeout в config.json (перезапуск обязателен).
- **Длинные сообщения**: Автоматическая разбивка на части с маркерами [1/3], задержка 1.5с между частями.
- **Логи**: mesh_bot.log для ошибок, messages_logs/ для сообщений (с префиксами [IN]/[OUT]/[BOT]).
- **Node mapping**: Автоматическое обновление из сканирования нод каждые 30с.

## Лицензия

MIT License. Подробности в LICENSE.