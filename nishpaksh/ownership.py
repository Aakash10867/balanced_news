"""Which owner group an outlet belongs to (config/ownership.yaml). Outlets with one owner count
as one independent source. Unknown outlets are their own group."""
from __future__ import annotations

import functools
import re
from urllib.parse import urlsplit

from .config import load_yaml


@functools.lru_cache(maxsize=1)
def _maps() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    by_outlet, by_domain, gov = {}, {}, {}
    for group, info in (load_yaml("ownership.yaml").get("groups") or {}).items():
        for o in info.get("outlets") or []:
            by_outlet[o.strip().lower()] = group
        for d in info.get("domains") or []:
            by_domain[d.strip().lower()] = group
        if info.get("government"):
            gov[group] = str(info["government"])
    return by_outlet, by_domain, gov


def _registrable(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "nic", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def owner_of(outlet: str | None, url: str | None = None) -> str:
    """Owner group: by outlet name, else by web domain, else the domain itself (two names on one
    site are one owner), else the name."""
    by_outlet, by_domain, _ = _maps()
    if outlet and outlet.strip().lower() in by_outlet:
        return by_outlet[outlet.strip().lower()]
    if outlet and _plain(outlet) in _plain_owner():
        return _plain_owner()[_plain(outlet)]
    host = urlsplit(url or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    h = host
    while h:
        if h in by_domain:
            return by_domain[h]
        h = h.partition(".")[2] if h.count(".") > 1 else ""
    if host:
        return _registrable(host)
    return (outlet or "unknown").strip()


@functools.lru_cache(maxsize=1)
def _plain_owner() -> dict[str, str]:
    by_outlet, _, _ = _maps()
    return {_plain(o): g for o, g in by_outlet.items()}


def is_known_outlet(name: str | None) -> bool:
    return bool(name) and (_plain(name) in _known_names() or _plain(name) in _plain_owner())


def government_of(owner: str) -> str | None:
    """'Union' for the government's own press office, else None."""
    return _maps()[2].get(owner)


def _plain(name: str) -> str:
    """'The Times of India', 'Times of India', 'ThePrint', 'The Print' -> one key each."""
    n = re.sub(r"[^a-z0-9\u0900-\u097f]", "", (name or "").lower())
    n = re.sub(r"(com|in|org|net)$", "", n) if "." in (name or "") else n   # 'organiser.org'
    return n[3:] if n.startswith("the") and len(n) > 6 else n


@functools.lru_cache(maxsize=1)
def _known_names() -> dict[str, str]:
    names = [f["name"] for f in load_yaml("feeds.yaml").get("feeds") or []]
    for info in (load_yaml("ownership.yaml").get("groups") or {}).values():
        names += info.get("outlets") or []
    return {_plain(n): n for n in names}


def canonical_outlet(name: str | None, url: str | None = None) -> str:
    """One spelling per outlet: a search engine's "The Times of India" is our feed's "Times of India"."""
    if name and _plain(name) in _known_names():
        return _known_names()[_plain(name)]
    if name:
        return name.strip()
    host = urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host
