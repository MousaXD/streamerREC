"""Live channel templates, scoped browser-session cookies and viewer analytics."""
from __future__ import annotations

import math
import re
import time
from urllib.parse import urlsplit

PLATFORMS = {
    'tiktok': {'label': 'TikTok Live', 'url': 'https://www.tiktok.com/@{username}/live',
               'login': 'https://www.tiktok.com/login', 'domains': ('tiktok.com',),
               'pattern': r'[A-Za-z0-9_.]{1,64}'},
    'bigo': {'label': 'BIGO Live', 'url': 'https://www.bigo.tv/{username}',
             'login': 'https://www.bigo.tv/', 'domains': ('bigo.tv', 'bigo.sg'),
             'pattern': r'[A-Za-z0-9_.-]{2,128}'},
}
SAMPLE_INTERVAL = 30
SAMPLE_MAX_AGE = 120
MAX_SAMPLES = 24 * 60 * 60 // SAMPLE_INTERVAL * 14


def platform_key(url: str) -> str | None:
    try:
        host = (urlsplit(url).hostname or '').lower().rstrip('.')
    except ValueError:
        return None
    if host == 'tiktok.com' or host.endswith('.tiktok.com'):
        return 'tiktok'
    if host == 'bigo.tv' or host.endswith('.bigo.tv') or host == 'slink.bigovideo.tv':
        return 'bigo'
    return None


def channel_url(platform: str, username: str) -> str:
    p = PLATFORMS.get(platform.lower())
    if not p:
        raise ValueError('Choose TikTok Live or BIGO Live')
    name = username.strip().removeprefix('@')
    if not re.fullmatch(p['pattern'], name) or name in ('.', '..'):
        raise ValueError('Enter a valid username or BIGO ID, not a URL')
    return p['url'].format(username=name)


def viewer_count(value) -> int | None:
    # Never substitute cumulative view_count, likes, or room popularity.
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,15}', value):
        value = int(value)
    if isinstance(value, int) and 0 <= value <= 10**12:
        return value
    return None


def add_viewer_sample(rec: dict, count, observed_at: float) -> bool:
    count = viewer_count(count)
    start = rec.get('media_started_at')
    if count is None or not start or not math.isfinite(observed_at) or observed_at < start:
        return False
    seconds = round(observed_at - start, 1)
    samples = rec.setdefault('viewer_samples', [])
    if samples and seconds <= samples[-1]['seconds']:
        return False
    if len(samples) >= MAX_SAMPLES:
        return False
    samples.append({'seconds': seconds, 'viewers': count, 'observed_at': observed_at})
    return True


def viewer_summary(samples: list) -> dict:
    if not samples:
        return {'samples': 0, 'peak_viewers': None, 'peak_seconds': None, 'average_viewers': None, 'top_moments': []}
    ranked = sorted(samples, key=lambda x: (-x['viewers'], x['seconds']))
    top = []
    for point in ranked:
        if all(abs(point['seconds'] - other['seconds']) >= 60 for other in top):
            top.append({'seconds': point['seconds'], 'viewers': point['viewers']})
        if len(top) == 5:
            break
    return {'samples': len(samples), 'peak_viewers': ranked[0]['viewers'],
            'peak_seconds': ranked[0]['seconds'],
            'average_viewers': round(sum(x['viewers'] for x in samples) / len(samples)),
            'top_moments': top}


def clip_samples(samples: list, start: float, end: float) -> list:
    return [{**s, 'seconds': round(s['seconds'] - start, 1)} for s in samples if start <= s['seconds'] <= end]


