from flask import Flask, request, jsonify
import subprocess
import os
import boto3
import uuid
import requests
import traceback
import time
import threading
import shutil

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
VIDEO_BITRATE = "1500k"

# ─────────────────────────────────────────────
# BACKGROUND RENDER (runs in separate thread)
# ─────────────────────────────────────────────
def render_in_background(job_id, input_data, callback_url, metadata):
    """
    Grok generates video WITH audio — no separate audio processing needed.
    Pipeline: download scenes → normalize video (keep audio) → concat → upload → callback
    """
    start_time = time.time()
    job_temp = f"{TEMP_DIR}/{job_id}"
    os.makedirs(job_temp, exist_ok=True)

    try:
        video_cover = input_data.get("video_cover")
        scenes = input_data.get("scenes", [])

        if not scenes:
            raise Exception("No scenes provided")

        clips = []

        # ─── 0. Cover (optional) ───────────────────────────────────────────
        if video_cover:
            cover_path = f"{job_temp}/cover_original"
            r = requests.get(video_cover, timeout=60)
            r.raise_for_status()
            with open(cover_path, 'wb') as f:
                f.write(r.content)

            norm_cover = f"{job_temp}/cover.mp4"

            # Check if cover is image or video
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", cover_path],
                stdout=subprocess.PIPE, text=True
            )
            is_image = "png" in probe.stdout or "jpg" in probe.stdout or "jpeg" in probe.stdout

            if is_image:
                # Image → 3 sec silent video
                subprocess.run([
                    "ffmpeg", "-y", "-loop", "1", "-i", cover_path,
                    "-t", "3",
                    "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,"
                           f"pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                    "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                    "-an", # no audio for cover image
                    norm_cover
                ], check=True, timeout=120)
            else:
                # Video → normalize resolution, KEEP original audio from cover
                subprocess.run([
                    "ffmpeg", "-y", "-i", cover_path,
                    "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,"
                           f"pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                    "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                    "-c:a", "aac", "-b:a", "128k", # keep audio
                    norm_cover
                ], check=True, timeout=120)

            clips.append(norm_cover)
            print(f"[{job_id}] Cover done in {time.time() - start_time:.1f}s")

        # ─── 1. Scenes ─────────────────────────────────────────────────────
        # Grok video already contains audio — just normalize video resolution
        for i, scene in enumerate(scenes):
            video_url = scene.get("video_url")

            if not video_url:
                raise Exception(f"Missing video_url in scene {i}")

            video_path = f"{job_temp}/video_{i}.mp4"
            r = requests.get(video_url, timeout=120)
            r.raise_for_status()
            with open(video_path, 'wb') as f:
                f.write(r.content)

            # Normalize resolution + FPS, KEEP original Grok audio (dialogues + music)
            norm_video = f"{job_temp}/norm_{i}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-vf", f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,"
                       f"pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p",
                "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k", # preserve Grok audio
                norm_video
            ], check=True, timeout=180)

            clips.append(norm_video)
            print(f"[{job_id}] Scene {i} done in {time.time() - start_time:.1f}s")

        # ─── 2. Concat all clips ───────────────────────────────────────────
        concat_file = f"{job_temp}/concat.txt"
        with open(concat_file, "w") as f:
            for c in clips:
                f.write(f"file '{os.path.abspath(c)}'\n")

        final_path = f"{job_temp}/final_{job_id}.mp4"

        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c:v", "libx264", "-preset", "ultrafast", # re-encode video for compatibility
            "-c:a", "aac", "-b:a", "128k", # keep audio from all scenes
            final_path
        ], check=True, timeout=900)

        total_time = time.time() - start_time
        print(f"[{job_id}] Concat done in {total_time:.1f}s")

        # ─── 3. Upload to Cloudflare R2 ────────────────────────────────────
        s3 = boto3.client(
            's3',
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )
        key = f"videos/final_{job_id}.mp4"
        s3.upload_file(final_path, R2_BUCKET, key)
        video_url_result = f"{R2_PUBLIC_URL}/{key}"

        total_time = time.time() - start_time
        print(f"[{job_id}] DONE in {total_time:.1f}s → {video_url_result}")

        # ─── 4. Callback to Make.com Scenario 2 ───────────────────────────
        # metadata (email, name, etc.) is passed back so Scenario 2 can send email
        requests.post(callback_url, json={
            "status": "success",
            "job_id": job_id,
            "url": video_url_result,
            "render_time_sec": round(total_time, 1),
            "metadata": metadata # ← user email, name, child_name, order_id
        }, timeout=30)

    except Exception as e:
        print(f"[{job_id}] ERROR: {e}")
        traceback.print_exc()
        try:
            requests.post(callback_url, json={
                "status": "error",
                "job_id": job_id,
                "message": str(e),
                "metadata": metadata # send metadata even on error
            }, timeout=30)
        except:
            pass

    finally:
        try:
            shutil.rmtree(job_temp)
        except:
            pass


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route('/render', methods=['POST'])
def render_video():
    """
    Accepts render job from Make.com Scenario 1.
    Immediately returns job_id (no timeout).
    Result is POSTed to callback_url when ready.

    Expected JSON:
    {
        "input": {
            "video_cover": "https://...", (optional)
            "scenes": [
                {"video_url": "https://..."}, // Grok video WITH audio
                ...
            ]
        },
        "callback_url": "https://hook.make.com/...",
        "metadata": {
            "user_email": "anna@gmail.com",
            "user_name": "Anna",
            "child_name": "Saly",
            "order_id": "12345"
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400

        input_data = data.get("input", {})
        callback_url = data.get("callback_url")
        metadata = data.get("metadata", {}) # ← user data from Scenario 1

        if not input_data:
            return jsonify({"status": "error", "message": "No 'input' data"}), 400
        if not callback_url:
            return jsonify({"status": "error", "message": "No 'callback_url' provided"}), 400

        job_id = uuid.uuid4().hex
        scenes_count = len(input_data.get('scenes', []))
        print(f"[NEW JOB] {job_id}, scenes: {scenes_count}, user: {metadata.get('user_email', 'unknown')}")

        # Start background thread — Make.com gets instant response (<1 sec)
        thread = threading.Thread(
            target=render_in_background,
            args=(job_id, input_data, callback_url, metadata),
            daemon=True
        )
        thread.start()

        return jsonify({
            "status": "processing",
            "job_id": job_id,
            "message": f"Render started for {scenes_count} scenes. Result will be sent to callback_url."
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "toontoonic-render"})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
