"""Titles learned from the outlets (owner, Oct 10 2026): the registry grows from the news, in a file on GitHub.

config/titles.yaml is written by hand and cannot hold every title in Indian news ("Captain", "Chancellor",
"Superintendent", ...). A title the registry does not know is a word the name pass cannot place: it cannot tell
the title from the name, nor when the person may be called by the surname. This module finds such words in the
articles the newsroom already holds and APPENDS them to config/titles_learned.yaml, which the job commits to the
repository (.github/scripts/learn_titles.sh) and titles.py reads together with the hand-written file.

A word is learned only on plain counting, with no model (a model alone is never the source of an entry):

  - it stands directly before the full name of a person the statements attribute something to (the name comes
    from `claims.attributed_to`, so "Anchor Rohit Sharma" counts and "Yesterday Rohit Sharma" is looked at below),
  - in the middle of a sentence (after a lower-case word): a sentence opener ("Meanwhile", "Yesterday") also
    stands before names but only at the start,
  - in at least 2 INDEPENDENT groups of outlets (wire copies, one owner, state media are one: wire.independence_groups)
    and at least 2 outlets,
  - before at least 2 different people (a title is shared; an organisation's name before its one spokesman is not),
  - and the same word also appears in lower case in the articles at least twice (\"the captain said\": a common
    noun, not a place, a party or a company).

An entry carries the date it was first seen and who it was seen before. Entries are only ever appended. A wrong one
is deleted by hand from the file and its word added to `rejected:` (at the top of the file), which this module reads
and never learns again. People change office, so the file records dates, not claims about who holds what.

    python -m nishpaksh.learn [--file config/titles_learned.yaml] [--days 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

from . import titles

log = logging.getLogger(__name__)

FILE = "titles_learned.yaml"
MIN_GROUPS = 2          # independent groups of outlets
MIN_OUTLETS = 2
MIN_NAMES = 2           # different people the word stood before
MIN_LOWER = 2           # lower-case uses of the same word in the articles
MAX_NEW = 20            # most entries one run adds: a bug cannot flood the file

HEADER = """# Titles learned from the news by nishpaksh/learn.py (owner, Oct 10 2026). The job appends here and commits it.
# A title enters this file only when the same word stood in the middle of a sentence directly before the full
# name of 2+ different people, in 2+ independent outlets, and also appears in lower case in the articles.
# `seen` says before whom, and `learned` on what day. Entries are only appended; the hand-written
# titles.yaml always wins over an entry here.
#
# A WRONG ENTRY: delete its line below and add its word to `rejected:`, so it is never learned again.
# Keep `roles:` LAST in this file: new entries are appended at the end.
rejected: []

