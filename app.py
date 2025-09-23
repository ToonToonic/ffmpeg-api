from flask import Flask, request, jsonify
import subprocess
import requests
import os
import tempfile
import uuid
from werkzeug.utils import secure_filename
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)

# Настройки для загрузки в S3 (замените на свои)
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
S3_BUCKET = os.environ.get('S3_BUCKET')

def download_file(url, filename):
    """Скачивает файл по URL"""
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"Ошибка скачивания {url}: {e}")
        return False

def upload_to_s3(file_path, object_name=None):
    """Загружает файл в S3"""
    if object_name is None:
        object_name = os.path.basename(file_path)
    
    s3_client = boto3.client(
        's3',
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY
    )
    
    try:
        s3_client.upload_file(file_path, S3_BUCKET, object_name)
        return f"https://{S3_BUCKET}.s3.amazonaws.com/{object_name}"
    except ClientError as e:
        print(f"Ошибка загрузки в S3: {e}")
        return None

@app.route('/merge-videos', methods=['POST'])
def merge_videos():
    """Основной эндпоинт для склейки видео"""
    
    try:
        data = request.json
        scenes = data.get('scenes', [])
        background_music = data.get('background_music_url')
        
        if not scenes:
            return jsonify({'error': 'No scenes provided'}), 400
        
        # Создаем временную директорию
        with tempfile.TemporaryDirectory() as temp_dir:
            video_files = []
            audio_files = []
            
            # Скачиваем все файлы
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
                return jsonify({'error': 'No valid video files found'}), 400
            
            # Склеиваем видео
            final_video = merge_video_files(video_files, audio_files, background_music, temp_dir)
            
            if not final_video:
                return jsonify({'error': 'Video merging failed'}), 500
            
            # Загружаем результат в S3
            unique_id = str(uuid.uuid4())
            s3_object_name = f"videos/{unique_id}.mp4"
            
            result_url = upload_to_s3(final_video, s3_object_name)
            
            if result_url:
                return jsonify({
                    'success': True,
                    'video_url': result_url,
                    'duration': sum(duration for _, duration in video_files)
                })
            else:
                return jsonify({'error': 'Upload failed'}), 500
                
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def merge_video_files(video_files, audio_files, background_music, temp_dir):
    """Склеивает видео и аудио файлы через FFmpeg"""
    
    try:
        # Создаем список файлов для FFmpeg
        concat_file = os.path.join(temp_dir, 'concat_list.txt')
        
        with open(concat_file, 'w') as f:
            for video_path, duration in video_files:
                f.write(f"file '{video_path}'\n")
                f.write(f"duration {duration}\n")
        
        output_file = os.path.join(temp_dir, 'final_video.mp4')
        
        # Базовая команда FFmpeg для склейки видео
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            output_file
        ]
        
        # Если есть аудио файлы, добавляем их
        if audio_files:
            # Склеиваем аудио отдельно
            audio_concat_file = os.path.join(temp_dir, 'audio_concat.txt')
            with open(audio_concat_file, 'w') as f:
                for audio_path, duration in audio_files:
                    f.write(f"file '{audio_path}'\n")
                    f.write(f"duration {duration}\n")
            
            merged_audio = os.path.join(temp_dir, 'merged_audio.wav')
            audio_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', audio_concat_file,
                '-c:a', 'pcm_s16le',
                merged_audio
            ]
            subprocess.run(audio_cmd, check=True)
            
            # Объединяем видео с аудио
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-i', merged_audio,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-shortest',
                output_file
            ]
        
        # Запускаем FFmpeg
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0 and os.path.exists(output_file):
            return output_file
        else:
            print(f"FFmpeg error: {result.stderr}")
            return None
            
    except Exception as e:
        print(f"Merge error: {e}")
        return None

@app.route('/health', methods=['GET'])
def health_check():
    """Проверка здоровья сервиса"""
    return jsonify({'status': 'healthy', 'service': 'ffmpeg-api'})

@app.route('/test', methods=['POST'])
def test_endpoint():
    """Тестовый эндпоинт для отладки"""
    data = request.json
    return jsonify({
        'received_data': data,
        'ffmpeg_available': subprocess.run(['ffmpeg', '-version'], capture_output=True).returncode == 0
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
