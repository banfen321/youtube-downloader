#!/bin/sh
set -e

# Pull the latest yt-dlp on start so we keep up with YouTube changes and avoid
# the "version is older than N days" warning. Disable with YTDLP_UPDATE=0.
if [ "${YTDLP_UPDATE:-1}" = "1" ]; then
  echo "[entrypoint] updating yt-dlp to the latest version..."
  pip install --no-cache-dir -U yt-dlp >/dev/null 2>&1 \
    || echo "[entrypoint] update failed (no network?), using the bundled version"
fi

echo "[entrypoint] yt-dlp $(yt-dlp --version 2>/dev/null || echo '?'), ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"

# A single worker: the in-memory progress/job store must be shared across requests.
exec gunicorn -b "0.0.0.0:${PORT:-8080}" --timeout 1800 --workers 1 --threads 8 app:app
