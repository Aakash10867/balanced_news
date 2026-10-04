"""How much a story matters, so the front page shows what readers most need, and filler is never
published.

Rank = model score (1-5, written rubric) + coverage breadth (independent outlets, both languages).
Many outlets independently choosing to cover something is itself a signal of importance.
One cheap call per story, cached until its statements change.
"""
from __future__ import annotations

import hashlib
import json
import logging

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

PROMPT = """Rate how important this Indian news story is for an ordinary Indian reader, and whether it is
filler. Use only what is below.

Headline: {headline}
Main statements:
{facts}

Score 1-5:
5  national significance: many people affected, or central government, Parliament, Supreme Court,
   national security, the economy, major disasters, rights of large groups
4  major state-level or national-interest story: state government action, a serious crime or accident
   with wide attention, a significant court ruling, a large protest
3  notable but limited: a local incident with wider interest, a regional political development
2  minor: a routine procedural step in a smaller case, a local event, a ceremonial occasion
1  trivial

filler = true for content that is not news reporting: horoscopes, lottery results, product launches or
reviews, celebrity gossip, recipes, explainers, quizzes, listicles, opinion or editorial pieces,
live-blog shells, sponsored content.

Reply with JSON only: {{"score": 3, "filler": false, "reason": "at most 12 words"}}"""


def _key(headline: str, facts: list[str]) -> str:
    return hashlib.sha256(json.dumps([headline, facts[:8]]).encode()).hexdigest()[:16]


def assess(router: Router | None, headline: str, facts: list[str], cached: dict | None) -> dict:
    """{"score", "filler", "reason", "key"}; reuses `cached` if nothing changed. With no model
    answer, a neutral 3 and not filler (a missing answer must not hide a real story)."""
    key = _key(headline, facts)
    if cached and cached.get("key") == key:
        return cached
    out = {"score": 3, "filler": False, "reason": "not rated", "key": key}
    if router is None:
        return out
    try:
        res = router.call("light", PROMPT.format(headline=headline, facts="\n".join(f"- {f}" for f in facts[:8])),
                          json_out=True, max_output_tokens=150)
        d = res.data if isinstance(res.data, dict) else {}
        score = int(d.get("score", 3))
        out.update(score=min(5, max(1, score)), filler=d.get("filler") is True, reason=str(d.get("reason") or "")[:120])
    except (QuotaExhausted, ValueError, TypeError) as e:
        log.info("importance: not rated (%s)", e)
    except Exception as e:  # noqa: BLE001
        log.warning("importance failed: %s", str(e)[:200])
    return out


def rank(score: int, independent_sources: int, languages: int) -> float:
    """Model score plus coverage: up to +1.5 for 6+ independent outlets, +0.5 if covered in both
    English and Hindi."""
    return round(score + 0.25 * min(independent_sources, 6) + (0.5 if languages >= 2 else 0), 2)
