"""Articles read aloud, on request (owner, Oct 9 2026).

A reader asks for an article's audio in English or Hindi (the site calls `public.request_audio`: one new request
per reader per IST day; an audio already made is free for everyone). This job makes the queued ones with Gemini's
text-to-speech models (every model with "tts" in its name on each key, 10 a day each on the free tier), then the
day's recap in both languages, and tells each reader who asked (notify.py).

What is read: the headline, then the article as written, a section heading read where a section starts. Nothing is
added or reworded. Audio cannot carry the sentence colours, so the site highlights each sentence in its colour as
it is read (owner: highlighting only, one voice): every audio has a timing file with the start of every sentence.
Times inside one TTS call are shared out by the length of each sentence; each call's own length is exact.

Files: `<key>.m4a` (AAC, mono) and `<key>.json` (timing) in the `audio` branch, published with the site under
/audio/ (site.yml and audio.yml put the branch in the Pages artifact), so the browser gets the right type from the
same address as the page. Only audio of live articles and recaps of the last 7 days is kept (the branch is one
commit, force-pushed).

    python -m nishpaksh.audio --dir audio-out --minutes 20
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import re
import subprocess
import tempfile
import time
import wave

from .db import (Store, audio_files, audio_requests, delete, insert, published, quota_usage, recaps, select, update,
                 utcnow)

log = logging.getLogger(__name__)

RATE = 24000                 # Gemini TTS: 16-bit mono PCM at 24 kHz
CHUNK_CHARS = 1400           # one TTS call; Hindi text is about 2-3 tokens a character, TPM is 10,000
TTS_RPD = 10                 # free tier, per model per key
TTS_RPM = 3
VOICE = "Kore"               # calm, even; one voice for everything (owner: no second voice)
KEEP_DAYS = 7
HEADINGS = {
    "en": {"happened": "What happened", "numbers": "By the numbers", "say": "What they say", "background": "Background",
           "related": "Related events", "explained": "Explained", "next": "What next"},
    "hi": {"happened": "क्या हुआ", "numbers": "आँकड़ों में", "say": "किसने क्या कहा", "background": "पृष्ठभूमि",
           "related": "जुड़ी घटनाएँ", "explained": "समझिए", "next": "आगे क्या"},
}
STYLE = {"en": "Read this news article aloud in a calm, neutral news-reader voice, exactly as written:",
         "hi": "इस समाचार को शांत, निष्पक्ष समाचार-वाचक की आवाज़ में, जैसा लिखा है वैसा ही पढ़ें:"}
RECAP_INTRO = {"en": "Nishpaksh. The day's news, {day}.", "hi": "निष्पक्ष। आज की ख़बरें, {day}।"}


# -- what is read ---------------------------------------------------------------------------------------------
def article_units(headline: str, payload: dict, lang: str) -> list[dict]:
    """The text to read, in order: {"text", "p", "s"}; p = paragraph index, s = sentence index in it (p = -1 for
    the headline, s = -1 for a section heading)."""
    nar = (payload or {}).get("narrative") or {}
    paras = nar.get("paragraphs") or []
    keys = nar.get("section_keys") or []
    units = [{"text": headline.strip(), "p": -1, "s": -1}] if headline else []
    last = None
    for pi, para in enumerate(paras):
        key = keys[pi] if pi < len(keys) else None
        if key and key != last and key in HEADINGS.get(lang, HEADINGS["en"]):
            units.append({"text": HEADINGS[lang][key] + ".", "p": pi, "s": -1})
        last = key
        for si, s in enumerate(para or []):
            t = (s.get("text") or "").strip()
            if t:
                units.append({"text": t, "p": pi, "s": si})
    return units


def recap_units(recap: dict, lang: str) -> list[dict]:
    """The recap: an opening line, then each article's headline (s = -1) and lead (p = article index)."""
    day = recap.get("day") or ""
    units = [{"text": RECAP_INTRO[lang].format(day=day), "p": -1, "s": -1}]
    for k, it in enumerate(recap.get("stories") or []):
        units.append({"text": (it.get("h") or "").strip().rstrip(".") + ".", "p": k, "s": -1})
        for si, s in enumerate(it.get("lead") or []):
            if (s.get("t") or "").strip():
                units.append({"text": s["t"].strip(), "p": k, "s": si})
    return units


