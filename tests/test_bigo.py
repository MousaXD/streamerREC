import json
import unittest
from unittest.mock import patch

from bigo import (
    BigoError,
    _curl_base,
    _fetch_bigo_info_sync,
    _site_id_from_bigo_url,
    _site_id_from_share_html,
    decrypt_web_protection_prefix,
    is_bigo_url,
)


class BigoUrlTests(unittest.TestCase):
    def test_detects_room_and_share_hosts(self):
        self.assertTrue(is_bigo_url("https://www.bigo.tv/J8023"))
        self.assertTrue(is_bigo_url("https://slink.bigovideo.tv/4ABaKo?sc=4ABaKo"))
        self.assertFalse(is_bigo_url("https://example.com/bigo.tv/J8023"))
        self.assertFalse(is_bigo_url("https://evil.bigovideo.tv/J8023"))

    def test_extracts_site_id_from_canonical_urls(self):
        self.assertEqual(_site_id_from_bigo_url("https://www.bigo.tv/J8023"), "J8023")
        self.assertEqual(_site_id_from_bigo_url("https://www.bigo.tv/cn/716418802"), "716418802")
        self.assertEqual(
            _site_id_from_bigo_url("https://www.bigo.tv/cn/anything?h=716418802"),
            "716418802",
        )

    def test_extracts_site_id_from_share_metadata(self):
        page = (
            '<html><head><meta data-n-head="ssr" data-hid="al:web:url" '
            'property="al:web:url" '
            'content="https://www.bigo.tv/cn/share?foo=1&amp;h=716418802">'
            '</head></html>'
        )
        self.assertEqual(_site_id_from_share_html(page), "716418802")

    def test_extracts_site_id_from_json_fallback(self):
        self.assertEqual(_site_id_from_share_html('{"siteId":"J8023"}'), "J8023")

    def test_rejects_invalid_site_id(self):
        self.assertEqual(_site_id_from_bigo_url("https://www.bigo.tv/%2Fetc%2Fpasswd"), "")


class BigoTransportTests(unittest.TestCase):
    def test_curl_is_https_only_tls12_and_does_not_follow_redirects(self):
        cmd = _curl_base()
        self.assertIn("--proto", cmd)
        self.assertIn("=https", cmd)
        self.assertIn("--tls-max", cmd)
        self.assertIn("1.2", cmd)
        self.assertNotIn("-L", cmd)
        self.assertNotIn("--location", cmd)

    @patch("bigo._resolve_site_id_sync", return_value="J8023")
    @patch("bigo._run_curl")
    def test_alive_flag_is_authoritative_even_with_stale_hls(self, run_curl, _resolve):
        run_curl.return_value = json.dumps({
            "code": 0,
            "data": {
                "alive": 0,
                "hls_src": "https://example.invalid/stale.m3u8",
                "nick_name": "offline",
                "roomTopic": "",
            },
        })
        info = _fetch_bigo_info_sync("https://www.bigo.tv/J8023")
        self.assertFalse(info.alive)
        self.assertEqual(info.hls_src, "https://example.invalid/stale.m3u8")

    @patch("bigo._resolve_site_id_sync", return_value="J8023")
    @patch("bigo._run_curl")
    def test_live_room_requires_valid_http_hls_url(self, run_curl, _resolve):
        run_curl.return_value = json.dumps({
            "code": 0,
            "data": {"alive": 1, "hls_src": "javascript:alert(1)"},
        })
        with self.assertRaises(BigoError):
            _fetch_bigo_info_sync("https://www.bigo.tv/J8023")

    @patch("bigo._resolve_site_id_sync", return_value="J8023")
    @patch("bigo._run_curl")
    def test_live_room_normalization(self, run_curl, _resolve):
        run_curl.return_value = json.dumps({
            "code": 0,
            "data": {
                "alive": 1,
                "hls_src": "https://cdn.example.invalid/live.m3u8",
                "nick_name": "Example",
                "roomTopic": "Live now",
                "roomId": "123",
            },
        })
        info = _fetch_bigo_info_sync("https://www.bigo.tv/J8023")
        self.assertTrue(info.alive)
        self.assertEqual(info.site_id, "J8023")
        self.assertEqual(info.display_name, "Example")
        self.assertEqual(info.stream_title, "Live now")
        self.assertEqual(info.room_id, "123")


class BigoProtectionTests(unittest.TestCase):
    def test_known_streamlink_protected_packet_vector(self):
        # First packet prefix from Streamlink's August 2026 BIGO protection
        # regression fixture. The expected bytes are a valid MPEG-TS PAT header.
        packets = bytearray(376)
        packets[:16] = bytes.fromhex("ccf42fbfdc1217ee518d9c76139d5ba6")
        decrypt_web_protection_prefix(packets, 2239971888)
        self.assertEqual(
            packets[:16],
            bytes.fromhex("474000100000b00d0001c100000001f0"),
        )

    def test_protection_requires_two_full_ts_packets(self):
        with self.assertRaises(BigoError):
            decrypt_web_protection_prefix(bytearray(375), 1)


if __name__ == "__main__":
    unittest.main()
