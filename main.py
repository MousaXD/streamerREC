import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import platform as _platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import uuid
import ipaddress
import socket
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from bigo import BigoError, BigoLoginRequired, BigoTransportError, fetch_bigo_info, is_bigo_url
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from network_policy import approved_url
from recording_library import build_library, probe_duration, media_duration, MEDIA_SUFFIXES, FINISHED
from live_features import (PLATFORMS, SAMPLE_INTERVAL, SAMPLE_MAX_AGE, platform_key, channel_url,
    viewer_count, add_viewer_sample, viewer_summary, clip_samples, scoped_cookie_text, parse_netscape_cookies, live_probe_status)
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("streamrec")

# ── Platform detection ────────────────────────────────────────────────────────
IS_WINDOWS = _platform.system() == "Windows"
PI_MODE = os.environ.get("STREAMREC_PI_MODE", "0") == "1"
HARDENED = os.environ.get("STREAMREC_HARDENED", "0") == "1"
EGRESS_PROXY = os.environ.get("STREAMREC_EGRESS_PROXY", "")
if HARDENED and not EGRESS_PROXY:
    raise RuntimeError("Hardened mode requires the isolated egress proxy")

# ── Paths ─────────────────────────────────────────────────────────────────────
# Docker sets RECORDINGS_DIR=/recordings  |  bare metal falls back to ~/StreamRec/recordings
_rec_env = os.environ.get("RECORDINGS_DIR", "")
if _rec_env:
    RECORDINGS_DIR = Path(_rec_env)
else:
    RECORDINGS_DIR = Path.home() / "StreamRec" / "recordings"

RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE   = RECORDINGS_DIR / "state.json"
COOKIES_DIR  = RECORDINGS_DIR / "cookies"
COOKIES_DIR.mkdir(exist_ok=True)
ACCOUNT_FILE = RECORDINGS_DIR / "account.json"
AVATAR_DIR   = RECORDINGS_DIR / "avatars"
AVATAR_DIR.mkdir(exist_ok=True)
ARCHIVE_DIR  = RECORDINGS_DIR / "_archived"
ARCHIVE_DIR.mkdir(exist_ok=True)

# Static files: Docker puts index.html in /app, bare metal uses same dir as main.py
_static_dir = os.environ.get("STATIC_DIR", str(Path(__file__).parent))

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "2.5.3"
START_TIME = time.time()

# ── Login rate limiting ───────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = {}  # IP -> [timestamps]
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW = 60  # seconds
_LOGIN_COOLDOWN = 30  # seconds lockout

# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_state()
    n = _sweep_orphan_recorders()
    if n:
        logger.warning("Killed %d orphaned recording process(es) from a previous run", n)
    logger.info("StreamRec %s starting — %d channels loaded", VERSION, len(channels))
    task = asyncio.create_task(_supervised_monitor())
    viewer_task = asyncio.create_task(_viewer_monitor())
    duration_task = asyncio.create_task(_library_duration_worker())
    yield
    viewer_task.cancel()
    duration_task.cancel()
    await asyncio.gather(viewer_task, duration_task, return_exceptions=True)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    # Drain any active recordings so we don't leave zombie yt-dlp children behind
    for rec in list(recordings.values()):
        if rec.get("status") in ("recording", "starting"):
            if rec.get("pid"):
                _stop_rec(rec, force=False)
            else:
                # Recording was starting but subprocess hadn't spawned yet
                rec["status"] = "completed"
                rec["ended_at"] = time.time()
                if (ch_id := rec.get("channel_id")) and ch_id in channels:
                    channels[ch_id]["recording_id"] = None
    # Wait briefly for run_recording tasks to reap their subprocesses, so the
    # final state flush reflects post-cleanup status (and children don't
    # outlive the server and write orphaned files).
    deadline = time.time() + 8
    while time.time() < deadline and any(
        r.get("status") in ("recording", "starting") for r in recordings.values()
    ):
        await asyncio.sleep(0.25)
    _save_state_sync()
    logger.info("StreamRec shutting down")


async def _supervised_monitor():
    """Keep the monitor loop alive even if a single tick raises unexpectedly."""
    while True:
        try:
            await monitor_loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("monitor_loop crashed: %s — restarting in 10s", e)
            await asyncio.sleep(10)

app = FastAPI(title="StreamRec API", version=VERSION, lifespan=lifespan,
              docs_url=None if HARDENED else "/docs",
              redoc_url=None if HARDENED else "/redoc",
              openapi_url=None if HARDENED else "/openapi.json")


