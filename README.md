# youtube-downloader

A small, self-hosted web service to download audio and video from YouTube.
Paste a link, tweak the metadata and cover, and download — high-quality **MP3
320k** with proper ID3 tags and an embedded cover, or video in any resolution.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/),
packaged as a single Docker container. Free and open source (MIT).

---

## Features

- 🎵 **MP3 320k CBR** with editable tags (title / artist / album) and an embedded
  JPEG cover that displays everywhere (Windows Explorer, car head units, phones).
- 🖼️ **Live preview on paste** — title, artist, album and the thumbnail load
  automatically. Edit any field before downloading.
- 🎨 **Custom cover** — drag-and-drop your own image (or click to pick one); it is
  silently downscaled and compressed so it never bloats the file.
- 📊 **Real-time progress** — download speed, downloaded/total size, and a true
  **conversion percentage**.
- 🎬 **Audio** mode (best stream, no re-encoding) and **video** mode (up to 4K).
- 🌍 **Proxy + cookies support** for regions/IPs where YouTube gates downloads.
- 🧹 **Stateless** — downloads stream straight to your browser and are cleaned up;
  nothing is written to disk (work happens in a size-capped RAM tmpfs).

## Quick start

```bash
git clone <this-repo>
cd youtube-downloader
docker compose up -d --build
```

Open <http://localhost:8080>, paste a YouTube link, pick a format, and download.

> Optional: copy `.env.example` to `.env` and set `YT_PROXY` if you need a proxy.

## How it works

```
Browser ──► Flask (app.py) ──► yt-dlp ──► ffmpeg
                │                 │           │
            preview /         download     transcode
            progress /        + cookies     mp3 320k
            result            + proxy       + tags/cover
```

1. **Paste** a link → `POST /api/info` runs `yt-dlp --skip-download` to fetch the
   title/artist/thumbnail. The thumbnail is returned inline (base64) because the
   browser often can't reach the YouTube CDN directly.
2. **Download** → `POST /api/download` starts a background job and returns a
   `job_id`. The browser polls `GET /api/progress/<id>` for live progress.
3. For MP3 the audio is downloaded by yt-dlp, then transcoded by ffmpeg (so we get
   a real conversion %), tagged, and given the chosen cover.
4. **Result** → `GET /api/result/<id>` streams the finished file with a clean
   `Artist - Title.mp3` name, then deletes the temporary files.

## Configuration

| Variable       | Default            | Description                                   |
|----------------|--------------------|-----------------------------------------------|
| `PORT`         | `8080`             | HTTP port                                     |
| `YTDLP_UPDATE` | `1`                | Pull the latest yt-dlp on container start     |
| `YT_PROXY`     | *(empty)*          | SOCKS/HTTP proxy (`socks://` → `socks5h://`)   |
| `YT_COOKIES`   | `/data/cookies.txt`| Path to a Netscape cookies.txt                 |

## Proxy & cookies (when YouTube is blocked)

YouTube increasingly gates downloads from datacenter IPs behind a "Sign in to
confirm you're not a bot" check, and requires a JS challenge solver. This service
handles all three:

- **Proxy** — set `YT_PROXY` in `.env`.
- **JS challenge** — Deno + the EJS solver are bundled and enabled automatically.
- **Cookies** — provide an authenticated YouTube session.

### Exporting cookies that don't expire immediately

A session exported from your everyday browser is rotated by Google within minutes.
To get one that lasts:

1. Open a **private/incognito** window and log into `youtube.com` (use a
   throwaway account).
2. Copy the `Cookie:` request header from DevTools → Network, **or** export with
   a "Get cookies.txt LOCALLY" extension.
3. **Close the incognito window immediately** and don't open that account anywhere
   else — yt-dlp then becomes the sole owner of the session and keeps it alive.
4. Paste the header into the **🍪 Cookies** box in the UI (or drop a `cookies.txt`
   into `./data/`).

The service writes rotated tokens back to the cookie file, so one good export
lasts a long time.

## Command-line helper

The repo also ships a standalone `yt` script (same engine, no container):

```bash
./yt mp3   "https://youtu.be/..."         # MP3 320k
./yt audio "https://youtu.be/..."         # best audio, no re-encode
./yt video "https://youtu.be/..." 1080    # video
./yt formats "https://youtu.be/..."       # list formats
```

## Security notes

- `.env` and `data/cookies.txt` hold secrets and are **git-ignored** — never commit them.
- The backend only accepts `youtube.com` / `youtu.be` URLs (no open-proxy abuse).
- Cookies grant access to the linked Google account — always use a throwaway one.

## License

[MIT](LICENSE) — do whatever you like.

## Disclaimer

For personal use. Respect YouTube's Terms of Service and the copyright of content
you download.
