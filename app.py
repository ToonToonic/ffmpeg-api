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
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")  # Убедись, что добавлен в Render

# Папка для временных файлов
TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)


@app.route('/render', methods=['POST'])
def render_video():
    try:
        data = request.get_json()
        scenes = data.get("scenes", [])
        bg_music = data.get("background_music_url")

        clips = []

        # 1️⃣ Скачиваем и объединяем каждую пару видео + аудио
        for i, scene in enumerate(scenes):
            video_url = scene["video_url"]
            audio_url = scene["audio_url"]

            video_path = f"{TEMP_DIR}/video_{i}.mp4"
            audio_path = f"{TEMP_DIR}/audio_{i}.wav"
            output_path = f"{TEMP_DIR}/clip_{i}.mp4"

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

        # 2️⃣ Объединяем все клипы с плавными переходами (1 секунда)
        concat_file = f"{TEMP_DIR}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")

        merged_path = f"{TEMP_DIR}/merged.mp4"

        # Создаём плавные переходы между сценами
        filter_complex = ""
        for i in range(len(clips)):
            filter_complex += f"[{i}:v][{i}:a]"
        filter_complex = "".join([f"[{i}:v][{i}:a]" for i in range(len(clips))])
        
        # Простое объединение через concat с плавными переходами
        # xfade применим позже (иначе Render может зависнуть)
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

        # 4️⃣ Повторяем фоновую музыку до конца видео + делаем fade in/out
        bg_extended = f"{TEMP_DIR}/bg_extended.mp3"
        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", "-1",  # повторять бесконечно
            "-i", bg_music,
            "-t", str(total_duration),  # длительность = длина видео
            "-af", "afade=t=in:ss=0:d=3,afade=t=out:st=" + str(total_duration - 3) + ":d=3",
            bg_extended
        ], check=True)

        # 5️⃣ Накладываем фоновую музыку с громкостью 0.2
        final_path = f"{TEMP_DIR}/final_{uuid.uuid4().hex}.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-i", merged_path,
            "-i", bg_extended,
            "-filter_complex", "[1:a]volume=0.2[a1];[0:a][a1]amix=inputs=2:duration=longest",
            "-c:v", "copy",
            "-shortest", final_path
        ], check=True)

        # 6️⃣ Загружаем в Cloudflare R2
        s3 = boto3.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )

        key = f"videos/{os.path.basename(final_path)}"
        s3.upload_file(final_path, R2_BUCKET, key)
        url = f"{R2_PUBLIC_URL}/{key}"

        # 7️⃣ Очистить временные файлы
        for f in os.listdir(TEMP_DIR):
            try:
                os.remove(os.path.join(TEMP_DIR, f))
            except:
                pass

        return jsonify({"status": "success", "url": url})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/', methods=['GET'])
def home():
    return "🎬 FFmpeg API is running smoothly 🚀"


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
