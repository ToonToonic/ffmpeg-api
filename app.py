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

# ✅ ВАЖНО: используй /tmp для Render!
TEMP_DIR = "/tmp"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.route('/', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "service": "toontoonic-api"
    })

@app.route('/render', methods=['POST'])
def render_video():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data"}), 400
           
        input_data = data.get("input", {})
        if not input_data:
            return jsonify({"status": "error", "message": "No 'input' field"}), 400

        video_cover = input_data.get("video_cover")
        scenes = input_data.get("scenes", [])
        bg_music = input_data.get("background_music_url")

        if not scenes:
            return jsonify({"status": "error", "message": "No scenes"}), 400
        if not bg_music:
            return jsonify({"status": "error", "message": "No background_music_url"}), 400

        clips = []

        # 0️⃣ Обработка video_cover (вставляем в начало)
        if video_cover:
            print(f"Downloading cover: {video_cover}")
            cover_path = f"{TEMP_DIR}/cover.mp4"
            r = requests.get(video_cover, timeout=60)
            r.raise_for_status()
            with open(cover_path, 'wb') as f:
                f.write(r.content)
            clips.append(cover_path)
            print(f"Cover downloaded: {cover_path}")

        # 1️⃣ Скачиваем и объединяем каждую пару видео + аудио
        for i, scene in enumerate(scenes):
            video_url = scene.get("video_url")
            audio_url = scene.get("audio_url")
           
            if not video_url or not audio_url:
                print(f"Warning: Scene {i} missing video or audio URL, skipping")
                continue

            video_path = f"{TEMP_DIR}/video_{i}.mp4"
            audio_path = f"{TEMP_DIR}/audio_{i}.wav"
            output_path = f"{TEMP_DIR}/clip_{i}.mp4"

            print(f"Downloading scene {i}...")
            # Скачать видео
            r = requests.get(video_url, timeout=60)
            r.raise_for_status()
            with open(video_path, 'wb') as f:
                f.write(r.content)

            # Скачать аудио
            r = requests.get(audio_url, timeout=60)
            r.raise_for_status()
            with open(audio_path, 'wb') as f:
                f.write(r.content)

            # Объединить видео и аудио
            print(f"Merging scene {i}...")
            subprocess.run([
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                output_path
            ], check=True, capture_output=True, text=True)

            clips.append(output_path)
            print(f"Scene {i} processed")

        # 2️⃣ Объединяем все клипы через concat
        concat_file = f"{TEMP_DIR}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{c}'\n")

        merged_path = f"{TEMP_DIR}/merged.mp4"

        print("Concatenating clips...")
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c", "copy",
            merged_path
        ], check=True, capture_output=True, text=True)

        # 3️⃣ Определяем длительность итогового видео
        print("Getting duration...")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", merged_path],
            stdout=subprocess.PIPE, text=True, check=True
        )
        total_duration = float(result.stdout.strip())
        print(f"Total duration: {total_duration}s")

        # 4️⃣ Скачиваем фоновую музыку
        bg_music_path = f"{TEMP_DIR}/bg_music.mp3"
        print(f"Downloading background music: {bg_music}")
        r = requests.get(bg_music, timeout=60)
        r.raise_for_status()
        with open(bg_music_path, 'wb') as f:
            f.write(r.content)

        # 5️⃣ Повторяем фоновую музыку до длины видео + fade in/out
        bg_extended = f"{TEMP_DIR}/bg_extended.mp3"
        print("Processing background music...")
        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", bg_music_path,
            "-t", str(total_duration),
            "-af", f"afade=t=in:ss=0:d=3,afade=t=out:st={max(0, total_duration - 3)}:d=3",
            bg_extended
        ], check=True, capture_output=True, text=True)

        # 6️⃣ Проверяем, есть ли аудио в merged.mp4
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            merged_path
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        has_audio = bool(probe.stdout.strip())
        print(f"Merged video has audio: {has_audio}")

        final_path = f"{TEMP_DIR}/final_{uuid.uuid4().hex}.mp4"

        # 7️⃣ Микшируем аудио
        print("Creating final video...")
        if has_audio:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", merged_path,
                "-i", bg_extended,
                "-filter_complex", "[1:a]volume=0.2[a1];[0:a][a1]amix=inputs=2:duration=first",
                "-c:v", "copy",
                "-c:a", "aac",
                final_path
            ], check=True, capture_output=True, text=True)
        else:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", merged_path,
                "-i", bg_extended,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final_path
            ], check=True, capture_output=True, text=True)

        # 8️⃣ Загружаем в Cloudflare R2
        print("Uploading to R2...")
        s3 = boto3.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )

        key = f"videos/{os.path.basename(final_path)}"
        s3.upload_file(final_path, R2_BUCKET, key)
        url = f"{R2_PUBLIC_URL}/{key}"

        print(f"✅ Success! URL: {url}")

        # 9️⃣ Очистить временные файлы
        for f in os.listdir(TEMP_DIR):
            if f.startswith(('video_', 'audio_', 'clip_', 'bg_', 'merged', 'final_', 'concat', 'cover')):
                try:
                    os.remove(os.path.join(TEMP_DIR, f))
                except:
                    pass

        return jsonify({"status": "success", "url": url})

    except Exception as e:
        error_msg = traceback.format_exc()
        print(f"ERROR: {error_msg}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    print(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port)
