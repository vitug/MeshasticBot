Этот проект представляет собой Telegram-бота для интеграции с сетью Meshtastic. Бот позволяет отправлять и получать текстовые сообщения между Telegram и Meshtastic-устройствами, автоматически отвечать на ключевые слова (с информацией о сигнале RSSI/SNR или количестве хопов), логировать сообщения в файлы и поддерживать приватные/групповые чаты. Поддерживается автопереподключение, разбивка длинных сообщений на части и сканирование нод для маппинга имен/ID.

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
    
3. Создайте файл config.json в корне проекта (пример ниже):
    
    text
    
    ```
    {
        "ip": "192.168.1.100",
        "port": 4403,
        "keywords": ["help", "signal"],
        "private_node_names": ["node1", "node2"],
        "general_suffix": " OK",
        "private_suffix": " private OK",
        "telegram_token": "YOUR_TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "YOUR_CHAT_ID",
        "default_channel": "LongFast"
    }
    ```
    
    - Получите telegram_token от [@BotFather](https://t.me/BotFather).
    - telegram_chat_id можно указать позже (бот сохранит его автоматически после первого сообщения).
4. Запустите бота:
    
    text
    
    ```
    python mesh_bot.py
    ```
    
5. В Telegram:
    - Отправляйте сообщения боту — они уйдут в Meshtastic (broadcast по умолчанию).
    - Используйте /pm <node_name> <text> для приватных сообщений (node_name из private_node_names).
    - /connect <ip> <port> — подключение к устройству.
    - /status — статус соединения.
    - /nodes — список известных нод.
    - /help — помощь.

Сообщения из Meshtastic пересылаются в Telegram с префиксами (например, [NodeName] Text (SNR: 20, RSSI: -70)). Автоответы на ключевые слова отправляются автоматически.

Логи сообщений сохраняются в папке messages_logs/ (general_messages.txt, private_messages.txt).

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

Убедитесь, что config.json настроен правильно. Сервис будет автоматически переподключаться при разрыве связи.
