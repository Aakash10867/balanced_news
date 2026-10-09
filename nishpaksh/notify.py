"""Notifications for readers (owner, Oct 9 2026).

A reader follows sections (any level: "justice", "justice/courts", "business/domains/hr"), states, people and
organisations, single stories and the daily recap. Whatever a reader chose, they are told, however many that is:
no daily cap, no quiet hours (owner). Follows are for notifications only; they never reorder or filter the site.

  * a new article in a followed section (primary or secondary: either counts), state or name -> "article"
  * a follow-up published to a followed story (it has the story as a parent)                   -> "article" too
  * the day's recap, for readers following it                                                   -> "recap"
  * audio a reader asked for is ready (audio.py)                                                -> "audio"

Every notification is a row in `notifications` (one per reader, kind and subject), which is also the reader's
inbox on the site (iPhones without the site installed, or readers who blocked notifications, still see it there).
Rows are sent by Web Push to every device the reader subscribed; a device that is gone (404/410) is removed.
The signing key is in Supabase Vault (`public.vapid_private_key()`, readable only by the pipeline role), or in the
environment as VAPID_PRIVATE_KEY.

Runs at the end of every desk run (`desk.py`, after publishing) and of the audio job:
    python -m nishpaksh.notify            # queue new articles and the recap, then send
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os

from sqlalchemy import and_, func, text as sql_text

from .db import (Store, follows, insert, notifications, notify_state, profiles, published, push_subscriptions, recaps,
                 select, update, utcnow, delete)

log = logging.getLogger(__name__)

SITE = "https://nishpakshnews.github.io/"
VAPID_PUBLIC = "BFEdVPEJCsATLjoBAx5aVqtwreIZuNadvNy6-5MdcM9blblHfBVHOd0aegFs5NVywQIJlzU4R4oI9fmUVi0PY5U"
VAPID_SUB = "https://nishpakshnews.github.io"   # the contact push services see
SEND_WITHIN = dt.timedelta(hours=48)       # an older unsent notification stays in the inbox, not pushed
IST = dt.timedelta(hours=5, minutes=30)
RECAP_HOUR_IST = 23                         # the desk run at 23:15 IST makes the day's recap

WORDS = {
    "en": {"followup": "Update to a story you follow", "established": "established", "outlets": "outlets",
           "recap": "Today on Nishpaksh", "recap_body": lambda n: f"{n} articles today: the recap is ready",
           "audio": "Your audio is ready", "place": "", "entity": ""},
    "hi": {"followup": "जिस ख़बर को आप फ़ॉलो करते हैं, उसमें नया", "established": "स्थापित", "outlets": "स्रोत",
           "recap": "आज निष्पक्ष पर", "recap_body": lambda n: f"आज {n} लेख: दिन का सार तैयार है",
           "audio": "आपका ऑडियो तैयार है", "place": "", "entity": ""},
}


# -- state ------------------------------------------------------------------------------------------------
def _state(store: Store, key: str) -> dict:
    r = store.one(select(notify_state.c.value).where(notify_state.c.key == key))
    return (r or {}).get("value") or {}


def _set_state(store: Store, key: str, value: dict) -> None:
    if store.one(select(notify_state.c.key).where(notify_state.c.key == key)):
        store.exec(update(notify_state).where(notify_state.c.key == key).values(value=value))
    else:
        store.exec(insert(notify_state).values(key=key, value=value))


def _naive(t):
    if t is None:
        return None
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t)
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


# -- matching -----------------------------------------------------------------------------------------------
def section_paths(cat: dict | None) -> set[str]:
    """Every section key an article is in, as readers follow them: "justice", "justice/courts",
    "business/domains/hr". Primary and secondary both count (owner)."""
    from .categories import PARENT, PARENT3, normalize
    cat = normalize(cat)
    out = set(cat.get("primary") or [])
    for s in cat.get("secondary") or []:
        if s in PARENT:
            out.add(f"{PARENT[s]}/{s}")
            out.add(PARENT[s])
    for t in cat.get("tertiary") or []:
        if t in PARENT3:
            s = PARENT3[t]
            out |= {f"{PARENT[s]}/{s}/{t}", f"{PARENT[s]}/{s}", PARENT[s]}
    return out


def reasons(article: dict, rows: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """reader -> [(kind, key)] of every follow the article matches."""
    from .people import matches as name_matches
    secs = section_paths(article.get("category"))
    places = set(article.get("places") or [])
    names = article.get("people") or []
    parents = {str(p.get("story_id")) for p in article.get("parents") or [] if isinstance(p, dict)}
    out: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        k, key = r["kind"], str(r["key"])
        hit = (k == "section" and key in secs) or (k == "place" and key in places) \
            or (k == "entity" and name_matches(key, names)) or (k == "story" and key in parents)
        if hit:
            out.setdefault(str(r["reader"]), []).append((k, key))
    return out


def _share(bar: dict) -> int:
    tot = sum(bar.values()) or 1
    return round(100 * bar.get("e", 0) / tot)


def _because(why: list[tuple[str, str]], article: dict, lang: str, label) -> str:
    if any(k == "story" for k, _ in why):
        return WORDS[lang]["followup"]
    names = []
    for k, key in why:
        names.append(label(k, key, lang, article))
    return " · ".join(dict.fromkeys(n for n in names if n))


def _label(kind: str, key: str, lang: str, article: dict) -> str:
    from .categories import labels as sec_labels
    from .places import labels as place_labels
    if kind == "section":
        sl = sec_labels(lang)
        last = key.split("/")[-1]
        return sl["primary"].get(last) or sl["secondary"].get(last) or sl["tertiary"].get(last) or key
    if kind == "place":
        return place_labels(lang).get(key, key)
    if kind == "entity":
        from .people import matches
        return next((n for n in article.get("people") or [] if matches(key, [n])), key)
    return ""


def _reader_langs(store: Store, readers: list[str]) -> dict[str, str]:
    if not readers:
        return {}
    out = {str(r["id"]): r["lang"] or "en" for r in store.rows(select(profiles.c.id, profiles.c.lang)
                                                             .where(profiles.c.id.in_(readers)))}
    for r in store.rows(select(push_subscriptions.c.reader, push_subscriptions.c.lang)
                        .where(push_subscriptions.c.reader.in_(readers))):
        out[str(r["reader"])] = r["lang"] or out.get(str(r["reader"]), "en")
    return out


def add_notification(store: Store, reader: str, kind: str, ref: str, title: str, body: str, url: str,
                     story_id: int | None = None) -> bool:
    """One notification per reader, kind and ref; False when it already exists."""
    if store.one(select(notifications.c.id).where(and_(notifications.c.reader == reader, notifications.c.kind == kind,
                                                         notifications.c.ref == ref))):
        return False
    try:
        store.exec(insert(notifications).values(reader=reader, kind=kind, ref=ref, story_id=story_id, title=title,
                                                body=body, url=url, created_at=utcnow()))
        return True
    except Exception as e:  # noqa: BLE001 - two jobs queueing the same one at once: the other won
        log.info("notification %s/%s/%s not added: %s", reader, kind, ref, e)
        return False


def story_url(story_id: int, lang: str) -> str:
    return f"{SITE}{'?lang=hi' if lang == 'hi' else ''}#/story/{story_id}"


def queue_articles(store: Store, now: dt.datetime | None = None) -> int:
    """Notifications for articles published since the last look. The first look only sets the starting point."""
    from .feed import bar_counts
    now = now or utcnow()
    st = _state(store, "articles")
    since = _naive(st.get("at"))
    if since is None:
        _set_state(store, "articles", {"at": now.isoformat()})
        return 0
    rows = store.rows(select(published.c.story_id, published.c.updated_at, published.c.headline_en,
                             published.c.headline_hi, published.c.payload_en)
                      .where(published.c.updated_at > since).order_by(published.c.updated_at))
    if not rows:
        return 0
    fl = store.rows(select(follows.c.reader, follows.c.kind, follows.c.key).where(follows.c.kind != "recap"))
    made = 0
    newest = since
    for r in rows:
        newest = max(newest, _naive(r["updated_at"]))
        p = r["payload_en"] or {}
        if not ((p.get("narrative") or {}).get("paragraphs")):
            continue
        who = reasons(p, fl)
        langs = _reader_langs(store, list(who))
        bar = bar_counts((p.get("narrative") or {}).get("paragraphs"))
        n = (p.get("counts") or {}).get("independent_sources") or 0
        for reader, why in who.items():
            lang = langs.get(reader, "en")
            w = WORDS[lang]
            head = (r["headline_hi"] if lang == "hi" else None) or r["headline_en"] or p.get("headline") or ""
            body = " · ".join(x for x in (_because(why, p, lang, _label), f"{_share(bar)}% {w['established']}",
                                          f"{n} {w['outlets']}") if x)
            made += add_notification(store, reader, "article", str(r["story_id"]), head, body,
                                     story_url(r["story_id"], lang), r["story_id"])
    _set_state(store, "articles", {"at": newest.isoformat()})
    return made


# -- the daily recap ------------------------------------------------------------------------------------------
def _lead(payload: dict) -> list[dict]:
    paras = ((payload or {}).get("narrative") or {}).get("paragraphs") or []
    out = []
    for s in (paras[0] if paras else [])[:2]:
        item = {"t": s.get("text") or "", "c": s.get("class") or "unverified"}
        parts = s.get("parts") or []
        if len(parts) > 1:
            item["p"] = [{"t": x.get("text") or "", "c": x.get("class") or "unverified"} for x in parts]
        out.append(item)
    return out


def make_recap(store: Store, now: dt.datetime | None = None, force: bool = False) -> str | None:
    """The day's recap (owner, Oct 9 2026): every article published since the last recap, its headline and its
    lead as written (code only: no new prose, so the writer's rules stand), in both languages. Made by the first
    desk run from 23:00 IST (or, if that run was missed, by one before 06:00 IST for the day before). Returns the
    IST day it made, or None."""
    from .feed import bar_counts
    from .categories import normalize
    now = now or utcnow()
    ist = now + IST
    if ist.hour >= RECAP_HOUR_IST:
        day = ist.date()
    elif ist.hour < 6:
        day = ist.date() - dt.timedelta(days=1)
    elif force:
        day = ist.date()
    else:
        return None
    key = day.isoformat()
    if store.one(select(recaps.c.day).where(recaps.c.day == key)):
        return None
    last = store.one(select(func.max(recaps.c.cutoff).label("c")))
    since = _naive((last or {}).get("c")) or (dt.datetime.combine(day, dt.time()) - IST)
    since = max(since, now - dt.timedelta(hours=30))
    rows = store.rows(select(published.c.story_id, published.c.updated_at, published.c.headline_en,
                             published.c.headline_hi, published.c.payload_en, published.c.payload_hi)
                      .where(published.c.updated_at > since, published.c.updated_at <= now)
                      .order_by(published.c.updated_at))
    rows = [r for r in rows if ((r["payload_en"] or {}).get("narrative") or {}).get("paragraphs")]
    if not rows:
        return None
    out = {}
    for lang in ("en", "hi"):
        items = []
        for r in rows:
            pe = r["payload_en"] or {}
            p = (r["payload_hi"] if lang == "hi" else None) or pe
            items.append({"id": r["story_id"], "at": _naive(r["updated_at"]).isoformat(timespec="seconds"),
                          "h": (r["headline_hi"] if lang == "hi" else None) or r["headline_en"],
                          "lead": _lead(p), "cat": normalize(pe.get("category")),
                          "bar": bar_counts((pe.get("narrative") or {}).get("paragraphs"))})
        out[lang] = {"day": key, "stories": items}
    store.exec(insert(recaps).values(day=key, cutoff=now, made_at=now, payload_en=out["en"], payload_hi=out["hi"]))
    readers = [str(r["reader"]) for r in store.rows(select(follows.c.reader).where(follows.c.kind == "recap"))]
    langs = _reader_langs(store, readers)
    for reader in readers:
        lang = langs.get(reader, "en")
        add_notification(store, reader, "recap", key, WORDS[lang]["recap"], WORDS[lang]["recap_body"](len(rows)),
                         f"{SITE}{'?lang=hi' if lang == 'hi' else ''}#/recap/{key}")
    log.info("recap %s: %d articles, %d readers told", key, len(rows), len(readers))
    return key


# -- sending ---------------------------------------------------------------------------------------------------
def _vapid_key(store: Store) -> str | None:
    k = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if k:
        return k
    if store.engine.dialect.name != "postgresql":
        return None
    try:
        r = store.one(sql_text("select public.vapid_private_key() as k"))
        return (r or {}).get("k")
    except Exception as e:  # noqa: BLE001
        log.warning("no signing key for notifications: %s", e)
        return None


def send_pending(store: Store, now: dt.datetime | None = None, pusher=None) -> dict:
    """Push every unsent notification of the last 48 hours to each of its reader's devices. A row is marked sent
    once tried (it stays in the inbox either way). `pusher(sub, data)` -> HTTP status, for tests."""
    now = now or utcnow()
    rows = store.rows(select(notifications).where(notifications.c.sent_at.is_(None),
                                                  notifications.c.created_at >= now - SEND_WITHIN)
                      .order_by(notifications.c.created_at))
    stats = {"notifications": len(rows), "pushed": 0, "gone": 0, "failed": 0, "answers": []}
    if not rows:
        return stats
    if pusher is None:
        key = _vapid_key(store)
        if not key:
            log.warning("notifications not pushed: no signing key")
            return stats
        pusher = _webpush(key)
    readers = sorted({str(r["reader"]) for r in rows})
    subs: dict[str, list[dict]] = {}
    for s in store.rows(select(push_subscriptions).where(push_subscriptions.c.reader.in_(readers))):
        subs.setdefault(str(s["reader"]), []).append(s)
    for r in rows:
        data = {"title": r["title"] or "Nishpaksh", "body": r["body"] or "", "url": r["url"] or SITE,
                "tag": f"{r['kind']}-{r['ref']}", "id": r["id"]}
        for s in subs.get(str(r["reader"]), []):
            status = pusher(s, data)
            host = s["endpoint"].split("/")[2] if "//" in s["endpoint"] else "?"
            stats["answers"].append(f"{host} {status} {getattr(pusher, 'last', '')}"[:260])
            if status == 410:                     # only "gone" removes a device (Oct 10 2026: one 404 had removed a new one)
                store.exec(delete(push_subscriptions).where(push_subscriptions.c.endpoint == s["endpoint"]))
                stats["gone"] += 1
            elif status and 200 <= status < 300:
                store.exec(update(push_subscriptions).where(push_subscriptions.c.endpoint == s["endpoint"])
                           .values(last_ok_at=now, failures=0))
                stats["pushed"] += 1
            else:
                stats["failed"] += 1
                store.exec(update(push_subscriptions).where(push_subscriptions.c.endpoint == s["endpoint"])
                           .values(failures=push_subscriptions.c.failures + 1))
        store.exec(update(notifications).where(notifications.c.id == r["id"]).values(sent_at=now))
    # a device failing for a week is dropped
    store.exec(delete(push_subscriptions).where(push_subscriptions.c.failures >= 40))
    return stats


def _webpush(private_key: str):
    from pywebpush import WebPushException, webpush

    def push(sub: dict, data: dict) -> int:
        try:
            r = webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                        data=json.dumps(data, ensure_ascii=False), vapid_private_key=private_key,
                        vapid_claims={"sub": VAPID_SUB}, ttl=24 * 3600, timeout=10)
            return getattr(r, "status_code", 201)
        except WebPushException as e:
            resp = getattr(e, "response", None)
            push.last = f"{getattr(resp, 'status_code', 0)} {(getattr(resp, 'text', '') or str(e))[:200]}"
            return getattr(resp, "status_code", 0) or 0
        except Exception as e:  # noqa: BLE001
            log.info("push failed: %s", e)
            push.last = f"error {str(e)[:200]}"
            return 0
    push.last = ""
    return push


def run(store: Store, now: dt.datetime | None = None) -> dict:
    now = now or utcnow()
    out = {"queued": queue_articles(store, now)}
    try:
        out["recap"] = make_recap(store, now)
    except Exception as e:  # noqa: BLE001 - the recap never stops notifications
        log.warning("recap failed: %s", e)
        out["recap"] = f"failed: {e}"
    out.update(send_pending(store, now))
    log_push(store, out)
    return out


def log_push(store: Store, stats: dict) -> None:
    """What the push services answered, for checking from the database (job logs cannot be read)."""
    if stats.get("answers"):
        from .db import diagnostics
        store.exec(insert(diagnostics).values(created_at=utcnow(), kind="push", report=stats))


def main() -> None:
    from .config import database_url
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-only", action="store_true")
    a = ap.parse_args()
    store = Store(database_url())
    out = send_pending(store) if a.send_only else run(store)
    log_push(store, out)
    print(out)


if __name__ == "__main__":
    main()
