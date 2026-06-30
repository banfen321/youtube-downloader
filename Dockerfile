FROM python:3.12-slim

# ffmpeg: audio extraction / cover embedding / muxing.
# curl + unzip: used below to install Deno.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno is the JS runtime yt-dlp uses to solve YouTube's player challenge.
# Without it, many videos return no downloadable formats.
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

# A recent yt-dlp is baked in; entrypoint.sh can pull an even newer one on start.
RUN pip install --no-cache-dir flask gunicorn yt-dlp

WORKDIR /app
COPY app.py entrypoint.sh ./
COPY templates ./templates
RUN chmod +x entrypoint.sh

ENV PORT=8080 YTDLP_UPDATE=1 YT_COOKIES=/data/cookies.txt
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
