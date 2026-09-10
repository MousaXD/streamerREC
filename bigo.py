"""Native BIGO Live helpers for StreamRec.

BIGO share links on slink.bigovideo.tv are not handled reliably by yt-dlp.
This module resolves both normal BIGO room URLs and share links, queries BIGO's
studio-info endpoint, and caps BIGO HTTPS requests at TLS 1.2 only. Some
networks can establish TCP to BIGO but stall during TLS 1.3; scoping the
workaround to BIGO avoids weakening TLS for other platforms.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import html as html_lib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


BIGO_API = "https://ta.bigo.tv/official_website/studio/getInternalStudioInfo"
_BIGO_SEC_HOST = "https://sec.bigo.sg"
_BIGO_TOKEN_PASSPHRASE = b"undefinedval0x01"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
    "Gecko/20100101 Firefox/119.0"
)
_SAFE_SITE_ID = re.compile(r"^[A-Za-z0-9_.-]{2,128}$")


class BigoError(RuntimeError):
    """Raised when a BIGO URL cannot be resolved or queried safely."""


def decrypt_web_protection_prefix(packets: bytearray, seed: int) -> None:
    """Decrypt BIGO's protected bytes in the first two MPEG-TS packets in-place.

    BIGO's EXT-X-BIGO-WEB-PROTECTION tag supplies a per-playlist SEED. Only
    the first 16 bytes of each of the first two 188-byte TS packets are
    obfuscated. The algorithm is adapted from Streamlink's BIGO plugin fix.
    """
    if len(packets) < 376:
        raise BigoError("protected BIGO segment prefix is shorter than two TS packets")

    for packet_index in range(2):
        mixed = ctypes.c_uint32((packet_index + 1) * 2654435769).value
        state = ctypes.c_uint32(seed ^ mixed).value
        if state == 0:
            state = 1831565813

        packet_offset = 188 * packet_index
        for offset in range(16):
            state ^= ctypes.c_uint32(state << 13).value
            state ^= state >> 17
            state ^= ctypes.c_uint32(state << 5).value
            state = ctypes.c_uint32(state).value

            mask = state & 0xFF
            if mask == 0:
                mask = 165
            packets[packet_offset + offset] ^= mask


@dataclass(frozen=True)
class BigoInfo:
    site_id: str
    alive: bool
    display_name: str = ""
    stream_title: str = ""
    thumbnail: str = ""
    hls_src: str = ""
    room_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "alive": self.alive,
            "display_name": self.display_name,
            "stream_title": self.stream_title,
            "thumbnail": self.thumbnail,
            "hls_src": self.hls_src,
            "room_id": self.room_id,
        }


def is_bigo_url(url: str) -> bool:
    """Return True for BIGO room URLs and BIGO's share-link domain."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return (
        host == "bigo.tv"
        or host.endswith(".bigo.tv")
        or host == "slink.bigovideo.tv"
    )


def _validated_site_id(value: str) -> str:
    value = unquote((value or "").strip())
    if not _SAFE_SITE_ID.fullmatch(value):
        return ""
    return value


def _site_id_from_bigo_url(url: str) -> str:
    """Extract a siteId from canonical BIGO URLs."""
    try:
        parsed = urlparse(html_lib.unescape(url))
        host = (parsed.hostname or "").lower().rstrip(".")
        if not (host == "bigo.tv" or host.endswith(".bigo.tv")):
            return ""

        query = parse_qs(parsed.query)
        for key in ("h", "siteId", "siteid"):
            values = query.get(key)
            if values:
                candidate = _validated_site_id(values[-1])
                if candidate:
                    return candidate

        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if not parts:
            return ""

        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z]{2}(?:-[A-Za-z]{2})?", parts[0]):
            parts = parts[1:]

        return _validated_site_id(parts[0] if parts else "")
    except Exception:
        return ""


def _site_id_from_share_html(page_html: str) -> str:
    """Extract BIGO's siteId from an slink.bigovideo.tv landing page."""
    if not page_html:
        return ""

    decoded = html_lib.unescape(page_html)

    for tag in re.findall(r"<meta\b[^>]*>", decoded, flags=re.IGNORECASE):
        if "al:web:url" not in tag.lower():
            continue
        match = re.search(r"content\s*=\s*[\"']([^\"']+)[\"']", tag, re.IGNORECASE)
        if match:
            candidate = _site_id_from_bigo_url(match.group(1))
            if candidate:
                return candidate
            parsed = urlparse(match.group(1))
            query = parse_qs(parsed.query)
            values = query.get("h")
            if values:
                candidate = _validated_site_id(values[-1])
                if candidate:
                    return candidate

    patterns = (
        r"\"siteId\"\s*:\s*\"([^\"]+)\"",
        r"'siteId'\s*:\s*'([^']+)'",
        r"siteId\s*[=:]\s*[\"']?([A-Za-z0-9_.-]{2,128})",
        r"https?://(?:www\.)?bigo\.tv/(?:[A-Za-z]{2}(?:-[A-Za-z]{2})?/)?([A-Za-z0-9_.-]{2,128})",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.IGNORECASE)
        if match:
            candidate = _validated_site_id(match.group(1))
            if candidate:
                return candidate
    return ""


