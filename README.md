<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/yt--dlp-latest-red?logo=youtube&logoColor=white" alt="yt-dlp">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/dependencies-4-purple" alt="4 dependencies">
</p>

<h1 align="center">StreamRec</h1>

<p align="center">
  <strong>Self-hosted live stream recorder with a fast, dependency-free web UI.</strong><br>
  Monitor channels across 30+ platforms and capture streams automatically — with recordings library, log viewer, webhooks, and optional password protection.
</p>

---

## Why StreamRec

- **Zero frontend dependencies** — the entire UI is one HTML file. No npm, no build step, no CDN requests. Loads instantly, works offline.
- **Light on resources** — polling pauses when the browser tab is hidden; a Raspberry Pi mode halves CPU/RAM/disk overhead.
- **Set-and-forget** — channels are polled for live status and recorded automatically the moment they go online, with infinite retry/backoff on dropouts.
- **Recordings library** — browse, preview, favorite, archive, and download finished captures from the web UI.

## Features

**Recording**
- Auto-record when a channel goes live (per-channel toggle or global master switch)
- One-click manual recording from the dashboard
- Quality selection (Best / 1080p / 720p / 480p / Lowest) and format (MP4 / MKV / TS)
- Per-channel overrides for quality, format, max duration, and max streams kept
- Automatic faststart remux on completion for smooth playback
- Stalled-stream detection with optional auto-stop

**Reliability**
- Auto-retry with configurable attempts and delay when a stream drops
- Process-tree termination (no orphaned yt-dlp/ffmpeg after crashes)
- Startup sweep kills leftover recorder processes from a previous unclean stop
- Live log viewer for the active session, retained after it ends

**Platform support** — 30+ sites via yt-dlp, including YouTube, Twitch, TikTok, Kick, Bilibili, Douyin, Huya, Douyu, SOOP, Niconico, Twitcasting, Bigo, Dailymotion, Facebook, Instagram, Twitter/X, Vimeo, Rumble, and more.

**Security**
- Optional local account (PBKDF2-hashed password) — when set, the whole API requires a session
- Same-origin protection for cookie-authenticated state changes
- SSRF guards on channel URLs and webhooks
- Dangerous yt-dlp flags blocked in user-supplied extra args

**Notifications** — Discord embeds (rich: thumbnail, duration, size) or generic JSON webhooks for Slack/ntfy/custom.

## Getting Started

### Docker (recommended)

```bash
git clone https://github.com/MousaXD/streamerREC.git
cd streamerREC
docker compose up -d
```

Open `http://127.0.0.1:8080` and create a password-protected account (at least 12 characters). Recordings land in `./recordings`.

The default Compose deployment is hardened: the recorder runs as UID/GID 999 without Linux capabilities, with a read-only root filesystem, a private internal network and a localhost-only published port. A fixed-target unprivileged relay publishes the dashboard on localhost without giving the recorder a route to the internet. A separate unprivileged Squid proxy permits only the streaming domains in `egress-domains.txt`; private addresses, arbitrary ports and other domains are denied. By default TikTok and BIGO are enabled. Other platforms require explicitly reviewing and adding their official API and media CDN domains, then rebuilding both images. External browser thumbnails, webhooks, custom proxies and custom downloader flags are disabled in this mode. HTTPS tunnels are not TLS-inspected; an allowed platform/CDN remains a trusted destination.

For an existing rootless Docker installation, ensure the recording directory is writable by the container's *mapped* UID (not necessarily host UID 999). Inspect the mapping with `docker compose exec streamrec cat /proc/self/uid_map`; grant a narrowly scoped ACL to the mapped UID on `recordings` only, never change ownership or permissions of your entire home directory. Back up `recordings/state.json` and any account file before migrating. A fresh directory can be initialized with the same scoped access. Do not commit recordings, credentials or exported configuration.

### Manual

