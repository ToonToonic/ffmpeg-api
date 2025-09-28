from flask import Flask, request, jsonify
import subprocess
import requests
import os
import tempfile
import uuid
import logging
from werkzeug.utils import secure_filename
import boto3
from botocore.exceptions import ClientError
import gunicorn.app.base

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Настройки для загрузки в R2
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET = os.environ.get('R2_BUCKET')

R2_PUBLIC_DOMAIN = os.environ.get('R2_PUBLIC_DOMAIN', 'https://pub-bd37e3cfae574077ab0d4461a749b0d3.r2.dev')  # Настрой в Variables

def download_file(url, filename):
    """Скачивает файл по URL"""
    try:
        logger.info(f"Downloading file from {url} to {filename}")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
       
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"Successfully downloaded {url}")
        return True
    except Exception as e:
        logger.error(f"Ошибка скачивания {url}: {e}")
        return False

def upload_to_r2(file_path, object_name=None):
    """Загружает файл в Cloudflare R2"""
    if object_name is None:
        object_name = os.path.basename(file_path)
   
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET]):
        logger.error("R2 credentials not configured")
        return None
   
    endpoint_url = f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com'
   
    s3_client = boto3.client(
        's3',
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name='auto'
    )
   
    try:
        logger.info(f"Uploading {file_path} to {R2_BUCKET}/{object_name}")
        s3_client.upload_file(file_path, R2_BUCKET, object_name)
        public_url = f"https://{R2_PUBLIC_DOMAIN}/{object_name}"
        logger.info(f"Uploaded successfully: {public_url}")
        return public_url
    except ClientError as e:
        logger.error(f"R2 upload error: {e.response['Error']}")
        return None
    except Exception as e:
        logger.error(f"Unexpected R2 error: {e}")
        return None

@app.route('/merge-videos', methods=['POST'])
def merge_videos():
    """Основной эндпоинт для склейки видео"""
    logger.info("Received /merge-videos request")
    try:
        data = request.json
        scenes = data.get('scenes', [])
        background_music = data.get('background_music_url')
       
        if not scenes:
            logger.warning("No scenes provided")
            return jsonify({'error': 'No scenes provided'}), 400
       
        with tempfile.TemporaryDirectory() as temp_dir:
            video_files = []
            audio_files = []
           
            for i, scene in enumerate(scenes):
                video_url = scene.get('video_url')
                audio_url = scene.get('audio_url')
                duration = scene.get('duration', 8)
               
                if video_url:
                    video_path = os.path.join(temp_dir, f"video_{i}.mp4")
                    if download_file(video_url, video_path):
                        video_files.append((video_path, duration))
               
                if audio_url:
                    audio_path = os.path.join(temp_dir, f"audio_{i}.wav")
                    if download_file(audio_url, audio_path):
                        audio_files.append((audio_path, duration))
           
            if not video_files:
                logger.error("No valid video files found")
                return jsonify({'error': 'No valid video files found'}), 400
           
            final_video = merge_video_files(video_files, audio_files, background_music, temp_dir)
           
            if not final_video:
                logger.error("Video merging failed")
                return jsonify({'error': 'Video merging failed'}), 500
           
            unique_id = str(uuid.uuid4())
            r2_object_name = f"videos/{unique_id}.mp4"
            result_url = upload_to_r2(final_video, r2_object_name)
           
            if result_url:
                logger.info(f"Returning video URL: {result_url}")
                return jsonify({
                    'success': True,
                    'video_url': result_url,
                    'duration': sum(duration for _, duration in video_files)
                })
            else:
                logger.error("Upload failed")
                return jsonify({'error': 'Upload failed'}), 500
               
    except Exception as e:
        logger.error(f"Error in merge_videos: {e}")
        return jsonify({'error': str(e)}), 500

def merge_video_files(video_files, audio_files, background_music, temp_dir):
    """Склеивает видео и аудио файлы через FFmpeg"""
    logger.info(f"Merging {len(video_files)} video files")
    try:
        concat_file = os.path.join(temp_dir, 'concat_list.txt')
       
        with open(concat_file, 'w') as f:
            for video_path, duration in video_files:
                f.write(f"file '{video_path}'\n")
                f.write(f"duration {duration}\n")
       
        output_file = os.path.join(temp_dir, 'final_video.mp4')
       
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-c:v', 'copy',  # Быстрее, без перекодирования
            '-c:a', 'aac',
            '-b:a', '128k',
            output_file
        ]
       
        if audio_files:
            audio_concat_file = os.path.join(temp_dir, 'audio_concat.txt')
            with open(audio_concat_file, 'w') as f:
                for audio_path, _ in audio_files:
                    f.write(f"file '{audio_path}'\n")
           
            merged_audio = os.path.join(temp_dir, 'merged_audio.wav')
            audio_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', audio_concat_file,
                '-c:a', 'pcm_s16le',
                merged_audio
            ]
            result = subprocess.run(audio_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"Audio merge error: {result.stderr}")
                return None
           
            cmd = [
                'ffmpeg', '-y',
                '-i', output_file,
                '-i', merged_audio,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-shortest',
                f"{output_file}_final.mp4"
            ]
            output_file = f"{output_file}_final.mp4"
       
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(output_file):
            logger.info(f"FFmpeg success: {output_file}")
            return output_file
        else:
            logger.error(f"FFmpeg error: {result.stderr}")
            return None
           
    except Exception as e:
        logger.error(f"Merge error: {e}")
        return None

@app.route('/health', methods=['GET'])
def health_check():
    """Проверка здоровья сервиса"""
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, check=True)
        return jsonify({'status': 'healthy', 'service': 'ffmpeg-api', 'ffmpeg': 'available'})
    except subprocess.CalledProcessError:
        return jsonify({'status': 'unhealthy', 'service': 'ffmpeg-api', 'ffmpeg': 'not available'}), 500

@app.route('/test', methods=['POST'])
def test_endpoint():
    """Тестовый эндпоинт для отладки"""
    data = request.json
    logger.info(f"Received test data: {data}")
    return jsonify({
        'received_data': data,
        'ffmpeg_available': subprocess.run(['ffmpeg', '-version'], capture_output=True).returncode == 0
    })

class StandaloneApplication(gunicorn.app.base.BaseApplication):
    def __init__(self, app, options=None):
        self.options = options or {}
        self.application = app
        super().__init__()

    def load_config(self):
        for key, value in self.options.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        return self.application

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    options = {
        'bind': f'0.0.0.0:{port}',
        'workers': 2,
        'timeout': 120  # Увеличен для FFmpeg
    }
    StandaloneApplication(app, options).run()
