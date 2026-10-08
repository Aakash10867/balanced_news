"""Does a context line belong to the story? (owner, Oct 8 2026)

A news page carries other news too: a sidebar, a list of other videos, "also read" links. Reading picks
some of it up as context (story 12099: a Kerala vigilance probe under "Related events" in the cheetah
story, from a video page; on the 60 latest articles, a cricket comeback in a story of students' deaths, a
plague death in Siberia in the story of Indians in the Russian army).

The owner's rule, for background, related events and explanations alike: "It's better to have something
unrelated in the story and think, why is this here, than not to have something important." So a line is
dropped only when the model says, twice, that it is other news; anything else keeps it: one "connected",
an unanswered question (quota), a model that never answers. Stricter bars were tried on 16 real stories
and lost real context every time (the Prakash Singh rules in a DGP story when the model wavered once,
"Section 22" in a story on voter deletion, the Bihar floods in a story about flood victims).

Two stages:
    0. reading is told what to ignore (extract.py: other stories the page lists around the article)
    1. the model is asked one plain question per background / related / explanation line: connected or
       other news, with the story's news and own statements in front of it and worked examples of the
       traps. A line that says "other news" is asked again, with the lines in reverse order; two "other
       news" drop it. Answers are kept per line text (stories.analysis.context_checks): a retried story
       is not asked again. Reactions and "what next" are not asked (owner).
No code rule drops a line by its words: shared words cannot tell (a word rule dropped "the project is
expected to generate employment" in the project's own story; a no-shared-word rule dropped "Indian Air
Force helicopters dropped rations in Saran" in a story on Saran's flood victims).
"""
from __future__ import annotations

import hashlib
import logging

from .db import Store, select, stories, update
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

MODEL_ROLES = ("background", "related", "explanation")

PROMPT = """A news story, and some lines from the pages that reported it. A news page often carries OTHER
news too (a sidebar, a list of other videos, "also read" links, a live blog). For each numbered line decide:

"connected"  - the line belongs to THIS story: its background (how it came about, earlier events of the
               same people, place or matter), the wider situation it happens in (the floods in a story about
               flood victims, the war in a story about a ship attacked in it), a fact about a person, place
               or body in the story, an explanation of something in it, or a separate event linked to it
               (the same people, the same dispute, an earlier case of the same kind).
"other news" - the line is about something else that only happened to be on the same page.

Examples, for a story "India's cheetah population reached 60 after a cheetah gave birth to five cubs in
Kuno National Park under Project Cheetah":
  "Cheetahs were brought to Kuno from Namibia in 2022." -> connected (background)
  "Cheetahs now live in Kuno National Park and Gandhi Sagar Sanctuary." -> connected (a fact about them)
  "Project Cheetah is India's programme to bring back the cheetah." -> connected (explains it)
  "Kerala's minister defended a vigilance probe into a road project." -> other news
For a story "Four students died in Vrindavan; the District Magistrate ordered an inquiry":
  "The gurukul is run by Ashish Sharma and has 200 students." -> connected (a fact about the place)
  "Bhuvneshwar Kumar has returned to the Indian T20 team." -> other news (sport)
For a story "A police officer pointed a gun at flood victims in Saran and was suspended":
  "Floods have affected 9.87 lakh people in six Bihar districts." -> connected (the situation)
For a story "A drone struck an oil tanker in the Black Sea":
  "Two days earlier, the cargo ship MV Royad Mammadov was attacked in the Black Sea." -> connected
If you are not sure, answer "connected".

THE STORY:
{story}

LINES:
{lines}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "connected"}}, {{"n": 2, "answer": "other news"}}]}}"""

BATCH = 8


def _key(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _story_text(payload: dict) -> str:
    """What the model sees: the news first, then the story's own statements, up to ~1,400 characters (with
    the news alone it did not see that floods were the setting of a story about flood victims)."""
    from .narrative import ordered_items
    from .news import pick_news
    items = [i for i in ordered_items(payload) if i.get("role", "core") == "core"] or ordered_items(payload)
    by_id = {i["id"]: i for i in items}
    news = [x for x in pick_news(items)[:2] if x in by_id]
    own = [by_id[x]["text"] for x in news] + [i["text"] for i in items if i["id"] not in news]
    shown, n = "", 0
    for t in own:
        if n >= 10 or len(shown) + len(t) > 1400:
            break
        shown += f"- {t}\n"
        n += 1
    return shown.strip()


def _other_news(answers: list) -> bool:
    """Dropped only on two "other news" answers (owner: better a line that makes a reader wonder than a
    missing one)."""
    return sum(1 for a in answers if isinstance(a, dict) and a.get("a") == "other") >= 2


def check(store: Store, router: Router | None, story_id: int, payload: dict) -> int:
    """Drops the background, related and explanation lines the model calls other news twice; the number
    dropped."""
    lines = [i for i in payload.get("context") or [] if i.get("role") in MODEL_ROLES and i.get("text")]
    if not lines:
        return 0
    row = store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}
    an = dict(row.get("analysis") or {})
    cache: dict[str, list] = {k: list(v) for k, v in (an.get("context_checks") or {}).items() if isinstance(v, list)}
    shown = _story_text(payload)

    def ask(todo: list[dict]) -> None:
        for start in range(0, len(todo), BATCH):
            chunk = todo[start:start + BATCH]
            body = "\n".join(f'{k + 1}. "{i["text"]}"' for k, i in enumerate(chunk))
            try:
                res = router.call("page", PROMPT.format(story=shown, lines=body), json_out=True, max_output_tokens=400)
            except QuotaExhausted:
                return
            except Exception as e:  # noqa: BLE001
                log.warning("context check failed: %s", str(e)[:200])
                continue
            got = set()
            for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
                try:
                    n = int(item["n"]) - 1
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= n < len(chunk) and n not in got:
                    got.add(n)
                    a = str(item.get("answer") or "").strip().lower()
                    cache.setdefault(_key(chunk[n]["text"]), []).append({"a": "other" if a == "other news" else "connected"})

    if router is not None:
        # first asking for lines never asked; a line that said "other news" once is asked again, the lines
        # in reverse order (two answers decide; a "connected" settles it at once)
        ask([i for i in lines if not cache.get(_key(i["text"]))])
        ask(list(reversed([i for i in lines if [a.get("a") for a in cache.get(_key(i["text"])) or []] == ["other"]])))
        if cache != (an.get("context_checks") or {}):
            an["context_checks"] = cache
            store.exec(update(stories).where(stories.c.id == story_id).values(analysis=an))
    keep, dropped = [], 0
    for i in payload.get("context") or []:
        if i in lines and _other_news(cache.get(_key(i["text"])) or []):
            dropped += 1
            log.info("context line dropped as other news (%s, story %s): %s", i["role"], story_id, i["text"][:100])
            continue
        keep.append(i)
    payload["context"] = keep
    return dropped
