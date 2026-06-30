"""
youtube-downloader — a small self-hosted web service to download audio/video from
YouTube. Built around yt-dlp + ffmpeg.

Features:
  * MP3 320k CBR with editable ID3 tags and an embedded (JPEG) cover.
  * "audio" mode that keeps the original best audio stream without re-encoding.
  * "video" mode (best video+audio merged into mkv) with a quality cap.
  * Live preview on paste: title / artist / album / thumbnail, all editable
    before downloading, including drag-and-drop cover replacement.
  * Real-time progress with download speed and a true conversion percentage.
  * Optional SOCKS/HTTP proxy and YouTube cookies for regions/IPs where YouTube
    gates downloads behind an anti-bot check.

The HTTP layer is a thin Flask app; the heavy lifting is delegated to yt-dlp and
ffmpeg subprocesses. Downloads run as background jobs so the UI can poll progress.
"""

import base64
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from flask import Flask, jsonify, make_response, render_template, request, send_file

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
#
# Every tunable below can be overridden through an environment variable; the
# defaults reproduce the previous hardcoded behaviour. See .env.example / README.


def env_str(name, default):
    return os.environ.get(name, default).strip() or default


def env_int(name, default):
    """Read an int env var, falling back to ``default`` on missing/garbage."""
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Cap the request body so an oversized cover upload can't exhaust memory.
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 64)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Only accept YouTube links; anything else would turn this into an open proxy.
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.I
)

QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
MODES = {"mp3", "audio", "video"}

# UI defaults applied when the request omits them.
DEFAULT_MODE = env_str("DEFAULT_MODE", "mp3")
DEFAULT_QUALITY = env_str("DEFAULT_QUALITY", "best")

# MP3 transcode / cover settings.
MP3_BITRATE = env_str("MP3_BITRATE", "320k")     # libmp3lame -b:a
COVER_MAX_PX = env_int("COVER_MAX_PX", 1200)     # longest cover edge
COVER_QUALITY = env_int("COVER_QUALITY", 5)      # ffmpeg -q:v (2=best..31=worst)

# yt-dlp robustness knobs.
YTDLP_RETRIES = env_int("YTDLP_RETRIES", 10)
YTDLP_CONCURRENT_FRAGMENTS = env_int("YTDLP_CONCURRENT_FRAGMENTS", 4)
# The EJS challenge solver is required for most videos; expose a kill switch in
# case GitHub is unreachable and you only need already-unlocked formats.
YTDLP_USE_EJS = env_bool("YTDLP_USE_EJS", True)

# Timeouts / cleanup timers (seconds).
INFO_TIMEOUT = env_int("INFO_TIMEOUT", 180)      # /api/info yt-dlp timeout
REAP_INTERVAL = env_int("REAP_INTERVAL", 120)    # cleanup sweep period
JOB_TTL = env_int("JOB_TTL", 300)                # drop finished jobs after this
ORPHAN_TTL = env_int("ORPHAN_TTL", 3600)         # drop stray temp dirs after this

# Proxy for yt-dlp. "socks://" is normalised to "socks5h://" so DNS is resolved
# on the proxy side — important when the client's DNS/YouTube is blocked.
PROXY = os.environ.get("YT_PROXY", "").strip()
if PROXY.startswith("socks://"):
    PROXY = "socks5h://" + PROXY[len("socks://"):]

# Path to a Netscape cookies.txt with an authenticated YouTube session. Required
# to bypass the "confirm you're not a bot" check on flagged (datacenter) IPs.
COOKIES = os.environ.get("YT_COOKIES", "").strip()

# Flags shared by every yt-dlp invocation.
YTDLP_COMMON = [
    "--no-playlist",
    "--no-overwrites",
    "--retries", str(YTDLP_RETRIES),
    "--fragment-retries", str(YTDLP_RETRIES),
    "--concurrent-fragments", str(YTDLP_CONCURRENT_FRAGMENTS),
    # Machine-readable progress lines we parse for the progress bar.
    "--newline",
    "--progress-template",
    "download:###%(progress._percent_str)s|%(progress._speed_str)s|"
    "%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str)s",
]
if YTDLP_USE_EJS:
    # Fetch the EJS challenge-solver from GitHub; without it YouTube only returns
    # storyboards (no real audio/video formats) for many videos.
    YTDLP_COMMON += ["--remote-components", "ejs:github"]

