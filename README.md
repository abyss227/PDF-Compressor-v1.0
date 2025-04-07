# PDF-Compressor-v1.0 

PDF Compressor v1.0 - это веб-приложение для сжатия PDF-файлов которое требует доработок в части безопасности и прочего. Приложение предоставляет три уровня сжатия и работает в полностью асинхронном режиме, обеспечивая стабильную обработку даже больших файлов.




**О проекте** 
Необходимо доработать в части бесопасности и прочего, просим не использовать в прод средах без дороботок и проверки работоспособности. 

PDF Compressor v1.0 - это веб-приложение для сжатия PDF-файлов которое требует доработок в части безопасности и прочего. Приложение предоставляет три уровня сжатия и работает в полностью асинхронном режиме, обеспечивая стабильную обработку даже больших файлов.


**Особенности** 

- Три уровня сжатия (сильное, среднее, слабое)
- Асинхронная обработка через Celery и RabbitMQ
- Масштабируемое хранилище на MinIO
- Кеширование и сессии через Redis
- Отслеживание прогресса в реальном времени
- Drag-and-drop интерфейс
- Полностью контейнеризованное решение (Docker)

**Технологический стек** 
- Backend
- Python 3.9
- Flask (веб-фреймворк)
- Celery (асинхронные задачи)
- Redis (кеширование и сессии)
- MinIO (хранилище файлов)
- RabbitMQ (очереди сообщений)
- img2pdf и pdf2image (обработка PDF)

**Frontend** 
- Чистый HTML/CSS/JavaScript
- Адаптивный дизайн
- Drag-and-drop интерфейс
- Инфраструктура
- Docker и Docker Compose
- Микросервисная архитектура

**Установка и запуск** 
- Требования
- Docker
- Docker Compose

**Запуск** 

Клонируйте репозиторий:
```
git clone https://github.com/ваш-username/pdf-compressor-pro.git
cd pdf-compressor-pro
```
Cоздайте файл .env (на основе .env.example):
```
cp .env.example .env
```
Запустите приложение:
```
Docker-compose up --build
```
Приложение будет доступно по адресу:
```
http://localhost:5000
```
Для доступа к MinIO UI:
```
http://localhost:9001
```
Логин: minioadmin
Пароль: minioadmin

**Структура проекта**
``````
pdf-compressor-pro/
├── app/                  # Основное приложение
│   ├── app.py            # Flask приложение
│   ├── Dockerfile        # Конфигурация Docker
│   └── requirements.txt  # Зависимости Python
├── docker-compose.yml    # Конфигурация сервисов
├── .env.example          # Пример переменных окружения
└── README.md             # Документация
``````
**Лицензия**
- Этот проект распространяется под лицензией MIT.

  