Prerequisites: Python 3.12+, [FFmpeg](https://ffmpeg.org/download.html), [yt-dlp](https://github.com/yt-dlp/yt-dlp#installation) on PATH.

```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000`. Recordings default to `~/StreamRec/recordings`. The manual deployment is a compatibility mode without Compose network isolation; use hardened Docker for privacy-sensitive use.

## Live channels, viewer analytics, and connected accounts

- **Add Channel:** choose TikTok Live or BIGO Live and enter the username / BIGO ID. StreamRec generates the canonical live URL automatically. The optional custom URL mode is for already-approved platform share links. Regular TikTok video URLs are rejected.
- **Live audience:** channel cards show the latest reported concurrent viewer count and recording peak. A separate lightweight metadata sampler collects an actual count roughly every 30 seconds while media is being written. A missing count is unavailable, not zero; total video views, likes and popularity are not substituted. TikTok's extractor exposes `concurrent_view_count`; BIGO counts are only available if the platform explicitly supplies a concurrent field. Older recordings have no backfilled viewer data.
- **Playback:** open any recording's in-app player and viewer timeline. Peak viewers, sample average, top moments and a seekable timeline are available for recordings with samples. Click a moment or **Jump to peak**. Offsets are wall-clock estimates from the first media bytes, not frame-accurate synchronization; stream buffering and discontinuities can shift them. Unsupported browser formats can be downloaded, with peak times still visible. Viewer samples persist in the recording index, including active samples recovered after a restart.
- **Connected Accounts:** public streams normally need no account. Under Settings → Connected Accounts, choose the platform, sign in on its official website, and connect the session using the local browser connector (or import a Netscape `cookies.txt` file). No platform password is entered into StreamRec. See [browser connector setup](browser-extension/README.md) for the one-time Chrome/Brave/Chromium setup. The session is scoped to that platform, mode 0600, and snapshotted privately for each request. A stored session does not prove the platform accepts it; sessions can expire. Disconnecting removes the stored session for future requests, not credentials already loaded by a running recorder. Stop that recording if necessary. No additional domains, host browser directories, or Docker privileges are granted.

### Offline regression verification

```bash
docker compose build streamrec
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:size=256m,mode=1777,exec \
  -e RECORDINGS_DIR=/tmp/test-recordings \
  -v "$PWD/tests:/tests:ro" --entrypoint sh streamerrec-streamrec:latest \
  -c 'python -m unittest discover -s /tests -p "test_*.py" -q && python /tests/smoke_live_features.py'
```

The temporary executable fixture in this **test-only** container supplies synthetic platform counts and creates real MP4 media with FFmpeg. The production container keeps its existing restrictions. No personal recordings, real cookies or external network access are needed.

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RECORDINGS_DIR` | `~/StreamRec/recordings` | Where recordings and state are stored |
| `STREAMREC_PI_MODE` | `0` | `1` enables low-power optimizations (fewer pollers, capped buffers, 1 ffmpeg thread) |
| `STATIC_DIR` | directory of `main.py` | Where `index.html` is served from |

### Web UI settings

Everything else is configured in the UI: check interval, default quality/format, auto-convert to MP4, retention days, max duration, auto-retry, proxy, cookies, webhooks, and low-power mode. All settings persist in `state.json` inside the recordings directory.

## Recordings layout

```
recordings/
├── Twitch/<channel>/<date>/<name>_<date>_<time>.mp4
├── Kick/<channel>/<date>/…
├── cookies/          uploaded cookies.txt files
├── _archived/        archived recordings
├── state.json        channels, settings, recording index
└── account.json      local account (only if you set a password)
```

## Cookies / sign-in-required streams

Prefer **Settings → Connected Accounts** to pair the platform-scoped connector or upload a Netscape cookie file for that platform. Legacy cookie uploads remain available under Settings → Cookies; the hardened recorder only supplies cookies for the matching platform. Never upload a browser-wide cookie dump.

## Network policy

The hardened Compose deployment does not support user-configurable proxies, extra downloader flags or webhooks. The optional legacy WireProxy Dockerfile is not part of the hardened deployment. The proxy logs destination hostnames and status codes, not stream URLs. Inspect them with `docker compose logs egress`.

## Authentication

Hardened Compose requires an account before API access and prevents reverting to guest mode. Log in again after password changes or server restarts. The localhost binding is intentional; do not publish this service directly to the internet. Manual compatibility mode retains optional guest mode.

## Updating yt-dlp and dependencies

In hardened mode in-place updates are disabled. Update the pinned `YTDLP_VERSION` and independently verified `YTDLP_SHA256` in the Dockerfile, review the release, and rebuild with `docker compose build && docker compose up -d`. The Python dependencies are installed from a version- and SHA-256-locked `requirements.lock` file. Review and regenerate the lock when upgrading packages. The base image is digest-pinned; update its digest deliberately. Build-time package retrieval uses the normal package repositories; runtime egress remains restricted.

## API

FastAPI auto-docs at `/docs`. Quick reference:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/POST` | `/api/channels` | List / add channels |
| `PATCH/DELETE` | `/api/channels/{id}` | Update / delete a channel |
| `POST` | `/api/channels/{id}/record` `…/stop` `…/kill` | Recording control |
| `POST` | `/api/channels/{id}/refresh` | Re-fetch metadata + live status |
| `GET` | `/api/recordings` | Recording index |
| `GET` | `/api/preview/{id}` `…/download/{id}` `…/{id}/log` | File access |
| `POST` | `/api/recordings/{id}/favorite` `…/archive` | Organize |
| `GET/PATCH` | `/api/settings` | Configuration |
| `GET/POST` | `/api/account` | Local auth |
| `GET` | `/api/health` `/api/disk` | Status |

## Raspberry Pi / low-power mode

Set `STREAMREC_PI_MODE=1` (or toggle in Settings): concurrent subprocess cap drops 6 → 3, polling intervals double, ffmpeg runs single-threaded, and download buffers shrink to 32 KB.

## Project structure

```
streamerREC/
├── main.py               # FastAPI backend — API, recording engine, monitor loop
├── index.html            # entire frontend (single file, no build)
├── Dockerfile            # main container
├── Dockerfile.wireproxy  # optional VPN sidecar
├── docker-compose.yml
└── requirements.txt      # 4 packages
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Recording fails immediately | Update yt-dlp (Settings → System). Extractors break often. |
| Logs empty during recording | Update yt-dlp — very old versions silence output with `--print`. |
| "URL rejected: private/internal host" | You added a LAN/local URL — blocked intentionally (SSRF guard). |
| Doubled recordings after a crash | Fixed by the startup sweep; make sure you're on the latest code. |
| Streams 403 on Twitch with cookies | Re-export fresh cookies; Twitch rotates tokens aggressively. |

## Contributing

PRs welcome. Keep the frontend a single file and dependency-free.

## License

[MIT](LICENSE)
