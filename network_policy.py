"""Shared local policy for the hardened Compose deployment."""
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DOMAIN_FILE = Path(__file__).with_name("egress-domains.txt")


def allowed_host(host: str, domains=None) -> bool:
    host = host.lower().rstrip(".")
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if domains is None:
        domains = DOMAIN_FILE.read_text().splitlines()
    for line in domains:
        domain = line.strip().lower().lstrip(".")
        if domain and not domain.startswith("#") and (host == domain or host.endswith("." + domain)):
            return True
    return False


def approved_url(url: str) -> str:
    """Validate initial destination; the proxy also validates every redirect."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or p.username or p.password or not allowed_host(p.hostname or ""):
        raise ValueError("URL is outside the approved streaming domains")
    if p.port not in (None, 443 if p.scheme == "https" else 80):
        raise ValueError("Nonstandard destination ports are not allowed")
    if p.hostname == "tiktok.com" or (p.hostname or "").endswith(".tiktok.com"):
        match = re.fullmatch(r"/@([A-Za-z0-9_.]+)/?(?:live/?)?", p.path)
        if not match:
            raise ValueError("Enter a TikTok profile or /@username/live URL, not a posted video")
        return urlunsplit(("https", "www.tiktok.com", "/@" + match.group(1) + "/live", "", ""))
    return url