def scoped_cookie_text(platform: str, cookies: list[dict]) -> str:
    """Encode only the platform's explicit domain-scoped, unpartitioned cookies.

    Cookie values never appear in response bodies, process args or logs.
    """
    cfg = PLATFORMS.get(platform)
    if not cfg or not isinstance(cookies, list) or not 1 <= len(cookies) <= 500:
        raise ValueError('No supported platform cookies received')
    rows = []
    seen = set()
    for c in cookies:
        if not isinstance(c, dict) or c.get('partitionKey'):
            continue
        domain = str(c.get('domain', '')).lower()
        bare = domain.lstrip('.')
        if not any(bare == d or bare.endswith('.' + d) for d in cfg['domains']):
            continue
        if not re.fullmatch(r'[a-z0-9.-]+', domain):
            continue
        host_only = c.get('hostOnly') is True
        domain = bare if host_only else '.' + bare
        name, value, path = c.get('name'), c.get('value'), c.get('path') or '/'
        if not all(isinstance(x, str) and not any(ch in x for ch in '\t\r\n\x00') for x in (name, value, path)):
            continue
        if not name or not path.startswith('/') or len(name) > 512 or len(value) > 16384 or len(path) > 2048:
            continue
        expiry = c.get('expirationDate', 0)
        if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or not math.isfinite(expiry):
            continue
        expiry = int(expiry)
        if expiry and expiry <= time.time():
            continue
        key = (domain, path, name)
        if key in seen:
            continue
        seen.add(key)
        prefix = '#HttpOnly_' if c.get('httpOnly') is True else ''
        rows.append('\t'.join((prefix + domain, 'FALSE' if host_only else 'TRUE', path,
                               'TRUE' if c.get('secure') is True else 'FALSE', str(expiry), name, value)))
    if not rows:
        raise ValueError('No matching unexpired platform cookies received')
    result = '# Netscape HTTP Cookie File\n# StreamRec platform-scoped browser session\n' + '\n'.join(rows) + '\n'
    if len(result.encode()) > 1024 * 1024:
        raise ValueError('Cookie session is too large')
    return result


def parse_netscape_cookies(text: str) -> list[dict]:
    if len(text.encode()) > 1024 * 1024:
        raise ValueError('Cookie file is too large')
    cookies = []
    for line in text.splitlines():
        http_only = line.startswith('#HttpOnly_')
        if http_only:
            line = line[len('#HttpOnly_'):]
        elif not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) != 7:
            raise ValueError('Use a Netscape cookies.txt export')
        domain, subdomains, path, secure, expiry, name, value = parts
        try:
            expiry = int(expiry)
        except ValueError:
            raise ValueError('Invalid cookie expiration') from None
        cookies.append({'domain': domain, 'hostOnly': subdomains.upper() != 'TRUE', 'path': path,
                        'secure': secure.upper() == 'TRUE', 'expirationDate': expiry,
                        'name': name, 'value': value, 'httpOnly': http_only})
    return cookies


def live_probe_status(url: str, data, returncode: int, stderr: bytes | str = '') -> tuple[str, str | None]:
    """Do not infer offline from failed extraction or missing metadata.

    TikTokLiveIE uses UserNotLive for both a missing room ID (including a
    challenge page) and a confirmed offline room. Its exit text alone cannot
    distinguish these cases. Only actual successful metadata can confirm state.
    Error messages are fixed strings: never expose raw network responses or URLs.
    """
    if returncode == 0 and isinstance(data, dict):
        if data.get('is_live') is True or data.get('live_status') == 'is_live':
            return 'live', None
        if data.get('is_live') is False or data.get('live_status') in ('not_live', 'was_live', 'post_live', 'is_upcoming'):
            return 'offline', None
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    message = (stderr or '').lower()
    if platform_key(url) == 'tiktok':
        if 'not currently live' in message or 'not live' in message:
            return 'unknown', 'TikTok did not provide usable live-room information. The stream may be live or offline. Automatic checks will continue.'
        if any(x in message for x in ('captcha', 'challenge', 'verification', 'access denied', 'http error 403', 'http error 429', 'login', 'sign in')):
            return 'unknown', 'TikTok declined the automated live check. Open the official live page to verify availability.'
    elif 'not currently live' in message or 'not live' in message or 'livestream has ended' in message:
        return 'offline', None
    return 'unknown', 'The recording engine could not verify live status. Automatic checks will continue.'
