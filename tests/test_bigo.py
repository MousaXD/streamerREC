import unittest

from bigo import _site_id_from_bigo_url, _site_id_from_share_html, is_bigo_url


class BigoUrlTests(unittest.TestCase):
    def test_detects_room_and_share_hosts(self):
        self.assertTrue(is_bigo_url("https://www.bigo.tv/J8023"))
        self.assertTrue(is_bigo_url("https://slink.bigovideo.tv/4ABaKo?sc=4ABaKo"))
        self.assertFalse(is_bigo_url("https://example.com/bigo.tv/J8023"))

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


if __name__ == "__main__":
    unittest.main()
