import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recording_library import build_library, media_duration
import main


class LibraryTests(unittest.TestCase):
    def test_groups_accounts_platforms_and_actual_media(self):
        channels = {'new': {'platform':'TikTok','url':'https://www.tiktok.com/@creator/live','display_name':'Creator','username':'creator','monitoring':True}, 'empty': {'platform':'Bigo','url':'https://www.bigo.tv/other','display_name':'Other','monitoring':False}}
        recs = {
            'one': {'id':'one','platform':'TikTok','url':'https://www.tiktok.com/@Creator/live','channel_id':'old','status':'completed','filepath':'one.mp4','duration_seconds':60.5,'bytes':100,'created_at':10,'viewer_samples':[{'viewers':23}]},
            'two': {'id':'two','platform':'TikTok','url':'https://www.tiktok.com/@creator/live','channel_id':'new','status':'archived','filepath':'two.mp4','duration_seconds':120,'bytes':200,'created_at':20},
            'clip': {'id':'clip','platform':'TikTok','channel_id':'new','status':'completed','filepath':'clip.mp4','duration_seconds':12,'clip_duration_seconds':15,'bytes':50,'created_at':30},
            'partial': {'id':'partial','platform':'TikTok','channel_id':'new','status':'error','filepath':'partial.mp4','bytes':20,'created_at':40},
            'failed': {'id':'failed','channel_id':'new','status':'error','bytes':0},
            'active': {'id':'active','channel_id':'new','status':'recording','filepath':'active.mp4'},
            'orphan': {'id':'orphan','platform':'Twitch','url':'https://www.twitch.tv/old','status':'completed','filepath':'orphan.mp4','duration_seconds':300,'bytes':300,'created_at':1},
        }
        groups, members = build_library(channels,recs,lambda r: bool(r.get('filepath')))
        by_platform={g['platform']:g for g in groups}
        a=by_platform['TikTok']
        self.assertEqual(a['recordings'],4)
        self.assertEqual(a['live_recordings'],3)
        self.assertEqual(a['clips'],1)
        self.assertEqual(a['duration_seconds'],192.5)
        self.assertEqual(a['unknown_durations'],1)
        self.assertEqual(a['bytes'],370)
        self.assertEqual(a['peak_viewers'],23)
        self.assertEqual({r['id'] for r in members[a['id']]},{'one','two','clip','partial'})
        self.assertEqual(by_platform['Bigo']['recordings'],0)
        self.assertEqual(by_platform['Twitch']['name'],'old')

    def test_duration_rejects_invalid_values(self):
        for v in (None,True,0,-1,math.inf,math.nan,'N/A'):
            self.assertIsNone(media_duration(v))
        self.assertEqual(media_duration('65.25'),65.25)

    def test_generic_query_identifies_separate_accounts(self):
        c={str(i):{'platform':'YouTube','url':f'https://youtube.com/watch?v={i}','display_name':f'Channel {i}'} for i in (1,2)}
        self.assertEqual(len(build_library(c,{},lambda r: True)[0]),2)


class LibraryApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_existing_media_is_listed_and_account_detail_matches(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); media=root/'saved.mp4'; media.write_bytes(b'fixture')
            channels={'c':{'platform':'TikTok','url':'https://www.tiktok.com/@creator/live','display_name':'Creator'}}
            recordings={'r':{'id':'r','channel_id':'c','status':'completed','created_at':1,'filepath':str(media),'duration_seconds':80,'bytes':7},'missing':{'id':'missing','channel_id':'c','status':'completed','created_at':2,'filepath':str(root/'missing.mp4')}}
            with patch.object(main,'HARDENED',True),patch.object(main,'RECORDINGS_DIR',root),patch.object(main,'channels',channels),patch.object(main,'recordings',recordings):
                summary=await main.list_library(); a=summary['accounts'][0]
                self.assertEqual(a['recordings'],1); self.assertEqual(a['duration_seconds'],80)
                detail=await main.account_recordings(a['id']);self.assertEqual([r['id'] for r in detail['recordings']],['r'])
                self.assertEqual(detail['account'],a)

    async def test_real_ffprobe_duration(self):
        import asyncio
        from recording_library import probe_duration
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'fixture.mp4'
            proc=await asyncio.create_subprocess_exec('ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=64x64:r=5','-t','3','-c:v','libx264','-threads','1',str(p))
            self.assertEqual(await proc.wait(),0)
            duration=await probe_duration(p)
            self.assertAlmostEqual(duration,3,places=1)
