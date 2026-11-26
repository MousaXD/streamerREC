"""Offline integration smoke: real HTTP, real MP4, synthetic platform metadata.

Run in the built image with a private tmpfs and no network. Never mounts a user's
recordings or reads browser credentials. The viewer counts are test fixtures.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

FIXTURE = '''#!/usr/local/bin/python
import fcntl,json,os,pathlib,subprocess,sys,time
args=sys.argv[1:]
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
if '--dump-single-json' in args:
    if (root/'ended').exists(): print(json.dumps({'is_live':False}));sys.exit(0)
    with (root/'counter').open('a+') as f:
        fcntl.flock(f,fcntl.LOCK_EX); f.seek(0); n=int(f.read() or 0)+1; f.seek(0); f.truncate();f.write(str(n));f.flush()
    print(json.dumps({'is_live':True,'live_status':'is_live','concurrent_view_count':n*20,'view_count':999999,'title':'Fixture live','uploader':'Fixture creator','uploader_id':'fixture'}));sys.exit(0)
if '-o' in args:
    if '--cookies' not in args or not pathlib.Path(args[args.index('--cookies')+1]).is_file(): sys.exit(2)
    output=args[args.index('-o')+1].replace('%(ext)s','mp4')
    rc=subprocess.call(['ffmpeg','-hide_banner','-loglevel','error','-re','-f','lavfi','-i','testsrc=size=160x90:rate=5','-t','17','-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-g','5','-movflags','frag_keyframe+empty_moov','-flush_packets','1','-y',output]);(root/'ended').touch();sys.exit(rc)
sys.exit(0)
'''

with tempfile.TemporaryDirectory(prefix='streamrec-e2e-') as td:
    root=Path(td); record=root/'recordings'; record.mkdir(); bin_dir=root/'bin'; bin_dir.mkdir()
    engine=bin_dir/'yt-dlp'; engine.write_text(FIXTURE); engine.chmod(0o755)
    env={**os.environ,'RECORDINGS_DIR':str(record),'FIXTURE_ROOT':str(root),'STREAMREC_HARDENED':'1','STREAMREC_EGRESS_PROXY':'http://fixture.invalid:3128','STATIC_DIR':'/app','PATH':str(bin_dir)+':'+os.environ['PATH']}
    log=(root/'server.log').open('w+')
    command=[sys.executable,'-c','import main,uvicorn;main.SAMPLE_INTERVAL=2;main.get_size_poll_interval=lambda:1;uvicorn.run(main.app,host="127.0.0.1",port=18765,log_level="warning")']
    base='http://127.0.0.1:18765'
    server=None; token=''; password=secrets.token_urlsafe(20)
    def req(path, expected=200, method='GET', body=None, auth=True, headers=None, raw=False):
        h=dict(headers or {}); data=None
        if auth and token: h['Authorization']='Bearer '+token
        if body is not None: h['Content-Type']='application/json'; data=json.dumps(body).encode()
        r=urllib.request.Request(base+path,method=method,headers=h,data=data)
        try:
            with urllib.request.urlopen(r,timeout=8) as f: code=f.status; result=f.read(); response_headers=f.headers
        except urllib.error.HTTPError as e: code=e.code; result=e.read(); response_headers=e.headers
        assert code==expected,(method,path,code,expected,result[:200])
        return (result,response_headers) if raw else json.loads(result)
    def start():
        global server
        server=subprocess.Popen(command,env=env,stdout=log,stderr=log)
        for _ in range(80):
            try: req('/api/health',auth=False); return
            except (OSError,urllib.error.URLError): time.sleep(.15)
        raise RuntimeError('Fixture server did not start')
    def stop():
        if server and server.poll() is None:
            server.terminate()
            try: server.wait(timeout=15)
            except subprocess.TimeoutExpired: server.kill();server.wait()
    try:
        start()
        req('/api/channels',401,auth=False)
        token=req('/api/account',method='POST',body={'username':'fixture','password':password,'confirm_password':password},auth=False)['token']
        assert req('/api/version')['version']=='2.4.0'
        assert {a['platform'] for a in req('/api/platform-accounts')}=={'tiktok','bigo'}
        pair=req('/api/platform-accounts/tiktok/pair',method='POST')['code']
        fixture=[{'domain':'.tiktok.com','name':'fixture_cookie','value':'fixture-value','path':'/','secure':True,'hostOnly':False,'httpOnly':True},{'domain':'.bigo.tv','name':'unrelated','value':'not-for-tiktok','path':'/'}]
        body={'platform':'tiktok','cookies':fixture}
        req('/api/platform-accounts/connect',401,method='POST',body=body,auth=False)
        req('/api/platform-accounts/connect',400,method='POST',body={'platform':'bigo','cookies':fixture},auth=False,headers={'X-StreamRec-Pairing':pair})
        req('/api/platform-accounts/connect',method='POST',body=body,auth=False,headers={'X-StreamRec-Pairing':pair,'Origin':'chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'})
        req('/api/platform-accounts/connect',401,method='POST',body=body,auth=False,headers={'X-StreamRec-Pairing':pair})
        stored=record/'cookies'/'platform-tiktok.txt'; assert stored.stat().st_mode&0o777==0o600
        assert 'fixture-value' in stored.read_text() and 'not-for-tiktok' not in stored.read_text()
        status=req('/api/platform-accounts'); assert status[0]['session_available'] and 'fixture-value' not in json.dumps(status)
        req('/api/channels',400,method='POST',body={'platform':'tiktok','handle':'https://evil.example'})
        req('/api/channels',400,method='POST',body={'platform':'tiktok','handle':'fixture','url':'https://www.tiktok.com/@fixture/live'})
        cid=req('/api/channels',method='POST',body={'platform':'tiktok','handle':'@fixture','format':'mp4','monitoring':True})['id']
        bigo=req('/api/channels',method='POST',body={'platform':'bigo','handle':'fixture123','monitoring':False})['id']
        # Do not wait for network: the BIGO metadata probe is outside this fixture.
        assert next(c for c in req('/api/channels') if c['id']==bigo)['url']=='https://www.bigo.tv/fixture123'
        assert next(c for c in req('/api/channels') if c['id']==cid)['url']=='https://www.tiktok.com/@fixture/live'
        print('Platform templates, one-time pairing, scoped cookie storage, replay denial: PASS',flush=True)
        rid=None; live_samples=0
        deadline=time.monotonic()+55
        while time.monotonic()<deadline:
            channel=next(c for c in req('/api/channels') if c['id']==cid)
            rid=rid or channel.get('rec_id') or channel.get('last_rec_id')
            if rid:
                d=req('/api/recordings/'+rid+'/viewers'); live_samples=max(live_samples,d['summary']['samples'])
                if d['status']=='completed': break
                assert d['status']!='error',req('/api/recordings/'+rid+'/log')
            time.sleep(.7)
        else: raise RuntimeError('Recording did not finish')
        assert d['summary']['samples']>=2,d
        assert 0<d['summary']['peak_viewers']<999999,d
        assert d['summary']['peak_seconds']>0 and d['playable']
        media,headers=req('/api/preview/'+rid,206,headers={'Range':'bytes=0-127'},raw=True)
        assert len(media)==128 and headers.get('Content-Type')=='video/mp4'
        assert 'video/mp4' in headers.get('Content-Type')
        indexed=next(r for r in req('/api/recordings') if r['id']==rid)
        assert indexed['viewer_summary']['peak_viewers']==d['summary']['peak_viewers'] and 'viewer_samples' not in indexed
        req('/api/recordings/'+rid+'/viewers',401,auth=False)
        print('Real FFmpeg media capture, sampled viewer curve, peak timestamp, authenticated MP4 range: PASS',flush=True)
        req('/api/platform-accounts/tiktok',method='DELETE'); assert not stored.exists()
        stop(); start()
        token=req('/api/account/login',method='POST',body={'username':'fixture','password':password},auth=False)['token']
        restored=req('/api/recordings/'+rid+'/viewers'); assert restored['samples']==d['samples']
        assert restored['summary']==d['summary'] and restored['playable']
        print('Recording and viewer timeline survived restart; session disconnect persisted: PASS',flush=True)
    except BaseException:
        stop();log.seek(0);print(log.read()[-5000:],file=sys.stderr);raise
    finally:
        stop();log.close()
