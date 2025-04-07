import os
import uuid
import img2pdf
import logging
from pdf2image import convert_from_path
from flask import Flask, request, send_file, render_template_string, jsonify, session
from flask_session import Session
from minio import Minio
from minio.error import S3Error
from celery import Celery
import redis
from dotenv import load_dotenv


# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Инициализация приложения Flask
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'supersecretkey')

# Конфигурация Flask-Session для использования Redis
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_REDIS'] = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    password=os.getenv('REDIS_PASSWORD'),
    db=int(os.getenv('REDIS_DB', 0))
)
Session(app)

# Конфигурация MinIO
minio_client = Minio(
    os.getenv('MINIO_ENDPOINT', 'minio:9000'),
    access_key=os.getenv('MINIO_ACCESS_KEY', 'minioadmin'),
    secret_key=os.getenv('MINIO_SECRET_KEY', 'minioadmin'),
    secure=False
)

# Создание bucket если не существует
MINIO_BUCKET = os.getenv('MINIO_BUCKET', 'pdf-compressor')
try:
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
except S3Error as e:
    logger.error(f"Error creating MinIO bucket: {e}")

# Конфигурация Celery
app.config['CELERY_BROKER_URL'] = os.getenv('CELERY_BROKER_URL', 'amqp://guest:guest@rabbitmq:5672//')
app.config['CELERY_RESULT_BACKEND'] = f"redis://:{os.getenv('REDIS_PASSWORD')}@redis:6379/0"
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# Настройки сжатия
COMPRESSION_SETTINGS = {
    "strong": {"dpi": 72, "quality": 30, "label": "Сильное сжатие", "desc": "Меньше качества, сильно уменьшенный размер"},
    "medium": {"dpi": 100, "quality": 50, "label": "Среднее сжатие", "desc": "Баланс между качеством и размером"},
    "weak": {"dpi": 150, "quality": 80, "label": "Слабое сжатие", "desc": "Наилучшее качество, минимальное сжатие"},
}

def validate_pdf(file_path):
    """Проверка валидности PDF файла"""
    try:
        with open(file_path, "rb") as f:
            header = f.read(4)
            return header == b'%PDF'
    except Exception as e:
        logger.error(f"PDF validation error: {str(e)}")
        return False

def upload_to_minio(file_path, object_name):
    """Загрузка файла в MinIO с валидацией"""
    try:
        if not os.path.exists(file_path):
            raise Exception(f"File {file_path} not found")
            
        if os.path.getsize(file_path) == 0:
            raise Exception("File is empty")
            
        if not validate_pdf(file_path):
            raise Exception("Invalid PDF file")
            
        minio_client.fput_object(MINIO_BUCKET, object_name, file_path)
        return True
    except Exception as e:
        logger.error(f"MinIO upload error: {str(e)}")
        return False

def download_from_minio(object_name, file_path):
    """Скачивание файла из MinIO"""
    try:
        minio_client.fget_object(MINIO_BUCKET, object_name, file_path)
        return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    except S3Error as e:
        logger.error(f"MinIO download error: {str(e)}")
        return False

