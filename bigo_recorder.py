"""BIGO HLS recorder for StreamRec.

This helper uses Streamlink's stable HLS engine plus BIGO's current web-
protection parser behavior. It is intentionally a separate subprocess so the
existing StreamRec process lifecycle, stop/kill semantics, and yt-dlp flow for
all other platforms stay unchanged.
"""

from __future__ import annotations

import argparse
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from bigo import BigoError, decrypt_web_protection_prefix
from streamlink import Streamlink
from streamlink.session.http import SSLContextAdapter
from streamlink.stream.hls import (
    HLSSegment,
    HLSStream,
    HLSStreamReader,
    HLSStreamWriter,
    M3U8Parser,
    parse_tag,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from requests import Response


class TLS12Adapter(SSLContextAdapter):
    """Limit BIGO media requests to TLS 1.2 without affecting other platforms."""

    def get_ssl_context(self) -> ssl.SSLContext:
        context = super().get_ssl_context()
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


class BigoM3U8Parser(M3U8Parser):
    __segment__ = BigoHLSSegment

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


def record(url: str, output: Path, proxy: str = "") -> int:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BigoError("BIGO media URL must be a valid HTTP or HTTPS URL")

    output.parent.mkdir(parents=True, exist_ok=True)

    session = Streamlink()
    session.http.mount("https://", TLS12Adapter())
    session.set_option("stream-segment-attempts", 5)
    session.set_option("stream-segment-timeout", 20.0)
    session.set_option("stream-timeout", 45.0)
    session.set_option("hls-live-edge", 3)
    if proxy:
        session.set_option("http-proxy", proxy)

    stream = BigoHLSStream(session, url)
    reader = stream.open()

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
    args = parser.parse_args()

    try:
        return record(args.url, Path(args.output), args.proxy)
    except Exception as exc:
        print(f"[BIGO] recorder error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