# --------------------------------------------------------------------------- #
# yt-dlp command builders
# --------------------------------------------------------------------------- #


def build_download_cmd(url, mode, quality, outdir):
    """Build the yt-dlp argv for a download into ``outdir``.

    For ``mp3`` we only download the best audio stream and convert it ourselves
    (see :func:`transcode_mp3`) so we can report conversion progress and embed a
    custom cover. ``audio`` and ``video`` are handled entirely by yt-dlp.
    """
    out_template = os.path.join(outdir, "%(title)s.%(ext)s")
    base = ["yt-dlp", *YTDLP_COMMON, "-o", out_template]
    if PROXY:
        base += ["--proxy", PROXY]

    if mode == "mp3":
        return base + [
            "-f", "bestaudio/best",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            url,
        ]

    if mode == "audio":
        return base + [
            "-f", "bestaudio",
            "--embed-thumbnail", "--add-metadata",
            url,
        ]

    # video
    if quality == "best":
        fmt = "bestvideo+bestaudio/best"
    else:
        fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
    return base + [
        "-f", fmt,
        "--merge-output-format", "mkv",
        "--embed-thumbnail", "--embed-metadata",
        url,
    ]


# --------------------------------------------------------------------------- #
# Cookie helpers
# --------------------------------------------------------------------------- #


def add_cookies(cmd, workdir):
    """Copy the master cookies into ``workdir`` and append ``--cookies``.

    yt-dlp rewrites the cookie file to persist rotated session tokens, so it must
    be writable. We work on a per-request copy to avoid concurrent corruption and
    return its path (or ``None`` when no cookies are configured).
    """
    if COOKIES and os.path.isfile(COOKIES):
        copy = os.path.join(workdir, "cookies.txt")
        shutil.copyfile(COOKIES, copy)
        cmd += ["--cookies", copy]
        return copy
    return None


def persist_cookies(cookie_copy):
    """Write the rotated cookies back to the master file (best effort)."""
    if cookie_copy:
        try:
            shutil.copyfile(cookie_copy, COOKIES)
        except OSError:
            pass


def cookie_header_to_netscape(raw):
    """Convert a browser ``Cookie:`` header into a Netscape cookies.txt.

    Returns ``(text, [names])``. Used by the UI so a session can be refreshed by
    pasting the header instead of fiddling with files.
    """
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    values, order = {}, []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name not in values:
            order.append(name)
        values[name] = value

    lines = ["# Netscape HTTP Cookie File", "# generated via web UI"]
    for name in order:
        lines.append("\t".join(
            [".youtube.com", "TRUE", "/", "TRUE", "2147483647", name, values[name]]))
    return "\n".join(lines) + "\n", order


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #


def ffprobe_duration(path):
    """Return the audio duration in seconds (used to compute conversion %)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError):
        return 0.0


def transcode_mp3(job_id, src, dst, meta, cover_path, duration):
    """Transcode ``src`` to a 320k CBR MP3 with tags and an optional cover.

    ffmpeg's ``-progress`` output is parsed against ``duration`` to report a real
    conversion percentage. The cover (custom or the YouTube thumbnail) is scaled
    down to <=1200px and re-encoded as JPEG so it is small and plays everywhere.
    """
    cmd = ["ffmpeg", "-y", "-i", src]
    if cover_path:
        cmd += [
            "-i", cover_path, "-map", "0:a", "-map", "1:v",
            "-c:v", "mjpeg", "-pix_fmt", "yuvj420p",
            "-vf", f"scale='min({COVER_MAX_PX},iw)':-2", "-q:v", str(COVER_QUALITY),
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            "-disposition:v", "attached_pic",
        ]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-id3v2_version", "3"]
    for key in ("title", "artist", "album"):
        value = (meta.get(key) or "").strip()
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-progress", "pipe:1", "-nostats", dst]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and duration > 0:
            try:
                pct = int(line.split("=", 1)[1]) / 1e6 / duration * 100
                set_job(job_id, stage="convert", percent=round(max(0, min(100, pct)), 1))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


# --------------------------------------------------------------------------- #
# Background jobs
# --------------------------------------------------------------------------- #

# In-memory job store. Requires a single worker process (see entrypoint.sh).
JOBS = {}
JOBS_LOCK = threading.Lock()

# yt-dlp download progress line emitted by our --progress-template.
PROGRESS_RE = re.compile(r"^###\s*([\d.]+)%\|(.*?)\|(.*?)\|(.*?)$")


def set_job(job_id, **fields):
    """Atomically update a job; stamp completion time on terminal states."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        # Reset the TTL clock to completion time so the reaper's window starts
        # when the file is ready, not when the (possibly long) job began.
        if fields.get("status") in ("done", "error"):
            fields.setdefault("ts", time.time())
        job.update(fields)