# Celery задача для обработки PDF
@celery.task(bind=True)
def process_pdf_task(self, session_id, original_filename, minio_object_name, compression_mode):
    """Задача обработки PDF"""
    temp_dir = f"temp_{session_id}"
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        # 1. Скачивание исходного файла
        input_pdf = os.path.join(temp_dir, f"input_{original_filename}")
        if not download_from_minio(minio_object_name, input_pdf):
            raise Exception("Failed to download from MinIO")

        # 2. Конвертация в изображения
        self.update_state(state='PROGRESS', meta={'step': 'converting', 'progress': 30})
        images = convert_from_path(
            input_pdf,
            dpi=COMPRESSION_SETTINGS[compression_mode]["dpi"],
            output_folder=temp_dir,
            fmt='jpeg',
            thread_count=4,
            poppler_path="/usr/bin"
        )

        # 3. Сохранение изображений
        image_paths = []
        for i, img in enumerate(images):
            img_path = os.path.join(temp_dir, f"page_{i+1}.jpg")
            img.save(img_path, "JPEG", quality=COMPRESSION_SETTINGS[compression_mode]["quality"])
            image_paths.append(img_path)

        # 4. Конвертация обратно в PDF
        self.update_state(state='PROGRESS', meta={'step': 'compressing', 'progress': 70})
        compressed_pdf = os.path.join(temp_dir, f"compressed_{original_filename}")
        with open(compressed_pdf, "wb") as f:
            f.write(img2pdf.convert(image_paths))

        # 5. Загрузка результата в MinIO
        compressed_object_name = f"{session_id}/{uuid.uuid4()}_compressed_{original_filename}"
        if not upload_to_minio(compressed_pdf, compressed_object_name):
            raise Exception("Failed to upload compressed file")

        return {
            'status': 'SUCCESS',
            'compressed_object_name': compressed_object_name,
            'original_size': os.path.getsize(input_pdf),
            'compressed_size': os.path.getsize(compressed_pdf),
            'compression_ratio': (os.path.getsize(input_pdf) - os.path.getsize(compressed_pdf)) / os.path.getsize(input_pdf) * 100
        }

    except Exception as e:
        logger.error(f"PDF processing error: {str(e)}")
        return {'status': 'FAILURE', 'error': str(e)}
        
    finally:
        # Очистка временных файлов
        if os.path.exists(temp_dir):
            for f in os.listdir(temp_dir):
                os.remove(os.path.join(temp_dir, f))
            os.rmdir(temp_dir)

