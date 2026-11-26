import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from network_policy import allowed_host, approved_url


class DestinationPolicyTests(unittest.TestCase):
    def test_allowed_stream_hosts(self):
        for host in ('www.tiktok.com', 'pull-hls-f16-tt01.tiktokcdn.com', 'www.bigo.tv', 'slink.bigovideo.tv'):
            self.assertTrue(allowed_host(host), host)

    def test_unapproved_hosts_and_ports(self):
        for url in ('https://example.com', 'https://tiktok.com.evil.example/@abc/live', 'https://127.0.0.1/', 'https://169.254.169.254/', 'https://www.tiktok.com:8443/@abc/live', 'https://user:password@www.tiktok.com/@abc/live', 'file:///etc/passwd'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                approved_url(url)

    def test_tiktok_profile_normalizes_to_live_only(self):
        self.assertEqual(approved_url('https://www.tiktok.com/@abc'), 'https://www.tiktok.com/@abc/live')
        self.assertEqual(approved_url('https://www.tiktok.com/@abc/live?x=1'), 'https://www.tiktok.com/@abc/live')
        with self.assertRaises(ValueError):
            approved_url('https://www.tiktok.com/@abc/video/123')

    def test_hardened_disallows_custom_network_and_cli_options(self):
        with patch.object(main, 'HARDENED', True):
            for field in ('proxy', 'webhook_url', 'extra_args'):
                with self.subTest(field=field), self.assertRaises(main.HTTPException):
                    main._validate_hardened_options({field: 'untrusted'})

    def test_cookie_and_media_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'recordings'; root.mkdir()
            cookies = root / 'cookies'; cookies.mkdir()
            media = root / 'TikTok' / 'video.mp4'; media.parent.mkdir(); media.touch()
            outside = Path(directory) / 'outside.mp4'; outside.touch()
            escape = root / 'escape.mp4'; escape.symlink_to(outside)
            with patch.object(main, 'RECORDINGS_DIR', root), patch.object(main, 'COOKIES_DIR', cookies):
                self.assertEqual(main._media_path(str(media)), media)
                for path in (str(outside), str(escape), str(root / 'account.json'), str(cookies / 'video.mp4')):
                    with self.assertRaises(ValueError):
                        main._media_path(path)
                for name in ('../account.json', '/etc/passwd', 'not-a-cookie.json'):
                    with self.assertRaises(ValueError):
                        main._cookie_path(name)


class RecorderRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_check_rejects_regular_video(self):
        for data, expected in (({'is_live': False}, False), ({'live_status': 'is_live'}, True)):
            proc = AsyncMock(); proc.returncode = 0; proc.communicate.return_value = (json.dumps(data).encode(), b'')
            with patch.object(main, 'HARDENED', True), patch.object(main, 'EGRESS_PROXY', 'http://egress:3128'), patch('main.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)) as create:
                self.assertEqual(await main.check_is_live('https://www.tiktok.com/@abc/live'), expected)
                self.assertIn('http://egress:3128', create.call_args.args)

    async def test_startup_failure_clears_recording_pointer(self):
        rec = {'id': 'test', 'channel_id': 'channel', 'status': 'starting', 'log': []}
        channel = {'recording_id': 'test'}
        with patch.object(main, 'recordings', {'test': rec}), patch.object(main, 'channels', {'channel': channel}), patch('main._run_recording', AsyncMock(side_effect=PermissionError('denied'))), patch('main._save_state'):
            await main.run_recording('test')
            self.assertEqual(rec['status'], 'error')
            self.assertIsNone(channel['recording_id'])
            self.assertIn('denied', rec['log'][0])