def safe_download_name(meta, path):
    """Build a clean "Artist - Title.ext" download name (no video id)."""
    ext = os.path.splitext(path)[1]
    title = (meta.get("title") or "").strip()
    artist = (meta.get("artist") or "").strip()
    if title and artist:
        name = f"{artist} - {title}{ext}"
    elif title:
        name = f"{title}{ext}"
    else:
        name = os.path.basename(path)
    return re.sub(r"[/\\\x00]", "_", name)


def run_job(job_id, url, mode, quality, meta, cover):
    """Worker: download, (for mp3) transcode, then mark the job done."""
    workdir = JOBS[job_id]["workdir"]
    log_tail = []  # keep the last lines for error reporting
    try:
        cmd = build_download_cmd(url, mode, quality, workdir)
        cookie_copy = add_cookies(cmd, workdir)

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            log_tail.append(line)
            if len(log_tail) > 40:
                log_tail.pop(0)

            match = PROGRESS_RE.match(line)
            if match:
                pct, speed, downloaded, total = match.groups()
                set_job(job_id, stage="download", percent=float(pct),
                        speed=speed.strip(), downloaded=downloaded.strip(),
                        total=total.strip())
            elif line.startswith(("[Merger]", "[Metadata]", "[EmbedThumbnail]")):
                set_job(job_id, stage="finalize", percent=None)
        proc.wait()

        if proc.returncode != 0:
            set_job(job_id, status="error", error="yt-dlp: " + "\n".join(log_tail[-12:]))
            return
        persist_cookies(cookie_copy)

        files = [f for f in glob.glob(os.path.join(workdir, "*"))
                 if os.path.isfile(f) and not f.endswith("cookies.txt")]
        files.sort(key=os.path.getsize, reverse=True)
        if not files:
            set_job(job_id, status="error", error="no output file")
            return

        if mode == "mp3":
            path = finalize_mp3(job_id, files, workdir, meta, cover)
        else:
            path = files[0]

        set_job(job_id, status="done", percent=100,
                file=path, name=safe_download_name(meta, path))
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "ignore") if exc.stderr else ""
        set_job(job_id, status="error", error="ffmpeg: " + detail[-600:])
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        set_job(job_id, status="error", error=str(exc))


def finalize_mp3(job_id, files, workdir, meta, cover):
    """Pick the audio + cover, then transcode to a tagged MP3. Returns the path."""
    audio = next((f for f in files if not f.lower().endswith(".jpg")), files[0])

    cover_path = None
    if cover and "," in cover:
        cover_path = os.path.join(workdir, "cover.img")
        with open(cover_path, "wb") as fh:
            fh.write(base64.b64decode(cover.split(",", 1)[1]))
    else:
        thumbs = [f for f in files if f.lower().endswith(".jpg")]
        cover_path = thumbs[0] if thumbs else None

    out = os.path.join(workdir, "out.mp3")
    set_job(job_id, stage="convert", percent=0)
    transcode_mp3(job_id, audio, out, meta, cover_path, ffprobe_duration(audio))
    return out


def reaper():
    """Periodically drop finished jobs and orphaned temp dirs so /tmp can't grow."""
    tmp = tempfile.gettempdir()
    while True:
        time.sleep(REAP_INTERVAL)
        now = time.time()
        with JOBS_LOCK:
            # Finished jobs (delivered or not) are cleaned JOB_TTL seconds after
            # completion; a safety net for call_on_close cleanup, which can lag.
            for job_id in [j for j, v in JOBS.items()
                           if v["status"] != "running" and now - v["ts"] > JOB_TTL]:
                shutil.rmtree(JOBS[job_id]["workdir"], ignore_errors=True)
                JOBS.pop(job_id, None)
            active = {v["workdir"] for v in JOBS.values()}

        # Sweep any temp dir not tied to a live job and older than ORPHAN_TTL.
        for d in (glob.glob(os.path.join(tmp, "ytdl_*"))
                  + glob.glob(os.path.join(tmp, "ytinfo_*"))):
            try:
                if d not in active and now - os.path.getmtime(d) > ORPHAN_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.route("/")