@app.route("/", methods=["GET"])
def index():
    # Генерация уникального ID сессии
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    
    return render_template_string('''
     <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>PDF Compressor Pro</title>
        <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css" rel="stylesheet">
        <style>
            :root {
                --primary: #4361ee;
                --primary-dark: #3a56d4;
                --secondary: #3f37c9;
                --light: #f8f9fa;
                --dark: #212529;
                --gray: #6c757d;
                --success: #4bb543;
                --danger: #ff3333;
            }
            
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            body {
                font-family: 'Roboto', sans-serif;
                background-color: #f5f7ff;
                color: var(--dark);
                line-height: 1.6;
            }
            
            .container {
                max-width: 800px;
                margin: 0 auto;
                padding: 2rem;
            }
            
            header {
                text-align: center;
                margin-bottom: 2rem;
            }
            
            h1 {
                font-size: 2.5rem;
                margin-bottom: 0.5rem;
                color: var(--primary);
            }
            
            .subtitle {
                color: var(--gray);
                font-weight: 300;
                margin-bottom: 1.5rem;
            }
            
            .card {
                background: white;
                border-radius: 10px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.1);
                padding: 2rem;
                transition: transform 0.3s, box-shadow 0.3s;
            }
            
            .card:hover {
                transform: translateY(-5px);
                box-shadow: 0 15px 35px rgba(0, 0, 0, 0.15);
            }
            
            .upload-area {
                border: 2px dashed #ccc;
                border-radius: 8px;
                padding: 3rem 1rem;
                text-align: center;
                cursor: pointer;
                transition: all 0.3s;
                margin-bottom: 1.5rem;
                position: relative;
                overflow: hidden;
            }
            
            .upload-area:hover {
                border-color: var(--primary);
                background: rgba(67, 97, 238, 0.05);
            }
            
            .upload-area.active {
                border-color: var(--primary);
                background: rgba(67, 97, 238, 0.1);
            }
            
            .upload-area i {
                font-size: 3rem;
                color: var(--primary);
                margin-bottom: 1rem;
            }
            
            .upload-area h3 {
                margin-bottom: 0.5rem;
            }
            
            .upload-area p {
                color: var(--gray);
            }
            
            #file-input {
                display: none;
            }
            
            .file-info {
                display: none;
                margin-top: 1rem;
                padding: 1rem;
                background: rgba(67, 97, 238, 0.1);
                border-radius: 5px;
                text-align: left;
            }
            
            .file-info.show {
                display: block;
                animation: fadeIn 0.5s;
            }
            
            .compression-options {
                margin: 1.5rem 0;
            }
            
            .option {
                display: flex;
                align-items: center;
                padding: 1rem;
                border: 1px solid #eee;
                border-radius: 8px;
                margin-bottom: 0.5rem;
                cursor: pointer;
                transition: all 0.3s;
            }
            
            .option:hover {
                border-color: var(--primary);
                background: rgba(67, 97, 238, 0.05);
            }
            
            .option input {
                margin-right: 1rem;
            }
            
            .option-label {
                font-weight: 500;
            }
            
            .option-desc {
                font-size: 0.9rem;
                color: var(--gray);
            }
            
            .btn {
                display: inline-block;
                background: var(--primary);
                color: white;
                border: none;
                padding: 0.8rem 1.5rem;
                border-radius: 8px;
                font-size: 1rem;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.3s;
                text-align: center;
                width: 100%;
            }
            
            .btn:hover {
                background: var(--primary-dark);
                transform: translateY(-2px);
            }
            
            .btn:disabled {
                background: #ccc;
                cursor: not-allowed;
                transform: none;
            }
            
            .progress-container {
                display: none;
                margin-top: 1.5rem;
            }
            
            .progress-container.show {
                display: block;
                animation: fadeIn 0.5s;
            }
            
            .progress-bar {
                height: 10px;
                background: #e9ecef;
                border-radius: 5px;
                overflow: hidden;
                margin-bottom: 0.5rem;
            }
            
            .progress {
                height: 100%;
                background: var(--primary);
                width: 0%;
                transition: width 0.3s;
            }
            
            .progress-text {
                text-align: center;
                font-size: 0.9rem;
                color: var(--gray);
            }
            
            .result {
                display: none;
                text-align: center;
                margin-top: 1.5rem;
                padding: 1.5rem;
                border-radius: 8px;
                background: rgba(75, 181, 67, 0.1);
                animation: fadeIn 0.5s;
            }
            
            .result.show {
                display: block;
            }
            
            .result i {
                font-size: 2.5rem;
                color: var(--success);
                margin-bottom: 1rem;
            }
            
            .download-btn {
                margin-top: 1rem;
            }
            
            footer {
                text-align: center;
                margin-top: 3rem;
                color: var(--gray);
                font-size: 0.9rem;
            }
            
            @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
            }
            
            @media (max-width: 600px) {
                .container {
                    padding: 1rem;
                }
                
                h1 {
                    font-size: 2rem;
                }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>PDF Compressor v1</h1>
                <p class="subtitle">Уменьшите размер ваших PDF файлов</p>
            </header>
            
            <div class="card">
                <div id="upload-area" class="upload-area">
                    <i class="fas fa-file-pdf"></i>
                    <h3>Перетащите PDF файл сюда</h3>
                    <p>или нажмите для выбора файла</p>
                    <input type="file" id="file-input" accept=".pdf">
                </div>
                
                <div id="file-info" class="file-info">
                    <p><strong>Файл:</strong> <span id="file-name"></span></p>
                    <p><strong>Размер:</strong> <span id="file-size"></span></p>
                </div>
                
                <div class="compression-options">
                    <h3>Выберите уровень сжатия:</h3>
                    
                    <label class="option">
                        <input type="radio" name="compression" value="strong" checked>
                        <div>
                            <div class="option-label">Сильное сжатие</div>
                            <div class="option-desc">Меньше качества, сильно уменьшенный размер</div>
                        </div>
                    </label>
                    
                    <label class="option">
                        <input type="radio" name="compression" value="medium">
                        <div>
                            <div class="option-label">Среднее сжатие</div>
                            <div class="option-desc">Баланс между качеством и размером</div>
                        </div>
                    </label>
                    
                    <label class="option">
                        <input type="radio" name="compression" value="weak">
                        <div>
                            <div class="option-label">Слабое сжатие</div>
                            <div class="option-desc">Наилучшее качество, минимальное сжатие</div>
                        </div>
                    </label>
                </div>
                
                <button id="compress-btn" class="btn" disabled>Сжать PDF</button>
                
                <div id="progress-container" class="progress-container">
                    <div class="progress-bar">
                        <div id="progress" class="progress"></div>
                    </div>
                    <p id="progress-text" class="progress-text">Обработка файла...</p>
                </div>
                
                <div id="result" class="result">
                    <i class="fas fa-check-circle"></i>
                    <h3>Файл успешно сжат!</h3>
                    <p id="result-info"></p>
                    <a id="download-link" href="#" class="btn download-btn">Скачать сжатый PDF</a>
                </div>
            </div>
            
            <footer>
                <p>PDF Compressor v1 &copy; 2025 | Все права защищены</p>
            </footer>
        </div>
        
<script>
    document.addEventListener('DOMContentLoaded', function() {
        const uploadArea = document.getElementById('upload-area');
        const fileInput = document.getElementById('file-input');
        const fileInfo = document.getElementById('file-info');
        const fileName = document.getElementById('file-name');
        const fileSize = document.getElementById('file-size');
        const compressBtn = document.getElementById('compress-btn');
        const progressContainer = document.getElementById('progress-container');
        const progressBar = document.getElementById('progress');
        const progressText = document.getElementById('progress-text');
        const result = document.getElementById('result');
        const resultInfo = document.getElementById('result-info');
        const downloadLink = document.getElementById('download-link');
        
        let selectedFile = null;
        let currentTaskId = null;
        let currentSessionId = null;
        
        // Обработка drag and drop
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.classList.add('active');
        });
        
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.classList.remove('active');
        });
        
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('active');
            
            if (e.dataTransfer.files.length) {
                handleFile(e.dataTransfer.files[0]);
            }
        });
        
        // Обработка клика для выбора файла
        uploadArea.addEventListener('click', () => {
            fileInput.click();
        });
        
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length) {
                handleFile(fileInput.files[0]);
            }
        });
        
        // Обработка выбранного файла
        function handleFile(file) {
            if (!file.name.toLowerCase().endsWith('.pdf')) {
                alert('Пожалуйста, выберите PDF файл');
                return;
            }
            
            selectedFile = file;
            
            // Показываем информацию о файле
            fileName.textContent = file.name;
            fileSize.textContent = formatFileSize(file.size);
            fileInfo.classList.add('show');
            
            // Активируем кнопку сжатия
            compressBtn.disabled = false;
            
            // Скрываем предыдущие результаты
            progressContainer.classList.remove('show');
            result.classList.remove('show');
        }
        
        // Форматирование размера файла
        function formatFileSize(bytes) {
            if (bytes === 0) return '0 Bytes';
            
            const k = 1024;
            const sizes = ['Bytes', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }
        
        // Проверка статуса задачи
        async function checkTaskStatus(taskId) {
            try {
                const response = await fetch(`/status/${taskId}`);
                if (!response.ok) {
                    throw new Error('Ошибка при проверке статуса');
                }
                
                const status = await response.json();
                
                if (status.state === 'PROGRESS') {
                    // Обновляем прогресс
                    progressBar.style.width = `${status.progress}%`;
                    progressText.textContent = status.status;
                    // Проверяем снова через секунду
                    setTimeout(() => checkTaskStatus(taskId), 1000);
                } 
                else if (status.state === 'SUCCESS') {
                    // Задача завершена успешно
                    progressBar.style.width = '100%';
                    progressText.textContent = 'Обработка завершена!';
                    
                    // Обновляем информацию о результате
                    resultInfo.textContent = `Исходный размер: ${formatFileSize(status.result.original_size)} | Сжатый размер: ${formatFileSize(status.result.compressed_size)} (${status.result.compression_ratio.toFixed(2)}% меньше)`;
                    
                    // Устанавливаем ссылку для скачивания
                    downloadLink.href = `/download/${currentSessionId}/${selectedFile.name}?task_id=${taskId}`;
                    downloadLink.download = `compressed_${selectedFile.name}`;
                    
                    // Показываем результат
                    result.classList.add('show');
                }
                else if (status.state === 'FAILURE') {
                    // Ошибка обработки
                    progressText.textContent = 'Ошибка: ' + (status.error || 'Неизвестная ошибка');
                    console.error(status.error);
                }
            } catch (error) {
                progressText.textContent = 'Ошибка: ' + error.message;
                console.error(error);
            }
        }
        
        // Обработка нажатия кнопки сжатия
        compressBtn.addEventListener('click', async () => {
            if (!selectedFile) return;
            
            const compressionMode = document.querySelector('input[name="compression"]:checked').value;
            
            // Показываем прогресс
            progressContainer.classList.add('show');
            progressBar.style.width = '0%';
            progressText.textContent = 'Начало обработки...';
            
            try {
                // Создаем FormData для отправки файла
                const formData = new FormData();
                formData.append('pdf', selectedFile);
                formData.append('compression_mode', compressionMode);
                
                // Отправляем файл на сервер
                const response = await fetch('/compress', {
                    method: 'POST',
                    body: formData,
                });
                
                if (!response.ok) {
                    throw new Error('Ошибка при обработке файла');
                }
                
                // Получаем данные о задаче
                const result = await response.json();
                currentTaskId = result.task_id;
                currentSessionId = result.session_id;
                
                // Начинаем отслеживание статуса задачи
                checkTaskStatus(currentTaskId);
                
            } catch (error) {
                progressText.textContent = 'Ошибка: ' + error.message;
                console.error(error);
            }
        });
        
        // Обработка клика по ссылке скачивания
        downloadLink.addEventListener('click', function(e) {
            if (!currentTaskId) {
                e.preventDefault();
                alert('Файл еще не готов к скачиванию');
            }
        });
    });
</script>
    </body>
    </html>
    ''')