def chunks(units: list[dict], limit: int = CHUNK_CHARS) -> list[list[dict]]:
    out: list[list[dict]] = [[]]
    n = 0
    for u in units:
        if out[-1] and n + len(u["text"]) > limit:
            out.append([])
            n = 0
        out[-1].append(u)
        n += len(u["text"]) + 2
    return [c for c in out if c]


def timing(chunk_units: list[list[dict]], chunk_seconds: list[float]) -> list[dict]:
    """Start time of every unit: each chunk's length is known; inside it, shared out by characters (plus a
    small fixed pause per unit)."""
    out, t0 = [], 0.0
    for units, secs in zip(chunk_units, chunk_seconds):
        weights = [len(u["text"]) + 12 for u in units]
        tot = sum(weights) or 1
        t = t0
        for u, w in zip(units, weights):
            out.append({"p": u["p"], "s": u["s"], "t": round(t, 2)})
            t += secs * w / tot
        t0 += secs
    return out


# -- speaking ---------------------------------------------------------------------------------------------------
class Voices:
    """Every TTS model on every key, used in turn; daily use is counted in quota_usage like other models."""

    def __init__(self, store: Store, keys: list[str], client_factory=None):
        self.store = store
        self.day = _quota_day()
        self.slots = []
        self.last_call: dict[tuple, float] = {}
        make = client_factory or _client
        for k, key in enumerate(keys):
            try:
                cl = make(key)
                names = [m for m in cl.list_tts() if "tts" in m and "pro" not in m]
            except Exception as e:  # noqa: BLE001
                log.warning("tts models not listed on key %d: %s", k + 1, e)
                continue
            for m in sorted(names, key=_model_rank):
                self.slots.append({"client": cl, "model": m, "key": k,
                                   "usage": m if k == 0 else f"{m}@k{k + 1}", "dead": False})
        used = {r["model"]: r["requests"] or 0 for r in store.rows(select(quota_usage).where(quota_usage.c.day == self.day))}
        for s in self.slots:
            s["used"] = used.get(s["usage"], 0)

    def left(self) -> int:
        return sum(max(0, TTS_RPD - s["used"]) for s in self.slots if not s["dead"])

    def speak(self, text: str, lang: str) -> tuple[bytes, str] | None:
        """PCM for the text, from the first slot with quota (waiting out its per-minute limit if needed)."""
        for s in sorted(self.slots, key=lambda s: self.last_call.get((s["model"], s["key"]), 0)):
            if s["dead"] or s["used"] >= TTS_RPD:
                continue
            wait = self.last_call.get((s["model"], s["key"]), 0) + 60 / TTS_RPM + 1 - time.time()
            if wait > 0:
                time.sleep(min(wait, 25))
            self.last_call[(s["model"], s["key"])] = time.time()
            s["used"] += 1
            self.store.quota_add(s["usage"], self.day, 1, len(text) * 2)
            try:
                pcm = s["client"].tts(s["model"], f"{STYLE[lang]}\n\n{text}", VOICE)
                if pcm:
                    return pcm, s["model"]
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                log.info("tts %s key %d: %s", s["model"], s["key"] + 1, msg[:200])
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "404" in msg or "not found" in msg.lower():
                    s["dead"] = True
        return None


def _model_rank(name: str):
    nums = [int(x) for x in re.findall(r"\d+", name)]
    return ("lite" in name, "preview" in name, [-n for n in nums])


def _quota_day() -> str:
    from .router import quota_day
    return quota_day()


