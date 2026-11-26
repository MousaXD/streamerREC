import functools
import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from bigo import BigoError, BigoLoginRequired


class BigoMainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_hands_off_fresh_resolution_without_persisting_source(self):
        url = 'https://www.bigo.tv/fixture'
        ch = {'id': 'channel', 'url': url, 'platform': 'Bigo', 'is_live': False, 'monitoring': True}
        info = {'alive': True, 'hls_src': 'https://cdn.invalid/live.m3u8?private=FIXTURE'}
        with patch.object(main, 'channels', {'channel': ch}), patch.object(main, 'recordings', {}), patch.object(main, 'HARDENED', False), patch('main._bigo_info', AsyncMock(return_value=info)), patch('main._start_recording_for_channel', AsyncMock(return_value=None)) as start:
            await main._check_one_channel('channel')
            resolved = start.call_args.kwargs['bigo_resolution']
            self.assertTrue(main._usable_bigo_resolution(resolved, url, '', ''))
            self.assertFalse(main._usable_bigo_resolution(resolved, url + 'other', '', ''))
            self.assertFalse(main._usable_bigo_resolution(resolved, url, 'http://other.invalid', ''))
            with patch('main.time.monotonic', return_value=resolved.observed_at + 21):
                self.assertFalse(main._usable_bigo_resolution(resolved, url, '', ''))
            self.assertNotIn('FIXTURE', json.dumps(ch))
            self.assertNotIn('FIXTURE', repr(resolved))

    async def test_session_change_invalidates_handoff(self):
        url = 'https://www.bigo.tv/fixture'
        with tempfile.TemporaryDirectory() as tmp:
            cookie = Path(tmp) / 'session.txt'
            cookie.write_text('first session')
            resolved = main._bigo_resolution(url, '', str(cookie), {'alive': True, 'hls_src': 'https://cdn.invalid/live.m3u8'})
            self.assertTrue(main._usable_bigo_resolution(resolved, url, '', str(cookie)))
            cookie.write_text('different session')
            self.assertFalse(main._usable_bigo_resolution(resolved, url, '', str(cookie)))
            self.assertFalse(main._usable_bigo_resolution(resolved, url, '', ''))

    async def test_real_bigo_hls_capture_remux_with_fresh_resolution(self):
        # Real native BIGO subprocess, HLS network reader, MPEG-TS media,
        # and ffmpeg remux. The local playlist is a synthetic test fixture.
        url = 'https://www.bigo.tv/fixture'
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'media'
            media.mkdir()
            output = root / 'recordings'
            output.mkdir()
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=10',
                '-t', '3', '-c:v', 'libx264', '-threads', '1', '-g', '10',
                '-hls_time', '1', '-hls_list_size', '0', '-f', 'hls', str(media / 'fixture.m3u8')],
                check=True, timeout=15)
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(media)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                source = f'http://127.0.0.1:{server.server_port}/fixture.m3u8'
                resolved = main._bigo_resolution(url, '', '', {'alive': True, 'hls_src': source, 'site_id': 'fixture'})
                ch = {'id': 'channel', 'url': url, 'platform': 'Bigo', 'display_name': 'Fixture', 'recording_id': 'rec', 'monitoring': False}
                rec = {'id': 'rec', 'channel_id': 'channel', 'url': url, 'platform': 'Bigo', 'quality': 'best', 'format': 'mp4', 'status': 'starting', 'created_at': time.time(), 'bytes': 0, 'log': [], 'viewer_samples': []}
                with patch.object(main, 'channels', {'channel': ch}), patch.object(main, 'recordings', {'rec': rec}), patch.object(main, 'RECORDINGS_DIR', output), patch.object(main, 'HARDENED', False), patch.dict(main.settings, {'proxy': '', 'auto_retry': False, 'auto_convert_mp4': False, 'webhook_url': ''}), patch('main._save_state'), patch('main._bigo_info', AsyncMock(side_effect=AssertionError('Duplicate lookup'))) as lookup:
                    await main._run_recording('rec', bigo_resolution=resolved)
                    lookup.assert_not_awaited()
                self.assertEqual(rec['status'], 'completed', rec['log'])
                self.assertTrue(rec['filepath'].endswith('.mp4'))
                self.assertGreater(Path(rec['filepath']).stat().st_size, 0)
                self.assertAlmostEqual(rec['duration_seconds'], 3, places=1)
                result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,codec_name', '-of', 'json', rec['filepath']], check=True, capture_output=True, timeout=15)
                self.assertIn({'codec_name': 'h264', 'codec_type': 'video'}, json.loads(result.stdout)['streams'])
                self.assertNotIn(source, json.dumps(rec))
                self.assertIsNone(ch['recording_id'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

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
