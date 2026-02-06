from flask import Flask, request, jsonify
import subprocess
import os
import boto3
import uuid
import requests
import traceback

app = Flask(__name__)

# R2 credentials (без изменений)
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# Стандартные параметры для нормализации
VIDEO_RES = "1280:720"
FPS = "30"
AUDIO_SR = "44100" # Hz
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

        # 0️⃣ Video_cover: скачиваем и нормализуем (если image — конверт в видео)
        if video_cover:
            head = requests.head(video_cover)
            if head.status_code != 200:
                raise Exception(f"Cover URL not accessible: {video_cover}")

            cover_path = f"{TEMP_DIR}/cover_original"
            with open(cover_path, 'wb') as f:
                f.write(requests.get(video_cover).content)

            # Проверяем тип: если image, конверт в 3-sec видео
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", cover_path], stdout=subprocess.PIPE, text=True)
            if "format_name=png_pipe" in probe.stdout or "jpg" in probe.stdout: # Image
                norm_cover = f"{TEMP_DIR}/cover.mp4"
                subprocess.run([
                    "ffmpeg", "-y", "-loop", "1", "-i", cover_path,
                    "-t", "3", "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                    "-c:v", "libx264", "-b:v", VIDEO_BITRATE,
                    norm_cover
                ], check=True)
            else: # Видео — нормализуем
                norm_cover = f"{TEMP_DIR}/cover.mp4"
                subprocess.run([
                    "ffmpeg", "-y", "-i", cover_path,
                    "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                    "-c:v", "libx264", "-b:v", VIDEO_BITRATE,
                    norm_cover
                ], check=True)

            clips.append(norm_cover)

        # 1️⃣ Scenes: скачиваем, нормализуем видео/аудио, merge в clip
        for i, scene in enumerate(scenes):
            video_url = scene["video_url"]
            audio_url = scene["audio_url"]

            for url in [video_url, audio_url]:
                head = requests.head(url)
                if head.status_code != 200:
                    raise Exception(f"URL not accessible: {url}")

            video_path = f"{TEMP_DIR}/video_{i}.mp4"
            audio_path = f"{TEMP_DIR}/audio_{i}.wav"
            with open(video_path, 'wb') as f:
                f.write(requests.get(video_url).content)
            with open(audio_path, 'wb') as f:
                f.write(requests.get(audio_url).content)

            # Нормализуем видео
            norm_video = f"{TEMP_DIR}/norm_video_{i}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                "-c:v", "libx264", "-b:v", VIDEO_BITRATE,
                norm_video
            ], check=True)

            # Нормализуем аудио
            norm_audio = f"{TEMP_DIR}/norm_audio_{i}.wav"
            subprocess.run([
                "ffmpeg", "-y", "-i", audio_path,
                "-ar", AUDIO_SR, "-ac", AUDIO_CHANNELS, "-b:a", AUDIO_BITRATE,
                norm_audio
            ], check=True)

            # Merge нормализованных
            output_path = f"{TEMP_DIR}/clip_{i}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", norm_video, "-i", norm_audio,
                "-c:v", "copy", "-c:a", "aac",
                output_path
            ], check=True)

            clips.append(output_path)

        # Проверяем bg_music
        head = requests.head(bg_music)
        if head.status_code != 200:
            raise Exception(f"Background music URL not accessible: {bg_music}")

        # 2️⃣-7️⃣ Concat, duration, bg extend, mix, upload, cleanup (без изменений, но с нормализованными clips)
        # ... (твой оригинальный код от concat_file до return jsonify)

        return jsonify({"status": "success", "url": url})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500