def _curl_base(proxy: str = "") -> list[str]:
    cmd = [
        "curl",
        "-fsS",
        "--proto",
        "=https",
        "--tls-max",
        "1.2",
        "--connect-timeout",
        "10",
        "--max-time",
        "20",
        "-A",
        _USER_AGENT,
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    return cmd


def _run_curl(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BigoError("curl is required for native BIGO support") from exc
    except subprocess.TimeoutExpired as exc:
        raise BigoError("BIGO request timed out") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"curl exited with {result.returncode}"
        raise BigoError(f"BIGO request failed: {message}")
    return result.stdout


def _parse_jsonp(body: str) -> dict[str, Any]:
    body = (body or "").strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        raise BigoError("BIGO integrity service returned invalid JSONP")
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise BigoError("BIGO integrity service returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BigoError("BIGO integrity service returned an unexpected response")
    return payload


def _evp_bytes_to_key(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """CryptoJS/OpenSSL EVP_BytesToKey-compatible MD5 derivation."""
    derived = bytearray()
    previous = b""
    while len(derived) < 48:
        previous = hashlib.md5(previous + password + salt).digest()
        derived.extend(previous)
    return bytes(derived[:32]), bytes(derived[32:48])


def _encrypt_bigo_fingerprint(plaintext: str) -> str:
    salt = os.urandom(8)
    key, iv = _evp_bytes_to_key(_BIGO_TOKEN_PASSPHRASE, salt)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return base64.b64encode(b"Salted__" + salt + encrypted).decode("ascii")


def _build_bigo_fingerprint(at_time: str) -> dict[str, str]:
    """Build the compact desktop fingerprint shape expected by sec.bigo.sg."""
    values = {
        "wc": "false",
        "rk": "",
        "wg": "false",
        "wk": "false",
        "wl": "false",
        "wp": "true",
        "wx": "false",
        "dz": _USER_AGENT,
        "pu": "false",
        "gs": "en-US",
        "kd": "24",
        "vk": "8",
        "gz": "8",
        "ec": "1920,1080",
        "tr": "1920,1040",
        "lb": "0",
        "mo": "UTC",
        "io": "true",
        "wz": "true",
        "mx": "true",
        "gb": "false",
        "nx": "false",
        "cp": "not available",
        "gu": "Linux x86_64",
        "ya": "PDF Viewer,Portable Document Format,application/pdf,pdf",
        "mq": "canvas winding:yes,canvas fp:data:image/png;base64,iVBORw0KGgo=",
        "ix": "data:image/png;base64,iVBORw0KGgo=",
        "dd": "Google Inc. (Google)~ANGLE (Google, Vulkan 1.3.0)",
        "vd": "false",
        "cm": "false",
        "ey": "false",
        "ui": "false",
        "nb": "false",
        "lu": "0,false,false",
        "ww": "Arial,Helvetica,Times,Times New Roman",
        "ni": "124.04347527516074",
        "dr": hashlib.md5(os.urandom(16)).hexdigest(),
        "business": "bigolive-video",
        "scene": "",
        "at_time": str(at_time),
        "ver": "2.0",
    }
    return {key: value[:100] for key, value in values.items()}


def _mint_bigo_token_sync(proxy: str = "") -> str:
    """Mint a fresh one-use BIGO integrity token."""
    callback = f"jsonpcallback_{int(time.time() * 1000)}_{secrets.randbelow(99000) + 1000}"
    common_headers = [
        "-H",
        "Referer: https://www.bigo.tv/",
        "-H",
        "Accept: */*",
    ]

    time_url = f"{_BIGO_SEC_HOST}/v1/webjs/t?callback=&callback={quote(callback, safe='')}"
    time_payload = _parse_jsonp(_run_curl(_curl_base(proxy) + common_headers + [time_url]))
    code = time_payload.get("code")
    if code not in (None, 0, "0"):
        raise BigoError(f"BIGO integrity time request failed: code={code}")
    at_time = time_payload.get("time")
    if at_time is None:
        raise BigoError("BIGO integrity time response is missing time")

    fingerprint = _build_bigo_fingerprint(str(at_time))
    plaintext = json.dumps(fingerprint, separators=(",", ":"), ensure_ascii=False)
    encrypted = _encrypt_bigo_fingerprint(plaintext)

    last_payload: dict[str, Any] = {}
    for data_value in (quote(encrypted, safe=""), encrypted):
        callback = f"jsonpcallback_{int(time.time() * 1000)}_{secrets.randbelow(99000) + 1000}"
        status_url = (
            f"{_BIGO_SEC_HOST}/v1/webjs/status?data={data_value}"
            f"&callback={quote(callback, safe='')}"
        )
        last_payload = _parse_jsonp(
            _run_curl(_curl_base(proxy) + common_headers + [status_url])
        )
        if last_payload.get("code") in (0, "0"):
            token = last_payload.get("token")
            if isinstance(token, str) and token:
                return token

    raise BigoError(f"BIGO integrity token mint failed: code={last_payload.get('code')}")


def _fetch_studio_payload(site_id: str, proxy: str = "", token: str = "") -> dict[str, Any]:
    cmd = _curl_base(proxy) + [
        "-X",
        "POST",
        "-H",
        "Accept: application/json",
        "-H",
        "Referer: https://www.bigo.tv/",
        "--data-urlencode",
        f"siteId={site_id}",
        "--data-urlencode",
        "verify=",
    ]
    if token:
        cmd += [
            "--data-urlencode",
            f"token={token}",
            "--data-urlencode",
            "supportHevc=1",
        ]
    cmd.append(BIGO_API)

    raw = _run_curl(cmd)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BigoError("BIGO returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BigoError("BIGO returned an unexpected response")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        msg = payload.get("msg") or "unknown BIGO API error"
        raise BigoError(f"BIGO API error {code}: {msg}")
    return payload


def _resolve_site_id_sync(url: str, proxy: str = "") -> str:
    site_id = _site_id_from_bigo_url(url)
    if site_id:
        return site_id

    if not is_bigo_url(url):
        raise BigoError("URL is not a BIGO URL")

    page_html = _run_curl(_curl_base(proxy) + [url])
    site_id = _site_id_from_share_html(page_html)
    if not site_id:
        raise BigoError("Could not resolve BIGO siteId from share link")
    return site_id


def _fetch_bigo_info_sync(url: str, proxy: str = "") -> BigoInfo:
    site_id = _resolve_site_id_sync(url, proxy=proxy)
    payload = _fetch_studio_payload(site_id, proxy=proxy)

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise BigoError("BIGO response is missing room data")

    alive_raw = data.get("alive")
    alive = alive_raw in (1, "1", True)
    room_id = str(data.get("roomId") or data.get("clientBigoId") or "")
    hls_src = str(data.get("hls_src") or "").strip()

    # Since August 2026, BIGO can report a live room while withholding hls_src
    # until the caller supplies a fresh one-use integrity token. Retry once
    # with a token instead of falsely treating that response as offline.
    if alive and room_id not in ("", "0") and not hls_src:
        token = _mint_bigo_token_sync(proxy=proxy)
        payload = _fetch_studio_payload(site_id, proxy=proxy, token=token)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise BigoError("BIGO tokenized response is missing room data")
        alive_raw = data.get("alive")
        alive = alive_raw in (1, "1", True)
        room_id = str(data.get("roomId") or data.get("clientBigoId") or "")
        hls_src = str(data.get("hls_src") or "").strip()

    if hls_src:
        parsed_hls = urlparse(hls_src)
        if parsed_hls.scheme not in ("http", "https") or not parsed_hls.hostname:
            raise BigoError("BIGO returned an invalid HLS URL")

    if alive and room_id not in ("", "0") and not hls_src:
        raise BigoError("BIGO reports a live room but withheld the HLS stream URL")

    return BigoInfo(
        site_id=site_id,
        alive=alive,
        display_name=str(data.get("nick_name") or ""),
        stream_title=str(data.get("roomTopic") or ""),
        thumbnail=str(data.get("snapshot") or ""),
        hls_src=hls_src,
        room_id=room_id,
    )


async def fetch_bigo_info(url: str, proxy: str = "") -> dict[str, Any]:
    """Resolve a BIGO URL and fetch normalized metadata without blocking the event loop."""
    info = await asyncio.to_thread(_fetch_bigo_info_sync, url, proxy)
    return info.as_dict()
