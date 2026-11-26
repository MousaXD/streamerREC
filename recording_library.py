"""Account-first recording library and actual media-duration measurement."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MEDIA_SUFFIXES = frozenset({'.mp4', '.mkv', '.ts', '.mp3', '.flv', '.webm', '.m4a', '.aac', '.mov', '.ogg', '.opus', '.wav', '.m4v'})
FINISHED = frozenset({'completed', 'archived', 'error'})


def media_duration(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and 0 < n < 100 * 365 * 86400 else None


async def probe_duration(path: Path) -> float | None:
    """Probe local media only, never a URL or a playlist; bound process lifetime."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
            '-format_whitelist', 'mov,matroska,webm,mpegts,flv,mp3,aac,ogg,wav',
            '-show_entries', 'format=duration', '-of', 'json', str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0:
            return media_duration(json.loads(stdout).get('format', {}).get('duration'))
    except (OSError, ValueError, TypeError, asyncio.TimeoutError):
        pass
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
    return None


def _identity(platform: str, url: str, fallback: str) -> str:
    # Group re-added channels by their original account URL. Preserve the query
    # for generic sources: it may be the only channel identifier.
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower().rstrip('.')
        if host and p.scheme in ('http', 'https'):
            path = p.path.rstrip('/') or '/'
            if platform.lower() == 'tiktok' and path.startswith('/@'):
                path = '/@' + path.split('/')[1][1:].lower() + '/live'
                host = 'www.tiktok.com'
                url = urlunsplit(('https', host, path, '', ''))
            elif platform.lower() == 'bigo' and host in ('www.bigo.tv', 'bigo.tv'):
                url = urlunsplit(('https', 'www.bigo.tv', path, '', ''))
            else:
                url = urlunsplit((p.scheme, p.netloc.lower(), path, p.query, ''))
        else:
            url = fallback
    except (ValueError, TypeError):
        url = fallback
    return hashlib.sha256((platform.lower() + '\0' + url).encode()).hexdigest()[:24]


def _fallback_name(url: str, default: str) -> str:
    try:
        p = urlsplit(url)
        parts = [x for x in p.path.split('/') if x]
        return (parts[0] if parts else p.hostname) or default
    except (ValueError, TypeError):
        return default


def build_library(channels: dict, recordings: dict, media_exists) -> tuple[list, dict]:
    """Return compact groups and account->recordings index (no large logs)."""
    groups, members = {}, {}

    def group(platform, url, fallback, name='', handle='', monitored=False):
        platform = str(platform or 'Unknown')
        key = _identity(platform, url or '', str(fallback))
        if key not in groups:
            groups[key] = {'id': key, 'platform': platform, 'name': name or handle or _fallback_name(url or '', 'Unknown account'),
                           'handle': handle or _fallback_name(url or '', ''), 'monitoring': False,
                           'recordings': 0, 'live_recordings': 0, 'clips': 0, 'duration_seconds': 0.0,
                           'unknown_durations': 0, 'bytes': 0, 'latest_at': None, 'peak_viewers': None}
            members[key] = []
        g = groups[key]
        if monitored:
            g['monitoring'] = True
        return g

    channel_groups = {}
    for cid, c in channels.items():
        g = group(c.get('platform'), c.get('url'), cid, c.get('display_name'), c.get('username'), c.get('monitoring', True))
        channel_groups[cid] = g['id']
    for rid, r in recordings.items():
        if r.get('status') not in FINISHED or not media_exists(r):
            continue
        cid = r.get('channel_id')
        g = groups.get(channel_groups.get(cid))
        if g is None:
            g = group(r.get('platform'), r.get('url'), cid or rid, r.get('account_display_name'), r.get('account_username'))
        members[g['id']].append(r)
        g['recordings'] += 1
        if r.get('is_clip') or r.get('clip_duration_seconds') is not None:
            g['clips'] += 1
        else:
            g['live_recordings'] += 1
        duration = media_duration(r.get('duration_seconds'))
        if duration is None:
            g['unknown_durations'] += 1
        else:
            g['duration_seconds'] += duration
        size = r.get('bytes')
        if isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size) and size >= 0:
            g['bytes'] += size
        created = r.get('created_at') or 0
        g['latest_at'] = max(g['latest_at'] or 0, created)
        for sample in r.get('viewer_samples', []):
            n = sample.get('viewers')
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                g['peak_viewers'] = max(g['peak_viewers'] or 0, n)
    rows = sorted(groups.values(), key=lambda g: (g['platform'].casefold(), -(g['latest_at'] or 0), g['name'].casefold()))
    for g in rows:
        g['duration_seconds'] = round(g['duration_seconds'], 3)
    return rows, members
