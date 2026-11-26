"""BIGO HLS recorder for StreamRec.

This helper uses Streamlink's stable HLS engine plus BIGO's current web-
protection parser behavior. It is intentionally a separate subprocess so the
existing StreamRec process lifecycle, stop/kill semantics, and yt-dlp flow for
all other platforms stay unchanged.
"""

from __future__ import annotations

import argparse
from http.cookiejar import MozillaCookieJar
import re
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from bigo import BIGO_USER_AGENT, BigoError, decrypt_web_protection_prefix
from streamlink import Streamlink
from streamlink.exceptions import StreamError
from streamlink.session.http import SSLContextAdapter
from streamlink.stream.hls import (
    HLSSegment,
    HLSStream,
    HLSStreamReader,
    HLSStreamWriter,
    M3U8Parser,
    parse_tag,
)
from streamlink.stream.hls.m3u8 import HLSPlaylist

if TYPE_CHECKING:
    from collections.abc import Iterator
    from requests import Response


class TLS12Adapter(SSLContextAdapter):
    """Limit BIGO media requests to TLS 1.2 without affecting other platforms."""

    def get_ssl_context(self) -> ssl.SSLContext:
        context = super().get_ssl_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        return context


class _ProtectedResponse:
    """Wrap a requests response and decrypt BIGO's protected TS packet prefix."""

    def __init__(self, response: Response, seed: int | None):
        self._response = response
        self._seed = seed

    def __getattr__(self, name):
        return getattr(self._response, name)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        iterator = self._response.iter_content(chunk_size)
        if self._seed is None:
            yield from iterator
            return

        prefix = bytearray()
        remainder = b""

        for chunk in iterator:
            needed = 376 - len(prefix)
            if len(chunk) <= needed:
                prefix.extend(chunk)
                if len(prefix) == 376:
                    break
            else:
                prefix.extend(chunk[:needed])
                remainder = chunk[needed:]
                break

        if len(prefix) < 376:
            # A protected segment without two full TS packets cannot be
            # decrypted correctly. Fail so StreamRec marks the capture as an
            # error and retries instead of silently saving corrupted bytes.
            raise BigoError("truncated protected BIGO HLS segment")

        decrypt_web_protection_prefix(prefix, self._seed)
        yield bytes(prefix)
        if remainder:
            yield remainder
        yield from iterator


@dataclass(kw_only=True)
class BigoHLSSegment(HLSSegment):
    seed: int | None = None


class BigoHLSPlaylist(HLSPlaylist):
    def get_name_pixels(self, with_framerate=None):
        # Quality labels describe the short edge (e.g. 540x960 is 540p),
        # not the long edge of a portrait video.
        resolution = self.stream_info.resolution
        if resolution and resolution.width and resolution.height:
            return f"{min(resolution.width, resolution.height)}p"
        return None


class BigoM3U8Parser(M3U8Parser):
    __segment__ = BigoHLSSegment
    __playlist__ = BigoHLSPlaylist

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seed: int | None = None

    @parse_tag("EXT-X-BIGO-WEB-PROTECTION")
    def parse_bigo_web_protection(self, value):
        attributes = self.parse_attributes(value)
        raw_seed = attributes.get("SEED")
        try:
            self.seed = int(raw_seed) if raw_seed is not None else None
        except (TypeError, ValueError):
            self.seed = None

    def get_segment(self, uri: str, **data):
        return super().get_segment(uri, seed=self.seed, **data)


class BigoHLSStreamWriter(HLSStreamWriter):
    def _write(self, segment: BigoHLSSegment, response: Response, is_map: bool):
        wrapped = _ProtectedResponse(response, None if is_map else segment.seed)
        return super()._write(segment, wrapped, is_map)


class BigoHLSStreamReader(HLSStreamReader):
    __writer__ = BigoHLSStreamWriter


class BigoHLSStream(HLSStream):
    __reader__ = BigoHLSStreamReader
    __parser__ = BigoM3U8Parser


