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
# Настройки для загрузки в R2
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET = os.environ.get('R2_BUCKET')

R2_PUBLIC_DOMAIN = os.environ.get('R2_PUBLIC_DOMAIN', 'https://pub-bd37e3cfae574077ab0d4461a749b0d3.r2.dev') 
# Настрой в Variables

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
    """Тестовая версия для отладки"""
    try:
        data = request.json
        print(f"Received data: {data}")
        print(f"Data type: {type(data)}")
        
        scenes = data.get('scenes', [])
        print(f"Scenes count: {len(scenes)}")
        
        return jsonify({
            "success": True, 
            "message": "Test response - API working",
            "received_scenes": len(scenes)
        })
    except Exception as e:
        print(f"Error in merge_videos: {str(e)}")
        return jsonify({"error": str(e)}), 500
           
            # Загружаем результат в R2
            unique_id = str(uuid.uuid4())
            r2_object_name = f"videos/{unique_id}.mp4"
           
            result_url = upload_to_r2(final_video, r2_object_name)
           
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
