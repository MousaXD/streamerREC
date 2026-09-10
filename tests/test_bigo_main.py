import unittest
from unittest.mock import AsyncMock, patch

import main
from bigo import BigoError


class BigoMainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bigo_protocol_error_propagates_from_live_check(self):
        with patch(
            "main.fetch_bigo_info",
            AsyncMock(side_effect=BigoError("integrity token failed")),
        ):
            with self.assertRaises(BigoError):
                await main.check_is_live("https://www.bigo.tv/J8023")

    async def test_explicit_offline_bigo_result_returns_false(self):
        with patch(
            "main.fetch_bigo_info",
            AsyncMock(return_value={"alive": False, "hls_src": ""}),
        ):
            self.assertFalse(
                await main.check_is_live("https://www.bigo.tv/J8023")
            )

    async def test_playable_live_bigo_result_returns_true(self):
        with patch(
            "main.fetch_bigo_info",
            AsyncMock(
                return_value={
                    "alive": True,
                    "hls_src": "https://cdn.example.invalid/live.m3u8",
                }
            ),
        ):
            self.assertTrue(
                await main.check_is_live("https://www.bigo.tv/J8023")
            )


    async def test_refresh_preserves_live_state_on_bigo_lookup_error(self):
        ch_id = "auditbigo"
        previous = main.channels.get(ch_id)
        main.channels[ch_id] = {
            "id": ch_id,
            "url": "https://www.bigo.tv/J8023",
            "platform": "Bigo",
            "proxy": "socks5://proxy.example:1080",
            "display_name": "Example",
            "username": "J8023",
            "avatar": "",
            "thumbnail": "",
            "stream_title": "Existing title",
            "is_live": True,
        }
        try:
            with (
                patch(
                    "main.fetch_metadata",
                    AsyncMock(
                        return_value={
                            "display_name": "Example",
                            "_lookup_error": True,
                        }
                    ),
                ) as fetch_metadata,
                patch("main._save_state"),
            ):
                result = await main.refresh_channel(ch_id)

            self.assertTrue(result["is_live"])
            self.assertEqual(result["stream_title"], "Existing title")
            fetch_metadata.assert_awaited_once_with(
                "https://www.bigo.tv/J8023",
                proxy="socks5://proxy.example:1080",
            )
        finally:
            if previous is None:
                main.channels.pop(ch_id, None)
            else:
                main.channels[ch_id] = previous


if __name__ == "__main__":
    unittest.main()
