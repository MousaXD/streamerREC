import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main

URL = 'https://www.tiktok.com/@fixture/live'


class StreamlinkFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_failure_uses_secondary(self):
        proc = AsyncMock(); proc.returncode = 1
        proc.communicate.return_value = (b'null', b'ERROR: The channel is not currently live')
        secondary = {'is_live': True, 'viewer_count': None, 'capture_engine': 'streamlink', 'live_check_status': 'live', 'observed_at': 123}
        with patch('main.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)), patch('main._probe_tiktok_streamlink', AsyncMock(return_value=secondary)) as fallback:
            r = await main.probe_live(URL)
            self.assertEqual(r, secondary)
            fallback.assert_awaited_once()
            r = await main.probe_live(URL, allow_fallback=False)
            self.assertEqual(r['live_check_status'], 'unknown')
            self.assertEqual(fallback.await_count, 1)

    async def test_good_primary_not_replaced(self):
        for info in ({'is_live': True, 'concurrent_view_count': 41}, {'is_live': False}):
            proc = AsyncMock(); proc.returncode = 0; proc.communicate.return_value = (json.dumps(info).encode(), b'')
            with patch('main.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)), patch('main._probe_tiktok_streamlink', AsyncMock()) as fallback:
                r = await main.probe_live(URL)
                self.assertEqual(r['is_live'], info['is_live'])
                fallback.assert_not_awaited()

    async def test_plugin_result_must_contain_video_and_never_leak_signed_url(self):
        proc=AsyncMock();proc.returncode=0
        for streams,expected in [({'h264_ao':{},'best':{}},False),({'h264_hd':{'type':'http','url':'https://cdn.example/live?private=SECRET'},'best':{}},True)]:
            proc.communicate.return_value=(json.dumps({'plugin':'tiktok','streams':streams,'metadata':{'title':'Public fixture'}}).encode(),b'')
            with patch('main.asyncio.create_subprocess_exec',AsyncMock(return_value=proc)) as execute,patch.object(main,'HARDENED',True),patch.object(main,'EGRESS_PROXY','http://egress:3128'):
                r=await main._probe_tiktok_streamlink(URL,'http://untrusted.example','/tmp/private/cookies.txt')
                self.assertEqual(bool(r),expected)
                self.assertIn('--no-plugin-sideloading',execute.call_args.args)
                self.assertIn('http://egress:3128',execute.call_args.args)
                self.assertIn('/tmp/private/cookies.txt',execute.call_args.args)
                if r:
                    self.assertEqual(r['viewer_count'],None)
                    self.assertNotIn('SECRET',str(r))

    async def test_real_local_flv_to_mp4_using_selected_fallback(self):
        # A test-only executable writes real media, not network data. The
        # production plugin was also separately tested against a public live.
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); bin_dir=root/'bin';bin_dir.mkdir(); recordings_dir=root/'recordings';recordings_dir.mkdir()
            exe=bin_dir/'streamlink'; exe.write_text('''#!/usr/local/bin/python
import subprocess,sys
args=sys.argv[1:]; assert '--no-config' in args and '--no-plugin-sideloading' in args
assert args[args.index('--http-proxy')+1]=='http://egress:3128'
out=args[args.index('--output')+1]; assert out.endswith('.flv')
raise SystemExit(subprocess.call(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=64x64:r=5','-t','3','-c:v','libx264','-threads','1','-f','flv',out]))
''');exe.chmod(0o755)
            ch={'id':'testch','url':URL,'platform':'TikTok','display_name':'Fixture','recording_id':'testrec','capture_engine':'yt-dlp','engine_checked_at':time.time(),'monitoring':False}
            rec={'id':'testrec','channel_id':'testch','url':URL,'platform':'TikTok','quality':'best','format':'mp4','status':'starting','created_at':time.time(),'bytes':0,'log':[],'viewer_samples':[]}
            with patch.object(main,'channels',{'testch':ch}),patch.object(main,'recordings',{'testrec':rec}),patch.object(main,'RECORDINGS_DIR',recordings_dir),patch.object(main,'HARDENED',True),patch.object(main,'EGRESS_PROXY','http://egress:3128'),patch.dict(main.settings,{'auto_retry':False,'auto_convert_mp4':False,'webhook_url':''}),patch.dict(os.environ,{'PATH':str(bin_dir)+':'+os.environ['PATH']}),patch('main._save_state'),patch('main._probe_tiktok_streamlink',AsyncMock(return_value={'is_live':True,'capture_engine':'streamlink'})) as choose:
                await main._run_recording('testrec')
                self.assertEqual(rec['status'],'completed',rec.get('log'))
                choose.assert_awaited_once()
                self.assertEqual(rec['capture_engine'],'streamlink')
                self.assertTrue(rec['filepath'].endswith('.mp4'))
                self.assertTrue(Path(rec['filepath']).stat().st_size>0)
                self.assertAlmostEqual(rec['duration_seconds'],3,places=1)
                self.assertEqual(list(recordings_dir.rglob('*.flv')),[])
                self.assertIsNone(ch['recording_id'])
