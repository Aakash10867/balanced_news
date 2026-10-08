"""Does a context line belong to the story? (owner, Oct 8 2026)

A news page carries other news too: a sidebar, a list of other videos, "also read" links. Reading picks
some of it up as context (story 12099: a Kerala vigilance probe under "Related events" in the cheetah
story, from a video page; on the 60 latest articles, a cricket comeback in a story of students' deaths, a
plague death in Siberia in the story of Indians in the Russian army).

Each context role tells the reader something different about the story, so a wrong one does different
harm, and each has its own bar (owner: background can be lenient, related should rarely slip, explanation
must not):

    role          what the page tells the reader            harm if it is other news   bar
    background    the setting, how it came about            mild                       kept unless the model says
                                                                                       "other news" twice
    related       a separate event linked to this one       a false link               "connected" twice
    explanation   what something in the story means         a false explanation        "connected" twice, and the
                                                                                       model names the term it
                                                                                       explains, which code finds in
                                                                                       the story and in the line
    reaction, next                                          (not asked; code stage only)

Three stages, the same for every line:
    0. reading is told what to ignore (extract.py: other stories the page lists)
    1. code drops what it can be sure of: a related, explanation, reaction or next line that shares no
       specific word with the story (`code_stage`). Background is never dropped by words: its setting
       often shares none ("Heavy rainfall in Nepal's catchment raised the Gandak" in a Bihar flood story)
    2. the model is asked one plain question, connected / other news (with the term for an explanation),
       and each line is judged against its role's bar (`model_stage`). Answers are kept raw per line text
       (stories.analysis.context_checks), so a retried story, or a line whose role changes, is not asked
       again for what is already known.
Not fully asked (quota): the story WAITS for the next desk run (`check` reports it pending; publish_story
returns "context not checked"). A paced night run once answered none of a story's lines and every related
and explanation line was dropped: unasked is not an answer.

Shared words alone could not do stage 2: on the 60 latest articles a word rule dropped "the project is
expected to generate employment" in the project's own story, and a name rule dropped 38 of 154 related
lines yet kept the cricket one.
"""
from __future__ import annotations

import hashlib
import logging
import re

from .db import Store, select, stories, update
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

MODEL_ROLES = ("background", "related", "explanation")
# the bar per role: how many "connected" answers are needed to keep a line, and whether the term an
# explanation explains must be found by code
LENIENT, STRICT, STRICTEST = "lenient", "strict", "strictest"
BAR = {"background": LENIENT, "related": STRICT, "explanation": STRICTEST}

GENERIC = {"india", "indian", "government", "state", "states", "said", "says", "also", "year", "years", "time",
           "people", "officials", "official", "report", "reports", "according", "country", "national", "today",
           "week", "month", "first", "second", "third", "most", "more", "many", "several", "after", "before"}

PROMPT = """A news story, and some lines from the pages that reported it. A news page often carries OTHER
news too (a sidebar, a list of other videos, "also read" links, a live blog). For each numbered line decide:

"connected"  - the line belongs to THIS story: its background (how it came about, earlier events of the
               same people, place or matter), the wider situation it happens in (the floods in a story about
               flood victims, the war in a story about a ship attacked in it), a fact about a person, place
               or body in the story, an explanation of something in it, or a separate event linked to it
               (the same people, the same dispute, an earlier case of the same kind).
"other news" - the line is about something else that only happened to be on the same page.

A line marked (explanation) says what something means. It is "connected" only if what it explains is
named in THE STORY: then give that thing, in the story's own words, as "term" ("Section 22", "Project
Cheetah", "special intensive revision"). If it explains something the story does not mention, it is
"other news".

Examples, for a story "India's cheetah population reached 60 after a cheetah gave birth to five cubs in
Kuno National Park under Project Cheetah":
  (background) "Cheetahs were brought to Kuno from Namibia in 2022." -> connected
  (background) "Cheetahs now live in Kuno National Park and Gandhi Sagar Sanctuary." -> connected
  (explanation) "Project Cheetah is India's programme to bring back the cheetah." -> connected, term "Project Cheetah"
  (related) "Kerala's minister defended a vigilance probe into a road project." -> other news
For a story "Four students died in Vrindavan; the District Magistrate ordered an inquiry":
  (background) "The gurukul is run by Ashish Sharma and has 200 students." -> connected (a fact about the place)
  (related) "Bhuvneshwar Kumar has returned to the Indian T20 team." -> other news (sport)
  (explanation) "The repo rate is the rate at which the RBI lends to banks." -> other news (not in the story)
For a story "A police officer pointed a gun at flood victims in Saran and was suspended":
  (background) "Floods have affected 9.87 lakh people in six Bihar districts." -> connected (the situation)
For a story "A drone struck an oil tanker in the Black Sea":
  (related) "Two days earlier, the cargo ship MV Royad Mammadov was attacked in the Black Sea." -> connected

THE STORY:
{story}

LINES:
{lines}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "connected"}}, {{"n": 2, "answer": "other news"}},
{{"n": 3, "answer": "connected", "term": "Project Cheetah"}}]}}"""

BATCH = 8
MAX_MISSES = 3      # a line the model was asked about this often without an answer is decided by its bar


def _roots(text: str) -> set[str]:
    from .frames import words as roots
    from .narrative import _subject_words
    return set(roots(" ".join(_subject_words(text or "") - GENERIC)))


def _core(payload: dict) -> list[dict]:
    from .narrative import is_core
    out, seen = [], set()
    for tier in payload.get("timeline") or []:
        out += tier
    for k in ("undated", "established", "contested"):
        out += payload.get(k) or []
    return [i for i in out if is_core(i) and not (id(i) in seen or seen.add(id(i)))]