@app.route("/compress", methods=["POST"])
def compress():
    if 'pdf' not in request.files:
        return jsonify({"error": "Файл не загружен"}), 400

    file = request.files['pdf']
    compression_mode = request.form.get("compression_mode", "medium")
    
    if file.filename == "":
        return jsonify({"error": "Файл не выбран"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Пожалуйста, загрузите PDF"}), 400

    # Генерация уникального имени файла
    session_id = session.get('session_id', str(uuid.uuid4()))
    session['session_id'] = session_id
    
    unique_id = str(uuid.uuid4())
    original_filename = file.filename
    minio_object_name = f"{session_id}/{unique_id}_{original_filename}"
    
    # Сохраняем файл временно и загружаем в MinIO
    temp_dir = f"temp_{session_id}"
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, original_filename)
    file.save(temp_path)
    
    if not upload_to_minio(temp_path, minio_object_name):
        os.remove(temp_path)
        return jsonify({"error": "Ошибка при загрузке файла"}), 500
    
    os.remove(temp_path)
    
    # Запускаем асинхронную задачу
    task = process_pdf_task.apply_async(
        args=[session_id, original_filename, minio_object_name, compression_mode]
    )
    
    return jsonify({
        "task_id": task.id,
        "session_id": session_id
    }), 202

@app.route("/status/<task_id>", methods=["GET"])
def task_status(task_id):
    task = process_pdf_task.AsyncResult(task_id)
    
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Ожидание обработки...'
        }
    elif task.state == 'PROGRESS':
        response = {
            'state': task.state,
            'status': task.info.get('step', 'Обработка...'),
            'progress': task.info.get('progress', 0)
        }
    elif task.state == 'SUCCESS':
        response = {
            'state': task.state,
            'status': 'Готово',
            'result': task.info
        }
    else:
        response = {
            'state': task.state,
            'status': 'Ошибка',
            'error': str(task.info)  # это может быть исключение
        }
    
    return jsonify(response)

