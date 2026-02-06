from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Server is running"})

@app.route('/test', methods=['GET'])
def test():
    return jsonify({
        "status": "ok",
        "env_vars": {
            "R2_BUCKET": os.getenv("R2_BUCKET", "NOT SET"),
            "R2_ENDPOINT": os.getenv("R2_ENDPOINT", "NOT SET"),
            "PORT": os.getenv("PORT", "NOT SET")
        }
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    print(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port)
```

Загрузи ЭТОТ простой код на GitHub и задеплой. Если **это не работает**, значит проблема в конфигурации Render, а не в коде.

---

## Если простой код работает, тогда проблема в основном коде

Проверь эти моменты в твоём **полном app.py**:

### ❌ Возможные проблемы:

1. **Отступы (indentation)** - Python очень чувствителен к отступам
2. **Импорты** - возможно какая-то библиотека не установлена
3. **sys.exit(1)** в коде - это принудительно завершает приложение

## ✅ Исправленный app.py БЕЗ sys.exit():

```python
from flask import Flask, request, jsonify
import subprocess
import os
import uuid
import traceback

app = Flask(__name__)

# Cloudflare R2 credentials
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

TEMP_DIR = "/tmp"
os.makedirs(TEMP_DIR, exist_ok=True)

print("=== Server Starting ===")
print(f"R2_BUCKET: {R2_BUCKET}")
print(f"R2_ENDPOINT: {R2_ENDPOINT}")
print("======================")

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "service": "toontoonic-api"
    })

@app.route('/render', methods=['POST'])
def render_video():
    try:
        # Импортируем boto3 и requests только когда нужны
        import boto3
        import requests
       
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data"}), 400
       
        input_data = data.get("input", {})
        video_cover_url = input_data.get("video_cover")
        scenes = input_data.get("scenes", [])
        bg_music_url = input_data.get("background_music_url")
       
        if not scenes or not bg_music_url:
            return jsonify({
                "status": "error",
                "message": "Missing scenes or background_music_url"
            }), 400

        clips = []

        # Download function
        def download(url, path):
            print(f"Downloading: {url}")
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            with open(path, 'wb') as f:
                f.write(r.content)
            print(f"Downloaded: {path}")

        # Process cover
        if video_cover_url:
            cover_path = f"{TEMP_DIR}/cover.mp4"
            download(video_cover_url, cover_path)
            clips.append(cover_path)

        # Process scenes
        for i, scene in enumerate(scenes):
            video_url = scene.get("video_url")
            audio_url = scene.get("audio_url")
           
            if not video_url or not audio_url:
                continue

            video_path = f"{TEMP_DIR}/video_{i}.mp4"
            audio_path = f"{TEMP_DIR}/audio_{i}.wav"
            output_path = f"{TEMP_DIR}/clip_{i}.mp4"

            download(video_url, video_path)
            download(audio_url, audio_path)

            subprocess.run([
                "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac", "-shortest", output_path
            ], check=True, capture_output=True)

            clips.append(output_path)

        # Concat
        concat_file = f"{TEMP_DIR}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{c}'\n")

        merged_path = f"{TEMP_DIR}/merged.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file, "-c", "copy", merged_path
        ], check=True, capture_output=True)

        # Get duration
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", merged_path
        ], stdout=subprocess.PIPE, text=True, check=True)
       
        duration = float(result.stdout.strip())

        # Background music
        bg_path = f"{TEMP_DIR}/bg_music.mp3"
        download(bg_music_url, bg_path)

        bg_extended = f"{TEMP_DIR}/bg_extended.mp3"
        subprocess.run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", bg_path,
            "-t", str(duration),
            "-af", f"afade=t=in:ss=0:d=3,afade=t=out:st={max(0,duration-3)}:d=3",
            bg_extended
        ], check=True, capture_output=True)

        # Final mix
        final_path = f"{TEMP_DIR}/final_{uuid.uuid4().hex}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", merged_path, "-i", bg_extended,
            "-filter_complex", "[1:a]volume=0.2[a1];[0:a][a1]amix=inputs=2:duration=first",
            "-c:v", "copy", "-c:a", "aac", final_path
        ], check=True, capture_output=True)

        # Upload to R2
        s3 = boto3.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY
        )

        key = f"videos/{os.path.basename(final_path)}"
        s3.upload_file(final_path, R2_BUCKET, key)
        url = f"{R2_PUBLIC_URL}/{key}"

        # Cleanup
        for f in os.listdir(TEMP_DIR):
            if f.startswith(('video_', 'audio_', 'clip_', 'bg_', 'merged', 'final_', 'concat', 'cover')):
                try:
                    os.remove(os.path.join(TEMP_DIR, f))
                except:
                    pass

        return jsonify({"status": "success", "url": url})

    except Exception as e:
        print(f"ERROR: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
