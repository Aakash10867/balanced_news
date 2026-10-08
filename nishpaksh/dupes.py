"""Finding the same fact told in other words (owner, Oct 8 2026, story 13970).

After compound statements are split (split.py), what still repeats are paraphrases: "rate cuts were off the
table" / "there is no option for interest rate cuts in the near term"; the decision as "by 0.25 percent" /
"by 25 basis points to 5.50 per cent". Code sees other words and calls them different; the story review's
one model call, doing seven jobs over up to 90 statements, found five of them on 13970.

Deciding was never the weak part (relate.py by code, then "same fact?" or "does A say all of B?" asked twice
in match.py); FINDING the pairs to ask about was. So this module only PROPOSES, in two small steps:
    1. one call sorts the story's statements into topics (the decision, the stance, what the repo rate is,
       EMIs, the governor's outlook): a far easier job for a small model than spotting duplicates among 80;
    2. per topic (at most 12 statements a call) one call answers one question: which pairs say the same
       fact, and which pairs where one says everything the other says and more.
Every proposal then goes through the existing deciders in consolidate.py: code first (numbers, negation,
dates), then the twice-asked question. A wrong proposal costs a call; it never merges two facts.
Answers are kept per topic (stories.analysis.dupe_checks, keyed by the topic's texts) and the topics per
statement set, so only what changed is asked again.
"""
from __future__ import annotations

import hashlib
import json
import logging

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

TOPIC_MAX = 12

TOPICS_PROMPT = """Below are numbered statements from news reports about ONE story. Sort them into TOPICS: a
topic is one subject a newspaper would write about in one paragraph (for a story on an interest-rate rise:
"the decision", "the policy stance", "what the repo rate is", "effect on loans and EMIs", "what the governor
said about the future"). Put statements that may say the same thing in other words in the SAME topic.
Every statement goes into exactly one topic. Keep topics small (2 to 12 statements); a statement unlike any
other is a topic of its own.

{lines}

Reply with JSON only: {{"topics": [[1, 4, 9], [2, 3], [5]]}}"""

PAIRS_PROMPT = """Below are numbered statements on ONE topic, from different news reports of one story.
Find the pairs that repeat each other:

"same"   - two statements that say the SAME fact in other words. Numbers may be written differently:
           "25 basis points" = "0.25 percent"; "Rs 1 crore" = "10 million rupees". Example: "Rate cuts are off
           the table" and "There is no option for interest rate cuts in the near term" -> same.
"covers" - one statement says everything the other says, and more. Give the longer one first. Example:
           "The RBI raised the repo rate by 25 basis points to 5.50 per cent" covers "The RBI increased the repo
           rate by 0.25 percent".
Not a pair: two DIFFERENT facts about the same subject ("EMIs on home loans will rise" / "fixed-rate loans are
not affected"), different numbers, different people, places or times, one saying "not" and the other not.
If you are not sure, leave the pair out.

{lines}

Reply with JSON only: {{"same": [[1, 3]], "covers": [[2, 4]]}}"""


def _h(texts: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(texts)).encode()).hexdigest()[:16]


def _numbered(ids: list[int], texts: dict[int, str]) -> str:
    return "\n".join(f"{k + 1}. {texts[i]}" for k, i in enumerate(ids))


def _pairs(xs, n: int) -> list[tuple[int, int]]:
    out = []
    for p in xs or []:
        try:
            a, b = int(p[0]) - 1, int(p[1]) - 1
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= a < n and 0 <= b < n and a != b:
            out.append((a, b))
    return out


def topics(router: Router, texts: dict[int, str], cache: dict) -> list[list[int]]:
    ids = sorted(texts)
    key = "t" + _h([texts[i] for i in ids])
    if key in cache:                                   # kept by text: ids change after merges
        by_text = {t: i for i, t in texts.items()}
        return [[by_text[t] for t in grp if t in by_text] for grp in cache[key]]
    try:
        res = router.call("light", TOPICS_PROMPT.format(lines=_numbered(ids, texts)), json_out=True,
                          max_output_tokens=1200)
    except QuotaExhausted:
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("topic grouping failed: %s", str(e)[:200])
        return []
    seen: set[int] = set()
    out: list[list[int]] = []
    for t in (res.data or {}).get("topics") or [] if isinstance(res.data, dict) else []:
        grp = []
        for x in t if isinstance(t, list) else []:
            try:
                i = ids[int(x) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            if i not in seen:
                seen.add(i)
                grp.append(i)
        if grp:
            out.append(grp)
    # a statement the model left out is a topic of its own; a topic too big for one question is cut in turns
    out += [[i] for i in ids if i not in seen]
    final = [g[k:k + TOPIC_MAX] for g in out for k in range(0, len(g), TOPIC_MAX)]
    cache[key] = [[texts[i] for i in g] for g in final]
    return final


def propose(router: Router | None, texts: dict[int, str], cache: dict) -> tuple[set[tuple[int, int]], list[tuple[int, int]]]:
    """(pairs proposed as the same fact, (detailed, short) pairs proposed as covering). Proposals only."""
    same: set[tuple[int, int]] = set()
    covers: list[tuple[int, int]] = []
    if router is None or len(texts) < 2:
        return same, covers
    for grp in topics(router, texts, cache):
        if len(grp) < 2:
            continue
        key = "p" + _h([texts[i] for i in grp])
        got = cache.get(key)
        if got is None:
            try:
                res = router.call("light", PAIRS_PROMPT.format(lines=_numbered(grp, texts)), json_out=True,
                                  max_output_tokens=600)
            except QuotaExhausted:
                break
            except Exception as e:  # noqa: BLE001
                log.warning("duplicate proposals failed: %s", str(e)[:200])
                continue
            data = res.data if isinstance(res.data, dict) else {}
            # kept by text, so the answer stays right when ids change after merges
            got = {"same": [[texts[grp[a]], texts[grp[b]]] for a, b in _pairs(data.get("same"), len(grp))],
                   "covers": [[texts[grp[a]], texts[grp[b]]] for a, b in _pairs(data.get("covers"), len(grp))]}
            cache[key] = got
        by_text = {texts[i]: i for i in grp}
        for a, b in got.get("same") or []:
            if a in by_text and b in by_text:
                same.add(tuple(sorted((by_text[a], by_text[b]))))
        for big, small in got.get("covers") or []:
            if big in by_text and small in by_text:
                covers.append((by_text[big], by_text[small]))
    return same, covers
