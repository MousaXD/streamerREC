"""Native BIGO Live helpers for StreamRec.

BIGO share links on slink.bigovideo.tv are not handled reliably by yt-dlp.
This module resolves both normal BIGO room URLs and share links, queries BIGO's
studio-info endpoint, and caps BIGO HTTPS requests at TLS 1.2 only. Some
networks can establish TCP to BIGO but stall during TLS 1.3; scoping the
workaround to BIGO avoids weakening TLS for other platforms.
"""

from __future__ import annotations

import asyncio
import ctypes
import html as html_lib
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


BIGO_API = "https://ta.bigo.tv/official_website/studio/getInternalStudioInfo"
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
        or host == "bigovideo.tv"
        or host.endswith(".bigovideo.tv")
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

    cmd = _curl_base(proxy) + [
        "-H",
        "Accept: application/json",
        "-H",
        "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        "-H",
        "Referer: https://www.bigo.tv/",
        "--data-urlencode",
        f"siteId={site_id}",
        BIGO_API,
    ]
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

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise BigoError("BIGO response is missing room data")

    hls_src = str(data.get("hls_src") or "").strip()
    if hls_src:
        parsed_hls = urlparse(hls_src)
        if parsed_hls.scheme not in ("http", "https") or not parsed_hls.hostname:
            raise BigoError("BIGO returned an invalid HLS URL")

    alive_raw = data.get("alive")
    alive = alive_raw in (1, "1", True)

    return BigoInfo(
        site_id=site_id,
        alive=alive,
        display_name=str(data.get("nick_name") or ""),
        stream_title=str(data.get("roomTopic") or ""),
        thumbnail=str(data.get("snapshot") or ""),
        hls_src=hls_src,
        room_id=str(data.get("roomId") or data.get("clientBigoId") or ""),
    )


async def fetch_bigo_info(url: str, proxy: str = "") -> dict[str, Any]:
    """Resolve a BIGO URL and fetch normalized metadata without blocking the event loop."""
    info = await asyncio.to_thread(_fetch_bigo_info_sync, url, proxy)
    return info.as_dict()
