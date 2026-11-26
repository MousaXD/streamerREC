"""Public-web channel policy; Squid enforces resolved destination IPs at runtime."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

_LOCAL_SUFFIXES = ('localhost', 'local', 'lan', 'internal', 'home', 'home.arpa', 'onion')
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def allowed_host(host: str) -> bool:
    """Accept public IP literals or valid public DNS names, never private literals.

    Hostname resolution is intentionally performed by the isolated egress proxy,
    which denies private/reserved destinations (also for CDN redirects).
    """
    host = host.lower().rstrip('.')
    if not host or '%' in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None or ip.is_site_local or ip.sixtofour is not None or ip.teredo is not None:
                return False
        return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_link_local or ip.is_loopback)
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        return False
    if len(host) > 253 or '.' not in host or any(host == s or host.endswith('.' + s) for s in _LOCAL_SUFFIXES):
        return False
    labels = host.split('.')
    # Reject legacy numeric IPv4 forms such as 127.1 or 0x7f.0.0.1.
    if labels[-1].isdigit() or re.fullmatch(r'0x[0-9a-f]+', labels[-1]):
        return False
    return all(_LABEL.fullmatch(label) for label in labels)


def approved_url(url: str) -> str:
    """Validate a public HTTP(S) URL; retain TikTok's live-only normalization."""
    if not url or any(ord(c) <= 32 or ord(c) == 127 or c == '\\' for c in url):
        raise ValueError('Enter a valid public HTTP or HTTPS URL')
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or p.username is not None or p.password is not None or not allowed_host(p.hostname or ''):
        raise ValueError('Only public HTTP or HTTPS destinations are allowed')
    if p.port not in (None, 443 if p.scheme == 'https' else 80):
        raise ValueError('Nonstandard destination ports are not allowed')
    host = (p.hostname or '').rstrip('.')
    if host == 'tiktok.com' or host.endswith('.tiktok.com'):
        match = re.fullmatch(r'/@([A-Za-z0-9_.]+)/?(?:live/?)?', p.path)
        if not match or match.group(1) in ('.', '..'):
            raise ValueError('Enter a TikTok profile or /@username/live URL, not a posted video')
        return urlunsplit(('https', 'www.tiktok.com', '/@' + match.group(1) + '/live', '', ''))
    return url