@app.route("/download/<session_id>/<filename>", methods=["GET"])
def download(session_id, filename):
    try:
        # Получаем информацию о задаче, чтобы узнать правильное имя файла в MinIO
        task_id = request.args.get('task_id')
        if not task_id:
            return jsonify({"error": "Не указан ID задачи"}), 400
            
        task = process_pdf_task.AsyncResult(task_id)
        if not task.successful():
            return jsonify({"error": "Задача не завершена или завершена с ошибкой"}), 400
            
        # Получаем настоящее имя файла из результата задачи
        compressed_object_name = task.result.get('compressed_object_name')
        if not compressed_object_name:
            return jsonify({"error": "Не удалось определить имя файла"}), 400
        
        # Скачиваем файл из MinIO
        temp_path = f"/tmp/{uuid.uuid4()}_{filename}"
        try:
            # Получаем объект из MinIO
            minio_client.fget_object(MINIO_BUCKET, compressed_object_name, temp_path)
            
            # Проверяем файл
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 1024:
                return jsonify({"error": "Файл поврежден или слишком мал"}), 500
                
            # Отправляем файл
            return send_file(
                temp_path,
                as_attachment=True,
                download_name=f"compressed_{filename}",
                mimetype='application/pdf'
            )
        except S3Error as e:
            logger.error(f"MinIO download error: {str(e)}")
            return jsonify({"error": "Ошибка при загрузке файла"}), 500
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        return jsonify({"error": str(e)}), 500
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
