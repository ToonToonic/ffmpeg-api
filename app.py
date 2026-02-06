from flask import Flask, request, jsonify
import subprocess
import os
import boto3
import uuid
import requests
import traceback

app = Flask(__name__)

# Cloudflare R2 credentials
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

# Папка для временных файлов
TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# Стандартные параметры для нормализации
VIDEO_RES = "1280:720"
FPS = "30"
AUDIO_SR = "44100"  # Hz
AUDIO_CHANNELS = "2"
AUDIO_BITRATE = "128k"
VIDEO_BITRATE = "1500k"

@app.route('/render', methods=['POST'])
def render_video():
    try:
        data = request.get_json()
        input_data = data.get("input", {})
        if not input_data:
            raise Exception("No 'input' data in request")

        video_cover = input_data.get("video_cover")
        scenes = input_data.get("scenes", [])
        bg_music = input_data.get("background_music_url")

        if not scenes:
            raise Exception("No scenes provided")
        if not bg_music:
            raise Exception("No background_music_url provided")

        clips = []

        # 0️⃣ Обработка video_cover (вставляем в начало)
        if video_cover:
            # Проверяем доступность
            head = requests.head(video_cover)
            if head.status_code != 200:
                raise Exception(f"Cover URL not accessible: {video_cover}")

            cover_path = f"{TEMP_DIR}/cover.mp4"
            r = requests.get(video_cover)
            r.raise_for_status()
            with open(cover_path, 'wb') as f:
                f.write(r.content)

            clips.append(cover_path)  # Добавляем как первый клип (без аудио)

        # 1️⃣ Скачиваем и объединяем каждую пару видео + аудио
        for i, scene in enumerate(scenes):
            video_url = scene["video_url"]
            audio_url = scene["audio_url"]

            video_path = f"{TEMP_DIR}/video_{i}.mp4"
            audio_path = f"{TEMP_DIR}/audio_{i}.wav"
            output_path = f"{TEMP_DIR}/clip_{i}.mp4"

            # Проверяем доступность видео и аудио перед скачиванием
            for url in [video_url, audio_url]:
                head = requests.head(url)
                if head.status_code != 200:
                    raise Exception(f"URL not accessible: {url}")

            # Скачать файлы
            for url, path in [(video_url, video_path), (audio_url, audio_path)]:
                r = requests.get(url)
                r.raise_for_status()
                with open(path, 'wb') as f:
                    f.write(r.content)

            # Объединить видео и аудио
            subprocess.run([
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac",
                output_path
            ], check=True)

            clips.append(output_path)

        # 2️⃣ Объединяем все клипы через concat
        concat_file = f"{TEMP_DIR}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")

        merged_path = f"{TEMP_DIR}/merged.mp4"

        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c:v", "libx264", "-c:a", "aac",
            merged_path
        ], check=True)

        # 3️⃣ Определяем длительность итогового видео
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", merged_path],
            stdout=subprocess.PIPE, text=True
        )
        total_duration = float(result.stdout.strip())

        # 4️⃣ Повторяем фоновую музыку до длины видео + fade in/out
        bg_extended = f"{TEMP_DIR}/bg_extended.mp3"
        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", bg_music,
            "-t", str(total_duration),
            "-af", f"afade=t=in:ss=0:d=3,afade=t=out:st={total_duration - 3}:d=3",
            bg_extended
        ], check=True)

        # 5️⃣ Проверяем, есть ли аудио в merged.mp4
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams
