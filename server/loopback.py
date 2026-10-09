"""Loopback HTTP that never honors process/system proxies.

A system proxy (macOS scutil --proxy, e.g. a local Clash on 7890 that is not
running after a reboot) makes urllib route ``http://127.0.0.1:<port>/api/health``
through the dead proxy: every loopback health check, port scan and register
POST then fails with ``Connection refused`` even though the report server is
fine (observed 2026-09-26: every run_tests server group failed "server did not
come up" while curl answered instantly — curl ignores macOS system proxies,
urllib does not). All loopback traffic in this skill must go through
:func:`urlopen_loopback`.
"""
from __future__ import annotations

import ipaddress
import urllib.request
from urllib.parse import urlparse

_OPENER = None


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def urlopen_loopback(url, timeout=2, data=None, method=None):
    """urlopen for a loopback URL with proxies forcibly disabled.

    ``url`` may be a str or a pre-built ``urllib.request.Request`` (headers
    preserved). A non-loopback URL falls through to the stock urlopen: a user
    may point serve_report --server at a LAN host that legitimately needs the
    system proxy.
    """
    req = url if isinstance(url, urllib.request.Request) \
        else urllib.request.Request(url, data=data, method=method)
    host = urlparse(req.full_url).hostname or ""
    if not _is_loopback_host(host):
        return urllib.request.urlopen(req, timeout=timeout)
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER.open(req, timeout=timeout)