# ------------------------------------------------------------------ stage 1: code, only where it is sure
def code_stage(payload: dict) -> int:
    """A related, explanation, reaction or next line sharing no specific word or name (by root) with the
    story's own event is dropped (Oct 7 2026: "Hindustan was founded in 1936 ..." in a story on stubble
    burning). Background is left to the model: a story's setting often shares no word with it."""
    core = _core(payload)
    if not core:
        return 0
    link = set()
    for i in core:
        link |= _roots(i.get("text") or "")
    keep, dropped = [], 0
    for i in payload.get("context") or []:
        if i.get("role") == "background" or _roots(i.get("text") or "") & link:
            keep.append(i)
        else:
            dropped += 1
            log.info("context line dropped, nothing in common with the story: %s", (i.get("text") or "")[:100])
    payload["context"] = keep
    return dropped


# ------------------------------------------------------------------ stage 2: the model, against each role's bar
def _key(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _story_text(payload: dict) -> tuple[str, str]:
    """What the model sees (the news first, then the story's own statements, up to ~1,400 characters) and
    all of the story's own text (for finding an explanation's term)."""
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
    return shown.strip(), " ".join(i["text"] for i in items)


ACRONYM = re.compile(r"\b[A-Z][A-Z0-9-]{1,7}s?\b")


def _term_found(term: str, line: str, story: str) -> bool:
    """The thing an explanation explains must be in the story AND in the line: by root for words ("Section 22"
    in a story that never mentions it is no explanation of this story), as written for acronyms ("DGP", "HAPS",
    "FIR": words under four letters have no root, and the first replay dropped a DGP explanation the model had
    called connected twice)."""
    def found(where: str) -> bool:
        if _roots(term) & _roots(where):
            return True
        return any(re.search(rf"\b{re.escape(a)}\b", where) for a in ACRONYM.findall(term))
    return bool(_roots(term) or ACRONYM.findall(term)) and found(story) and found(line)


def _verdict(role: str, answers: list[dict], line: str, story: str) -> bool | None:
    """True keep, False drop, None: another answer is needed."""
    bar = BAR[role]
    yes = [a for a in answers if a.get("a") == "connected"]
    no = [a for a in answers if a.get("a") != "connected"]
    if bar == LENIENT:
        if yes:
            return True
        return False if len(no) >= 2 else None
    if no:
        return False
    if bar == STRICTEST and any(not _term_found(a.get("term") or "", line, story) for a in yes):
        return False
    return True if len(yes) >= 2 else None


def model_stage(store: Store, router: Router | None, story_id: int, payload: dict) -> tuple[int, int]:
    """(lines dropped, lines still undecided). Undecided lines are left in; the caller decides (the desk
    makes the story wait)."""
    lines = [i for i in payload.get("context") or [] if i.get("role") in MODEL_ROLES and i.get("text")]
    if not lines:
        return 0, 0
    row = store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}
    an = dict(row.get("analysis") or {})
    cache: dict[str, list[dict]] = {k: list(v) for k, v in (an.get("context_checks") or {}).items()
                                    if isinstance(v, list)}       # earlier entries were plain verdicts
    misses: dict[str, int] = dict(an.get("context_misses") or {})   # asked, no usable answer
    shown, story = _story_text(payload)

    def ask(todo: list[dict]) -> None:
        for start in range(0, len(todo), BATCH):
            chunk = todo[start:start + BATCH]
            body = "\n".join(f'{k + 1}. ({i["role"]}) "{i["text"]}"' for k, i in enumerate(chunk))
            try:
                res = router.call("page", PROMPT.format(story=shown, lines=body), json_out=True, max_output_tokens=500)
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
                    cache.setdefault(_key(chunk[n]["text"]), []).append(
                        {"a": "connected" if a == "connected" else "other", "term": str(item.get("term") or "")[:120]})
            for n, i in enumerate(chunk):
                if n not in got:
                    misses[_key(i["text"])] = misses.get(_key(i["text"]), 0) + 1

    if router is not None:
        # the first asking for lines never asked; the second, with the lines in reverse order, only where
        # the bar still needs it (a background line that said other news, a related or explanation line
        # that said connected)
        ask([i for i in lines if not cache.get(_key(i["text"]))])
        ask(list(reversed([i for i in lines if len(cache.get(_key(i["text"])) or []) == 1
                           and _verdict(i["role"], cache[_key(i["text"])], i["text"], story) is None])))
        if cache != (an.get("context_checks") or {}) or misses != (an.get("context_misses") or {}):
            an["context_checks"], an["context_misses"] = cache, misses
            store.exec(update(stories).where(stories.c.id == story_id).values(analysis=an))
    keep, dropped, pending = [], 0, 0
    for i in payload.get("context") or []:
        if i in lines:
            v = _verdict(i["role"], cache.get(_key(i["text"])) or [], i["text"], story)
            if v is None and misses.get(_key(i["text"]), 0) >= MAX_MISSES:
                v = BAR[i["role"]] == LENIENT               # the model never answers this line: the bar's safe side
            if v is None:                                   # not fully asked (quota): not decided
                pending += 1
                v = True
            if not v:
                dropped += 1
                log.info("context line dropped as other news (%s, story %s): %s", i["role"], story_id, i["text"][:100])
                continue
        keep.append(i)
    payload["context"] = keep
    return dropped, pending


def check(store: Store, router: Router | None, story_id: int, payload: dict) -> dict:
    """Stage 1 then stage 2: {"dropped": n, "pending": lines the model could not yet answer}."""
    n = code_stage(payload)
    dropped, pending = model_stage(store, router, story_id, payload)
    return {"dropped": n + dropped, "pending": pending}