class _client:
    def __init__(self, api_key: str):
        from google import genai
        from google.genai import types
        self.types = types
        self.c = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=180_000, retry_options=types.HttpRetryOptions(attempts=1)))

    def list_tts(self) -> list[str]:
        return [m.name.split("/")[-1] for m in self.c.models.list() if "tts" in m.name]

    def tts(self, model: str, text: str, voice: str) -> bytes | None:
        t = self.types
        r = self.c.models.generate_content(model=model, contents=text, config=t.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=t.SpeechConfig(voice_config=t.VoiceConfig(
                prebuilt_voice_config=t.PrebuiltVoiceConfig(voice_name=voice)))))
        for part in (r.candidates[0].content.parts if r.candidates else []) or []:
            data = getattr(getattr(part, "inline_data", None), "data", None)
            if data:
                return data
        return None


def encode(pcm: bytes, out: pathlib.Path) -> None:
    """16-bit mono PCM at 24 kHz -> AAC in .m4a (plays everywhere, iPhones included)."""
    with tempfile.TemporaryDirectory() as d:
        w = pathlib.Path(d) / "a.wav"
        with wave.open(str(w), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(RATE)
            f.writeframes(pcm)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(w), "-c:a", "aac", "-b:a", "48k", "-ac", "1",
                        "-movflags", "+faststart", str(out)], check=True)


GAP = b"\x00\x00" * int(RATE * 0.45)       # a pause between calls


def make(units: list[dict], lang: str, voices: Voices) -> tuple[bytes, list[dict], float, str] | None:
    """All chunks spoken, or None (nothing kept) when any chunk could not be."""
    parts = chunks(units)
    pcm_all, secs, model = b"", [], ""
    for c in parts:
        got = voices.speak("\n\n".join(u["text"] for u in c), lang)
        if not got:
            return None
        pcm, model = got
        pcm += GAP
        pcm_all += pcm
        secs.append(len(pcm) / 2 / RATE)
    return pcm_all, timing(parts, secs), round(sum(secs), 1), model


# -- the job ----------------------------------------------------------------------------------------------------
def todo(store: Store) -> list[dict]:
    """What to make, oldest request first: requested articles (each story and language once), then recaps of the
    last two days without audio."""
    have = {r["key"] for r in store.rows(select(audio_files.c.key))}
    out, seen = [], set()
    for r in store.rows(select(audio_requests.c.story_id, audio_requests.c.lang).where(audio_requests.c.status == "queued")
                        .order_by(audio_requests.c.requested_at)):
        key = f"{r['story_id']}-{r['lang']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "story_id": r["story_id"], "lang": r["lang"], "done": key in have})
    since = (utcnow() - dt.timedelta(days=2)).date().isoformat()
    for r in store.rows(select(recaps.c.day).where(recaps.c.day >= since).order_by(recaps.c.day.desc())):
        day = str(r["day"])[:10]
        for lang in ("en", "hi"):
            key = f"recap-{day}-{lang}"
            if key not in have:
                out.append({"key": key, "recap": day, "lang": lang, "done": False})
    return out


def _finish_requests(store: Store, story_id: int, lang: str, status: str, url: str | None) -> list[str]:
    rows = store.rows(select(audio_requests.c.reader).where(audio_requests.c.story_id == story_id,
                                                            audio_requests.c.lang == lang,
                                                            audio_requests.c.status == "queued"))
    store.exec(update(audio_requests).where(audio_requests.c.story_id == story_id, audio_requests.c.lang == lang,
                                            audio_requests.c.status == "queued").values(status=status, done_at=utcnow()))
    return [str(r["reader"]) for r in rows]


def _tell(store: Store, readers: list[str], story_id: int, lang: str, headline: str) -> None:
    from .notify import WORDS, add_notification, story_url
    for reader in readers:
        add_notification(store, reader, "audio", f"{story_id}-{lang}", WORDS[lang]["audio"], headline,
                         story_url(story_id, lang) + "?listen=1", story_id)   # the site opens the player


