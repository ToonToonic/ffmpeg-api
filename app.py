from flask import Flask, request, jsonify
import subprocess
import os
import boto3
import uuid
import requests
import traceback
import time

app = Flask(__name__)

R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.route('/render', methods=['POST'])
def render_video():
    start_time = time.time()
    print(f"[START] Render started at {start_time}")
    try:
        data = request.get_json()
        print(f"[DEBUG] Received data: {data}")  # Debug input
        input_data = data.get("input", {})
        if not input_data:
            raise Exception("No 'input' data in request")

        video_cover = input_data.get("video_cover")
        scenes = input_data.get("scenes", [])
        bg_music = input_data.get("background_music_url")

        print(f"[DEBUG] Scenes count: {len(scenes)}, Cover: {video_cover}, BG: {bg_music}")

        if not scenes:
            raise Exception("No scenes provided")
        if not bg_music:
            raise Exception("No background_music_url provided")

        clips = []

        # 0️⃣ Обработка video_cover
        if video_cover:
            head = requests.head(video_cover)
            if head.status_code != 200:
                raise Exception(f"Cover URL not accessible: {video_cover}")

            cover_path = f"{TEMP_DIR}/cover.mp4"
            r = requests.get(video_cover)
            r.raise_for_status()
            with open(cover_path, 'wb') as f:
                f.write(r.content)
            # Если cover - image, конверт в 3-sec видео
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", cover_path], stdout=subprocess.PIPE, text=True)
            if "png" in probe.stdout or "jpg" in probe.stdout:
                subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", cover_path, "-t", "3", "-c:v", "libx264", cover_path], check=True)
            clips.append(cover_path)
            print(f"[TIME] Cover processed in {time.time() - start_time} sec")

        # 1️⃣ Скачиваем и объединяем каждую пару видео + аудио
        for i, scene in enumerate(scenes):
            video_url = scene.get("video_url")
            audio_url = scene.get("audio_url")
            print(f"[DEBUG] Scene {i}: Video {video_url}, Audio {audio_url}")

            for url in [video_url, audio_url]:
                if not url:
                    raise Exception(f"Missing URL in scene {i}")
                head = requests.head(url)
                if head.status_code != 200:
                    raise Exception(f"URL not accessible: {url}")

            video_path = f"{TEMP_DIR}/video_{i}.mp4"
            audio_path = f"{TEMP_DIR}/audio_{i}.wav"
            output_path = f"{TEMP_DIR}/clip_{i}.mp4"

            r = requests.get(video_url)
            r.raise_for_status()
            with open(video_path, 'wb') as f:
                f.write(r.content)
            r = requests.get(audio_url)
            r.raise_for_status()
            with open(audio_path, 'wb') as f:
                f.write(r.content)

            subprocess.run([
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy", "-c:a", "aac",
                output_path
            ], check=True, stderr=subprocess.PIPE)  # Добавь stderr для debug FFmpeg errors

            clips.append(output_path)
            print(f"[TIME] Scene {i} processed in {time.time() - start_time} sec")

        # Проверяем bg_music
        head = requests.head(bg_music)
        if head.status_code != 200:
            raise Exception(f"Background music URL not accessible: {bg_music}")

        # 2️⃣ Объединяем все клипы через concat
        concat_file = f"{TEMP_DIR}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")

        merged_path = f"{TEMP_DIR}/merged.mp4"

        concat_result = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c:v", "libx264", "-c:a", "aac",
            merged_path
        ], check=True, stderr=subprocess.PIPE)
        print(f"[DEBUG] Concat stderr: {concat_result.stderr}")

        print(f"[TIME] Concat done in {time.time() - start_time} sec")

        # 3️⃣ Определяем длительность итогового видео
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", merged_path],
            stdout=subprocess.PIPE, text=True
        )
        total_duration = float(result.stdout.strip())
        print(f"[TIME] Duration probe done in {time.time() - start_time} sec")

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
        print(f"[TIME] BG music extended in {time.time() - start_time} sec")

        # 5️⃣ Проверяем, есть ли аудио в merged.mp4
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            merged_path
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        has_audio = bool(probe.stdout.strip())

        final_path = f"{TEMP_DIR}/final_{uuid.uuid4().hex}.mp4"

        if has_audio:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", merged_path,
                "-i", bg_extended,
                "-filter_complex", "[1:a]volume=0.2[a1];[0:a][a1]amix=inputs=2:duration=longest",
                "-c:v", "copy",
                "-shortest", final_path
            ], check=True)
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
            ], check=True)
        print(f"[TIME] Audio mix done in {time.time() - start_time} sec")

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
        print(f"[TIME] Upload done in {time.time() - start_time} sec")

        # 7️⃣ Очистить временные файлы
        for f in os.listdir(TEMP_DIR):
            try:
                os.remove(os.path.join(TEMP_DIR, f))
            except:
                pass

        print(f"[END] Total time: {time.time() - start_time} sec")
        return jsonify({"status": "success", "url": url})

    except Exception as e:
        print(f"[ERROR] Exception: {str(e)}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
