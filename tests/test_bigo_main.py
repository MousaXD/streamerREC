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


if __name__ == "__main__":
    unittest.main()
