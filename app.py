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

# ─── Transition settings ───────────────────────────────────────────────────────
TRANSITION_TYPE     = "fadewhite"   # fadewhite / fadeblack / dissolve / smoothleft
TRANSITION_DURATION = 1.0           # seconds


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_video_duration(path):
    """Return duration of a video file in seconds (float)."""
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())


def has_audio_stream(path):
    """Return True if video file contains at least one audio stream."""
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        path
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return bool(result.stdout.strip())


def normalize_clip(input_path, output_path, job_id, label, job_temp, duration_sec=None):
    """
    Normalize video to uniform resolution/fps/codec.
    Keeps original audio if present; adds silent audio track if missing
    (so all clips have audio stream — required for xfade audio mixing).
    duration_sec: if set, trims clip to this length (used for image covers).
    """
    vf = (
        f"scale={VIDEO_RES}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_RES}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={FPS},"
        f"format=yuv420p"
    )

    has_audio = has_audio_stream(input_path)

    cmd = ["ffmpeg", "-y"]

    # For image: use -loop 1 to create video from still image
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", input_path],
        stdout=subprocess.PIPE, text=True
    )
    is_image = any(fmt in probe.stdout for fmt in ("png", "jpg", "jpeg", "gif", "webp"))

    if is_image:
        cmd += ["-loop", "1", "-i", input_path, "-t", str(duration_sec or 5)]
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-map", "0:v", "-map", "1:a",
            "-shortest", output_path
        ]
    elif not has_audio:
        # Video without audio — add silent track
        cmd += ["-i", input_path, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-map", "0:v", "-map", "1:a",
            "-shortest", output_path
        ]
    else:
        # Normal video with audio
        cmd += ["-i", input_path]
        if duration_sec:
            cmd += ["-t", str(duration_sec)]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            output_path
        ]

    subprocess.run(cmd, check=True, timeout=180)
    print(f"[{job_id}] {label} normalized → {os.path.basename(output_path)}")


def build_xfade_filter(clips_with_durations, td):
    """
    Build FFmpeg filter_complex string for chained xfade (video) + acrossfade (audio).

    clips_with_durations : list of (path, duration_float)
    td                   : transition duration in seconds

    Returns: (filter_complex_str, final_video_label, final_audio_label)
    """
    n = len(clips_with_durations)

    if n == 1:
        return None, "[0:v]", "[0:a]"

    video_parts = []
    audio_parts = []

    # ── VIDEO xfade chain ──────────────────────────────────────────────────
    # offset = cumulative duration of previous clips minus accumulated transitions
    video_prev = "[0:v]"
    offset = 0.0

    for i in range(1, n):
        prev_dur = clips_with_durations[i - 1][1]
        offset += prev_dur - td
        offset = round(offset, 4)

        out_label = f"[vx{i}]"
        video_parts.append(
            f"{video_prev}[{i}:v]xfade="
            f"transition={TRANSITION_TYPE}:"
            f"duration={td}:"
            f"offset={offset}"
            f"{out_label}"
        )
        video_prev = out_label

    # ── AUDIO acrossfade chain ─────────────────────────────────────────────
    audio_prev = "[0:a]"

    for i in range(1, n):
        out_label = f"[ax{i}]"
        audio_parts.append(
            f"{audio_prev}[{i}:a]acrossfade="
            f"d={td}:"
            f"c1=tri:c2=tri"   # triangular — smooth natural fade
            f"{out_label}"
        )
        audio_prev = out_label

    filter_complex = ";".join(video_parts + audio_parts)
    return filter_complex, video_prev, audio_prev


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND RENDER
# ─────────────────────────────────────────────────────────────────────────────

