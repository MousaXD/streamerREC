import ssl
import unittest

from bigo import BigoError
from bigo_recorder import (
    BigoM3U8Parser,
    TLS12Adapter,
    _ProtectedResponse,
    _rank_variants,
    _open_stream,
)
from unittest.mock import Mock, patch


class DummyResponse:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def iter_content(self, _chunk_size):
        yield from self._chunks


class BigoRecorderTests(unittest.TestCase):
    def test_quality_ranking_prefers_resolution_then_bitrate(self):
        streams = dict.fromkeys(("360p|500k", "720p|2000k", "720p|3000k", "1080p|4000k", "540p|1000k"))
        self.assertEqual(_rank_variants(streams, "best")[0], "1080p|4000k")
        self.assertEqual(_rank_variants(streams, "720p")[:2], ["720p|3000k", "720p|2000k"])
        self.assertEqual(_rank_variants(streams, "480p"), ["360p|500k"])
        self.assertEqual(_rank_variants(streams, "worst")[0], "360p|500k")
        self.assertEqual(_rank_variants({"None|500k": None, "None|1000k": None}, "best")[0], "None|1000k")

    def test_unavailable_highest_rendition_tries_next(self):
        high, low = Mock(), Mock()
        high.open.side_effect = OSError("unavailable")
        with patch("bigo_recorder.BigoHLSStream.parse_variant_playlist", return_value={"720p|2000k": high, "540p|1000k": low}):
            self.assertIs(_open_stream(Mock(), "https://cdn.example.invalid/master.m3u8", "best"), low.open.return_value)
        high.open.assert_called_once()
        low.open.assert_called_once()

    def test_tls_adapter_caps_media_at_tls12(self):
        context = TLS12Adapter().get_ssl_context()
        self.assertEqual(context.maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_parser_attaches_web_protection_seed_to_segment(self):
        parser = BigoM3U8Parser("https://cdn.example.invalid/live/")
        playlist = parser.parse(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:6\n"
            "#EXT-X-MEDIA-SEQUENCE:1\n"
            "#EXT-X-BIGO-WEB-PROTECTION:VERSION=1,SEED=2239971888\n"
            "#EXTINF:6,\n"
            "segment1.ts\n"
        )
        self.assertEqual(len(playlist.segments), 1)
        self.assertEqual(playlist.segments[0].seed, 2239971888)

    def test_protected_response_handles_split_http_chunks(self):
        protected = bytearray(376)
        protected[:16] = bytes.fromhex("ccf42fbfdc1217ee518d9c76139d5ba6")
        response = DummyResponse([
            bytes(protected[:73]),
            bytes(protected[73:241]),
            bytes(protected[241:]) + b"tail",
        ])

        output = b"".join(_ProtectedResponse(response, 2239971888).iter_content(8192))
        self.assertEqual(output[:16], bytes.fromhex("474000100000b00d0001c100000001f0"))
        self.assertTrue(output.endswith(b"tail"))

    def test_unprotected_response_is_passthrough(self):
        response = DummyResponse([b"abc", b"def"])
        output = b"".join(_ProtectedResponse(response, None).iter_content(8192))
        self.assertEqual(output, b"abcdef")

    def test_truncated_protected_segment_fails_closed(self):
        response = DummyResponse([b"x" * 375])
        with self.assertRaises(BigoError):
            b"".join(_ProtectedResponse(response, 1).iter_content(8192))


if __name__ == "__main__":
    unittest.main()
