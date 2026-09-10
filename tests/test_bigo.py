import json
import unittest
from unittest.mock import patch

from bigo import (
    BIGO_API,
    BigoError,
    _curl_base,
    _evp_bytes_to_key,
    _fetch_bigo_info_sync,
    _fetch_studio_payload,
    _mint_bigo_token_sync,
    _parse_jsonp,
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
        cmd = run_curl.call_args.args[0]
        self.assertIn("-X", cmd)
        self.assertIn("POST", cmd)
        self.assertEqual(cmd[-1], BIGO_API)
        self.assertIn("--data-urlencode", cmd)
        self.assertIn("siteId=J8023", cmd)
        self.assertIn("verify=", cmd)

    @patch("bigo._resolve_site_id_sync", return_value="J8023")
    @patch("bigo._mint_bigo_token_sync", return_value="fresh-token")
    @patch("bigo._fetch_studio_payload")
    def test_live_room_without_source_retries_once_with_fresh_token(
        self, fetch_studio, mint_token, _resolve
    ):
        fetch_studio.side_effect = [
            {
                "code": 0,
                "data": {
                    "alive": 1,
                    "roomId": "123",
                    "hls_src": "",
                    "nick_name": "Example",
                },
            },
            {
                "code": 0,
                "data": {
                    "alive": 1,
                    "roomId": "123",
                    "hls_src": "https://cdn.example.invalid/live.m3u8",
                    "nick_name": "Example",
                },
            },
        ]

        info = _fetch_bigo_info_sync("https://www.bigo.tv/J8023")

        self.assertTrue(info.alive)
        self.assertEqual(info.hls_src, "https://cdn.example.invalid/live.m3u8")
        mint_token.assert_called_once_with(proxy="")
        self.assertEqual(fetch_studio.call_count, 2)
        self.assertEqual(fetch_studio.call_args_list[1].kwargs["token"], "fresh-token")

    @patch("bigo._resolve_site_id_sync", return_value="J8023")
    @patch("bigo._mint_bigo_token_sync", return_value="fresh-token")
    @patch("bigo._fetch_studio_payload")
    def test_live_room_still_without_source_fails_instead_of_false_offline(
        self, fetch_studio, _mint_token, _resolve
    ):
        fetch_studio.return_value = {
            "code": 0,
            "data": {"alive": 1, "roomId": "123", "hls_src": ""},
        }
        with self.assertRaises(BigoError):
            _fetch_bigo_info_sync("https://www.bigo.tv/J8023")

    def test_jsonp_parser_accepts_callback_wrapper(self):
        payload = _parse_jsonp('cb({"code":0,"time":"123"});')
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["time"], "123")

    def test_evp_bytes_to_key_matches_known_vector(self):
        key, iv = _evp_bytes_to_key(b"undefinedval0x01", b"12345678")
        self.assertEqual(
            key.hex(),
            "2e8659e5b6d14b258d16d7a5cee673c01ff7c35ada1cef113c96b315115b26a9",
        )
        self.assertEqual(iv.hex(), "b5bc371c83633a20302cb2972f961721")

    @patch("bigo._run_curl")
    def test_tokenized_studio_request_uses_form_fields(self, run_curl):
        run_curl.return_value = '{"code":0,"data":{"alive":1,"hls_src":"https://cdn.invalid/live.m3u8"}}'
        _fetch_studio_payload("J8023", token="token-abc")
        cmd = run_curl.call_args.args[0]
        self.assertEqual(cmd[-1], BIGO_API)
        self.assertIn("siteId=J8023", cmd)
        self.assertIn("verify=", cmd)
        self.assertIn("token=token-abc", cmd)
        self.assertIn("supportHevc=1", cmd)

    @patch("bigo._run_curl")
    def test_integrity_token_mint_uses_server_time_then_status(self, run_curl):
        run_curl.side_effect = [
            'jsonpcallback_1({"code":0,"time":"12345"});',
            'jsonpcallback_2({"code":0,"token":"token-abc"});',
        ]
        token = _mint_bigo_token_sync()
        self.assertEqual(token, "token-abc")
        self.assertEqual(run_curl.call_count, 2)
        self.assertTrue(any("sec.bigo.sg/v1/webjs/t" in arg for arg in run_curl.call_args_list[0].args[0]))
        self.assertTrue(any("sec.bigo.sg/v1/webjs/status" in arg for arg in run_curl.call_args_list[1].args[0]))


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
