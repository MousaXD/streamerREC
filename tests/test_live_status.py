import time
import unittest
from unittest.mock import AsyncMock, patch

import main
from live_features import live_probe_status

URL = 'https://www.tiktok.com/@fixture/live'


class LiveStatusTests(unittest.TestCase):
    def test_tiktok_offline_error_is_not_conclusive(self):
        status, detail = live_probe_status(URL, None, 1, b'ERROR: [tiktok:live] The channel is not currently live')
        self.assertEqual(status, 'unknown')
        self.assertIn('may be live or offline', detail)

    def test_no_false_live_from_failed_result(self):
        self.assertEqual(live_probe_status(URL, {'is_live': True}, 1)[0], 'unknown')
        self.assertEqual(live_probe_status(URL, {}, 0)[0], 'unknown')
        self.assertEqual(live_probe_status(URL, None, 0)[0], 'unknown')

    def test_explicit_live_and_offline(self):
        self.assertEqual(live_probe_status(URL, {'live_status': 'is_live'}, 0), ('live', None))
        self.assertEqual(live_probe_status(URL, {'is_live': False}, 0), ('offline', None))
        self.assertEqual(live_probe_status('https://www.twitch.tv/fixture', None, 1, 'Channel is not currently live'), ('offline', None))

    def test_no_sensitive_response_echo(self):
        detail=live_probe_status(URL,None,1,'HTTP Error 403 token=PRIVATE_FIXTURE')[1]
        self.assertNotIn('PRIVATE_FIXTURE',detail)


class MonitorStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_check_keeps_monitoring_and_does_not_start_capture(self):
        ch={'url':URL,'monitoring':True,'is_live':True}
        failed={'is_live':False,'viewer_count':None,'observed_at':time.time(),'live_check_status':'unknown','live_check_message':'Inconclusive'}
        with patch.object(main,'channels',{'fixture':ch}),patch.object(main,'recordings',{}),patch('main.probe_live',AsyncMock(return_value=failed)),patch('main._start_recording_for_channel',AsyncMock()) as start:
            await main._check_one_channel('fixture')
            self.assertEqual(ch['live_check_status'],'unknown')
            self.assertTrue(ch['monitoring'])
            self.assertTrue(ch['is_live']) # last known good state retained, not shown as live in UI
            start.assert_not_awaited()

    async def test_next_confirmed_live_check_starts_capture(self):
        ch={'url':URL,'monitoring':True,'is_live':False,'live_check_status':'unknown'}
        good={'is_live':True,'viewer_count':12,'observed_at':time.time(),'live_check_status':'live','live_check_message':None}
        with patch.object(main,'channels',{'fixture':ch}),patch.object(main,'recordings',{}),patch('main.probe_live',AsyncMock(return_value=good)),patch('main._start_recording_for_channel',AsyncMock(return_value=None)) as start:
            await main._check_one_channel('fixture')
            self.assertEqual(ch['live_check_status'],'live')
            start.assert_awaited_once_with('fixture')

    async def test_extractor_failure_classified_unknown(self):
        proc=AsyncMock();proc.returncode=1;proc.communicate.return_value=(b'null',b'ERROR: The channel is not currently live')
        with patch('main.asyncio.create_subprocess_exec',AsyncMock(return_value=proc)):
            r=await main.probe_live(URL)
        self.assertFalse(r['is_live']);self.assertEqual(r['live_check_status'],'unknown')