def work(store: Store, out_dir: pathlib.Path, voices: Voices, until: float | None = None,
         encoder=encode) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"made": [], "failed": [], "waiting": 0}
    for job in todo(store):
        if until and time.time() > until:
            stats["waiting"] += 1
            continue
        lang, key = job["lang"], job["key"]
        if job.get("done"):                                  # made already (asked again meanwhile)
            row = store.one(select(published.c.headline_en, published.c.headline_hi).where(published.c.story_id == job["story_id"]))
            readers = _finish_requests(store, job["story_id"], lang, "done", f"/audio/{key}.m4a")
            _tell(store, readers, job["story_id"], lang, ((row or {}).get("headline_hi") if lang == "hi" else None)
                  or (row or {}).get("headline_en") or "")
            continue
        if "recap" in job:
            r = store.one(select(recaps).where(recaps.c.day == job["recap"]))
            payload = (r or {}).get("payload_hi" if lang == "hi" else "payload_en") or {}
            units, story_id, head = recap_units(payload, lang), None, ""
        else:
            r = store.one(select(published).where(published.c.story_id == job["story_id"]))
            if not r:                                        # archived since it was asked for
                _finish_requests(store, job["story_id"], lang, "failed", None)
                stats["failed"].append(key)
                continue
            p = (r["payload_hi"] if lang == "hi" else None) or r["payload_en"]
            head = (r["headline_hi"] if lang == "hi" else None) or r["headline_en"] or ""
            units, story_id = article_units(head, p, lang), job["story_id"]
        if not units:
            continue
        if voices.left() < len(chunks(units)):
            stats["waiting"] += 1                            # not enough calls left today: tomorrow
            continue
        got = make(units, lang, voices)
        if not got:
            stats["waiting"] += 1
            continue
        pcm, tm, secs, model = got
        encoder(pcm, out_dir / f"{key}.m4a")
        (out_dir / f"{key}.json").write_text(json.dumps({"key": key, "lang": lang, "seconds": secs, "units": tm},
                                                        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        url = f"/audio/{key}.m4a"
        store.exec(delete(audio_files).where(audio_files.c.key == key))
        store.exec(insert(audio_files).values(key=key, story_id=story_id, lang=lang, url=url, seconds=secs, model=model,
                                              made_at=utcnow()))
        stats["made"].append(key)
        if story_id is not None:
            readers = _finish_requests(store, story_id, lang, "done", url)
            _tell(store, readers, story_id, lang, head)
    return stats


def prune(store: Store, out_dir: pathlib.Path) -> int:
    """Keep audio of live articles and of recaps of the last KEEP_DAYS days; the rest leaves the branch and the
    table (an archived article is no longer on the front page)."""
    live = {r["story_id"] for r in store.rows(select(published.c.story_id))}
    since = (utcnow() - dt.timedelta(days=KEEP_DAYS)).date().isoformat()
    gone = 0
    for r in store.rows(select(audio_files.c.key, audio_files.c.story_id)):
        k = r["key"]
        keep = (r["story_id"] in live) if r["story_id"] is not None else (k[6:16] >= since)
        if not keep:
            store.exec(delete(audio_files).where(audio_files.c.key == k))
            gone += 1
    kept = {r["key"] for r in store.rows(select(audio_files.c.key))}
    for f in out_dir.glob("*"):
        if f.is_file() and f.suffix in (".m4a", ".json") and f.stem not in kept:
            f.unlink()
    return gone


def main() -> None:
    from .config import database_url, gemini_api_keys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--check", action="store_true", help="only say whether anything is waiting (exit 0 = yes)")
    a = ap.parse_args()
    store = Store(database_url())
    if a.check:
        n = sum(1 for j in todo(store))
        print(f"waiting={n}")
        raise SystemExit(0 if n else 1)
    t0 = time.time()
    voices = Voices(store, gemini_api_keys())
    out = pathlib.Path(a.dir)
    stats = work(store, out, voices, until=t0 + a.minutes * 60)
    stats["pruned"] = prune(store, out)
    stats["calls_left"] = voices.left()
    log.info("audio: %s", stats)
    from .db import diagnostics
    store.exec(insert(diagnostics).values(created_at=utcnow(), kind="audio", report=stats))
    print(json.dumps(stats))
    if os.environ.get("GITHUB_OUTPUT"):          # the workflow publishes only when something changed
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"changed={'true' if stats['made'] or stats['pruned'] else 'false'}\n")


if __name__ == "__main__":
    main()
