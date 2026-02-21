from flask import Flask, request, jsonify
import subprocess
import os
import boto3
import uuid
import requests
import traceback
import time
import threading

app = Flask(__name__)

R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")

TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

VIDEO_RES = "1280:720"
FPS = "30"
AUDIO_SR = "44100"
AUDIO_CHANNELS = "2"
AUDIO_BITRATE = "128k"
VIDEO_BITRATE = "1500k"


def render_in_background(job_id, input_data, callback_url):
    """Runs FFmpeg render in background thread, then POSTs result to Make webhook."""
    start_time = time.time()
    job_temp = f"{TEMP_DIR}/{job_id}"
    os.makedirs(job_temp, exist_ok=True)

    try:
        video_cover = input_data.get("video_cover")
        scenes = input_data.get("scenes", [])
        bg_music = input_data.get("background_music_url")

        if not scenes:
            raise Exception("No scenes provided")
        if not bg_music:
            raise Exception("No background_music_url provided")

        clips = []

        # 0️⃣ Cover
        if video_cover:
            cover_path = f"{job_temp}/cover_original"
            r = requests.get(video_cover, timeout=60)
            r.raise_for_status()
            with open(cover_path, 'wb') as f:
                f.write(r.content)

            norm_cover = f"{job_temp}/cover.mp4"
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", cover_path],
                stdout=subprocess.PIPE, text=True
            )
            if "png" in probe.stdout or "jpg" in probe.stdout:
                subprocess.run([
                    "ffmpeg", "-y", "-loop", "1", "-i", cover_path,
                    "-t", "3",
                    "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                    "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                    norm_cover
                ], check=True, timeout=120)
            else:
                subprocess.run([
                    "ffmpeg", "-y", "-i", cover_path,
                    "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                    "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                    norm_cover
                ], check=True, timeout=120)

            clips.append(norm_cover)
            print(f"[{job_id}] Cover done in {time.time() - start_time:.1f}s")

        # 1️⃣ Scenes
        for i, scene in enumerate(scenes):
            video_url = scene.get("video_url")
           
            video_path = f"{job_temp}/video_{i}.mp4"
           
            r = requests.get(video_url, timeout=60)
            r.raise_for_status()
            with open(video_path, 'wb') as f:
                f.write(r.content)

            r = requests.get(audio_url, timeout=60)
            r.raise_for_status()
            with open(audio_path, 'wb') as f:
                f.write(r.content)

            norm_video = f"{job_temp}/norm_video_{i}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                norm_video
            ], check=True, timeout=120)

            norm_audio = f"{job_temp}/norm_audio_{i}.aac"
            subprocess.run([
                "ffmpeg", "-y", "-i", audio_path,
                "-ar", AUDIO_SR, "-ac", AUDIO_CHANNELS, "-b:a", AUDIO_BITRATE,
                "-c:a", "aac", "-strict", "-2",
                norm_audio
            ], check=True, timeout=60)

            output_path = f"{job_temp}/clip_{i}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", norm_video, "-i", norm_audio,
                "-c:v", "libx264", "-c:a", "aac", "-preset", "ultrafast",
                output_path
            ], check=True, timeout=120)

            clips.append(output_path)
            print(f"[{job_id}] Scene {i} done in {time.time() - start_time:.1f}s")

        # 2️⃣ Concat
        concat_file = f"{job_temp}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")

        merged_path = f"{job_temp}/merged.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c:v", "libx264", "-c:a", "aac", "-preset", "ultrafast",
            merged_path
        ], check=True, timeout=900)
        print(f"[{job_id}] Concat done in {time.time() - start_time:.1f}s")

        # 3️⃣ Duration
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", merged_path],
            stdout=subprocess.PIPE, text=True
        )
        total_duration = float(result.stdout.strip())

        # 4️⃣ BG music
        bg_extended = f"{job_temp}/bg_extended.mp3"
        subprocess.run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", bg_music,
            "-t", str(total_duration),
            "-af", f"afade=t=in:ss=0:d=3,afade=t=out:st={total_duration - 3}:d=3",
            bg_extended
        ], check=True, timeout=120)

        # 5️⃣ Audio mix
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", merged_path
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        has_audio = bool(probe.stdout.strip())

        final_path = f"{job_temp}/final_{job_id}.mp4"

        if has_audio:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", merged_path, "-i", bg_extended,
                "-filter_complex", "[0:a]volume=1.0[a0];[1:a]volume=0.1[a1];[a0][a1]amix=inputs=2:duration=longest",
                "-c:v", "copy", "-shortest", final_path
            ], check=True, timeout=600)
        else:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", merged_path, "-i", bg_extended,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                final_path
            ], check=True, timeout=600)

        # 6️⃣ Upload to R2
        s3 = boto3.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )
        key = f"videos/final_{job_id}.mp4"
        s3.upload_file(final_path, R2_BUCKET, key)
        video_url = f"{R2_PUBLIC_URL}/{key}"
        total_time = time.time() - start_time
        print(f"[{job_id}] DONE in {total_time:.1f}s → {video_url}")

        # 7️⃣ Callback to Make.com
        requests.post(callback_url, json={
            "status": "success",
            "job_id": job_id,
            "url": video_url,
            "render_time_sec": round(total_time, 1)
        }, timeout=30)

    except Exception as e:
        print(f"[{job_id}] ERROR: {e}")
        traceback.print_exc()
        try:
            requests.post(callback_url, json={
                "status": "error",
                "job_id": job_id,
                "message": str(e)
            }, timeout=30)
        except:
            pass

    finally:
        # Cleanup
        import shutil
        try:
            shutil.rmtree(job_temp)
        except:
            pass


@app.route('/render', methods=['POST'])
def render_video():
    """
    Accepts render job, starts background thread, immediately returns job_id.
    Make.com should listen on a webhook to receive the result.
    """
    try:
        data = request.get_json()
        input_data = data.get("input", {})
        # ⚠️ Make.com webhook URL must be passed in the request
        callback_url = data.get("callback_url")

        if not input_data:
            return jsonify({"status": "error", "message": "No 'input' data"}), 400
        if not callback_url:
            return jsonify({"status": "error", "message": "No 'callback_url' provided"}), 400

        job_id = uuid.uuid4().hex
        print(f"[NEW JOB] {job_id}, scenes: {len(input_data.get('scenes', []))}")

        # Start background thread — Make.com gets instant response
        thread = threading.Thread(
            target=render_in_background,
            args=(job_id, input_data, callback_url),
            daemon=True
        )
        thread.start()

        return jsonify({
            "status": "processing",
            "job_id": job_id,
            "message": "Render started. Result will be sent to callback_url."
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