def index():
    # no-cache: index.html ships all the JS, so we never want a stale copy.
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/cookies", methods=["POST"])
def set_cookies():
    """Save a pasted ``Cookie:`` header as the cookies.txt used by yt-dlp."""
    if not COOKIES:
        return jsonify(error="YT_COOKIES is not configured"), 400

    data = request.get_json(force=True, silent=True) or {}
    text, names = cookie_header_to_netscape(data.get("cookie") or "")
    if not names:
        return jsonify(error="doesn't look like a Cookie header"), 400
    try:
        with open(COOKIES, "w") as fh:
            fh.write(text)
    except OSError as exc:
        return jsonify(error=f"can't write cookies: {exc}"), 500

    authenticated = any(n in names for n in ("SID", "__Secure-1PSID", "__Secure-3PSID"))
    return jsonify(ok=True, count=len(names), authenticated=authenticated)


@app.route("/api/info", methods=["POST"])
def info():
    """Return title/artist/album/duration and an inline (base64) thumbnail.

    The thumbnail is inlined because the client's browser usually can't reach the
    YouTube CDN directly when YouTube is blocked.
    """
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not YOUTUBE_URL_RE.match(url):
        return jsonify(error="YouTube links only"), 400

    workdir = tempfile.mkdtemp(prefix="ytinfo_")
    try:
        cmd = ["yt-dlp", *YTDLP_COMMON]
        if PROXY:
            cmd += ["--proxy", PROXY]
        cookie_copy = add_cookies(cmd, workdir)
        cmd += ["--skip-download", "--write-info-json",
                "--write-thumbnail", "--convert-thumbnails", "jpg",
                "-o", os.path.join(workdir, "%(id)s.%(ext)s"), url]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=INFO_TIMEOUT)
        persist_cookies(cookie_copy)
        if proc.returncode != 0:
            return jsonify(error="failed to fetch info",
                           detail=(proc.stderr or "")[-1500:]), 500

        info_files = glob.glob(os.path.join(workdir, "*.info.json"))
        if not info_files:
            return jsonify(error="no metadata"), 500
        with open(info_files[0]) as fh:
            meta = json.load(fh)

        thumb = None
        jpgs = glob.glob(os.path.join(workdir, "*.jpg"))
        if jpgs:
            with open(jpgs[0], "rb") as fh:
                thumb = "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()

        return jsonify(
            title=meta.get("track") or meta.get("title") or "",
            artist=meta.get("artist") or meta.get("creator") or meta.get("uploader") or "",
            album=meta.get("album") or "",
            duration=meta.get("duration"),
            thumb=thumb,
        )
    except subprocess.TimeoutExpired:
        return jsonify(error="info fetch timeout"), 504
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.route("/api/download", methods=["POST"])
def download():
    """Start a download job and return its id; progress is polled separately."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode") or DEFAULT_MODE
    quality = data.get("quality") or DEFAULT_QUALITY

    if not YOUTUBE_URL_RE.match(url):
        return jsonify(error="YouTube links only"), 400
    if mode not in MODES:
        return jsonify(error="unknown mode"), 400
    if quality not in QUALITIES:
        quality = "best"

    meta = data.get("meta") or {}
    cover = data.get("cover")  # data:image/...;base64,... or None

    job_id = uuid.uuid4().hex
    workdir = tempfile.mkdtemp(prefix="ytdl_")
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "stage": "init", "percent": 0,
            "speed": "", "downloaded": "", "total": "", "ts": time.time(),
            "workdir": workdir, "file": None, "name": None, "error": None,
        }
    threading.Thread(target=run_job, daemon=True,
                     args=(job_id, url, mode, quality, meta, cover)).start()
    return jsonify(job_id=job_id)


@app.route("/api/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error="no such job"), 404
        return jsonify({k: job[k] for k in
                        ("status", "stage", "percent", "speed",
                         "downloaded", "total", "error")})


@app.route("/api/result/<job_id>")
def result(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify(error="no such job"), 404
    if job["status"] != "done":
        return jsonify(error="not ready yet"), 409

    resp = send_file(job["file"], as_attachment=True, download_name=job["name"])

    @resp.call_on_close
    def _cleanup():
        shutil.rmtree(job["workdir"], ignore_errors=True)
        with JOBS_LOCK:
            JOBS.pop(job_id, None)

    return resp


threading.Thread(target=reaper, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
