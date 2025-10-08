from flask import Flask, request, jsonify
import subprocess
import os
import boto3
import uuid
import requests

app = Flask(__name__)

# Cloudflare R2 credentials (заменишь на свои)
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")

# Папка для временных файлов
TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.route('/render', methods=['POST'])
def render_video():
    data = request.get_json()
    scenes = data.get("scenes", [])
    bg_music = data.get("background_music_url")

    clips = []

    # 1. Скачиваем и объединяем каждую пару видео + аудио
    for i, scene in enumerate(scenes):
        video_url = scene["video_url"]
        audio_url = scene["audio_url"]

        video_path = f"{TEMP_DIR}/video_{i}.mp4"
        audio_path = f"{TEMP_DIR}/audio_{i}.wav"
        output_path = f"{TEMP_DIR}/clip_{i}.mp4"

        # Скачать файлы
        for url, path in [(video_url, video_path), (audio_url, audio_path)]:
            r = requests.get(url)
            with open(path, 'wb') as f:
                f.write(r.content)

        # Объединить видео и аудио
        subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac",
            output_path
        ])

        clips.append(output_path)

    # 2. Объединяем все клипы
    concat_file = f"{TEMP_DIR}/concat.txt"
    with open(concat_file, "w") as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")

    merged_path = f"{TEMP_DIR}/merged.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_file, "-c", "copy", merged_path
    ])

    # 3. Добавляем фоновую музыку
    final_path = f"{TEMP_DIR}/final_{uuid.uuid4().hex}.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", merged_path,
        "-i", bg_music,
        "-filter_complex", "[1:a]volume=0.2[a1];[0:a][a1]amix=inputs=2:duration=longest",
        "-c:v", "copy",
        "-shortest", final_path
    ])

    # 4. Загружаем в Cloudflare R2
    s3 = boto3.client('s3',
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
    )

    key = f"videos/{os.path.basename(final_path)}"
    s3.upload_file(final_path, R2_BUCKET, key)
    url = f"{R2_ENDPOINT}/{R2_BUCKET}/{key}"

    # Очистить временные файлы
    for f in os.listdir(TEMP_DIR):
        os.remove(os.path.join(TEMP_DIR, f))

    return jsonify({"status": "success", "url": url})


@app.route('/', methods=['GET'])
def home():
    return "FFmpeg API is running 🚀"


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