roles:
"""

# a capitalised word before a name that is not a title: names of months and days, openers, honorifics
_NOT = {"the", "a", "an", "and", "but", "or", "for", "from", "with", "that", "this", "also", "after", "before",
        "while", "when", "then", "later", "earlier", "meanwhile", "however", "yesterday", "today", "tomorrow",
        "according", "said", "says", "told", "by", "on", "in", "at", "to", "of", "as", "if", "since", "during"}

_WORD = r"[A-Z][a-z][\w'’]*(?:-[A-Z][a-z]+)?"


def rejected() -> set[str]:
    """The words the owner has struck out, lower case."""
    try:
        from .config import load_yaml
        return {str(x).lower() for x in (load_yaml(FILE).get("rejected") or [])}
    except Exception:  # noqa: BLE001 (no file, or one that does not parse: nothing is rejected, nothing breaks)
        return set()


def _known(word: str) -> bool:
    w = word.lower()
    return (word in titles.title_words() or word in titles.person_title_words() or w in titles.role_nouns()
            or w in {h.lower() for h in titles.honorifics()} or w in _NOT or w in titles._OPENERS)


def _bare(name: str) -> str:
    return titles.strip(re.sub(r"\s+", " ", name or "").strip())


def person_names(attributed: list[str]) -> set[str]:
    """Full names (2 to 4 capitalised words) among the names statements are attributed to; titles stripped, bodies out."""
    from . import voice
    out = set()
    for raw in attributed:
        name = _bare(raw)
        words = name.split()
        if (2 <= len(words) <= 4 and all(re.fullmatch(r"[A-Z][\w.'’-]*", w) for w in words)
                and not voice.is_body(name) and name == " ".join(words)):
            out.add(name)
    return out


def scan(articles: list[dict], names_of: dict[int, set[str]], groups: dict[int, str],
         outlets_known: set[str] = frozenset()) -> dict[str, dict]:
    """Evidence per word: {word: {groups, outlets, names, lower, first_name}}. `articles`: id, outlet, text;
    `names_of`: article id -> the full names of its speakers; `groups`: article id -> independence group."""
    ev: dict[str, dict] = defaultdict(lambda: {"groups": set(), "outlets": set(), "names": set(), "lower": 0})
    corpus = "\n".join(a.get("text") or "" for a in articles)
    skip = {o.lower() for o in outlets_known}
    for a in articles:
        text = a.get("text") or ""
        for name in names_of.get(a["id"]) or ():
            sur = {w.lower() for w in name.split()}
            # mid-sentence only: the word follows a lower-case word, and stands right before the full name
            for m in re.finditer(rf"(?<=[a-z] )({_WORD})\s+{re.escape(name)}(?![\w-])", text):
                word = m.group(1)
                if _known(word) or word.lower() in sur or word.lower() in skip or len(word) < 3:
                    continue
                e = ev[word]
                e["groups"].add(groups.get(a["id"], f"a{a['id']}"))
                e["outlets"].add(a.get("outlet") or f"a{a['id']}")
                e["names"].add(name)
    for word, e in ev.items():
        e["lower"] = len(re.findall(rf"(?<![\w-]){re.escape(word.lower())}(?![\w-])", corpus))
    return dict(ev)


def pick(ev: dict[str, dict], today: dt.date | None = None) -> list[dict]:
    """The words with enough evidence, as role entries for the file (most evidence first, at most MAX_NEW)."""
    today = today or dt.date.today()
    no = rejected()
    out = []
    for word, e in sorted(ev.items(), key=lambda kv: (-len(kv[1]["groups"]), -len(kv[1]["names"]), kv[0])):
        if (len(e["groups"]) >= MIN_GROUPS and len(e["outlets"]) >= MIN_OUTLETS and len(e["names"]) >= MIN_NAMES
                and e["lower"] >= MIN_LOWER and word.lower() not in no and not _known(word)):
            out.append({"id": f"learned_{re.sub(r'[^a-z0-9]+', '_', word.lower()).strip('_')}", "forms": [word],
                        "ref": f"the {word.lower()}", "learned": today.isoformat(), "seen": sorted(e["names"])[:3]})
    return out[:MAX_NEW]


def line(entry: dict) -> str:
    """One entry as one flow-style YAML line (JSON strings are valid YAML strings), appended to the file."""
    return "  - {" + ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in entry.items()) + "}\n"


def append(entries: list[dict], path: Path) -> int:
    """Add the entries the file does not hold yet (by id or by form, case-insensitive), at its end. The file is
    created with its header when missing. Returns how many lines were added."""
    if not path.exists():
        path.write_text(HEADER, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    have = text.lower()
    new = [e for e in entries if f"forms: {json.dumps(e['forms'], ensure_ascii=False)}".lower() not in have
           and f"id: {json.dumps(e['id'])}" not in text]
    if new:
        path.write_text(text + ("" if text.endswith("\n") else "\n") + "".join(line(e) for e in new), encoding="utf-8")
    return len(new)


def run(store, path: Path | None = None, days: float = 3, dry_run: bool = False) -> dict:
    """Read the newsroom's recent articles and their speakers, append what has the evidence."""
    from . import heavy, wire
    from .config import CONFIG_DIR
    from .db import articles, claims, select
    path = path or (CONFIG_DIR / FILE)
    since = dt.datetime.utcnow() - dt.timedelta(days=days)
    rows = store.rows(select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.agency,
                             articles.c.wire_group, *heavy.columns(store, "text"))
                      .where(articles.c.published_at >= since))
    rows = heavy.fill(store, rows, "text")
    rows = [r for r in rows if r.get("text")]
    ids = [r["id"] for r in rows]
    attributed: dict[int, list[str]] = defaultdict(list)
    for start in range(0, len(ids), 400):
        for c in store.rows(select(claims.c.article_id, claims.c.attributed_to)
                            .where(claims.c.article_id.in_(ids[start:start + 400]))):
            if c.get("attributed_to"):
                attributed[c["article_id"]].append(c["attributed_to"])
    names_of = {i: person_names(v) for i, v in attributed.items()}
    ev = scan(rows, names_of, wire.independence_groups(rows), {r["outlet"] for r in rows if r.get("outlet")})
    found = pick(ev)
    added = 0 if dry_run else append(found, path)
    return {"articles": len(rows), "candidates": len(ev), "qualified": len(found), "added": added,
            "words": [e["forms"][0] for e in found]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=None)
    p.add_argument("--days", type=float, default=3)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .config import database_url
    from .db import Store
    store = Store(database_url())
    res = run(store, Path(a.file) if a.file else None, a.days, a.dry_run)
    log.info("learned titles: %s", res)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
