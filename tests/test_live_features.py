import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from live_features import (add_viewer_sample, channel_url, clip_samples, parse_netscape_cookies,
                           platform_key, scoped_cookie_text, viewer_count, viewer_summary)


def test_cookie(domain, name='session', value='fixture-value', **kwargs):
    return {'domain': domain, 'name': name, 'value': value, 'path': '/', 'secure': True,
            'hostOnly': not domain.startswith('.'), 'httpOnly': True, **kwargs}


class LiveFeatureTests(unittest.TestCase):
    def test_channel_templates_and_invalid_input(self):
        self.assertEqual(channel_url('tiktok', '@creator_1'), 'https://www.tiktok.com/@creator_1/live')
        self.assertEqual(channel_url('bigo', '123ABC'), 'https://www.bigo.tv/123ABC')
        for platform, handle in [('other','a'), ('bigo','../evil'), ('bigo','..'), ('tiktok','https://example.com'), ('tiktok','a?x=1'), ('tiktok','')]:
            with self.subTest(platform=platform, handle=handle), self.assertRaises(ValueError):
                channel_url(platform, handle)
        self.assertIsNone(platform_key('https://tiktok.com.evil.example/'))

    def test_counts_are_concurrent_numbers_only(self):
        for bad in (None, True, -1, '1.2k', float('nan'), 4.5, {}):
            self.assertIsNone(viewer_count(bad))
        self.assertEqual(viewer_count(0), 0)
        self.assertEqual(viewer_count('124'), 124)

    def test_timeline_relative_peak_no_fabricated_missing_data(self):
        r={'media_started_at':100.0}
        self.assertFalse(add_viewer_sample(r,123,99))
        self.assertFalse(add_viewer_sample(r,None,130))
        for t,n in [(100,0),(130,40),(160,120),(190,95),(250,200),(310,50)]:
            self.assertTrue(add_viewer_sample(r,n,t))
        self.assertFalse(add_viewer_sample(r,999,310))
        stats=viewer_summary(r['viewer_samples'])
        self.assertEqual(stats['samples'],6)
        self.assertEqual(stats['peak_viewers'],200)
        self.assertEqual(stats['peak_seconds'],150)
        self.assertEqual(stats['top_moments'][0],{'seconds':150,'viewers':200})
        self.assertEqual(viewer_summary([])['peak_viewers'],None)
        self.assertEqual(clip_samples(r['viewer_samples'],60,160)[0]['seconds'],0)

    def test_cookie_scope_and_netscape_roundtrip(self):
        future=time.time()+3600
        data=[test_cookie('.tiktok.com',expirationDate=future), test_cookie('www.tiktok.com','host-cookie'),
              test_cookie('.bigo.tv','other'),test_cookie('tiktok.com.evil.example','bad'),
              test_cookie('.tiktok.com','expired',expirationDate=1),test_cookie('.tiktok.com','partitioned',partitionKey={'topLevelSite':'https://example.com'}),
              test_cookie('.tiktok.com','bad\nname'),test_cookie('.tiktok.com','bad-value',value='a\tb')]
        text=scoped_cookie_text('tiktok',data)
        parsed=parse_netscape_cookies(text)
        self.assertEqual({c['name'] for c in parsed},{'session','host-cookie'})
        self.assertTrue(all(c['httpOnly'] for c in parsed))
        self.assertTrue(next(c for c in parsed if c['name']=='host-cookie')['hostOnly'])
        with self.assertRaises(ValueError): scoped_cookie_text('bigo',[test_cookie('.tiktok.com')])

    def test_platform_cookie_snapshot_is_scoped_and_private(self):
        with tempfile.TemporaryDirectory() as d, patch.object(main,'COOKIES_DIR',Path(d)), patch.object(main,'HARDENED',True), patch.dict(main.settings,{'cookies_file':''}):
            main._store_platform_session('tiktok',[test_cookie('.tiktok.com'),test_cookie('.bigo.tv','unrelated')])
            stored=Path(d)/'platform-tiktok.txt'
            self.assertEqual(stored.stat().st_mode & 0o777,0o600)
            with main._cookie_snapshot('https://www.tiktok.com/@abc/live') as snapshot:
                self.assertNotEqual(snapshot,str(stored))
                self.assertIn('fixture-value',Path(snapshot).read_text())
                self.assertNotIn('unrelated',Path(snapshot).read_text())
                self.assertEqual(Path(snapshot).stat().st_mode & 0o777,0o600)
                Path(snapshot).write_text('test mutation')
            self.assertFalse(Path(snapshot).exists())
            self.assertIn('fixture-value',stored.read_text())
            with main._cookie_snapshot('https://www.bigo.tv/abc','platform-tiktok.txt') as snapshot:
                self.assertEqual(snapshot,'')


class ViewerProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_concurrent_field_not_total_views(self):
        for data,wanted in [({'is_live':True,'view_count':123456,'concurrent_view_count':42},42),({'is_live':True,'view_count':100000},None),({'is_live':False,'concurrent_view_count':99},None),(None,None)]:
            proc=AsyncMock();proc.returncode=0;proc.communicate.return_value=(json.dumps(data).encode(),b'')
            with patch('main.asyncio.create_subprocess_exec',AsyncMock(return_value=proc)),patch.dict(main.settings,{'cookies_file':''}):
                info=await main.probe_live('https://www.tiktok.com/@abc/live')
                self.assertEqual(info['viewer_count'],wanted)

    async def test_recording_sample_rejects_late_response(self):
        rec={'status':'recording','media_started_at':100.0,'viewer_samples':[]}
        ch={'recording_id':'r','url':'https://www.tiktok.com/@abc/live'}
        async def replaced(*args,**kwargs):
            ch['recording_id']='new'
            return {'is_live':True,'viewer_count':1000,'observed_at':130.0}
        with patch.object(main,'channels',{'c':ch}),patch.object(main,'recordings',{'r':rec}),patch('main.probe_live',side_effect=replaced):
            await main._sample_recording_viewers('c','r')
            self.assertEqual(rec['viewer_samples'],[])

    async def test_sample_persisted_for_current_recording(self):
        rec={'status':'recording','media_started_at':100.0,'viewer_samples':[]}
        ch={'recording_id':'r','url':'https://www.tiktok.com/@abc/live'}
        with patch.object(main,'channels',{'c':ch}),patch.object(main,'recordings',{'r':rec}),patch('main.probe_live',AsyncMock(return_value={'is_live':True,'viewer_count':0,'observed_at':130.0})),patch('main._save_state') as save:
            await main._sample_recording_viewers('c','r')
            self.assertEqual(rec['viewer_samples'][0]['seconds'],30)
            self.assertEqual(ch['viewer_count'],0)
            save.assert_called_once()