def _sweep_orphan_recorders() -> int:
    """Kill recorder leftovers from a previous crashed/stopped instance.
    On startup no legit recording children exist yet, so any known recorder
    process whose command line references our recordings dir is an orphan."""
    killed = 0
    rec_str = str(RECORDINGS_DIR)

    def _is_recorder_process(name: str, cmdline: str) -> bool:
        lname = (name or "").lower()
        return (
            lname in ("yt-dlp.exe", "yt-dlp", "ffmpeg", "ffmpeg.exe")
            or (lname.startswith("python") and "bigo_recorder.py" in cmdline)
        )

    try:
        import psutil  # optional dependency
    except ImportError:
        psutil = None
    if psutil is not None:
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name = p.info["name"] or ""
                cmdline = " ".join(p.info["cmdline"] or [])
                if not _is_recorder_process(name, cmdline):
                    continue
                if rec_str in cmdline:
                    p.kill()
                    killed += 1
            except Exception:
                continue
    elif IS_WINDOWS:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.Name -match '^(yt-dlp|ffmpeg|python)' } | "
                 "Select-Object Name, ProcessId, CommandLine | ConvertTo-Json"],
                capture_output=True, text=True, timeout=20,
            )
            data = json.loads(out.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for entry in data or []:
                cmdline = entry.get("CommandLine") or ""
                if rec_str in cmdline and _is_recorder_process(entry.get("Name") or "", cmdline):
                    subprocess.run(["taskkill", "/F", "/PID", str(entry["ProcessId"])],
                                   capture_output=True, check=False)
                    killed += 1
        except Exception as e:
            logger.debug("orphan sweep fallback failed: %s", e)
    else:
        try:
            out = subprocess.run(
                ["pgrep", "-af", "yt-dlp|ffmpeg|bigo_recorder.py"],
                capture_output=True, text=True, timeout=10,
            )
            for line in (out.stdout or "").splitlines():
                if rec_str in line:
                    subprocess.run(["kill", "-9", line.split(None, 1)[0]],
                                   capture_output=True, check=False)
                    killed += 1
        except Exception as e:
            logger.debug("orphan sweep fallback failed: %s", e)
    return killed


# ── Auth middleware ──────────────────────────────────────────────────────────
# When an account exists, all /api/* endpoints require a valid session cookie
# or Bearer token — except health/version and the account bootstrap endpoints
# (login / create / status).  Without an account the app runs in open guest
# mode.  Cookie-authenticated state-changing requests also get a same-origin
# (CSRF) check on Origin/Referer.
def _is_auth_exempt(method: str, path: str) -> bool:
    """Only bootstrap endpoints are reachable without a session.
    Account PATCH/DELETE (password change, account removal) REQUIRE auth —
    otherwise an attacker could delete the account and reopen guest mode."""
    if path in ("/api/health", "/api/version"):
        return True
    if path == "/api/account" and method == "GET":
        return True
    if path in ("/api/account/login", "/api/account/logout") and method == "POST":
        return True
    return False


def _client_origin_ok(request: Request) -> bool:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if not request.cookies.get("streamrec_session"):
        return True  # Bearer-token or guest clients carry no cookie
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True  # non-browser client
    try:
        return urlparse(origin).netloc == request.url.netloc
    except Exception:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        if path.startswith("/api/"):
            # One-time browser pairing is authenticated by a short-lived capability,
            # not by a browser cookie. The endpoint independently checks that capability.
            pairing = path == "/api/platform-accounts/connect" and method == "POST"
            if HARDENED and not pairing and method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin") or request.headers.get("referer")
                if (origin and urlparse(origin).netloc != request.url.netloc) or request.headers.get("sec-fetch-site") == "cross-site":
                    return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
            if not pairing and not _is_auth_exempt(method, path):
                acc = _load_account()
                setup = path == "/api/account" and method == "POST" and not ACCOUNT_FILE.exists()
                if acc or (HARDENED and not setup):
                    if not _client_origin_ok(request):
                        return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
                    token = request.cookies.get("streamrec_session")
                    if not token:
                        auth_header = request.headers.get("authorization", "")
                        if auth_header.lower().startswith("bearer "):
                            token = auth_header[7:].strip()
                    if not acc or not _is_valid_session(token):
                        return JSONResponse({"detail": "Authentication required"}, status_code=401)
        response = await call_next(request)
        if HARDENED:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
            if path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(AuthMiddleware)
if HARDENED:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


_proc_semaphore = asyncio.Semaphore(3 if PI_MODE else 6)

channels:   dict[str, dict] = {}
recordings: dict[str, dict] = {}
_recording_locks: dict[str, asyncio.Lock] = {}
_lock_cleanup_counter = 0


def _get_recording_lock(ch_id: str) -> asyncio.Lock:
    """Return a per-channel lock to prevent double-recording races."""
    global _lock_cleanup_counter
    if ch_id not in _recording_locks:
        _recording_locks[ch_id] = asyncio.Lock()
    # Periodically remove locks for deleted channels
    _lock_cleanup_counter += 1
    if _lock_cleanup_counter % 50 == 0:
        for cid in list(_recording_locks):
            if cid not in channels:
                del _recording_locks[cid]
    return _recording_locks[ch_id]

_disk_cache:    dict  = {}
_disk_cache_ts: float = 0

# ── Tuning helpers (Pi vs. normal) ───────────────────────────────────────────
# These read from `settings["pi_mode"]` at call time so the toggle takes
# effect immediately without a restart.

def _is_pi() -> bool:
    """True when Potato / Pi mode is active (env var OR settings toggle)."""
    return settings.get("pi_mode", PI_MODE)

def get_size_poll_interval() -> int:
    return 8 if _is_pi() else 3

def get_log_limits() -> tuple[int, int]:
    """Return (max_lines, trim_to) for the recording log buffer."""
    return (60, 30) if _is_pi() else (100, 50)

def get_frontend_poll_hint() -> int:
    return 10 if _is_pi() else 5

def get_disk_cache_ttl() -> int:
    return 60 if _is_pi() else 30

def get_ffmpeg_threads() -> str:
    return "1" if _is_pi() else "2"

# Streaming / preview constants — large chunks reduce HTTP round-trips and
# I/O syscalls, critical on Raspberry Pi / slow SD cards.
PREVIEW_RANGE_CHUNK  = 10 * 1024 * 1024   # 10 MB per HTTP range response
PREVIEW_READ_CHUNK   = 512 * 1024         # 512 KB per file read()

# Remux timeout — 5 min to handle large files on slow Pi SD cards
REMUX_TIMEOUT        = 300

# ── Debounced state persistence ──────────────────────────────────────────────
_save_state_pending = False
_save_state_task:   Optional[asyncio.Task] = None

def _schedule_save_state():
    """Debounce state saves — collapse rapid writes into one disk write."""
    global _save_state_pending, _save_state_task
    _save_state_pending = True
    if _save_state_task and not _save_state_task.done():
        return  # already scheduled
    _save_state_task = asyncio.get_running_loop().create_task(_debounced_save())

async def _debounced_save():
    """Wait for writes to settle, then flush once."""
    global _save_state_pending
    while True:
        _save_state_pending = False
        await asyncio.sleep(2)  # coalesce window
        if not _save_state_pending:
            break                # no new writes arrived — safe to flush
    _save_state_sync()

settings: dict = {
    "pi_mode":          PI_MODE,
    "monitor_interval": 120 if PI_MODE else 60,
    "default_quality":  "best",
    "default_format":   "mp4",
    "auto_convert_mp4": False,
    "delete_original":  False,
    "record_on_add":    False,
    "auto_retry":       True,
    "max_retries":      5,
    "retry_delay":      15,
    "proxy":            "",
    "cookies_file":     "",
    "extra_args":       "",
    "retention_days":   0,   # 0 = keep forever
    "max_duration":     0,   # 0 = no limit, in minutes
    "webhook_url":      "",
    "auto_stop_stalled": False,
}

# ── Cross-platform process helpers ────────────────────────────────────────────

def _effective_proxy(proxy: str = "") -> str:
    return EGRESS_PROXY if HARDENED else proxy


def _media_path(name: str) -> Path:
    if not name:
        raise ValueError("Missing media path")
    path = Path(name).resolve()
    root = RECORDINGS_DIR.resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Path is outside recordings")
    if path.relative_to(root).parts[0] in {"cookies", "avatars"}:
        raise ValueError("Not a recording")
    if path.suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("Not a media file")
    return path


def _cookie_path(name: str) -> Optional[Path]:
    if not name:
        return None
    if Path(name).name != name or name in (".", "..") or not name.lower().endswith(".txt"):
        raise ValueError("Invalid cookies filename")
    path = COOKIES_DIR / name
    if path.is_symlink() or not path.resolve().is_relative_to(COOKIES_DIR.resolve()):
        raise ValueError("Invalid cookies path")
    return path


def _platform_cookie_name(platform: str) -> str:
    if platform not in PLATFORMS:
        raise ValueError("Unsupported platform")
    return f"platform-{platform}.txt"


@contextmanager
def _cookie_snapshot(url: str, name: str = ""):
    platform = platform_key(url)
    selected = name
    if not selected and platform:
        automatic = _cookie_path(_platform_cookie_name(platform))
        if automatic and automatic.is_file():
            selected = automatic.name
    selected = selected or settings.get("cookies_file", "")
    path = _cookie_path(selected)
    if not path or not path.is_file():
        yield ""
        return
    # Read only the chosen cookie file, never the host browser profile. Each
    # process gets its own private snapshot; downloader writes cannot race.
    with tempfile.TemporaryDirectory(prefix="streamrec-cookies-") as directory:
        snapshot = Path(directory) / "cookies.txt"
        if platform:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("Cookie file exceeds size limit")
            try:
                content = scoped_cookie_text(platform, parse_netscape_cookies(path.read_text(encoding="utf-8-sig")))
            except ValueError:
                # An expired/other-platform session must not break public capture.
                yield ""
                return
            snapshot.write_text(content, encoding="utf-8")
        elif HARDENED:
            yield ""
            return
        else:
            shutil.copyfile(path, snapshot)
        os.chmod(snapshot, 0o600)
        yield str(snapshot)


def _yt_dlp_options(proxy: str = "", cookie_path: str = "") -> list[str]:
    args = ["--ignore-config", "--no-plugin-dirs"]
    if proxy := _effective_proxy(proxy):
        args += ["--proxy", proxy]
    if cookie_path:
        args += ["--cookies", cookie_path]
    return args


def _bigo_check_message(exc: Exception) -> str:
    if isinstance(exc, BigoLoginRequired):
        return "BIGO requires a signed-in session to verify this room. Connect your BIGO account in Settings → Platform Accounts."
    if isinstance(exc, BigoTransportError):
        return "BIGO network connection failed. Automatic checks will continue."
    return "BIGO could not verify a playable live stream. Automatic checks will continue."


async def _bigo_info(url: str, proxy: str, cookie_path: str = "") -> dict:
    if cookie_path:
        return await fetch_bigo_info(url, proxy=proxy, cookies_file=cookie_path)
    return await fetch_bigo_info(url, proxy=proxy)


def _validate_hardened_options(values: dict) -> None:
    if HARDENED:
        for field in ("proxy", "extra_args", "webhook_url"):
            if values.get(field):
                raise HTTPException(400, f"{field} is disabled in hardened mode")
    if values.get("cookies_file"):
        try:
            _cookie_path(values["cookies_file"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


def _approved_channel_url(url: str) -> str:
    if not HARDENED:
        return url
    try:
        return approved_url(url)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _subprocess_kwargs() -> dict:
    """
    Extra kwargs for asyncio.create_subprocess_exec so we can kill the whole
    process tree later.
    - Windows: CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK_EVENT to the group.
    - Unix:    setsid creates a new session; killpg kills the whole group.
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"preexec_fn": os.setsid}


def _kill_proc(pid: int, force: bool = False) -> None:
    """Terminate or kill a process (and its children) cross-platform."""
    try:
        if IS_WINDOWS:
            # First attempt graceful CTRL_BREAK_EVENT
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
            if force:
                # Force kill the entire process tree (/T) on Windows
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        else:
            sig = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass


def _safe_replace_file(src: Path, dst: Path, max_retries: int = 5, delay: float = 0.5) -> bool:
    """Safely replace dst with src, handling transient Windows file locks."""
    for attempt in range(max_retries):
        try:
            if IS_WINDOWS:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
            else:
                src.replace(dst)
            return True
        except (PermissionError, OSError) as e:
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                logger.warning("Failed to replace %s with %s after %d attempts: %s", src, dst, max_retries, e)
                return False
    return False


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state_sync():
    """Write state to disk immediately (internal — prefer _save_state)."""
    saved_channels = {}
    for cid, ch in list(channels.items()):
        c = dict(ch)
        for k in ("recording_id", "is_live", "last_checked", "viewer_count", "viewer_checked_at", "live_check_status", "live_check_message", "capture_engine", "engine_checked_at"):
            c.pop(k, None)
        # Strip sensitive credentials from disk-persisted state
        for k in ("ch_password",):
            c.pop(k, None)
        saved_channels[cid] = c
    # Persist active recording metadata too, so a crash does not discard
    # viewer samples collected while the file was being written.
    saved_recordings = {}
    for rid, rec in list(recordings.items()):
        if rec.get("status") in ("completed", "error", "archived", "recording", "starting"):
            saved_recordings[rid] = {k: v for k, v in rec.items() if k not in ("pid", "_viewer_probe_at")}
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({
                "channels": saved_channels,
                "settings": dict(settings),
                "recordings": saved_recordings,
            }, indent=2),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)


def _save_state():
    """Save state — debounced in async context, immediate otherwise."""
    try:
        asyncio.get_running_loop()  # check if we're in an async context
        _schedule_save_state()
    except RuntimeError:
        # No running event loop (startup / sync context) — write immediately
        _save_state_sync()


def _load_state():
    global settings
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for cid, ch in data.get("channels", {}).items():
            ch["recording_id"] = None
            ch["is_live"]      = False
            ch["last_checked"] = None
            ch.pop("viewer_count", None)
            ch.pop("viewer_checked_at", None)
            ch.pop("live_check_status", None)
            ch.pop("live_check_message", None)
            ch.pop("capture_engine", None)
            ch.pop("engine_checked_at", None)
            # Re-detect platform for any channels saved as Unknown
            if ch.get("platform") in ("Unknown", "", None) and ch.get("url"):
                ch["platform"] = detect_platform(ch["url"])
            # Fill display_name from URL if blank
            if not ch.get("display_name") and ch.get("url"):
                ch["display_name"] = _username_from_url(ch["url"])
            if not ch.get("username") and ch.get("url"):
                ch["username"] = _username_from_url(ch["url"])
            if HARDENED:
                ch["proxy"] = ch["extra_args"] = ""
                try:
                    ch["url"] = approved_url(ch["url"])
                except (ValueError, KeyError, TypeError):
                    ch["monitoring"] = False
            channels[cid] = ch
        for k, v in data.get("settings", {}).items():
            if k in settings:
                settings[k] = v
        if HARDENED:
            for key in ("proxy", "extra_args", "webhook_url"):
                settings[key] = ""
        for rid, rec in data.get("recordings", {}).items():
            # Strip any in-progress state fields that no longer apply
            rec.pop("pid", None)
            rec.pop("stopping", None)
            rec.pop("speed", None)
            rec.pop("_viewer_probe_at", None)
            if rec.get("status") in ("recording", "starting"):
                rec["status"] = "error"
                rec["error"] = "Recording interrupted by server restart"
                rec["ended_at"] = rec.get("last_media_at") or rec.get("created_at")
                rec.setdefault("log", []).append("[StreamRec] Interrupted recording recovered from disk index")
                if not rec.get("filepath") and rec.get("capture_path"):
                    rec["filepath"] = rec["capture_path"]
                    rec["filename"] = Path(rec["capture_path"]).name
            if HARDENED and rec.get("filepath"):
                try:
                    _media_path(rec["filepath"])
                except (ValueError, OSError, TypeError):
                    rec["filepath"] = None
                    logger.warning("Ignored invalid media path in recording index")
            recordings[rid] = rec
    except Exception as e:
        logger.warning("Failed to load state: %s", e)


# ── Platform detection ────────────────────────────────────────────────────────

PLATFORM_MAP = [
    (r"youtube\.com|youtu\.be",  "YouTube"),
    (r"twitch\.tv",              "Twitch"),
    (r"tiktok\.com",             "TikTok"),
    (r"kick\.com",               "Kick"),
    (r"bilibili\.com",           "Bilibili"),
    (r"douyin\.com",             "Douyin"),
    (r"afreecatv\.com",          "Afreeca"),
    (r"sooplive\.co(?:\.kr)?",            "Sooplive"),
    (r"naver\.com",              "Naver"),
    (r"weibo\.com",              "Weibo"),
    (r"huya\.com",               "Huya"),
    (r"douyu\.com",              "Douyu"),
    (r"nicovideo\.jp",           "Niconico"),
    (r"dailymotion\.com",        "Dailymotion"),
    (r"facebook\.com|fb\.watch", "Facebook"),
    (r"instagram\.com",          "Instagram"),
    (r"twitter\.com|x\.com",     "Twitter/X"),
    (r"vimeo\.com",              "Vimeo"),
    (r"rumble\.com",             "Rumble"),
    (r"stripchat\.com",          "Stripchat"),
    (r"twitcasting\.tv",         "Twitcasting"),
    (r"pandalive\.co(?:\.kr)?",           "Pandalive"),
    (r"bigo\.tv|bigovideo\.tv", "Bigo"),
    (r"chaturbate\.com",         "Chaturbate"),
    (r"cam4\.com",               "Cam4"),
    (r"myfreecams\.com",         "MyFreeCams"),
    (r"camsoda\.com",            "CamSoda"),
    (r"bongacams\.com",          "BongaCams"),
    (r"cammodels\.com",          "CamModels"),
    (r"streamate\.com",          "Streamate"),
    (r"flirt4free\.com",         "Flirt4Free"),
]

def detect_platform(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").rstrip(".")
    except ValueError:
        return "Unknown"
    for pattern, name in PLATFORM_MAP:
        if re.search(r"(?:^|\.)(?:" + pattern + r")$", host, re.I):
            return name
    return "Unknown"


# ── Metadata fetch ────────────────────────────────────────────────────────────

def _username_from_url(url: str) -> str:
    """Best-effort username extraction directly from the URL path."""
    try:
        path = urlparse(url).path.strip("/")
        # Take first non-empty path segment
        part = path.split("/")[0] if path else ""
        # Ignore generic segments
        if part and part not in ("live", "channel", "watch", "user", "c", "videos"):
            return part
    except Exception:
        pass
    return ""


def _streamlink_options(proxy: str, cookie_path: str = "") -> list[str]:
    args = ["streamlink", "--no-config", "--no-plugin-sideloading", "--no-plugin-cache",
            "--http-timeout", "12", "--stream-timeout", "30", "--retry-open", "1", "--retry-streams", "0", "--retry-max", "0"]
    if proxy := _effective_proxy(proxy):
        args += ["--http-proxy", proxy, "--https-proxy", proxy]
    if cookie_path:
        args += ["--http-cookies-file", cookie_path]
    return args


def _streamlink_quality(quality: str) -> str:
    # TikTok's FLV stream names are codec/quality tiers, not exact pixel sizes.
    # Prefer H.264 video (not the audio-only rendition) for browser playback.
    choices = {
        "1080p": "h264_origin,h264_hd,h264_sd,h264_ld,best",
        "720p": "h264_hd,h264_sd,h264_ld,h264_origin,best",
        "480p": "h264_sd,h264_ld,h264_hd,h264_origin,best",
    }
    return choices.get(quality, "h264_origin,h264_uhd,h264_hd,h264_sd,h264_ld,best")


async def _probe_tiktok_streamlink(url: str, proxy: str, cookie_path: str = "") -> dict | None:
    if platform_key(url) != "tiktok":
        return None
    proc = None
    try:
        async with _proc_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *_streamlink_options(proxy, cookie_path), "--json", "--", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **_subprocess_kwargs())
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        if proc.returncode != 0:
            return None
        data = json.loads(stdout)
        if not isinstance(data, dict) or data.get("plugin") != "tiktok":
            return None
        streams = data.get("streams")
        if not isinstance(streams, dict) or not streams:
            return None
        # A resolved audio-only stream isn't proof that video can be recorded.
        if not any(name not in ("best", "worst") and not name.endswith("_ao") and isinstance(value, dict)
                   for name, value in streams.items()):
            return None
        meta = data.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        return {"is_live": True, "viewer_count": None, "observed_at": time.time(),
                "live_check_status": "live", "live_check_message": None, "capture_engine": "streamlink",
                "stream_title": str(meta.get("title") or "")[:500]}
    except (OSError, ValueError, TypeError, asyncio.TimeoutError):
        return None
    finally:
        if proc is not None and proc.returncode is None:
            _kill_proc(proc.pid, force=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, OSError):
                pass


async def fetch_metadata(url: str, proxy: str = "", cookies_file: str = "") -> dict:
    with _cookie_snapshot(url, cookies_file) as cookie_path:
        return await _fetch_metadata(url, proxy, cookie_path)


async def _fetch_metadata(url: str, proxy: str, cookie_path: str) -> dict:
    # Always extract a fallback username from the URL itself
    url_username = _username_from_url(url)
    proxy = _effective_proxy(proxy or settings.get("proxy", ""))

    if is_bigo_url(url):
        try:
            info = await _bigo_info(url, proxy or settings.get("proxy", ""), cookie_path)
            if info.get("alive") and not info.get("hls_src"):
                raise BigoError("BIGO live room returned no HLS stream")
            return {
                "display_name": info.get("display_name") or url_username,
                "username": info.get("site_id") or url_username,
                "avatar": "",
                "thumbnail": info.get("thumbnail") or "",
                "is_live": bool(info.get("alive") and info.get("hls_src")),
                "viewer_count": viewer_count(info.get("concurrent_view_count")),
                "stream_title": (info.get("stream_title") or "").strip(),
            }
        except Exception as e:
            logger.debug("BIGO metadata lookup failed for %s: %s", url, e)
            if url_username:
                return {
                    "display_name": url_username,
                    "username": url_username,
                    "avatar": "",
                    "thumbnail": "",
                    "stream_title": "",
                    "_lookup_error": True,
                    "live_check_status": "unknown",
                    "live_check_message": _bigo_check_message(e),
                }
            return {"_lookup_error": True, "live_check_status": "unknown",
                    "live_check_message": _bigo_check_message(e)}

    proc = None
    try:
        async with _proc_semaphore:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--dump-single-json", "--no-download",
                "--playlist-items", "1", "--socket-timeout", "15",
                *_yt_dlp_options(proxy, cookie_path), "--", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_subprocess_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        # Even if yt-dlp returns non-zero (offline room), stdout may still have data
        if stdout:
            try:
                data = json.loads(stdout)
            except Exception:
                data = {}
        else:
            data = {}

        if not isinstance(data, dict):
            data = {}
        check_status, check_message = live_probe_status(url, data, proc.returncode, stderr)
        if check_status == "unknown" and platform_key(url) == "tiktok":
            fallback = await _probe_tiktok_streamlink(url, proxy, cookie_path)
            if fallback:
                return {**fallback, "display_name": url_username, "username": url_username,
                        "avatar": "", "thumbnail": "", "_lookup_error": False}
        thumbnails = data.get("thumbnails") or []
        thumbnail  = data.get("thumbnail") or (thumbnails[-1]["url"] if thumbnails else "")
        avatar = ""
        for t in reversed(thumbnails):
            tid = (t.get("id") or "").lower()
            if "avatar" in tid or "profile" in tid:
                avatar = t.get("url", "")
                break
        if not avatar and not HARDENED:
            avatar = await _try_scrape_avatar(url)

        # Prefer yt-dlp data, fall back to URL-parsed username
        yt_name = data.get("uploader") or data.get("channel") or data.get("creator") or ""
        yt_user = data.get("uploader_id") or data.get("channel_id") or ""

        display_name = yt_name or url_username
        username     = yt_user or url_username

        return {
            "display_name": display_name,
            "username":     username,
            "avatar":       avatar,
            "thumbnail":    thumbnail,
            "is_live":      check_status == "live",
            "live_check_status": check_status,
            "live_check_message": check_message,
            "capture_engine": "yt-dlp" if check_status == "live" else None,
            "_lookup_error": check_status == "unknown",
            "viewer_count": viewer_count(data.get("concurrent_view_count")),
            "stream_title": (data.get("title") or "").strip(),
        }
    except Exception:
        # Total failure — still return URL-derived name so card isn't blank
        if url_username:
            return {
                "display_name": url_username,
                "username":     url_username,
                "avatar":       "",
                "thumbnail":    "",
                "is_live":      False,
                "_lookup_error": True,
                "live_check_status": "unknown",
                "live_check_message": "Metadata check could not complete. Automatic checks will continue.",
                "stream_title": "",
            }
        return {}
    finally:
        if proc is not None and proc.returncode is None:
            _kill_proc(proc.pid, force=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, OSError):
                pass


async def _try_scrape_avatar(url: str) -> str:
    try:
        async with _proc_semaphore:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-sL", "--max-time", "10", "-A",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if not stdout:
            return ""
        html = stdout.decode("utf-8", errors="replace")
        for pat in (r'"avatarLarger"\s*:\s*"([^"]+)"',
                    r'"avatarMedium"\s*:\s*"([^"]+)"',
                    r'"avatarThumb"\s*:\s*"([^"]+)"'):
            m = re.search(pat, html)
            if m:
                return m.group(1).replace(r'\u002F', '/')
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\'>]+)["\']',
            html, re.I,
        )
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


async def _check_chaturbate_live(url: str, proxy: str = "") -> Optional[bool]:
    """Scrape Chaturbate page directly — returns True/False/None (None = inconclusive)."""
    try:
        curl_cmd = [
            "curl", "-sL", "--max-time", "10", "-A",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        if proxy:
            curl_cmd += ["--proxy", proxy]
        curl_cmd.append(url)
        async with _proc_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *curl_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if not stdout:
            return None
        html = stdout.decode("utf-8", errors="replace")
        # Chaturbate embeds room status in the page
        if '"room_status": "public"' in html or '"room_status":"public"' in html:
            return True
        if 'offline' in html.lower() and 'room_status' in html:
            return False
        return None
    except Exception:
        return None


async def check_is_live(url: str, proxy: str = "", cookies_file: str = "") -> bool:
    return (await probe_live(url, proxy, cookies_file))["is_live"]


async def probe_live(url: str, proxy: str = "", cookies_file: str = "", *, allow_fallback: bool = True) -> dict:
    """Use normal supported extractors; distinguish unknown from offline."""
    proxy = _effective_proxy(proxy)
    with _cookie_snapshot(url, cookies_file) as cookie_path:
        if is_bigo_url(url):
            info = await _bigo_info(url, proxy, cookie_path)
            if info.get("alive") and not info.get("hls_src"):
                raise BigoError("BIGO live room returned no HLS stream")
            live = bool(info.get("alive") and info.get("hls_src"))
            return {"is_live": live, "viewer_count": viewer_count(info.get("concurrent_view_count")) if live else None, "observed_at": time.time()}
        if not HARDENED and re.search(r"chaturbate\.com", url, re.I):
            result = await _check_chaturbate_live(url, proxy=proxy)
            if result is not None:
                return {"is_live": result, "viewer_count": None, "observed_at": time.time()}
        cmd = ["yt-dlp", "--dump-single-json", "--skip-download",
               "--socket-timeout", "20", "--playlist-items", "1",
               *_yt_dlp_options(proxy, cookie_path), "--", url]
        proc = None
        data = None
        status, message = "unknown", "Live check could not complete. Automatic checks will continue."
        try:
            async with _proc_semaphore:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, **_subprocess_kwargs())
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)
            try:
                data = json.loads(stdout) if stdout and proc.returncode == 0 else None
            except (ValueError, TypeError):
                data = None
            status, message = live_probe_status(url, data, proc.returncode, stderr)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Live probe failed: %s", type(exc).__name__)
        finally:
            if proc is not None and proc.returncode is None:
                _kill_proc(proc.pid, force=True)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except (asyncio.TimeoutError, OSError):
                    pass
        if status == "unknown" and allow_fallback and platform_key(url) == "tiktok":
            fallback = await _probe_tiktok_streamlink(url, proxy, cookie_path)
            if fallback:
                return fallback
        live = status == "live"
        return {"is_live": live, "viewer_count": viewer_count(data.get("concurrent_view_count")) if live else None,
                "observed_at": time.time(), "live_check_status": status, "live_check_message": message,
                "capture_engine": "yt-dlp" if live else None}


def _remember_capture_engine(ch: dict, info: dict):
    engine = info.get("capture_engine")
    if info.get("is_live") and engine in ("yt-dlp", "streamlink"):
        ch["capture_engine"] = engine
        ch["engine_checked_at"] = time.time()
    else:
        ch.pop("capture_engine", None)
        ch.pop("engine_checked_at", None)


def _set_channel_viewers(ch: dict, count, observed_at: float):
    ch["viewer_count"] = viewer_count(count)
    ch["viewer_checked_at"] = observed_at


async def _sample_recording_viewers(ch_id: str, rec_id: str):
    ch = channels.get(ch_id)
    rec = recordings.get(rec_id)
    if not ch or not rec or not rec.get("media_started_at"):
        return
    rec["_viewer_probe_at"] = time.monotonic()
    try:
        if HARDENED:
            approved_url(ch["url"])
        info = await probe_live(ch["url"], proxy=ch.get("proxy") or settings.get("proxy", ""), cookies_file=ch.get("cookies_file", ""), allow_fallback=False)
        # Never attribute an old request to a replacement recording, nor count
        # samples collected after the actual recording has ended.
        if channels.get(ch_id) is not ch or ch.get("recording_id") != rec_id or rec.get("status") != "recording":
            return
        _set_channel_viewers(ch, info["viewer_count"], info["observed_at"])
        if info["is_live"] and add_viewer_sample(rec, info["viewer_count"], info["observed_at"]):
            _save_state()
    except Exception as exc:
        logger.debug("Viewer sample unavailable for channel %s: %s", ch_id, type(exc).__name__)


async def _viewer_monitor():
    while True:
        await asyncio.sleep(5)
        tasks = []
        for cid, ch in list(channels.items()):
            rid = ch.get("recording_id")
            rec = recordings.get(rid, {})
            if rec.get("status") == "recording" and rec.get("media_started_at") and time.monotonic() - rec.get("_viewer_probe_at", -SAMPLE_INTERVAL) >= SAMPLE_INTERVAL:
                tasks.append(_sample_recording_viewers(cid, rid))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ── Recording ─────────────────────────────────────────────────────────────────

async def _start_recording_for_channel(ch_id: str) -> Optional[str]:
    # Per-channel lock prevents double-recording from concurrent calls
    lock = _get_recording_lock(ch_id)
    async with lock:
        ch = channels.get(ch_id)
        if not ch:
            return None
        existing = ch.get("recording_id")
        if existing and existing in recordings and recordings[existing]["status"] in ("recording", "starting"):
            return None
        rec_id = str(uuid.uuid4())[:8]
        recordings[rec_id] = {
            "id": rec_id, "channel_id": ch_id,
            "url": ch["url"], "platform": ch["platform"],
            "account_display_name": ch.get("display_name") or ch.get("username"),
            "account_username": ch.get("username"),
            "quality": ch.get("quality") or settings["default_quality"],
            "format":  ch.get("format")  or settings["default_format"],
            "status": "starting", "created_at": time.time(),
            "started_at": None, "ended_at": None,
            "bytes": 0, "speed": None,
            "filepath": None, "filename": None,
            "log": [], "stopping": False, "auto": False,
            "is_favorite": False,
            "viewer_samples": [],
        }
        channels[ch_id]["recording_id"] = rec_id
        asyncio.create_task(run_recording(rec_id))
        return rec_id


async def run_recording(rec_id: str):
    try:
        rec = recordings[rec_id]
        ch = channels.get(rec["channel_id"], {})
        with _cookie_snapshot(rec["url"], ch.get("cookies_file", "")) as cookie_path:
            await _run_recording(rec_id, cookie_path)
    except Exception as exc:
        rec = recordings.get(rec_id)
        if rec is None:
            return
        rec["status"] = "error"
        rec["ended_at"] = time.time()
        rec["error"] = str(exc)
        rec.setdefault("log", []).append(f"[StreamRec] Recording failed: {exc}")
        ch = channels.get(rec.get("channel_id"), {})
        if ch.get("recording_id") == rec_id:
            ch["recording_id"] = None
        _save_state()
        logger.exception("Recording %s failed before cleanup", rec_id)


async def _run_recording(rec_id: str, cookie_path: str = ""):
    rec = recordings[rec_id]
    ch  = channels.get(rec["channel_id"], {})
    quality      = rec.get("quality") or settings["default_quality"]
    fmt          = rec.get("format")  or settings["default_format"]
    url          = rec["url"]
    if HARDENED:
        url = approved_url(url)
    platform_raw = rec.get("platform") or "Unknown"
    platform     = platform_raw.lower()

    source_url = url
    capture_engine = "yt-dlp"
    if platform_key(url) == "tiktok":
        if ch.get("capture_engine") in ("yt-dlp", "streamlink") and time.time() - (ch.get("engine_checked_at") or 0) <= 75:
            capture_engine = ch["capture_engine"]
        else:
            result = await probe_live(url, proxy=ch.get("proxy") or settings.get("proxy", ""), cookies_file=ch.get("cookies_file", ""))
            _remember_capture_engine(ch, result)
            if rec.get("stopping") or channels.get(rec["channel_id"]) is not ch or ch.get("recording_id") != rec_id:
                rec["status"] = "completed"
                rec["ended_at"] = time.time()
                if ch.get("recording_id") == rec_id:
                    ch["recording_id"] = None
                _save_state()
                return
            if not result.get("is_live"):
                rec["status"] = "error"
                rec["ended_at"] = time.time()
                rec["live_check_status"] = result.get("live_check_status", "unknown")
                rec["error"] = result.get("live_check_message") or "No playable live stream found"
                rec["log"] = ["[StreamRec] " + rec["error"]]
                ch["recording_id"] = None
                ch["live_check_status"] = rec["live_check_status"]
                ch["live_check_message"] = result.get("live_check_message")
                ch["last_checked"] = time.time()
                _set_channel_viewers(ch, None, time.time())
                _save_state()
                return
            capture_engine = result.get("capture_engine") or "yt-dlp"
        # A successful yt-dlp metadata lookup does not guarantee its second
        # lookup at capture time will work. Prefer the independently available
        # Streamlink video path, while keeping yt-dlp for viewer sampling.
        if capture_engine == "yt-dlp":
            alternate = await _probe_tiktok_streamlink(url, ch.get("proxy") or settings.get("proxy", ""), cookie_path)
            if alternate:
                capture_engine = "streamlink"
                _remember_capture_engine(ch, alternate)
        if rec.get("stopping") or channels.get(rec["channel_id"]) is not ch or ch.get("recording_id") != rec_id:
            rec["status"] = "completed"
            rec["ended_at"] = time.time()
            if ch.get("recording_id") == rec_id:
                ch["recording_id"] = None
            _save_state()
            return
    rec["capture_engine"] = capture_engine
    if platform == "bigo":
        rec["capture_engine"] = "bigo"
        bigo_proxy = _effective_proxy(ch.get("proxy") or settings.get("proxy", ""))
        status, message = "unknown", None
        try:
            bigo_info = await _bigo_info(url, bigo_proxy, cookie_path)
            source_url = bigo_info.get("hls_src") or ""
            if not bigo_info.get("alive"):
                status, message = "offline", "BIGO reports the channel is currently offline."
            elif not source_url:
                raise BigoError("BIGO live room returned no HLS stream")
            else:
                status = "live"
        except Exception as e:
            message = _bigo_check_message(e)
            logger.warning("BIGO recording startup could not verify room %s: %s", rec["channel_id"], type(e).__name__)

        # The lookup can take several seconds. A stop, deletion or new recording
        # during that await must not be overwritten by this old startup task.
        if rec.get("stopping") or channels.get(rec["channel_id"]) is not ch or ch.get("recording_id") != rec_id:
            rec["status"] = "completed"
            rec["ended_at"] = time.time()
            if ch.get("recording_id") == rec_id:
                ch["recording_id"] = None
            _save_state()
            return
        ch["last_checked"] = time.time()
        ch["live_check_status"] = status
        ch["live_check_message"] = message if status == "unknown" else None
        if status != "unknown":
            ch["is_live"] = status == "live"
        if status != "live":
            rec["status"] = "error"
            rec["ended_at"] = time.time()
            rec["live_check_status"] = status
            rec["error"] = message
            rec["log"] = [f"[StreamRec] {message}"]
            ch["recording_id"] = None
            _set_channel_viewers(ch, None, time.time())
            _save_state()
            return
        if bigo_info.get("display_name"):
            ch["display_name"] = bigo_info["display_name"]
        if bigo_info.get("site_id"):
            ch["username"] = bigo_info["site_id"]
        if bigo_info.get("stream_title"):
            rec["stream_title"] = bigo_info["stream_title"]
            ch["stream_title"] = bigo_info["stream_title"]

    username     = ch.get("display_name") or ch.get("username") or rec_id
    safe_plat    = re.sub(r'[^\w\-]', '_', platform_raw)
    safe_user    = re.sub(r'[^\w\-]', '_', username)
    # Monotonic-safe timestamp: DST fall-back can repeat wall-clock times,
    # which would overwrite earlier recordings.
    now          = datetime.now(timezone.utc)
    date_str     = now.strftime("%Y-%m-%d")
    time_str     = now.strftime("%H-%M-%S")
    rec_dir      = RECORDINGS_DIR / safe_plat / safe_user / date_str
    rec_dir.mkdir(parents=True, exist_ok=True)
    stem         = f"{safe_user}_{date_str}_{time_str}" + (f"_{rec_id}" if capture_engine == "streamlink" else "")
    output_path  = rec_dir / f"{stem}.%(ext)s"

    _no_live_from_start = (
        "twitch", "tiktok", "kick", "stripchat", "bigo", "pandalive",
        "chaturbate", "cam4", "myfreecams", "camsoda", "bongacams",
        "cammodels", "streamate", "flirt4free",
    )
    _cam_platforms = (
        "tiktok", "kick", "stripchat", "bigo", "pandalive",
        "chaturbate", "cam4", "myfreecams", "camsoda", "bongacams",
        "cammodels", "streamate", "flirt4free",
    )

    # Cam/HLS sites use numeric format IDs — "best" keyword alone fails.
    # Build a fallback chain that works for both cam sites and regular streams.
    _height_re = re.match(r'^(\d+)p?$', quality)
    if quality in ("best", ""):
        if platform in _cam_platforms:
            effective_quality = "bestvideo+bestaudio/best"
        else:
            effective_quality = "bestvideo+bestaudio/bestvideo/best"
    elif quality == "bestvideo+bestaudio":
        effective_quality = "bestvideo+bestaudio/best"
    elif _height_re and platform in _cam_platforms:
        # e.g. "720p" or "720" on cam sites — use height filter with fallback
        h = _height_re.group(1)
        effective_quality = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/bestvideo+bestaudio/best"
    else:
        # User specified explicit quality (1080p, 720p, etc.) for non-cam platforms
        effective_quality = quality

    cmd = ["yt-dlp", "--no-part", "--ignore-config", "--no-plugin-dirs"]
    if platform not in _no_live_from_start:
        cmd += ["--live-from-start", "--hls-use-mpegts"]

    # Limit ffmpeg thread count — critical for Pi / low-power CPUs
    ffmpeg_threads = get_ffmpeg_threads()
    cmd += [
        "--retries", "infinite", "--fragment-retries", "infinite",
        "--retry-sleep", "5", "--socket-timeout", "30",
        "--no-warnings", "--newline",
        "--concurrent-fragments", "1",
        # force-fixup is MP4-specific; for mkv/ts it warns and can abort on
        # recoverable errors.  "warn" is the safe mode for other containers.
        "--fixup", "force" if fmt == "mp4" else "warn",
        # -stats + -loglevel info override yt-dlp's default ffmpeg silence so
        # live HLS recordings produce visible progress in the log viewer.
        "--downloader-args",
        f"ffmpeg:-threads {ffmpeg_threads} -fflags +genpts+discardcorrupt -stats -loglevel info",
        "-f", effective_quality,
        "--merge-output-format", fmt,
        # NOTE: no --print here — it implies --quiet and silences the entire
        # log for live recordings.  The final filepath is resolved via the
        # output glob after the process exits.
        "--progress",
    ]
    # On Pi / low-memory systems, cap the download buffer to reduce RAM usage
    if _is_pi():
        cmd += ["--buffer-size", "32K"]
    # Chaturbate (and similar HLS cam sites) have audio segments that start
    # exactly 1 second ahead of video — delay audio by 1s to compensate.
    if platform in _cam_platforms:
        cmd += ["--postprocessor-args", "ffmpeg:-c:v copy -c:a aac -af adelay=1000|1000"]

    # Proxy (channel > global)
    proxy = _effective_proxy(ch.get("proxy") or settings.get("proxy", ""))
    if proxy:
        cmd += ["--proxy", proxy]

    # Only a file uploaded into the dedicated cookie directory is accepted.
    if cookie_path:
        cmd += ["--cookies", cookie_path]

    # Username / password for age-restricted sites
    ch_user = ch.get("ch_username", "")
    ch_pass = ch.get("ch_password", "")
    if ch_user:
        cmd += ["--username", ch_user]
    if ch_pass:
        cmd += ["--password", ch_pass]

    # Extra yt-dlp args (channel > global) — hardened against injection
    extra = ch.get("extra_args") or settings.get("extra_args", "")
    if extra and HARDENED:
        raise ValueError("Custom downloader arguments are disabled in hardened mode")
    if extra:
        try:
            parts = shlex.split(extra, posix=not IS_WINDOWS)
            # Block dangerous yt-dlp flags that could execute arbitrary commands or manipulate filesystem
            _DANGEROUS_FLAGS = {
                "--exec", "-X", "--exec-before-download", "--config-location", "--config-locations",
                "--print", "--print-to-file", "--output-na-placeholder",
                "--downloader", "--external-downloader", "--downloader-args", "--external-downloader-args",
                "--postprocessor-args", "--ppa", "--exec-after-download",
                "--proxy", "--cookies", "--cookies-from-browser",
                "--ffmpeg-location", "--prefer-ffmpeg", "-P", "--paths",
                "-a", "--batch-file", "--load-info-json", "--plugin-dirs",
                "-o", "--output", "--alias",
            }
            for part in parts:
                flag = part.split("=")[0].strip() if "=" in part else part.strip()
                flag_lower = flag.lower()
                if (
                    flag_lower in _DANGEROUS_FLAGS
                    or flag_lower.startswith("--exec")
                    or flag_lower.startswith("--config")
                    or flag_lower.startswith("--paths")
                    or flag_lower.startswith("-P")
                ):
                    logger.warning("Blocked dangerous extra_args flag: %s", flag)
                    raise ValueError(f"Blocked dangerous yt-dlp flag: {flag}")
            cmd += parts
        except ValueError:
            raise  # re-raise our own validation errors
        except Exception as e:
            logger.warning("Failed to parse extra_args %r: %s", extra, e)

    if platform == "bigo":
        bigo_output = rec_dir / f"{stem}.ts"
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("bigo_recorder.py")),
            "--url", source_url,
            "--output", str(bigo_output),
        ]
        if proxy:
            cmd += ["--proxy", proxy]
        if cookie_path:
            cmd += ["--cookies-file", cookie_path]
    elif capture_engine == "streamlink":
        capture_path = rec_dir / f"{stem}.flv"
        if capture_path.exists():
            raise FileExistsError("Capture destination already exists")
        cmd = [*_streamlink_options(proxy, cookie_path), "--output", str(capture_path),
               "--", source_url, _streamlink_quality(quality)]
    else:
        cmd += ["-o", str(output_path), "--", source_url]

    rec["status"]     = "recording"
    rec["started_at"] = time.time()
    rec["log"]        = ["[StreamRec] Using Streamlink TikTok fallback; selecting a browser-compatible video rendition when available."] if capture_engine == "streamlink" else []
    size_task         = None
    proc              = None
    duration_task     = None
    file_captured     = False  # pre-init so `finally` can read it even if we fail early
    logger.info("Recording started: %s for %s (%s)", rec_id, username, url)

    # If a stop/kill was requested before the task got scheduled, bail out now.
    if rec.get("stopping"):
        rec["status"]   = "completed"
        rec["ended_at"] = time.time()
        if (ch_id := rec.get("channel_id")) and ch_id in channels:
            channels[ch_id]["recording_id"] = None
        _save_state()
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_subprocess_kwargs(),
        )
        rec["pid"] = proc.pid
        # Race: stop was requested between starting and pid assignment — kill now.
        if rec.get("stopping"):
            _kill_proc(proc.pid, force=False)

        async def _poll_size():
            last_bytes = 0
            last_time = time.time()
            last_change = time.time()
            while True:
                await asyncio.sleep(get_size_poll_interval())
                for f in rec_dir.glob(f"{stem}.*"):
                    try:
                        sz = f.stat().st_size
                        if sz > 0:
                            now = time.time()
                            delta_bytes = sz - last_bytes
                            delta_time = now - last_time
                            if delta_time > 0 and last_bytes > 0 and delta_bytes >= 0:
                                bps = delta_bytes / delta_time
                                if bps >= 1024**2:
                                    rec["speed"] = f"{bps/1024**2:.1f}MiB/s"
                                elif bps >= 1024:
                                    rec["speed"] = f"{bps/1024:.1f}KiB/s"
                                else:
                                    rec["speed"] = f"{bps:.0f}B/s"
                            if sz != last_bytes:
                                last_change = now
                            last_bytes = sz
                            last_time = now
                            rec["bytes"] = sz
                            if delta_bytes > 0 or not rec.get("last_media_at"):
                                rec["last_media_at"] = now
                            if f.suffix.lower() in (".mp4", ".mkv", ".ts", ".flv", ".webm", ".m4a", ".mp3"):
                                rec["capture_path"] = str(f)
                            if not rec.get("media_started_at"):
                                rec["media_started_at"] = now
                                _save_state()
                    except Exception:
                        pass
                    break
                # Stalled detection: no data written for 120s (240s in Pi mode)
                stall_timeout = 240 if _is_pi() else 120
                if time.time() - last_change > stall_timeout:
                    rec["log"].append(
                        f"[StreamRec] WARNING — no data received for {stall_timeout}s. "
                        "Stream may have ended silently."
                    )
                    if settings.get("auto_stop_stalled", False) and not rec.get("stopping"):
                        rec["log"].append(
                            "[StreamRec] Auto-stop-stalled enabled — stopping recording."
                        )
                        _stop_rec(rec, force=False)

        size_task = asyncio.create_task(_poll_size())

        # Max duration enforcement
        max_dur_mins = int(ch.get("max_duration") or settings.get("max_duration", 0))
        if max_dur_mins > 0:
            async def _enforce_duration():
                await asyncio.sleep(max_dur_mins * 60)
                if rec.get("status") == "recording" and not rec.get("stopping"):
                    rec["log"].append(
                        f"[StreamRec] Max recording duration ({max_dur_mins} min) reached — stopping gracefully"
                    )
                    _stop_rec(rec, force=False)
            duration_task = asyncio.create_task(_enforce_duration())

        rec_dir_str = str(RECORDINGS_DIR)
        async for raw_line in proc.stdout:
            # ffmpeg uses \r to overwrite progress; split on both \r and \n
            for line in raw_line.decode("utf-8", errors="replace").replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line:
                    continue
                rec["log"].append(line)
                log_max, log_trim = get_log_limits()
                if len(rec["log"]) > log_max:
                    rec["log"] = rec["log"][-log_trim:]
                # ffmpeg progress line (HLS via ffmpeg downloader):
                # "frame= 49 fps=0.0 size=  256KiB time=00:00:01 bitrate=1279.8kbits/s speed= 3.2x"
                if "frame=" in line and "bitrate=" in line:
                    m_size = re.search(r"size=\s*(\d+\.?\d*)\s*(GiB|MiB|KiB|B)\b", line)
                    if m_size:
                        v = float(m_size.group(1))
                        u = {"GiB": 1024**3, "MiB": 1024**2, "KiB": 1024, "B": 1}[m_size.group(2)]
                        rec["bytes"] = int(v * u)
                    m_br = re.search(r"bitrate=\s*(\d+\.?\d*\s*\w+bits/s)", line)
                    if m_br:
                        rec["speed"] = m_br.group(1)
                # yt-dlp download progress line (HTTP downloads)
                elif "[download]" in line or "[hlsnative]" in line:
                    m = re.search(r"(\d+\.?\d*)\s*(GiB|MiB|KiB|B)\b", line)
                    if m:
                        v = float(m.group(1))
                        u = {"GiB": 1024**3, "MiB": 1024**2, "KiB": 1024, "B": 1}[m.group(2)]
                        rec["bytes"] = int(v * u)
                    m2 = re.search(r"(\d+\.?\d*\s*(?:GiB|MiB|KiB|B)/s)", line)
                    if m2:
                        rec["speed"] = m2.group(1)
                # Detect final filepath printed by --print after_move:filepath
                if (not line.startswith("[") and not line.startswith("ERROR")
                        and len(line) > 4 and rec_dir_str in line):
                    p = Path(line)
                    if p.suffix:
                        rec["filepath"] = line
                        rec["filename"] = p.name

        await proc.wait()
        rc = proc.returncode
        # Resolve filepath now (before status check) in case --print didn't fire
        if not rec.get("filepath"):
            for f in rec_dir.glob(f"{stem}.*"):
                rec["filepath"] = str(f)
                rec["filename"] = f.name
                break
        # Treat as completed if manually stopped OR if a file with real content was captured
        # (yt-dlp exits non-zero when broadcaster goes offline, even after a full capture)
        file_captured = bool(rec.get("filepath") and Path(rec["filepath"]).exists()
                             and Path(rec["filepath"]).stat().st_size > 0)
        # yt-dlp can exit non-zero after a successful capture when a live stream ends.
        # The native BIGO helper is different: non-zero means the HLS reader hit an
        # actual error, so keep any partial file but mark the session failed so
        # StreamRec's existing retry policy can recover it.
        if platform == "bigo" and rc != 0 and not rec.get("stopping"):
            rec["status"] = "error"
        else:
            rec["status"] = "completed" if (rc == 0 or rec.get("stopping") or file_captured) else "error"
        if rc != 0 and not rec.get("stopping") and rec["status"] == "error":
            rec["error"] = f"Exit code {rc}"
            if not file_captured and platform_key(url) == "tiktok":
                status, message = live_probe_status(url, None, rc, "\n".join(rec.get("log", [])[-15:]))
                rec["live_check_status"] = status
                rec["error"] = message or rec["error"]
                if message:
                    rec["log"].append("[StreamRec] " + message)
            logger.warning("Recording %s failed with exit code %d", rec_id, rc)
        else:
            logger.info("Recording %s completed", rec_id)

    except asyncio.CancelledError:
        rec["status"]   = "completed" if rec.get("stopping") else "error"
        if not rec.get("stopping"):
            rec["error"] = "Cancelled"
        raise
    except Exception as e:
        rec["status"] = "error"
        rec["error"]  = str(e)
        logger.error("Recording %s exception: %s", rec_id, e)

    finally:
        if size_task:
            size_task.cancel()
        if duration_task:
            duration_task.cancel()
        # Make sure we never leave an orphaned recorder subprocess running
        if proc is not None and proc.returncode is None:
            try:
                _kill_proc(proc.pid, force=False)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    _kill_proc(proc.pid, force=True)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                pass
        rec["ended_at"] = time.time()
        rec.pop("pid", None)

        # Fall back to file glob if the recorder didn't report the path
        if not rec.get("filepath"):
            for f in rec_dir.glob(f"{stem}.*"):
                rec["filepath"] = str(f)
                rec["filename"] = f.name
                break

        if fp := rec.get("filepath"):
            try:
                rec["bytes"] = Path(fp).stat().st_size
            except Exception:
                pass

        # Native BIGO and Streamlink capture to transport/FLV containers.
        # Remux locally to the requested container, retaining the original on failure.
        fp = rec.get("filepath", "")
        target_fmt = fmt.lower() if fmt.lower() in ("mp4", "mkv", "ts") else "ts"
        if (
            (platform == "bigo" or capture_engine == "streamlink")
            and rec.get("status") == "completed"
            and fp
            and Path(fp).exists()
            and Path(fp).suffix.lower() != "." + target_fmt
        ):
            target_path = str(Path(fp).with_suffix(f".{target_fmt}"))
            try:
                async with _proc_semaphore:
                    remux_cmd = ["ffmpeg", "-protocol_whitelist", "file", "-i", fp, "-c", "copy"]
                    if target_fmt == "mp4":
                        remux_cmd += ["-movflags", "+faststart"]
                    remux_cmd += ["-threads", ffmpeg_threads, target_path, "-y"]
                    remux_proc = await asyncio.create_subprocess_exec(
                        *remux_cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(remux_proc.wait(), timeout=REMUX_TIMEOUT)
                if remux_proc.returncode == 0 and Path(target_path).exists():
                    (_media_path(fp) if HARDENED else Path(fp)).unlink(missing_ok=True)
                    rec["filepath"] = target_path
                    rec["filename"] = Path(target_path).name
                    rec["bytes"] = Path(target_path).stat().st_size
                else:
                    Path(target_path).unlink(missing_ok=True)
            except Exception:
                try:
                    Path(target_path).unlink(missing_ok=True)
                except Exception:
                    pass

        # Remux to fix containers and move moov atom to file start for
        # smooth playback (faststart).  Run on ALL completed recordings —
        # not just manual stops — so files play without lag on any device.
        fp = rec.get("filepath", "")
        if platform != "bigo" and capture_engine != "streamlink" and fp and Path(fp).exists():
            suffix    = Path(fp).suffix
            fixed_path = str(Path(fp).with_name(Path(fp).stem + "_fixed" + suffix))
            try:
                async with _proc_semaphore:
                    fix_proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-i", fp, "-c", "copy", "-movflags", "+faststart",
                        "-threads", ffmpeg_threads, fixed_path, "-y",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(fix_proc.wait(), timeout=REMUX_TIMEOUT)
                if fix_proc.returncode == 0 and Path(fixed_path).exists():
                    if _safe_replace_file(Path(fixed_path), Path(fp)):
                        try:
                            rec["bytes"] = Path(fp).stat().st_size
                        except Exception:
                            pass
                    else:
                        Path(fixed_path).unlink(missing_ok=True)
                else:
                    Path(fixed_path).unlink(missing_ok=True)
            except Exception:
                try:
                    Path(fixed_path).unlink(missing_ok=True)
                except Exception:
                    pass

        # Optional MP4 conversion
        auto_convert = ch.get("auto_convert_mp4", settings["auto_convert_mp4"])
        delete_orig  = ch.get("delete_original",  settings["delete_original"])
        fp = rec.get("filepath", "")
        if auto_convert and fp and not fp.endswith(".mp4") and Path(fp).exists():
            mp4_path = str(Path(fp).with_suffix(".mp4"))
            try:
                async with _proc_semaphore:
                    conv = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-i", fp, "-c", "copy",
                        "-movflags", "+faststart",
                        "-threads", ffmpeg_threads,
                        mp4_path, "-y",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(conv.wait(), timeout=REMUX_TIMEOUT)
                if conv.returncode == 0:
                    if delete_orig:
                        (_media_path(fp) if HARDENED else Path(fp)).unlink(missing_ok=True)
                    rec["filepath"] = mp4_path
                    rec["filename"] = Path(mp4_path).name
                    try:
                        rec["bytes"] = Path(mp4_path).stat().st_size
                    except Exception:
                        pass
            except Exception:
                pass

        await _measure_recording_duration(rec)

        # Only clear the channel's recording pointer if it still points at US —
        # a subsequent Record click could have already started a new rec_id.
        if ch_id := rec.get("channel_id"):
            ch_entry = channels.get(ch_id)
            if ch_entry and ch_entry.get("recording_id") == rec_id:
                ch_entry["recording_id"] = None
                ch_entry["is_live"]      = False
                ch_entry["viewer_count"] = None
                ch_entry.pop("capture_engine", None)
                ch_entry.pop("engine_checked_at", None)
                if rec.get("live_check_status") == "unknown":
                    ch_entry["live_check_status"] = "unknown"
                    ch_entry["live_check_message"] = rec.get("error")
                else:
                    ch_entry.pop("live_check_status", None)
                    ch_entry.pop("live_check_message", None)

        _save_state()

        if ch_id := rec.get("channel_id"):
            _prune_channel_recordings(ch_id)

        # Webhook notification on recording completion
        if rec.get("status") in ("completed", "error"):
            ch_data = channels.get(rec.get("channel_id", ""), {})
            asyncio.create_task(_send_webhook("recording_complete", {
                "recording_id": rec.get("id"),
                "channel_id": rec.get("channel_id"),
                "name": ch_data.get("display_name") or ch_data.get("username", ""),
                "stream_title": ch_data.get("stream_title", ""),
                "status": rec.get("status"),
                "filename": rec.get("filename", ""),
                "bytes": rec.get("bytes", 0),
                "error": rec.get("error", ""),
                "platform": rec.get("platform", ""),
                "avatar": ch_data.get("avatar", ""),
                "thumbnail": ch_data.get("thumbnail", ""),
                "duration_seconds": int((rec.get("ended_at") or time.time()) - (rec.get("started_at") or time.time())),
            }))

        # Auto-retry on unexpected disconnect
        retry_ch_id = rec.get("channel_id")
        # Don't retry if stream ran for more than 30s — that's a natural end, not a crash
        run_duration = (rec.get("ended_at") or time.time()) - (rec.get("started_at") or time.time())
        natural_end  = (
            run_duration > 30
            and file_captured
            and not (platform == "bigo" and rec.get("status") == "error")
        )
        should_retry = (
            not rec.get("stopping")
            and not natural_end
            and rec.get("status") == "error"
            and rec.get("live_check_status") != "unknown"
            and not any("not currently live" in line.lower() or "not live" in line.lower() for line in rec.get("log", [])[-15:])
            and settings.get("auto_retry", True)
            and retry_ch_id
            and retry_ch_id in channels
            and channels[retry_ch_id].get("monitoring", True)
        )
        if should_retry:
            attempt    = rec.get("_retry_attempt", 0) + 1
            max_retries = settings.get("max_retries", 5)
            delay      = settings.get("retry_delay", 15)
            if attempt <= max_retries:
                rec["log"].append(
                    f"[StreamRec] Stream disconnected. Retrying in {delay}s "
                    f"(attempt {attempt}/{max_retries})…"
                )
                await asyncio.sleep(delay)
                if retry_ch_id in channels and not channels[retry_ch_id].get("recording_id"):
                    new_id = await _start_recording_for_channel(retry_ch_id)
                    if new_id:
                        recordings[new_id]["_retry_attempt"] = attempt
                        recordings[new_id]["auto"] = True


# ── Monitor loop ──────────────────────────────────────────────────────────────

async def _send_webhook(event: str, payload: dict):
    """Send a JSON POST to the configured webhook URL (with native Discord embeds if Discord)."""
    url = settings.get("webhook_url", "").strip()
    if HARDENED or not url:
        return
    # SSRF guard: only http(s), never internal hosts (webhook URL is set by
    # the admin, but validate anyway since it auto-fires on every recording).
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.warning("Webhook skipped — invalid URL scheme: %r", url[:60])
        return
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254"):
        logger.warning("Webhook skipped — internal host: %r", hostname)
        return
    try:
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            # Build clean Discord embed
            color_map = {
                "stream_live": 0x34D399,          # Green
                "recording_complete": 0x818CF8,   # Indigo/Purple
            }
            title_map = {
                "stream_live": f"🔴 Stream Live: {payload.get('name') or 'Channel'}",
                "recording_complete": f"📼 Recording Finished: {payload.get('name') or 'Channel'}",
            }
            embed = {
                "title": title_map.get(event, f"StreamRec: {event}"),
                "color": color_map.get(event, 0x818CF8),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if payload.get("url"):
                embed["url"] = payload["url"]
            if payload.get("stream_title"):
                embed["description"] = payload["stream_title"]
            fields = []
            if payload.get("platform"):
                fields.append({"name": "Platform", "value": payload["platform"], "inline": True})
            if payload.get("status"):
                fields.append({"name": "Status", "value": payload["status"].capitalize(), "inline": True})
            if payload.get("duration_seconds"):
                mins = payload["duration_seconds"] // 60
                secs = payload["duration_seconds"] % 60
                fields.append({"name": "Duration", "value": f"{mins}m {secs}s", "inline": True})
            if payload.get("bytes"):
                b = payload["bytes"]
                sz = f"{b/(1024**3):.2f} GiB" if b >= 1024**3 else f"{b/(1024**2):.1f} MiB"
                fields.append({"name": "Size", "value": sz, "inline": True})
            if fields:
                embed["fields"] = fields
            if payload.get("thumbnail"):
                embed["image"] = {"url": payload["thumbnail"]}
            elif payload.get("avatar"):
                embed["thumbnail"] = {"url": payload["avatar"]}
            post_data = {"embeds": [embed]}
        else:
            post_data = {"event": event, **payload}

        async with _proc_semaphore:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
                "-d", json.dumps(post_data, default=str),
                "--max-time", "10", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=12)
    except Exception:
        pass


def _prune_channel_recordings(ch_id: str):
    """Enforce max_streams per channel: keep the N newest recordings (never deleting favorites)."""
    ch = channels.get(ch_id)
    if not ch:
        return
    max_s = int(ch.get("max_streams", 0) or 0)
    if max_s <= 0:
        return
    # Find all completed/error/archived recordings for this channel, newest first
    ch_recs = [
        (rid, r) for rid, r in recordings.items()
        if r.get("channel_id") == ch_id and r.get("status") in ("completed", "error", "archived")
    ]
    ch_recs.sort(key=lambda x: x[1].get("created_at", 0), reverse=True)
    
    kept = 0
    changed = False
    for rid, r in ch_recs:
        if r.get("is_favorite"):
            continue  # Favorites are shielded from max_streams pruning
        kept += 1
        if kept > max_s:
            if fp := r.get("filepath"):
                try:
                    (_media_path(fp) if HARDENED else Path(fp)).unlink(missing_ok=True)
                except Exception:
                    pass
            recordings.pop(rid, None)
            changed = True
    if changed:
        _save_state()


async def _run_retention_cleanup():
    """Delete recordings older than retention_days (shielding favorites)."""
    days = int(settings.get("retention_days", 0))
    if days <= 0:
        return
    cutoff = time.time() - (days * 86400)
    for rec_id, rec in list(recordings.items()):
        if rec.get("status") not in ("completed", "error"):
            continue
        if rec.get("is_favorite"):
            continue  # Favorites are shielded from retention cleanup
        if rec.get("created_at", time.time()) > cutoff:
            continue
        if fp := rec.get("filepath"):
            try:
                (_media_path(fp) if HARDENED else Path(fp)).unlink(missing_ok=True)
            except Exception:
                pass
        recordings.pop(rec_id, None)
    _save_state()

async def _check_one_channel(ch_id: str) -> None:
    """Check a single channel's live state and auto-record if needed.

    Runs per-channel error handling so one bad URL can't take down the tick.
    """
    ch = channels.get(ch_id)
    if not ch:
        return
    # Global "auto-record when live" (record_on_add) forces monitoring even
    # when the per-channel toggle is paused.
    if not ch.get("monitoring", True) and not settings.get("record_on_add", False):
        return
    existing = ch.get("recording_id")
    if existing and existing in recordings:
        r = recordings[existing]
        if r.get("status") in ("recording", "starting"):
            # Still recording — no need to re-check, just mark live
            if ch_id in channels:
                channels[ch_id]["is_live"]      = True
                channels[ch_id]["last_checked"] = time.time()
            return

    proxy = ch.get("proxy") or settings.get("proxy", "")
    try:
        if HARDENED:
            approved_url(ch["url"])
        info = await probe_live(ch["url"], proxy=proxy, cookies_file=ch.get("cookies_file", ""))
        is_live = info["is_live"]
    except Exception as e:
        logger.warning("Live check failed for %s: %s", ch_id, type(e).__name__)
        if channels.get(ch_id) is ch and not ch.get("recording_id"):
            ch["live_check_status"] = "unknown"
            ch["live_check_message"] = _bigo_check_message(e) if is_bigo_url(ch["url"]) else "Live check could not complete. Automatic checks will continue."
            ch["last_checked"] = time.time()
            _set_channel_viewers(ch, None, time.time())
        return

    # Channel could have been deleted while we were awaiting
    if channels.get(ch_id) is not ch or ch.get("recording_id"):
        return
    check_status = info.get("live_check_status", "live" if is_live else "offline")
    ch["live_check_status"] = check_status
    ch["live_check_message"] = info.get("live_check_message")
    _remember_capture_engine(ch, info)
    if info.get("stream_title"):
        ch["stream_title"] = info["stream_title"]
    if check_status != "unknown":
        ch["is_live"] = is_live
    ch["last_checked"] = time.time()
    _set_channel_viewers(channels[ch_id], info["viewer_count"], info["observed_at"])
    if is_live and not channels[ch_id].get("recording_id"):
        logger.info("Channel %s is live — starting auto-record", ch_id)
        try:
            rec_id = await _start_recording_for_channel(ch_id)
        except Exception as e:
            logger.exception("Failed to start auto-record for %s: %s", ch_id, e)
            return
        if rec_id and rec_id in recordings:
            recordings[rec_id]["auto"] = True
            asyncio.create_task(_send_webhook("stream_live", {
                "channel_id": ch_id,
                "name": channels[ch_id].get("display_name", ""),
                "url": channels[ch_id].get("url", ""),
                "platform": channels[ch_id].get("platform", ""),
                "recording_id": rec_id,
            }))


async def monitor_loop():
    _last_retention_check = 0
    while True:
        interval = max(5, int(settings.get("monitor_interval", 60)))
        await asyncio.sleep(interval)
        auto_record = settings.get("record_on_add", False)
        ids = [cid for cid, c in channels.items() if c.get("monitoring", True) or auto_record]
        if not ids:
            continue
        logger.debug("Monitor tick — checking %d channels", len(ids))
        # Run per-channel checks concurrently; gather never raises because
        # each coroutine traps its own exceptions.
        await asyncio.gather(
            *(_check_one_channel(cid) for cid in ids),
            return_exceptions=True,
        )
        # Retention cleanup runs once per hour
        now = time.time()
        if now - _last_retention_check > 3600:
            _last_retention_check = now
            await _run_retention_cleanup()


# ── Request models ────────────────────────────────────────────────────────────

class AddChannelRequest(BaseModel):
    url:              str = ""
    platform:         str = ""
    handle:           str = ""
    quality:          str  = ""
    format:           str  = ""
    monitoring:       bool = True
    auto_convert_mp4: bool = False
    delete_original:  bool = False
    record_now:       bool = False
    proxy:            str  = ""
    cookies_file:     str  = ""
    username:         str  = ""
    password:         str  = ""
    extra_args:       str  = ""
    max_duration:     int  = 0
    max_streams:      int  = 0
    notes:            str  = ""

class UpdateChannelRequest(BaseModel):
    monitoring:       Optional[bool] = None
    quality:          Optional[str]  = None
    format:           Optional[str]  = None
    auto_convert_mp4: Optional[bool] = None
    delete_original:  Optional[bool] = None
    proxy:            Optional[str]  = None
    cookies_file:     Optional[str]  = None
    username:         Optional[str]  = None
    password:         Optional[str]  = None
    extra_args:       Optional[str]  = None
    max_duration:     Optional[int]  = None
    max_streams:      Optional[int]  = None
    notes:            Optional[str]  = None

class CutRecordingRequest(BaseModel):
    start_time:       float  # in seconds
    end_time:         float  # in seconds
    title:            Optional[str] = None

class UpdateSettingsRequest(BaseModel):
    pi_mode:          Optional[bool] = None
    monitor_interval: Optional[int]  = None
    default_quality:  Optional[str]  = None
    default_format:   Optional[str]  = None
    auto_convert_mp4: Optional[bool] = None
    delete_original:  Optional[bool] = None
    record_on_add:    Optional[bool] = None
    auto_retry:       Optional[bool] = None
    max_retries:      Optional[int]  = None
    retry_delay:      Optional[int]  = None
    proxy:            Optional[str]  = None
    cookies_file:     Optional[str]  = None
    extra_args:       Optional[str]  = None
    retention_days:   Optional[int]  = None
    max_duration:     Optional[int]  = None
    webhook_url:      Optional[str]  = None
    auto_stop_stalled: Optional[bool] = None

class ReorderRequest(BaseModel):
    order: list[str]

class BulkActionRequest(BaseModel):
    ids:    list[str]
    action: str  # "record" | "stop" | "delete"

class BulkEditRequest(BaseModel):
    ids:     list[str]
    quality: Optional[str]  = None
    format:  Optional[str]  = None

class ImportRequest(BaseModel):
    channels: dict = {}
    settings: dict = {}
    merge:    bool = True


# ── Helper: stop a recording by rec dict ─────────────────────────────────────

def _stop_rec(rec: dict, force: bool = False):
    rec["stopping"] = True
    if pid := rec.get("pid"):
        _kill_proc(pid, force=force)


# ── Channel endpoints ─────────────────────────────────────────────────────────

@app.post("/api/channels")
async def add_channel(req: AddChannelRequest):
    if req.platform or req.handle:
        if req.url:
            raise HTTPException(400, "Use a platform username or a URL, not both")
        try:
            req.url = channel_url(req.platform, req.handle)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    req.url = _approved_channel_url(req.url)
    _validate_hardened_options(req.model_dump())
    # Validate URL — must be http/https to prevent SSRF
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Invalid URL — must start with http:// or https://")
    # Block private/internal IPs to prevent SSRF
    _BLOCKED_HOSTS = (
        "localhost", "127.0.0.1", "::1", "0.0.0.0",
        "169.254.169.254",  # AWS metadata
    )
    hostname = parsed.hostname or ""
    if not hostname:
        raise HTTPException(400, "Invalid URL — missing host")
    if hostname in _BLOCKED_HOSTS:
        raise HTTPException(400, "URL rejected: private/internal host")
    
    # Resolve domain names to verify target IPs are not internal / private.
    # getaddrinfo is blocking — run it in a worker thread so the event loop
    # doesn't stall on slow/unresponsive DNS servers.
    if not HARDENED:
        try:
            resolved_addrs = await asyncio.get_running_loop().getaddrinfo(hostname, None)
            for family, _, _, _, sockaddr in resolved_addrs:
                ip_str = sockaddr[0]
                addr = ipaddress.ip_address(ip_str)
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
                    raise HTTPException(400, f"URL rejected: host resolves to private/internal IP ({ip_str})")
        except socket.gaierror:
            # DNS resolution failure will be rejected early
            raise HTTPException(400, "URL rejected: unable to resolve host")
    ch_id    = str(uuid.uuid4())[:8]
    platform = detect_platform(req.url)
    ch = {
        "id": ch_id, "url": req.url, "platform": platform,
        "quality": req.quality, "format": req.format,
        "monitoring":       req.monitoring,
        "auto_convert_mp4": req.auto_convert_mp4,
        "delete_original":  req.delete_original,
        "proxy":            req.proxy,
        "cookies_file":     req.cookies_file,
        "ch_username":      req.username,
        "ch_password":      req.password,
        "extra_args":       req.extra_args,
        "max_duration":     req.max_duration,
        "max_streams":      req.max_streams,
        "notes":            req.notes,
        "created_at":  time.time(),
        "display_name": "", "username": "", "avatar": "", "thumbnail": "",
        "stream_title": "",
        "is_live": False, "last_checked": None, "recording_id": None,
    }
    channels[ch_id] = ch

    async def _fetch_and_record():
        meta = await fetch_metadata(req.url, proxy=req.proxy, cookies_file=req.cookies_file)
        if meta and ch_id in channels:
            channels[ch_id].update({
                "display_name": meta.get("display_name", ""),
                "username":     meta.get("username", ""),
                "avatar":       meta.get("avatar", ""),
                "thumbnail":    meta.get("thumbnail", ""),
                "stream_title": meta.get("stream_title", ""),
                "is_live":      meta.get("is_live", False),
                "live_check_status": meta.get("live_check_status", "unknown" if meta.get("_lookup_error") else "live" if meta.get("is_live") else "offline"),
                "live_check_message": meta.get("live_check_message"),
                "viewer_count": meta.get("viewer_count"),
                "viewer_checked_at": time.time(),
                "last_checked": time.time(),
            })
            _remember_capture_engine(channels[ch_id], meta)
            _save_state()
            if meta.get("is_live") and channels[ch_id].get("monitoring", True) and not channels[ch_id].get("recording_id"):
                rid = await _start_recording_for_channel(ch_id)
                if rid and rid in recordings:
                    recordings[rid]["auto"] = True

    asyncio.create_task(_fetch_and_record())
    if req.record_now:
        asyncio.create_task(_start_recording_for_channel(ch_id))
    _save_state()
    logger.info("Channel added: %s (%s) [%s]", ch_id, platform, req.url)
    return {"id": ch_id, "platform": platform}


@app.get("/api/channels")
async def list_channels():
    result = []
    for ch in channels.values():
        c = dict(ch)
        c.pop("ch_password", None)
        if not c.get("is_live") or time.time() - (c.get("viewer_checked_at") or 0) > SAMPLE_MAX_AGE:
            c["viewer_count"] = None
        rec_id = c.get("recording_id")
        if rec_id and rec_id in recordings:
            r = recordings[rec_id]
            c["rec_status"]  = r["status"]
            c["rec_engine"] = r.get("capture_engine")
            c["rec_bytes"]   = r.get("bytes", 0)
            c["rec_speed"]   = r.get("speed")
            c["rec_started"] = r.get("started_at")
            c["rec_id"]      = rec_id
            c["viewer_peak"] = viewer_summary(r.get("viewer_samples", []))["peak_viewers"]
        else:
            c["rec_status"] = None; c["rec_bytes"] = 0
            c["rec_speed"]  = None; c["rec_started"] = None; c["rec_id"] = None
            # Expose the most recent finished session so the UI log viewer
            # can show its log after a recording ends.
            last = max(
                (r for r in recordings.values()
                 if r.get("channel_id") == ch.get("id")
                 and r.get("status") in ("completed", "error")),
                key=lambda r: r.get("ended_at") or r.get("created_at", 0),
                default=None,
            )
            if last:
                c["last_rec_id"] = last["id"]
        result.append(c)
    return sorted(result, key=lambda x: (x.get("sort_order", 9999), -x["created_at"]))


@app.patch("/api/channels/{ch_id}")
async def update_channel(ch_id: str, req: UpdateChannelRequest):
    if ch_id not in channels:
        raise HTTPException(404, "Not found")
    _validate_hardened_options(req.model_dump(exclude_none=True))
    for field, val in req.model_dump(exclude_none=True).items():
        # username/password map to ch_username/ch_password to avoid clash with metadata field
        if field == "username":
            channels[ch_id]["ch_username"] = val
        elif field == "password":
            channels[ch_id]["ch_password"] = val
        else:
            channels[ch_id][field] = val
    _save_state()
    return channels[ch_id]


@app.post("/api/channels/{ch_id}/record")
async def record_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    existing = ch.get("recording_id")
    if existing and existing in recordings and recordings[existing]["status"] in ("recording", "starting"):
        raise HTTPException(400, "Already recording")
    rec_id = await _start_recording_for_channel(ch_id)
    if not rec_id:
        raise HTTPException(400, "Already recording")
    return {"rec_id": rec_id}


@app.post("/api/channels/{ch_id}/stop")
async def stop_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Not found")
    rec_id = ch.get("recording_id")
    if not rec_id or rec_id not in recordings:
        raise HTTPException(400, "Not recording")
    _stop_rec(recordings[rec_id], force=False)
    return {"ok": True}


@app.post("/api/channels/{ch_id}/kill")
async def kill_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Not found")
    rec_id = ch.get("recording_id")
    if not rec_id or rec_id not in recordings:
        raise HTTPException(400, "Not recording")
    rec = recordings[rec_id]
    _stop_rec(rec, force=True)
    # Give run_recording's finally block a moment to reap the process tree,
    # otherwise a monitor tick can start a new recording while the old
    # yt-dlp/ffmpeg processes are still dying.
    for _ in range(20):
        await asyncio.sleep(0.25)
        if rec.get("status") not in ("recording", "starting"):
            break
    if rec.get("status") in ("recording", "starting"):
        rec["status"]   = "completed"
        rec["ended_at"] = time.time()
        if ch_id in channels and channels[ch_id].get("recording_id") == rec_id:
            channels[ch_id]["recording_id"] = None
    return {"ok": True}


@app.post("/api/channels/{ch_id}/refresh")
async def refresh_channel(ch_id: str):
    ch = channels.get(ch_id)
    if not ch:
        raise HTTPException(404, "Not found")
    # Re-detect platform in case it was saved as Unknown
    if ch.get("platform") == "Unknown" or not ch.get("platform"):
        channels[ch_id]["platform"] = detect_platform(ch["url"])
    proxy = ch.get("proxy") or settings.get("proxy", "")
    if ch.get("cookies_file"):
        meta = await fetch_metadata(ch["url"], proxy=proxy, cookies_file=ch["cookies_file"])
    else:
        meta = await fetch_metadata(ch["url"], proxy=proxy)
    if meta:
        updates = {
            "display_name": meta.get("display_name") or ch.get("display_name") or "",
            "username":     meta.get("username")     or ch.get("username") or "",
            "avatar":       meta.get("avatar")       or ch.get("avatar", ""),
            "thumbnail":    meta.get("thumbnail")    or ch.get("thumbnail", ""),
            "stream_title": meta.get("stream_title") or ch.get("stream_title", ""),
        }
        updates["live_check_status"] = meta.get("live_check_status", "unknown" if meta.get("_lookup_error") else "live" if meta.get("is_live") else "offline")
        updates["live_check_message"] = meta.get("live_check_message") or ("Live check could not complete. Automatic checks will continue." if meta.get("_lookup_error") else None)
        updates["last_checked"] = time.time()
        if not meta.get("_lookup_error"):
            updates["is_live"] = meta.get("is_live", False)
            updates["viewer_count"] = meta.get("viewer_count")
            updates["viewer_checked_at"] = time.time()
        if channels.get(ch_id) is not ch:
            raise HTTPException(404, "Not found")
        if ch.get("recording_id"):
            for key in ("is_live", "live_check_status", "live_check_message", "viewer_count", "viewer_checked_at"):
                updates.pop(key, None)
        channels[ch_id].update(updates)
        if not ch.get("recording_id"):
            _remember_capture_engine(ch, meta)
            if meta.get("is_live") and (ch.get("monitoring", True) or settings.get("record_on_add", False)):
                rid = await _start_recording_for_channel(ch_id)
                if rid and rid in recordings:
                    recordings[rid]["auto"] = True
    _save_state()
    return channels[ch_id]


@app.delete("/api/channels/{ch_id}")
async def delete_channel(ch_id: str):
    ch = channels.pop(ch_id, None)
    if not ch:
        raise HTTPException(404, "Not found")
    rec_id = ch.get("recording_id")
    if rec_id and rec_id in recordings:
        _stop_rec(recordings[rec_id], force=False)
        # Brief wait for recording cleanup so the finally block in run_recording
        # can still find the channel entry to clear recording_id
        await asyncio.sleep(0.5)
    _save_state()
    logger.info("Channel deleted: %s", ch_id)
    return {"ok": True}


@app.post("/api/channels/reorder")
async def reorder_channels(req: ReorderRequest):
    for i, ch_id in enumerate(req.order):
        if ch_id in channels:
            channels[ch_id]["sort_order"] = i
    _save_state()
    return {"ok": True}


@app.post("/api/channels/bulk")
async def bulk_action(req: BulkActionRequest):
    results = []
    for ch_id in req.ids:
        try:
            if req.action == "record":
                rid = await _start_recording_for_channel(ch_id)
                results.append({"id": ch_id, "ok": bool(rid)})
            elif req.action == "stop":
                ch = channels.get(ch_id)
                if ch and ch.get("recording_id"):
                    rec = recordings.get(ch["recording_id"])
                    if rec:
                        _stop_rec(rec, force=False)
                results.append({"id": ch_id, "ok": True})
            elif req.action == "delete":
                ch = channels.pop(ch_id, None)
                if ch:
                    rec_id = ch.get("recording_id")
                    if rec_id and rec_id in recordings:
                        _stop_rec(recordings[rec_id], force=False)
                results.append({"id": ch_id, "ok": True})
        except Exception:
            results.append({"id": ch_id, "ok": False})
    _save_state()
    return {"results": results}


@app.post("/api/channels/bulk-edit")
async def bulk_edit(req: BulkEditRequest):
    updated = 0
    for ch_id in req.ids:
        if ch_id not in channels:
            continue
        if req.quality is not None:
            channels[ch_id]["quality"] = req.quality
        if req.format is not None:
            channels[ch_id]["format"] = req.format
        updated += 1
    _save_state()
    return {"ok": True, "updated": updated}


# ── Recording endpoints ───────────────────────────────────────────────────────

def _library_media_path(rec: dict) -> Optional[Path]:
    fp = rec.get("filepath")
    if not fp:
        return None
    try:
        path = _media_path(fp) if HARDENED else Path(fp)
        return path if path.is_file() and path.stat().st_size > 0 else None
    except (ValueError, OSError, TypeError):
        return None


async def _measure_recording_duration(rec: dict):
    path = _library_media_path(rec)
    if path is None:
        return
    # No duration estimate from a process clock; the media file is authoritative.
    try:
        async with _proc_semaphore:
            duration = await probe_duration(path)
        if rec.get("filepath") and _library_media_path(rec) == path:
            rec["duration_seconds"] = duration
            rec["duration_checked"] = True
            _save_state()
    except Exception:
        logger.debug("Could not measure recording duration", exc_info=True)


async def _library_duration_worker():
    # Backfill old or crash-recovered media gradually, without probing every
    # recording on every dashboard request. Avoid files still being finalized.
    while True:
        await asyncio.sleep(5)
        busy = {c.get("recording_id") for c in channels.values()}
        pending = [r for r in recordings.values() if r.get("status") in FINISHED
                   and r.get("id") not in busy and not r.get("duration_checked")
                   and media_duration(r.get("duration_seconds")) is None and _library_media_path(r)]
        for rec in pending[:8]:
            if recordings.get(rec.get("id")) is rec:
                await _measure_recording_duration(rec)


def _public_recording(rec: dict) -> dict:
    r = dict(rec)
    for key in ("log", "pid", "_viewer_probe_at", "duration_checked"):
        r.pop(key, None)
    r["viewer_summary"] = viewer_summary(r.pop("viewer_samples", []))
    r["duration_seconds"] = media_duration(rec.get("duration_seconds"))
    if ch_id := rec.get("channel_id"):
        ch = channels.get(ch_id, {})
        r["channel_thumbnail"] = ch.get("thumbnail", "")
        r["channel_display_name"] = ch.get("display_name") or rec.get("account_display_name") or rec.get("account_username") or _username_from_url(rec.get("url") or "")
        r["channel_avatar"] = ch.get("avatar", "")
    return r


@app.get("/api/recordings")
async def list_recordings():
    return sorted((_public_recording(r) for r in recordings.values()), key=lambda x: x["created_at"], reverse=True)


@app.get("/api/library")
async def list_library():
    accounts, _ = build_library(channels, recordings, _library_media_path)
    return {"accounts": accounts}


@app.get("/api/library/{account_id}/recordings")
async def account_recordings(account_id: str):
    accounts, members = build_library(channels, recordings, _library_media_path)
    account = next((a for a in accounts if a["id"] == account_id), None)
    if account is None:
        raise HTTPException(404, "Account library not found")
    return {"account": account, "recordings": sorted((_public_recording(r) for r in members[account_id]), key=lambda r: r.get("created_at", 0), reverse=True)}


@app.get("/api/recordings/{rec_id}/viewers")
async def recording_viewers(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Recording not found")
    samples = rec.get("viewer_samples", [])
    elapsed = rec.get("clip_duration_seconds")
    if elapsed is None:
        elapsed = max(0, (rec.get("last_media_at") or rec.get("ended_at") or time.time()) - (rec.get("media_started_at") or rec.get("started_at") or rec.get("created_at") or time.time()))
    playable = False
    if rec.get("filepath") and rec.get("status") not in ("recording", "starting"):
        try:
            path = _media_path(rec["filepath"]) if HARDENED else Path(rec["filepath"])
            playable = path.is_file() and path.stat().st_size > 0
        except (ValueError, OSError, TypeError):
            pass
    return {"samples": samples, "summary": viewer_summary(samples), "interval_seconds": SAMPLE_INTERVAL,
            "timing": "approximate", "status": rec.get("status"), "elapsed_seconds": elapsed,
            "filename": rec.get("filename"), "playable": playable}


@app.get("/api/recordings/{rec_id}/log")
async def get_log(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    return {"log": rec.get("log", [])}


@app.get("/api/download/{rec_id}")
async def download_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    fp = rec.get("filepath")
    if HARDENED and fp:
        try:
            fp = str(_media_path(fp))
        except (ValueError, OSError, TypeError):
            raise HTTPException(404, "File not found")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(fp, filename=rec.get("filename", f"{rec_id}.mp4"))


@app.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    # Stop active recording process before deleting
    if rec.get("status") in ("recording", "starting"):
        _stop_rec(rec, force=True)
        # Wait for run_recording's finally block to release the file handles
        for _ in range(20):
            await asyncio.sleep(0.25)
            if rec.get("status") not in ("recording", "starting"):
                break
        if rec.get("status") in ("recording", "starting"):
            rec["status"] = "completed"
            rec["ended_at"] = time.time()
            if (ch_id := rec.get("channel_id")) and ch_id in channels \
                    and channels[ch_id].get("recording_id") == rec_id:
                channels[ch_id]["recording_id"] = None
    if fp := rec.get("filepath"):
        try:
            (_media_path(fp) if HARDENED else Path(fp)).unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            raise HTTPException(500, "Could not delete recording file") from None
    recordings.pop(rec_id, None)
    _save_state()
    return {"ok": True}


@app.post("/api/recordings/{rec_id}/archive")
async def archive_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    if rec.get("status") not in ("completed", "error"):
        raise HTTPException(400, "Only completed recordings can be archived")
    fp = rec.get("filepath")
    if HARDENED and fp:
        try:
            fp = str(_media_path(fp))
        except (ValueError, OSError, TypeError):
            raise HTTPException(404, "File not found")
    if fp and Path(fp).exists():
        try:
            dest = ARCHIVE_DIR / Path(fp).name
            if HARDENED:
                _media_path(str(dest))
                if dest.exists():
                    raise FileExistsError("Destination already exists")
            shutil.move(fp, str(dest))
            rec["filepath"] = str(dest)
            rec["filename"] = dest.name
        except Exception as e:
            raise HTTPException(500, f"Failed to archive: {e}")
    rec["status"] = "archived"
    _save_state()
    return {"ok": True}


@app.post("/api/recordings/{rec_id}/restore")
async def restore_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    if rec.get("status") != "archived":
        raise HTTPException(400, "Only archived recordings can be restored")
    fp = rec.get("filepath")
    if HARDENED and fp:
        try:
            fp = str(_media_path(fp))
        except (ValueError, OSError, TypeError):
            raise HTTPException(404, "File not found")
    if fp and Path(fp).exists():
        try:
            ch_id = rec.get("channel_id")
            ch = channels.get(ch_id, {}) if ch_id else {}
            plat = (ch.get("platform") or rec.get("platform") or "Unknown").lower()
            safe_plat = re.sub(r'[^\w\-]', '_', plat)
            user = ch.get("display_name") or ch.get("username") or "unknown"
            safe_user = re.sub(r'[^\w\-]', '_', user)
            rec_dir = RECORDINGS_DIR / safe_plat / safe_user
            rec_dir.mkdir(parents=True, exist_ok=True)
            dest = rec_dir / Path(fp).name
            if HARDENED:
                _media_path(str(dest))
                if dest.exists():
                    raise FileExistsError("Destination already exists")
            shutil.move(fp, str(dest))
            rec["filepath"] = str(dest)
            rec["filename"] = dest.name
        except Exception as e:
            raise HTTPException(500, f"Failed to restore: {e}")
    rec["status"] = "completed"
    _save_state()
    return {"ok": True}


@app.get("/api/preview/{rec_id}")
async def preview_recording(rec_id: str, request: Request):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    fp = rec.get("filepath")
    if HARDENED and fp:
        try:
            fp = str(_media_path(fp))
        except (ValueError, OSError, TypeError):
            raise HTTPException(404, "File not found")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "File not found")
    file_path = Path(fp)
    file_size = file_path.stat().st_size
    suffix    = file_path.suffix.lower()
    content_type = (
        "video/mp4"        if suffix == ".mp4" else
        "video/x-matroska" if suffix == ".mkv" else
        "audio/mpeg" if suffix == ".mp3" else
        "audio/mp4" if suffix == ".m4a" else
        "video/webm" if suffix == ".webm" else
        "video/mp2t"
    )

    range_header = request.headers.get("range")
    if range_header:
        try:
            spec = range_header.replace("bytes=", "").strip()
            first, _, last = spec.partition("-")
            if first:
                start = int(first)
                end   = min(int(last) if last else start + PREVIEW_RANGE_CHUNK - 1,
                            file_size - 1)
            else:
                # suffix range: bytes=-N -> last N bytes
                n = int(last)
                start = max(0, file_size - n)
                end   = file_size - 1
        except (ValueError, TypeError):
            raise HTTPException(416, "Invalid Range header")
        if start > end or start >= file_size:
            raise HTTPException(416, "Requested range not satisfiable")
        length = end - start + 1

        def iterfile():
            f = open(file_path, "rb")
            try:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(PREVIEW_READ_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                f.close()

        return StreamingResponse(
            iterfile(), status_code=206, media_type=content_type,
            headers={
                "Content-Range":  f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges":  "bytes",
                "Content-Length": str(length),
                "Cache-Control":  "private, max-age=3600",
                "Connection":     "keep-alive",
            },
        )
    return FileResponse(fp, media_type=content_type, headers={
        "Accept-Ranges": "bytes",
        "Cache-Control":  "private, max-age=3600",
    })


@app.post("/api/recordings/{rec_id}/favorite")
async def toggle_favorite(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    rec["is_favorite"] = not rec.get("is_favorite", False)
    _save_state()
    return {"ok": True, "is_favorite": rec["is_favorite"]}


@app.post("/api/recordings/{rec_id}/cut")
async def cut_recording(rec_id: str, req: CutRecordingRequest):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    fp = rec.get("filepath")
    if HARDENED and fp:
        try:
            fp = str(_media_path(fp))
        except (ValueError, OSError, TypeError):
            raise HTTPException(404, "File not found")
    if not fp or not Path(fp).exists():
        raise HTTPException(404, "Source file not found")
    if req.start_time < 0 or req.end_time <= req.start_time:
        raise HTTPException(400, "Invalid start or end time")

    src_path = Path(fp)
    clip_id  = str(uuid.uuid4())[:8]
    clip_name = (req.title or f"{src_path.stem}_clip_{int(req.start_time)}-{int(req.end_time)}").strip()
    safe_clip = re.sub(r'[^\w\-]', '_', clip_name)
    dst_path  = src_path.parent / f"{safe_clip}_{clip_id}{src_path.suffix}"

    duration = req.end_time - req.start_time
    ffmpeg_threads = get_ffmpeg_threads()

    try:
        async with _proc_semaphore:
            # Fast stream-copy cut without re-encoding
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-ss", str(req.start_time), "-i", str(src_path),
                "-t", str(duration), "-c", "copy", "-movflags", "+faststart",
                "-threads", ffmpeg_threads, str(dst_path), "-y",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=120)

        if proc.returncode != 0 or not dst_path.exists() or dst_path.stat().st_size == 0:
            if dst_path.exists():
                dst_path.unlink(missing_ok=True)
            raise HTTPException(500, "Failed to cut video segment")

        clip_rec_id = str(uuid.uuid4())[:8]
        recordings[clip_rec_id] = {
            "id": clip_rec_id,
            "channel_id": rec.get("channel_id"),
            "url": rec.get("url"),
            "platform": rec.get("platform"),
            "account_display_name": rec.get("account_display_name"),
            "account_username": rec.get("account_username"),
            "quality": rec.get("quality"),
            "format": rec.get("format"),
            "status": "completed",
            "created_at": time.time(),
            "started_at": rec.get("started_at"),
            "ended_at": time.time(),
            "bytes": dst_path.stat().st_size,
            "speed": None,
            "filepath": str(dst_path),
            "filename": dst_path.name,
            "log": [f"[StreamRec] Highlight clip created from {rec_id} ({int(duration)}s)"],
            "stopping": False,
            "auto": False,
            "viewer_samples": clip_samples(rec.get("viewer_samples", []), req.start_time, req.end_time),
            "clip_duration_seconds": duration,
            "is_clip": True,
            "is_favorite": True,  # Auto-favorite user clips
        }
        await _measure_recording_duration(recordings[clip_rec_id])
        _save_state()
        return {"ok": True, "clip_id": clip_rec_id, "filename": dst_path.name, "bytes": dst_path.stat().st_size}

    except asyncio.TimeoutError:
        if dst_path.exists():
            dst_path.unlink(missing_ok=True)
        raise HTTPException(504, "Video cutting timed out")
    except HTTPException:
        raise
    except Exception as e:
        if dst_path.exists():
            dst_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Error creating clip: {e}")


# ── Settings endpoints ────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return settings


@app.patch("/api/settings")
async def update_settings(req: UpdateSettingsRequest):
    _validate_hardened_options(req.model_dump(exclude_none=True))
    for field, val in req.model_dump(exclude_none=True).items():
        settings[field] = val
    _save_state()
    return settings


@app.post("/api/settings/test-webhook")
async def test_webhook():
    if HARDENED:
        raise HTTPException(403, "Webhooks are disabled in hardened mode")
    url = settings.get("webhook_url", "").strip()
    if not url:
        raise HTTPException(400, "No webhook URL configured in settings")
    test_payload = {
        "name": "StreamRec Test",
        "url": "http://localhost:8000",
        "platform": "Twitch",
        "stream_title": "🔔 This is a test notification from StreamRec! Webhook connection verified successfully.",
        "status": "online",
        "duration_seconds": 125,
        "bytes": 52428800,
    }
    await _send_webhook("stream_live", test_payload)
    return {"ok": True, "message": "Test notification sent"}


# ── Cookies endpoints ─────────────────────────────────────────────────────────

MAX_COOKIES_SIZE = 5 * 1024 * 1024   # 5 MiB — cookies files are tiny in practice


@app.post("/api/cookies/upload")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_COOKIES_SIZE:
        raise HTTPException(
            413, f"File too large — max {MAX_COOKIES_SIZE // 1024} KiB"
        )
    safe_name = re.sub(r'[^\w\-\.]', '_', file.filename or "cookies.txt")
    if safe_name.startswith("platform-"):
        raise HTTPException(400, "Use Connected Accounts for platform sessions")
    try:
        dest = _cookie_path(safe_name)
        if dest is None:
            raise ValueError("Invalid filename")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    dest.write_bytes(content)
    if not IS_WINDOWS:
        os.chmod(dest, 0o600)
    return {"ok": True, "filename": safe_name, "size": len(content)}


@app.get("/api/cookies")
async def list_cookies():
    files = []
    for f in sorted(COOKIES_DIR.iterdir()):
        if f.is_file() and not f.name.startswith(("platform-", ".session-")):
            files.append({"filename": f.name, "size": f.stat().st_size})
    return files


@app.delete("/api/cookies/{filename}")
async def delete_cookies(filename: str):
    safe = re.sub(r'[^\w\-\.]', '_', filename)
    if safe.startswith("platform-"):
        raise HTTPException(400, "Use Connected Accounts for platform sessions")
    try:
        dest = _cookie_path(safe)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not dest or not dest.exists():
        raise HTTPException(404, "Not found")
    dest.unlink()
    return {"ok": True}


# ── Connected platform sessions ───────────────────────────────────────────────

_platform_pairings: dict[str, dict] = {}
_PAIRING_TTL = 300
_MAX_PLATFORM_BODY = 2 * 1024 * 1024


def _store_platform_session(platform: str, cookies: list[dict]) -> None:
    content = scoped_cookie_text(platform, cookies)
    dest = _cookie_path(_platform_cookie_name(platform))
    if dest is None:
        raise ValueError("Unsupported platform")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=COOKIES_DIR, prefix=".session-", delete=False) as f:
            temp = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, dest)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _request_session(request: Request) -> str:
    token = request.cookies.get("streamrec_session", "")
    if not token and request.headers.get("authorization", "").lower().startswith("bearer "):
        token = request.headers["authorization"][7:].strip()
    return token


def _prune_pairings():
    now = time.monotonic()
    for key, entry in list(_platform_pairings.items()):
        if entry["expires"] <= now or not _is_valid_session(entry["session"]):
            _platform_pairings.pop(key, None)


@app.get("/api/platform-accounts")
async def platform_accounts():
    result = []
    for key, cfg in PLATFORMS.items():
        path = _cookie_path(_platform_cookie_name(key))
        stored = bool(path and path.is_file())
        available = False
        if stored:
            try:
                if path.stat().st_size <= 1024 * 1024:
                    scoped_cookie_text(key, parse_netscape_cookies(path.read_text(encoding="utf-8-sig")))
                    available = True
            except (ValueError, OSError, UnicodeError):
                pass
        result.append({"platform": key, "label": cfg["label"], "login_url": cfg["login"],
                       "session_stored": stored, "session_available": available, "updated_at": path.stat().st_mtime if stored else None})
    return result


@app.post("/api/platform-accounts/{platform}/pair")
async def pair_platform(platform: str, request: Request):
    if platform not in PLATFORMS:
        raise HTTPException(400, "Unsupported platform")
    session = _request_session(request)
    if not _is_valid_session(session):
        raise HTTPException(401, "Log into StreamRec first")
    _prune_pairings()
    for key, entry in list(_platform_pairings.items()):
        if entry["platform"] == platform and entry["session"] == session:
            _platform_pairings.pop(key, None)
    if len(_platform_pairings) >= 20:
        raise HTTPException(429, "Too many active pairing codes")
    code = secrets.token_urlsafe(24)
    _platform_pairings[hashlib.sha256(code.encode()).hexdigest()] = {"platform": platform, "session": session, "expires": time.monotonic() + _PAIRING_TTL}
    return {"code": code, "expires_in": _PAIRING_TTL}


@app.post("/api/platform-accounts/connect")
async def connect_platform(request: Request):
    # No cookie authentication, no CORS wildcard, no password or code in URLs.
    # The code binds one upload to the chosen platform and an active UI session.
    code = request.headers.get("x-streamrec-pairing", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", code):
        raise HTTPException(401, "Invalid or expired pairing code")
    _prune_pairings()
    key = hashlib.sha256(code.encode()).hexdigest()
    entry = _platform_pairings.get(key)
    if not entry:
        raise HTTPException(401, "Invalid or expired pairing code")
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise HTTPException(415, "JSON required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_PLATFORM_BODY:
            raise HTTPException(413, "Cookie session is too large")
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("platform") != entry["platform"]:
            raise ValueError("Pairing code is for another platform")
        # Re-check after the asynchronous body read, then consume without an await.
        _prune_pairings()
        if _platform_pairings.pop(key, None) is None:
            raise HTTPException(401, "Invalid or expired pairing code")
        _store_platform_session(entry["platform"], data.get("cookies"))
    except (ValueError, TypeError, OSError) as exc:
        # Never echo submitted cookie content, JSON parser input or file paths.
        raise HTTPException(400, "Could not store matching platform cookies; generate a new pairing code") from None
    return {"ok": True, "platform": entry["platform"], "session_stored": True}


@app.post("/api/platform-accounts/{platform}/upload")
async def upload_platform_session(platform: str, file: UploadFile = File(...)):
    if platform not in PLATFORMS:
        raise HTTPException(400, "Unsupported platform")
    data = await file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise HTTPException(413, "Cookie file is too large")
    try:
        _store_platform_session(platform, parse_netscape_cookies(data.decode("utf-8-sig")))
    except (ValueError, UnicodeError, OSError):
        raise HTTPException(400, "Use a valid Netscape cookies.txt file for the selected platform") from None
    return {"ok": True, "platform": platform, "session_stored": True}


@app.delete("/api/platform-accounts/{platform}")
async def disconnect_platform(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(400, "Unsupported platform")
    path = _cookie_path(_platform_cookie_name(platform))
    if path:
        path.unlink(missing_ok=True)
    for key, entry in list(_platform_pairings.items()):
        if entry["platform"] == platform:
            _platform_pairings.pop(key, None)
    return {"ok": True}


# ── Platforms endpoint ────────────────────────────────────────────────────────

SUPPORTED_PLATFORMS = [
    {"name": "YouTube",     "domain": "youtube.com / youtu.be",  "color": "#FF0000", "emoji": "▶",  "notes": "Livestreams, premieres, scheduled streams"},
    {"name": "Twitch",      "domain": "twitch.tv",               "color": "#6441a5", "emoji": "🎮", "notes": "Live channels, VOD replay"},
    {"name": "TikTok",      "domain": "tiktok.com",              "color": "#010101", "emoji": "♪",  "notes": "Live streams"},
    {"name": "Kick",        "domain": "kick.com",                "color": "#53fc18", "emoji": "⚡", "notes": "Live channels"},
    {"name": "Bilibili",    "domain": "bilibili.com",            "color": "#00a1d6", "emoji": "B",  "notes": "Live streams"},
    {"name": "Douyin",      "domain": "douyin.com",              "color": "#010101", "emoji": "♪",  "notes": "Chinese TikTok live"},
    {"name": "Huya",        "domain": "huya.com",                "color": "#ff8c00", "emoji": "🎮", "notes": "Chinese game streaming"},
    {"name": "Douyu",       "domain": "douyu.com",               "color": "#ff6600", "emoji": "🐟", "notes": "Chinese game streaming"},
    {"name": "Afreeca TV",  "domain": "afreecatv.com",           "color": "#006aff", "emoji": "🔵", "notes": "Korean streaming platform"},
    {"name": "Sooplive",    "domain": "sooplive.co.kr",          "color": "#1a6cff", "emoji": "🔵", "notes": "Korean streaming (formerly AfreecaTV)"},
    {"name": "Naver",       "domain": "naver.com",               "color": "#03c75a", "emoji": "N",  "notes": "Korean portal live streams"},
    {"name": "Niconico",    "domain": "nicovideo.jp",            "color": "#888888", "emoji": "⚪", "notes": "Japanese video / live"},
    {"name": "Facebook",    "domain": "facebook.com / fb.watch", "color": "#1877f2", "emoji": "f",  "notes": "Live videos — use cookies for age-restricted"},
    {"name": "Instagram",   "domain": "instagram.com",           "color": "#e1306c", "emoji": "📷", "notes": "Live, Reels — requires cookies for private"},
    {"name": "Twitter/X",   "domain": "twitter.com / x.com",    "color": "#1da1f2", "emoji": "🐦", "notes": "Spaces, live video"},
    {"name": "Vimeo",       "domain": "vimeo.com",               "color": "#1ab7ea", "emoji": "V",  "notes": "Live events"},
    {"name": "Rumble",      "domain": "rumble.com",              "color": "#85c742", "emoji": "🟢", "notes": "Live streams"},
    {"name": "Stripchat",   "domain": "stripchat.com",           "color": "#ff4500", "emoji": "🔞", "notes": "Adult live — use cookies for age verification"},
    {"name": "Twitcasting", "domain": "twitcasting.tv",          "color": "#2f6db5", "emoji": "📡", "notes": "Japanese mobile streaming"},
    {"name": "Bigo Live",   "domain": "bigo.tv",                 "color": "#00c73c", "emoji": "🎤", "notes": "Global live streaming"},
    {"name": "Pandalive",   "domain": "pandalive.co.kr",         "color": "#ff6699", "emoji": "🐼", "notes": "Korean streaming"},
    {"name": "Weibo",       "domain": "weibo.com",               "color": "#e6162d", "emoji": "微",  "notes": "Chinese social media live"},
    {"name": "Dailymotion", "domain": "dailymotion.com",         "color": "#0066dc", "emoji": "D",  "notes": "Live & VOD"},
    {"name": "Chaturbate", "domain": "chaturbate.com",           "color": "#f47321", "emoji": "🔞", "notes": "Adult live — cookies for age verification"},
    {"name": "Cam4",       "domain": "cam4.com",                 "color": "#e8403a", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "MyFreeCams", "domain": "myfreecams.com",           "color": "#009b77", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "CamSoda",    "domain": "camsoda.com",              "color": "#ff6b35", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "BongaCams",  "domain": "bongacams.com",            "color": "#f5a623", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "CamModels",  "domain": "cammodels.com",            "color": "#7b2d8b", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "Streamate",  "domain": "streamate.com",            "color": "#c0392b", "emoji": "🔞", "notes": "Adult live streaming"},
    {"name": "Flirt4Free", "domain": "flirt4free.com",           "color": "#e91e8c", "emoji": "🔞", "notes": "Adult live streaming"},
]

@app.get("/api/platforms")
async def list_platforms():
    return SUPPORTED_PLATFORMS


# ── Misc endpoints ────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    active_recs = sum(1 for r in recordings.values() if r.get("status") in ("recording", "starting"))
    return {
        "ok": True,
        "channels": len(channels),
        "recordings": len(recordings),
        "active": active_recs,
        "uptime": int(time.time() - START_TIME),
        "pi_mode": _is_pi(),
    }


@app.get("/api/version")
async def version():
    return {
        "version": VERSION,
        "pi_mode": _is_pi(),
        "poll_interval": get_frontend_poll_hint(),
        "hardened": HARDENED,
        "egress_policy": "public-web" if HARDENED else "compatibility",
    }


@app.get("/api/disk")
async def disk_usage():
    global _disk_cache, _disk_cache_ts
    now = time.time()
    if _disk_cache and now - _disk_cache_ts < get_disk_cache_ttl():
        return _disk_cache
    try:
        usage     = shutil.disk_usage(str(RECORDINGS_DIR))
        rec_bytes = sum(
            f.stat().st_size for f in RECORDINGS_DIR.rglob("*")
            if f.is_file() and f.name != "state.json"
        )
        _disk_cache = {
            "total": usage.total, "used": usage.used,
            "free":  usage.free,  "recordings_bytes": rec_bytes,
        }
        _disk_cache_ts = now
        return _disk_cache
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "recordings_bytes": 0}


@app.get("/api/export")
async def export_config():
    saved_channels = {}
    for cid, ch in channels.items():
        c = dict(ch)
        for k in ("recording_id", "is_live", "last_checked", "viewer_count", "viewer_checked_at", "live_check_status", "live_check_message", "capture_engine", "engine_checked_at"):
            c.pop(k, None)
        c.pop("ch_password", None)  # never export credentials
        saved_channels[cid] = c
    return {"channels": saved_channels, "settings": settings}


@app.post("/api/import")
async def import_config(req: ImportRequest):
    imported = 0
    _validate_hardened_options(req.settings)
    for cid, ch in req.channels.items():
        if not re.fullmatch(r"[a-f0-9]{8}", cid):
            raise HTTPException(400, "Invalid imported channel ID")
        if not isinstance(ch, dict) or not isinstance(ch.get("url"), str):
            raise HTTPException(400, "Invalid imported channel")
        ch["url"] = _approved_channel_url(ch["url"])
        ch["id"] = cid
        ch["platform"] = detect_platform(ch["url"])
        _validate_hardened_options(ch)
    # Known safe channel keys — reject anything else to prevent state corruption
    _SAFE_CH_KEYS = {
        "id", "url", "platform", "quality", "format", "monitoring",
        "auto_convert_mp4", "delete_original", "proxy", "cookies_file",
        "ch_username", "extra_args", "max_duration", "max_streams", "notes",
        "created_at", "display_name", "username", "avatar", "thumbnail",
        "stream_title", "sort_order",
    }
    for cid, ch in req.channels.items():
        if req.merge and cid in channels:
            continue
        # Sanitize: only keep known-safe keys
        ch = {k: v for k, v in ch.items() if k in _SAFE_CH_KEYS}
        ch["recording_id"] = None
        ch["is_live"]      = False
        ch["last_checked"] = None
        ch.setdefault("id",         cid)
        ch.setdefault("created_at", time.time())
        # Type-coerce numeric fields
        try:
            ch["max_duration"] = int(ch.get("max_duration", 0) or 0)
        except (ValueError, TypeError):
            ch["max_duration"] = 0
        channels[cid] = ch
        imported += 1
    # Validate settings types before applying
    _TYPE_MAP = {
        "pi_mode": bool, "monitor_interval": (int, float),
        "record_on_add": bool, "default_quality": str, "default_format": str,
        "auto_convert_mp4": bool, "delete_original": bool,
        "auto_retry": bool, "max_retries": int, "retry_delay": (int, float),
        "proxy": str, "extra_args": str, "cookies_file": str,
        "retention_days": int, "max_duration": int,
        "webhook_url": str, "auto_stop_stalled": bool,
    }
    for k, v in req.settings.items():
        if k in settings:
            expected = _TYPE_MAP.get(k)
            if expected and not isinstance(v, expected):
                logger.warning("Import: skipping %s (expected %s, got %s)", k, expected, type(v).__name__)
                continue
            # Clamp numeric values
            if k in ("monitor_interval", "max_retries", "retry_delay", "retention_days", "max_duration"):
                try:
                    v = max(0, int(v) if k != "retry_delay" else float(v))
                except (ValueError, TypeError):
                    v = settings[k]  # keep default
            settings[k] = v
    _save_state()
    return {"ok": True, "imported_channels": imported}


# ── Account (local user profile) ──────────────────────────────────────────────
#
# A single local profile stored at RECORDINGS_DIR/account.json.  The password is
# hashed with PBKDF2-HMAC-SHA256 (200k iterations) over a 32-byte random salt —
# the plaintext password is never written to disk.  Username is stored in the
# clear so the UI can greet the user, but the file is chmod 600 on Unix and
# lives in the private recordings dir on Windows.

PBKDF2_ITERATIONS = 200_000
MAX_AVATAR_SIZE   = 2 * 1024 * 1024   # 2 MiB
_AVATAR_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def _hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return digest.hex(), salt.hex()


def _verify_password(password: str, hex_hash: str, hex_salt: str) -> bool:
    try:
        salt = bytes.fromhex(hex_salt)
    except ValueError:
        return False
    cand, _ = _hash_password(password, salt)
    return hmac.compare_digest(cand, hex_hash)


_active_sessions: dict[str, float] = {}  # token -> created_at
SESSION_TTL = 7 * 24 * 3600  # 7 days


def _create_session_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _active_sessions[token] = time.time()
    return token


def _is_valid_session(token: Optional[str]) -> bool:
    if not token:
        return False
    created = _active_sessions.get(token)
    if not created:
        return False
    if time.time() - created > SESSION_TTL:
        _active_sessions.pop(token, None)
        return False
    return True


def require_auth(request: Request) -> bool:
    """If an account exists, ensure the request has a valid session token (via cookie or header)."""
    acc = _load_account()
    if not acc:
        if HARDENED:
            raise HTTPException(401, "Authentication required")
        return True  # Guest / unconfigured mode
    token = request.cookies.get("streamrec_session")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not _is_valid_session(token):
        raise HTTPException(status_code=401, detail="Authentication required")
    return True


def _load_account() -> Optional[dict]:
    if not ACCOUNT_FILE.exists():
        return None
    try:
        return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load account: %s", e)
        return None


def _save_account(acc: dict) -> None:
    try:
        tmp = ACCOUNT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(acc, indent=2), encoding="utf-8")
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        tmp.replace(ACCOUNT_FILE)
        if not IS_WINDOWS:
            try:
                os.chmod(ACCOUNT_FILE, 0o600)
            except Exception:
                pass
    except Exception as e:
        logger.warning("Failed to save account: %s", e)
        raise HTTPException(500, "Account could not be saved") from e


def _public_account(acc: dict) -> dict:
    return {
        "exists":     True,
        "username":   acc.get("username", ""),
        "avatar":     acc.get("avatar", ""),
        "created_at": acc.get("created_at"),
    }


class CreateAccountRequest(BaseModel):
    username:         str
    password:         str
    confirm_password: str


class UpdateAccountRequest(BaseModel):
    username:             Optional[str] = None
    current_password:     Optional[str] = None
    new_password:         Optional[str] = None
    confirm_new_password: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


def _validate_username(u: str) -> str:
    u = (u or "").strip()
    if len(u) < 2:
        raise HTTPException(400, "Username must be at least 2 characters")
    if len(u) > 32:
        raise HTTPException(400, "Username must be at most 32 characters")
    return u


def _validate_password(p: str) -> None:
    minimum = 12 if HARDENED else 4
    if not p or len(p) < minimum:
        raise HTTPException(400, f"Password must be at least {minimum} characters")
    if len(p) > 256:
        raise HTTPException(400, "Password too long (max 256 characters)")


@app.get("/api/account")
async def get_account():
    acc = _load_account()
    if not acc:
        return {"exists": False}
    return _public_account(acc)


@app.post("/api/account")
async def create_account(req: CreateAccountRequest, response: Response):
    if _load_account() or ACCOUNT_FILE.exists():
        raise HTTPException(400, "An account already exists or requires local recovery")
    username = _validate_username(req.username)
    _validate_password(req.password)
    if req.password != req.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    h, salt = _hash_password(req.password)
    acc = {
        "username":      username,
        "password_hash": h,
        "password_salt": salt,
        "avatar":        "",
        "created_at":    time.time(),
    }
    _save_account(acc)
    logger.info("Account created for user %r", username)
    token = _create_session_token(username)
    response.set_cookie(
        key="streamrec_session",
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    res = _public_account(acc)
    res["token"] = token
    return res


@app.patch("/api/account")
async def update_account(req: UpdateAccountRequest):
    acc = _load_account()
    if not acc:
        raise HTTPException(404, "No account exists")

    if req.new_password is not None:
        if not req.current_password or not _verify_password(
            req.current_password, acc["password_hash"], acc["password_salt"]
        ):
            raise HTTPException(401, "Current password is incorrect")
        _validate_password(req.new_password)
        if req.new_password != (req.confirm_new_password or ""):
            raise HTTPException(400, "New passwords do not match")
        h, salt = _hash_password(req.new_password)
        acc["password_hash"] = h
        acc["password_salt"] = salt

    if req.username is not None:
        acc["username"] = _validate_username(req.username)

    _save_account(acc)
    if req.new_password is not None:
        _active_sessions.clear()
    return _public_account(acc)


@app.delete("/api/account")
async def delete_account(response: Response):
    if HARDENED:
        raise HTTPException(403, "Account removal is disabled in hardened mode")
    if ACCOUNT_FILE.exists():
        try:
            ACCOUNT_FILE.unlink()
        except Exception:
            pass
    for f in AVATAR_DIR.glob("avatar.*"):
        try:
            f.unlink()
        except Exception:
            pass
    _active_sessions.clear()
    response.delete_cookie(key="streamrec_session")
    return {"ok": True}


@app.post("/api/account/avatar")
async def upload_avatar(file: UploadFile = File(...)):
    acc = _load_account()
    if not acc:
        raise HTTPException(404, "No account exists")

    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_AVATAR_SIZE:
        raise HTTPException(
            413, f"File too large — max {MAX_AVATAR_SIZE // 1024} KiB"
        )

    ext = ".png"
    if file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix in _AVATAR_MIME:
            ext = suffix
    content_type = _AVATAR_MIME.get(ext, "image/png")

    # Wipe any previous avatar files before writing the new one
    for f in AVATAR_DIR.glob("avatar.*"):
        try:
            f.unlink()
        except Exception:
            pass
    try:
        (AVATAR_DIR / f"avatar{ext}").write_bytes(content)
    except Exception:
        pass  # data-URL is the source of truth; disk copy is best-effort

    acc["avatar"] = (
        f"data:{content_type};base64,"
        + base64.b64encode(content).decode("ascii")
    )
    _save_account(acc)
    return {"ok": True, "avatar": acc["avatar"]}


@app.delete("/api/account/avatar")
async def delete_avatar():
    acc = _load_account()
    if not acc:
        raise HTTPException(404, "No account exists")
    acc["avatar"] = ""
    for f in AVATAR_DIR.glob("avatar.*"):
        try:
            f.unlink()
        except Exception:
            pass
    _save_account(acc)
    return {"ok": True}


def _check_login_rate(client_ip: str) -> bool:
    """Return True if the IP is rate-limited (too many failed attempts)."""
    now = time.time()
    # Prune expired IPs so the map cannot grow unbounded on public deployments
    for ip in list(_login_attempts):
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
        if not _login_attempts[ip]:
            del _login_attempts[ip]
    return len(_login_attempts.get(client_ip, [])) >= _LOGIN_MAX_ATTEMPTS

def _record_login_attempt(client_ip: str):
    _login_attempts.setdefault(client_ip, []).append(time.time())

@app.post("/api/account/login")
async def login(req: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    if _check_login_rate(client_ip):
        # Reject immediately with Retry-After — never hold the event loop
        return JSONResponse(
            {"detail": "Too many login attempts — try again later"},
            status_code=429,
            headers={"Retry-After": str(_LOGIN_COOLDOWN)},
        )
    acc = _load_account()
    if not acc:
        raise HTTPException(404, "No account exists")
    # Always compute the hash check regardless of username match
    # to prevent username enumeration via timing side-channel
    user_matches = req.username.strip() == acc.get("username")
    pw_ok = _verify_password(req.password, acc["password_hash"], acc["password_salt"])
    if not user_matches or not pw_ok:
        _record_login_attempt(client_ip)
        # Deliberate short sleep to blunt naive brute force
        await asyncio.sleep(0.3)
        raise HTTPException(401, "Invalid credentials")
    token = _create_session_token(acc.get("username", ""))
    response.set_cookie(
        key="streamrec_session",
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    res = _public_account(acc)
    res["token"] = token
    return res


@app.post("/api/account/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("streamrec_session")
    if token:
        _active_sessions.pop(token, None)
    response.delete_cookie(key="streamrec_session")
    return {"ok": True}


# ── yt-dlp management ────────────────────────────────────────────────────────

@app.get("/api/ytdlp-version")
async def get_ytdlp_version():
    """Get current and latest yt-dlp version."""
    current = "unknown"
    latest = None

    # Get current version
    try:
        result = await asyncio.create_subprocess_exec(
            "yt-dlp", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await result.communicate()
        if result.returncode == 0:
            current = stdout.decode("utf-8").strip()
    except Exception as e:
        logger.warning("Failed to get yt-dlp version: %s", e)

    if HARDENED:
        return {"current": current, "latest": None, "updates_disabled": True}
    # Get latest version from GitHub
    try:
        result = await asyncio.create_subprocess_exec(
            "curl", "-sL", "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await result.communicate()
        if result.returncode == 0:
            import json
            data = json.loads(stdout.decode("utf-8"))
            latest = data.get("tag_name", "").lstrip("v")
    except Exception as e:
        logger.debug("Failed to fetch latest yt-dlp version: %s", e)

    return {"current": current, "latest": latest}


@app.post("/api/ytdlp-update")
async def update_ytdlp():
    """Update yt-dlp to the latest version."""
    if HARDENED:
        raise HTTPException(403, "Updates require rebuilding with a verified version and checksum")
    logger.info("Updating yt-dlp...")

    try:
        # Try pip update first (works for pip-installed yt-dlp)
        result = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-U", "yt-dlp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(result.communicate(), timeout=120)
        output = stdout.decode("utf-8", errors="replace")

        if result.returncode == 0:
            # Get new version
            version_check = await asyncio.create_subprocess_exec(
                "yt-dlp", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ver_stdout, _ = await version_check.communicate()
            if version_check.returncode == 0:
                new_version = ver_stdout.decode("utf-8").strip()
                logger.info("yt-dlp updated to %s", new_version)
                return {"success": True, "version": new_version}
            else:
                logger.info("yt-dlp updated (version check failed)")
                return {"success": True, "version": "unknown"}
        else:
            # Pip failed, try downloading the binary directly (works in Docker)
            logger.info("pip update failed, trying direct download...")
            result = await asyncio.create_subprocess_exec(
                "curl", "-L", "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp",
                "-o", "/usr/local/bin/yt-dlp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=60)

            if result.returncode == 0:
                # Make executable
                chmod_proc = await asyncio.create_subprocess_exec("chmod", "+x", "/usr/local/bin/yt-dlp")
                await chmod_proc.wait()

                # Get new version
                version_check = await asyncio.create_subprocess_exec(
                    "yt-dlp", "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                ver_stdout, _ = await version_check.communicate()
                if version_check.returncode == 0:
                    new_version = ver_stdout.decode("utf-8").strip()
                    logger.info("yt-dlp updated to %s (via direct download)", new_version)
                    return {"success": True, "version": new_version}

            error_msg = f"Update failed: {output[:500]}" if result.returncode != 0 else "Update failed"
            logger.warning(error_msg)
            return {"success": False, "error": error_msg}

    except asyncio.TimeoutError:
        logger.warning("yt-dlp update timed out")
        return {"success": False, "error": "Update timed out"}
    except Exception as e:
        logger.exception("yt-dlp update error")
        return {"success": False, "error": str(e)}


# ── Static files (must be last) ──────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(_static_dir) / "index.html", media_type="text/html")
