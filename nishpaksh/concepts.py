"""Hindi loaded words mapped to English concepts, so framing can be compared across languages
("दंगा" and "riot" are the same choice of word). Without it, framing split outlets by language
(real data, Oct 2026). Mappings are cached in `translations` and asked for once per word."""
from __future__ import annotations

import hashlib
import logging
import re

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)
DEVA = re.compile(r"[ऀ-ॿ]")

PROMPT = """Each numbered item is a loaded or emotive word or phrase from a Hindi news report. Give the
English word or short phrase (1-3 words, lowercase) a careful English-language reporter would recognise
as the SAME choice of wording, keeping its tone (e.g. "दंगाई" -> "rioters", "आतंकी" -> "terrorist").

{items}

Reply with JSON only: {{"items": [{{"n": 1, "en": "..."}}]}}"""


def _key(word: str) -> str:
    return hashlib.sha256(("concept|" + word).encode("utf-8")).hexdigest()


def lookup(store, words: set[str]) -> dict[str, str]:
    """Known English concepts for these words (Devanagari only; others map to themselves)."""
    hi = sorted(w for w in words if DEVA.search(w))
    got = store.translation_get([_key(w) for w in hi])
    out = {w: w.lower() for w in words if not DEVA.search(w)}
    for w in hi:
        if _key(w) in got:
            out[w] = got[_key(w)]
    return out


def map_new(store, router: Router | None, words: set[str], max_calls: int = 3) -> int:
    """Ask for the Hindi words not yet mapped (at most `max_calls` calls a run). Returns how many
    were added."""
    if router is None:
        return 0
    known = lookup(store, words)
    todo = sorted(w for w in words if DEVA.search(w) and w not in known)
    added = 0
    for start in range(0, min(len(todo), 60 * max_calls), 60):
        chunk = todo[start:start + 60]
        try:
            res = router.call("light", PROMPT.format(items="\n".join(f"{k + 1}. {w}" for k, w in enumerate(chunk))),
                              json_out=True, max_output_tokens=1500)
        except QuotaExhausted:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("concept mapping failed: %s", str(e)[:200])
            continue
        put = {}
        for it in (res.data or {}).get("items", []) if isinstance(res.data, dict) else []:
            try:
                k = int(it["n"]) - 1
                en = str(it.get("en") or "").strip().lower()
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= k < len(chunk) and en and not DEVA.search(en) and len(en) <= 40:
                put[_key(chunk[k])] = en
        store.translation_put(put)
        added += len(put)
    return added