def render_in_background(job_id, input_data, callback_url, metadata):
    """
    Pipeline:
      1. Download & normalize all clips (cover + scenes)
      2. Apply xfade (video) + acrossfade (audio) transitions
      3. Upload final video to Cloudflare R2
      4. POST result + metadata to Make.com callback_url (Scenario 2)
    """
    start_time = time.time()
    job_temp = f"{TEMP_DIR}/{job_id}"
    os.makedirs(job_temp, exist_ok=True)

    try:
        video_cover = input_data.get("video_cover")
        scenes      = input_data.get("scenes", [])

        if not scenes:
            raise Exception("No scenes provided")

        norm_clips = []  # normalized file paths in order

        # ── 0. Cover ──────────────────────────────────────────────────────
        if video_cover:
            cover_raw  = f"{job_temp}/cover_raw"
            cover_norm = f"{job_temp}/cover_norm.mp4"

            r = requests.get(video_cover, timeout=60)
            r.raise_for_status()
            with open(cover_raw, "wb") as f:
                f.write(r.content)

            normalize_clip(cover_raw, cover_norm, job_id, "Cover", job_temp, duration_sec=5)
            norm_clips.append(cover_norm)
            print(f"[{job_id}] Cover done in {time.time() - start_time:.1f}s")

        # ── 1. Scenes ──────────────────────────────────────────────────────
        for i, scene in enumerate(scenes):
            video_url = scene.get("video_url")
            if not video_url:
                raise Exception(f"Missing video_url in scene {i}")

            raw_path  = f"{job_temp}/scene_{i}_raw.mp4"
            norm_path = f"{job_temp}/scene_{i}_norm.mp4"

            r = requests.get(video_url, timeout=120)
            r.raise_for_status()
            with open(raw_path, "wb") as f:
                f.write(r.content)

            normalize_clip(raw_path, norm_path, job_id, f"Scene {i}", job_temp)
            norm_clips.append(norm_path)
            print(f"[{job_id}] Scene {i} done in {time.time() - start_time:.1f}s")

        # ── 2. Get durations for xfade offset calculation ──────────────────
        clips_with_durations = []
        for path in norm_clips:
            dur = get_video_duration(path)
            clips_with_durations.append((path, dur))
            print(f"[{job_id}]   {os.path.basename(path)}: {dur:.2f}s")

        # ── 3. Merge with xfade transitions ────────────────────────────────
        final_path = f"{job_temp}/final_{job_id}.mp4"
        td = TRANSITION_DURATION

        if len(norm_clips) == 1:
            shutil.copy(norm_clips[0], final_path)
            print(f"[{job_id}] Single clip — no transitions needed")
        else:
            filter_complex, v_out, a_out = build_xfade_filter(clips_with_durations, td)

            inputs = []
            for path, _ in clips_with_durations:
                inputs += ["-i", path]

            cmd = ["ffmpeg", "-y"] + inputs + [
                "-filter_complex", filter_complex,
                "-map", v_out,
                "-map", a_out,
                "-c:v", "libx264", "-b:v", VIDEO_BITRATE, "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k",
                final_path
            ]

            print(f"[{job_id}] Merging {len(norm_clips)} clips with {TRANSITION_TYPE} transitions...")
            subprocess.run(cmd, check=True, timeout=1200)

        print(f"[{job_id}] Merge done in {time.time() - start_time:.1f}s")

        # ── 4. Upload to Cloudflare R2 ─────────────────────────────────────
        s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
        )
        key = f"videos/final_{job_id}.mp4"
        s3.upload_file(final_path, R2_BUCKET, key)
        video_url_result = f"{R2_PUBLIC_URL}/{key}"

        total_time = time.time() - start_time
        print(f"[{job_id}] DONE in {total_time:.1f}s → {video_url_result}")

        # ── 5. Callback to Make.com Scenario 2 ────────────────────────────
        requests.post(callback_url, json={
            "status":          "success",
            "job_id":          job_id,
            "url":             video_url_result,
            "render_time_sec": round(total_time, 1),
            "metadata":        metadata   # user_email, user_name, child_name, order_id
        }, timeout=30)

    except Exception as e:
        print(f"[{job_id}] ERROR: {e}")
        traceback.print_exc()
        try:
            requests.post(callback_url, json={
                "status":   "error",
                "job_id":   job_id,
                "message":  str(e),
                "metadata": metadata
            }, timeout=30)
        except:
            pass

    finally:
        try:
            shutil.rmtree(job_temp)
        except:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/render", methods=["POST"])
def render_video():
    """
    POST /render
    Accepts render job from Make.com Scenario 1.
    Returns job_id immediately (<1 sec) — no timeout risk.
    Final video URL + metadata POSTed to callback_url when ready.

    Expected JSON:
    {
        "input": {
            "video_cover": "https://...",       ← optional
            "scenes": [
                {"video_url": "https://..."},   ← Grok video WITH audio
                ...
            ]
        },
        "callback_url": "https://hook.make.com/...",
        "metadata": {
            "user_email":  "anna@gmail.com",
            "user_name":   "Anna",
            "child_name":  "Saly",
            "order_id":    "12345"
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400

        input_data   = data.get("input", {})
        callback_url = data.get("callback_url")
        metadata     = data.get("metadata", {})

        if not input_data:
            return jsonify({"status": "error", "message": "No 'input' data"}), 400
        if not callback_url:
            return jsonify({"status": "error", "message": "No 'callback_url' provided"}), 400

        job_id       = uuid.uuid4().hex
        scenes_count = len(input_data.get("scenes", []))

        print(f"[NEW JOB] {job_id} | scenes: {scenes_count} | user: {metadata.get('user_email', '?')}")
        print(f"[NEW JOB] transition: {TRANSITION_TYPE} {TRANSITION_DURATION}s (audio: acrossfade tri)")

        thread = threading.Thread(
            target=render_in_background,
            args=(job_id, input_data, callback_url, metadata),
            daemon=True
        )
        thread.start()

        return jsonify({
            "status":  "processing",
            "job_id":  job_id,
            "message": (
                f"Render started: {scenes_count} scenes | "
                f"transition={TRANSITION_TYPE} {TRANSITION_DURATION}s"
            )
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":            "ok",
        "service":           "toontoonic-render",
        "transition_video":  f"{TRANSITION_TYPE} {TRANSITION_DURATION}s",
        "transition_audio":  f"acrossfade tri {TRANSITION_DURATION}s"
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