def _rank_variants(streams: dict, quality: str) -> list[str]:
    """Rank advertised renditions by short-edge resolution, then bitrate."""
    def score(name):
        pixels, _, bitrate = name.partition("|")
        height_match = re.fullmatch(r"(\d+)p", pixels)
        bitrate_match = re.match(r"([\d.]+)k", bitrate)
        return (
            int(height_match[1]) if height_match else 0,
            float(bitrate_match[1]) if bitrate_match else 0,
        )

    ranked = sorted(streams, key=score, reverse=True)
    if quality == "worst":
        return list(reversed(ranked))
    cap = re.fullmatch(r"(\d+)p?", quality)
    if cap:
        allowed = [name for name in ranked if 0 < score(name)[0] <= int(cap[1])]
        if allowed:
            return allowed
        known = [name for name in ranked if score(name)[0]]
        if known:
            return list(reversed(known))
    return ranked


def _open_stream(session: Streamlink, url: str, quality: str):
    # Use Streamlink's master-playlist implementation, including external
    # audio handling. Keep our BIGO parser for the actual media segments.
    variants = BigoHLSStream.parse_variant_playlist(
        session, url, name_fmt="{pixels}|{bitrate}",
    )
    if not variants:
        print("[BIGO] Single source supplied; no selectable quality renditions", flush=True)
        return BigoHLSStream(session, url).open()
    error = None
    for selected in _rank_variants(variants, quality):
        try:
            reader = variants[selected].open()
        except (StreamError, OSError) as exc:
            error = exc
            print(f"[BIGO] Advertised rendition {selected} unavailable; trying next", flush=True)
            continue
        print(f"[BIGO] Selected advertised rendition {selected} ({len(variants)} available)", flush=True)
        return reader
    raise BigoError("No advertised BIGO rendition could be opened") from error


def record(url: str, output: Path, proxy: str = "", cookies_file: str = "", quality: str = "best") -> int:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BigoError("BIGO media URL must be a valid HTTP or HTTPS URL")

    output.parent.mkdir(parents=True, exist_ok=True)

    session = Streamlink()
    if cookies_file:
        jar = MozillaCookieJar(cookies_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        # Netscape 0 denotes a session cookie; MozillaCookieJar otherwise
        # interprets it as an expired Unix timestamp.
        for cookie in list(jar):
            if cookie.expires == 0:
                cookie.expires = None
                cookie.discard = True
            elif cookie.is_expired():
                jar.clear(cookie.domain, cookie.path, cookie.name)
        session.http.cookies.update(jar)
    session.http.mount("https://", TLS12Adapter())
    session.set_option("http-headers", {
        "User-Agent": BIGO_USER_AGENT,
        "Referer": "https://www.bigo.tv/",
        "Origin": "https://www.bigo.tv",
    })
    session.set_option("stream-segment-attempts", 5)
    session.set_option("stream-segment-timeout", 20.0)
    session.set_option("stream-timeout", 45.0)
    session.set_option("hls-live-edge", 3)
    if proxy:
        session.set_option("http-proxy", proxy)

    reader = _open_stream(session, url, quality)

    total = 0
    last_report = time.monotonic()
    try:
        with output.open("wb", buffering=0) as destination:
            while True:
                chunk = reader.read(256 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
                total += len(chunk)

                now = time.monotonic()
                if now - last_report >= 5:
                    print(f"[BIGO] downloaded {total} bytes", flush=True)
                    last_report = now
    finally:
        try:
            reader.close()
        except Exception:
            pass

    print(f"[BIGO] finished with {total} bytes", flush=True)
    return 0 if total > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--cookies-file", default="")
    parser.add_argument("--quality", default="best")
    args = parser.parse_args()

    try:
        return record(args.url, Path(args.output), args.proxy, args.cookies_file, args.quality)
    except Exception as exc:
        print(f"[BIGO] recorder error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
