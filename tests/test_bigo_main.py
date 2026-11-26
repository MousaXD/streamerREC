import unittest
from unittest.mock import AsyncMock, patch

import main
from bigo import BigoError, BigoLoginRequired


class BigoMainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_required_metadata_is_unknown_and_actionable(self):
        with patch('main.fetch_bigo_info', AsyncMock(side_effect=BigoLoginRequired('login'))):
            info = await main.fetch_metadata('https://www.bigo.tv/m4xibby')
        self.assertTrue(info['_lookup_error'])
        self.assertEqual(info['live_check_status'], 'unknown')
        self.assertIn('Connect your BIGO account', info['live_check_message'])

    async def test_record_startup_distinguishes_offline_and_login_required(self):
        url = 'https://www.bigo.tv/m4xibby'
        for answer, expected in [({'alive': False, 'hls_src': ''}, 'offline'), (BigoLoginRequired('login'), 'unknown'), ({'alive': True, 'hls_src': ''}, 'unknown')]:
            with self.subTest(expected=expected, answer=answer):
                ch = {'id': 'channel', 'url': url, 'platform': 'Bigo', 'recording_id': 'rec', 'is_live': True, 'monitoring': True}
                rec = {'id': 'rec', 'channel_id': 'channel', 'url': url, 'platform': 'Bigo', 'status': 'starting'}
                fetch = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer)
                with patch.object(main, 'channels', {'channel': ch}), patch.object(main, 'recordings', {'rec': rec}), patch('main._bigo_info', fetch), patch('main._save_state'), patch('main.asyncio.create_subprocess_exec', AsyncMock()) as spawn:
                    await main._run_recording('rec')
                    spawn.assert_not_awaited()
                self.assertEqual(rec['status'], 'error')
                self.assertEqual(ch['live_check_status'], expected)
                self.assertEqual(rec['live_check_status'], expected)
                self.assertEqual(ch['is_live'], expected == 'unknown')
                self.assertIsNone(ch['recording_id'])
                self.assertTrue(ch['monitoring'])
                self.assertEqual(rec['capture_engine'], 'bigo')

    async def test_monitor_retains_login_required_message(self):
        ch = {'id': 'channel', 'url': 'https://www.bigo.tv/m4xibby', 'is_live': True, 'monitoring': True}
        with patch.object(main, 'channels', {'channel': ch}), patch.object(main, 'recordings', {}), patch('main.probe_live', AsyncMock(side_effect=BigoLoginRequired('login'))), patch('main._start_recording_for_channel', AsyncMock()) as start:
            await main._check_one_channel('channel')
            start.assert_not_awaited()
        self.assertEqual(ch['live_check_status'], 'unknown')
        self.assertIn('Connect your BIGO account', ch['live_check_message'])
        self.assertTrue(ch['is_live'])

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
