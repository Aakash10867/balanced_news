"""Re-run the writing stages on stored real stories, with real models, without touching the site.

Copies chosen stories (and every published page, for thread links) from the database into a local
SQLite file, then runs consolidation, importance, threads, headline and the essay writer there.
Results go to the `diagnostics` table (kind 'replay'). Nothing in the real tables changes except
the shared quota counters.

    python -m nishpaksh.tools.replay --stories 10192,10254,10360
    python -m nishpaksh.tools.replay --stories 12099,14259 --mode context   # only the context checks (cheap)
    python -m nishpaksh.tools.replay --stories 13970 --mode split           # compound statements split, matched
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile

from sqlalchemy import insert as sa_insert, text as sql

from ..config import database_url, gemini_api_keys, load_yaml
from ..db import Store, articles, canonical, claims, delete, md, published, select, stories, story_links
from ..router import GeminiBackend, Router

log = logging.getLogger("replay")
SKIP_COLS = {"embedding", "minhash"}   # article bodies are skipped separately (only articles have them)


def _copy(src: Store, dst: Store, table, where) -> int:
    cols = [c for c in table.c if c.name not in SKIP_COLS and not (table is articles and c.name == "text")]
    rows = src.rows(select(*cols).where(where))
    if rows:
        with dst.engine.begin() as c:
            for i in range(0, len(rows), 500):
                c.execute(sa_insert(table), rows[i:i + 500])
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stories", required=True)
    p.add_argument("--mode", default="write", choices=["write", "context", "split"],
                   help="context: only the context checks; split: split compound statements, match and review "
                        "(statements before and after); both a few Flash-Lite calls per story")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ids = [int(x) for x in a.stories.split(",") if x.strip()]
    prod = Store(database_url())
    local = Store(f"sqlite:///{tempfile.mkdtemp()}/replay.db")
    local.init()
    pub_ids = [r["story_id"] for r in prod.rows(select(published.c.story_id))]
    keep = sorted(set(ids) | set(pub_ids))
    for t, col in ((stories, stories.c.id), (articles, articles.c.story_id), (claims, claims.c.story_id),
                   (canonical, canonical.c.story_id), (published, published.c.story_id)):
        log.info("copied %d rows of %s", _copy(prod, local, t, col.in_(keep)), t.name)
    _copy(prod, local, story_links, story_links.c.child_id.in_(keep))
    old = {r["story_id"]: r["headline_en"] for r in local.rows(select(published.c.story_id, published.c.headline_en))}

    # the context checks are a test: not held back by the day's pacing (a paced night run answered nothing)
    router = Router(load_yaml("models.yaml")["tiers"], [GeminiBackend(k) for k in gemini_api_keys()], prod,
                    paced=False)   # a test on stored stories: not held back by the day's pacing
    router.resolve()
    from .. import compose
    from ..consolidate import consolidate_story
    compose.translate_payload = lambda store, router, payload: dict(payload)   # no Hindi needed here
    report = []
    for sid in ids:
        entry = {"story": sid, "old_headline": old.get(sid)}
        if a.mode == "split":
            try:
                from .. import match, split
                from ..consolidate import consolidate_story
                from ..narrative import ordered_items

                def statements():
                    pl = compose.build_payload(local, router, sid) or {}
                    return [i["text"] for i in ordered_items(pl)] if pl else []   # context included
                before = statements()
                entry["split"] = split.split_story(local, router, sid)
                match.match_story(local, router, sid)
                entry["consolidate"] = consolidate_story(local, router, sid)
                after = statements()
                entry.update(before=len(before), after=len(after), statements=[t[:160] for t in after][:80])
            except Exception as e:  # noqa: BLE001
                log.exception("story %s failed", sid)
                entry["error"] = repr(e)[:500]
            report.append(entry)
            log.info("%s", json.dumps(entry)[:800])
            _save(prod, [entry])
            continue
        if a.mode == "context":
            try:
                payload = compose.build_payload(local, router, sid)
                if payload is None:
                    entry["error"] = "no payload"
                else:
                    before = [(i.get("role"), i["text"]) for i in payload.get("context") or []]
                    from .. import belong
                    compose.drop_outlet_self_talk(payload)
                    belong.check(local, router, sid, payload)
                    kept = {i["text"] for i in payload.get("context") or []}
                    checks = ((local.one(select(stories.c.analysis).where(stories.c.id == sid)) or {})
                              .get("analysis") or {}).get("context_checks") or {}
                    entry["context"] = [{"role": r, "text": t[:200],
                                         "result": "kept" if t in kept else "other news",
                                         "answers": checks.get(belong._key(t))}
                                        for r, t in before]
            except Exception as e:  # noqa: BLE001
                log.exception("story %s failed", sid)
                entry["error"] = repr(e)[:500]
            report.append(entry)
            log.info("%s", json.dumps(entry)[:800])
            _save(prod, [entry])
            continue
        try:
            # as in preparation (run.analyse): compound statements split, then matched, then reviewed
            from .. import match, split
            entry["split"] = split.split_story(local, router, sid)
            match.match_story(local, router, sid)
            entry["consolidate"] = consolidate_story(local, router, sid)
            # a published article is never rewritten; on this scratch copy it is written afresh
            local.exec(delete(published).where(published.c.story_id == sid))
            ok = compose.publish_story(local, router, sid)
            row = local.one(select(published).where(published.c.story_id == sid))
            pe = (row or {}).get("payload_en") or {}
            nar = pe.get("narrative") or {}
            entry.update(
                published=ok, headline=pe.get("headline"), importance=pe.get("importance"), rank=pe.get("rank"),
                parents=pe.get("parents"), background=[b["text"] for b in pe.get("background") or []],
                model=nar.get("model"), rejected=nar.get("rejected"), reasons=nar.get("reject_reasons"),
                lead=pe.get("lead"), review=_review(local, sid),
                paragraphs=[[x["text"] for x in para] for para in nar.get("paragraphs") or []],
                section_keys=nar.get("section_keys"), first_draft=nar.get("first_draft_reasons"),
                plan=nar.get("plan"), flow=nar.get("flow"))
        except Exception as e:  # noqa: BLE001
            log.exception("story %s failed", sid)
            entry["error"] = repr(e)[:500]
        report.append(entry)
        log.info("%s", json.dumps(entry)[:800])
        _save(prod, [entry])           # each story saved as it finishes: a run cut off still leaves its results
    log.info("replay done: %d stories", len(report))


def _review(local: Store, sid: int) -> dict:
    """What the story review did, readable: topics, proposed pairs, and which line each folded line went into."""
    an = (local.one(select(stories.c.analysis).where(stories.c.id == sid)) or {}).get("analysis") or {}
    text = {r["id"]: r["text"] for r in local.rows(select(canonical.c.id, canonical.c.text).where(canonical.c.story_id == sid))}
    dc = an.get("dupe_checks") or {}
    sc = an.get("same_checks") or {}
    return {"topics": [g for k, v in dc.items() if k.startswith("t") for g in v if len(g) > 1][:30],
            "proposed": {k: v for k, v in dc.items() if k.startswith("p")},
            "asked": sum(1 for k in sc if not k.startswith("f")), "first_yes": sum(1 for k, v in sc.items() if k.startswith("f") and v),
            "folded": [[text.get(int(a), a), text.get(int(b), b)] for a, b in (an.get("covered") or {}).items()][:40]}


def _save(prod: Store, report: list) -> None:
    with prod.engine.begin() as c:
        c.execute(sql("insert into diagnostics (kind, report) values ('replay', :r)"), {"r": json.dumps(report)})


if __name__ == "__main__":
    main()
