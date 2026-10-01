"""Quota-aware router over the Gemini API free tier.

Every model is a bucket with RPM / TPM / RPD limits. A call names a *tier*
(bulk, light, judge, grounded, embed); the router picks whichever model in that
tier can serve it soonest, waits if needed, and falls back across models when one
hits its limit, returns 429, or does not exist on this API key.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific


def quota_day() -> str:
    return dt.datetime.now(PACIFIC).date().isoformat()


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return int(ascii_chars / 4 + other / 1.5) + 1  # Devanagari tokenizes heavier


def parse_json(text: str):
    if not text:
        return None
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        return json.loads(t)
    except Exception:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        i, j = t.find(open_), t.rfind(close)
        if i != -1 and j > i:
            chunk = t[i:j + 1]
            for cand in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):  # trailing commas
                try:
                    return json.loads(cand)
                except Exception:
                    continue
    return None


class QuotaExhausted(Exception):
    pass


@dataclass
class ModelSlot:
    id: str
    rpm: int
    tpm: int
    rpd: int
    json_mode: bool = False
    grounding: bool = False
    used_today: int = 0
    tokens_today: int = 0
    window: list = field(default_factory=list)  # [(timestamp, tokens)] in the last 60 s
    cooldown_until: float = 0.0
    disabled: bool = False

    def wait_time(self, est_tokens: int, now: float) -> float | None:
        """Seconds until this model can take the call; None if it cannot today."""
        if self.disabled or self.used_today >= self.rpd or est_tokens > self.tpm:
            return None
        self.window = [(t, k) for t, k in self.window if now - t < 60]
        wait = max(0.0, self.cooldown_until - now)
        if len(self.window) >= self.rpm:
            wait = max(wait, 60 - (now - self.window[0][0]) + 0.2)
        used = sum(k for _, k in self.window)
        if used + est_tokens > self.tpm:
            remaining = used
            for t, k in self.window:
                remaining -= k
                if remaining + est_tokens <= self.tpm:
                    wait = max(wait, 60 - (now - t) + 0.2)
                    break
        return wait


@dataclass
class LLMResult:
    text: str
    data: object
    model: str
    sources: list
    tokens: int


class GeminiBackend:
    """Thin wrapper over google-genai so tests can swap in a fake."""

    def __init__(self, api_key: str):
        from google import genai
        self.client = genai.Client(api_key=api_key)

    def list_models(self) -> list[str]:
        return [m.name.split("/")[-1] for m in self.client.models.list()]

    def generate(self, model: str, prompt: str, json_mode: bool, grounded: bool):
        from google.genai import types
        cfg: dict = {"temperature": 0.0}
        if grounded:
            cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        elif json_mode:
            cfg["response_mime_type"] = "application/json"
        r = self.client.models.generate_content(
            model=model, contents=prompt, config=types.GenerateContentConfig(**cfg))
        sources = []
        try:
            gm = r.candidates[0].grounding_metadata
            for ch in (gm.grounding_chunks or []) if gm else []:
                if getattr(ch, "web", None):
                    sources.append({"url": ch.web.uri, "title": ch.web.title})
        except Exception:
            pass
        tokens = getattr(getattr(r, "usage_metadata", None), "total_token_count", 0) or 0
        return (r.text or ""), sources, tokens

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        r = self.client.models.embed_content(model=model, contents=texts)
        return [list(e.values) for e in r.embeddings]


def _norm(model_id: str) -> str:
    return re.sub(r"(-it|-preview.*|-latest|-\d{3,})$", "", model_id.lower())


class Router:
    def __init__(self, tiers_cfg: dict, backend, store=None, max_wait: float = 75.0):
        self.backend = backend
        self.store = store
        self.max_wait = max_wait
        self.day = quota_day()
        self.tiers: dict[str, list[ModelSlot]] = {
            name: [ModelSlot(**m) for m in models] for name, models in tiers_cfg.items()
        }
        usage = store.quota_load(self.day) if store else {}
        for slot in self.all_slots():
            if slot.id in usage:
                slot.used_today, slot.tokens_today = usage[slot.id]

    def all_slots(self):
        seen = set()
        for slots in self.tiers.values():
            for s in slots:
                if id(s) not in seen:
                    seen.add(id(s))
                    yield s

    # model discovery -----------------------------------------------------------
    def resolve(self) -> None:
        """Map configured IDs to the IDs this key actually serves; disable missing ones."""
        try:
            available = self.backend.list_models()
        except Exception as e:  # listing failed: keep configured IDs and learn from 404s
            log.warning("model listing failed (%s); using configured IDs", e)
            return
        avail_set = set(available)
        for slot in self.all_slots():
            if slot.id in avail_set:
                continue
            target = _norm(slot.id)
            best = None
            for a in available:
                na = _norm(a)
                if na == target:
                    ok = True
                elif na.startswith(target + "-"):
                    # accept size/variant suffixes (gemma-4-26b -> gemma-4-26b-a4b) but never a
                    # different product line (flash -> flash-lite, -tts, -image, -live ...)
                    suffix = na[len(target) + 1:]
                    ok = not any(w in suffix for w in ("lite", "tts", "image", "live", "audio", "thinking"))
                else:
                    ok = False
                if ok and (best is None or len(a) < len(best)
                           or (len(a) == len(best) and SequenceMatcher(None, a, slot.id).ratio()
                               > SequenceMatcher(None, best, slot.id).ratio())):
                    best = a
            if best:
                log.info("model %s -> %s", slot.id, best)
                usage = self.store.quota_load(self.day).get(best) if self.store else None
                slot.id = best
                if usage:
                    slot.used_today, slot.tokens_today = usage
            else:
                log.warning("model %s not available on this key; disabled", slot.id)
                slot.disabled = True

    # budgets -------------------------------------------------------------------
    def remaining_today(self, tier: str) -> int:
        return sum(max(0, s.rpd - s.used_today) for s in self.tiers.get(tier, []) if not s.disabled)

    def per_run_budget(self, tier: str) -> int:
        """Spread what is left of today's quota over the hourly runs left today."""
        now = dt.datetime.now(PACIFIC)
        runs_left = max(1, 24 - now.hour)
        left = self.remaining_today(tier)
        return 0 if left == 0 else max(1, left // runs_left)

    # calls -----------------------------------------------------------------------
    def _pick(self, tier: str, est: int):
        best = None
        now = time.time()
        for s in self.tiers.get(tier, []):
            w = s.wait_time(est, now)
            if w is not None and (best is None or w < best[0]):
                best = (w, s)
        return best

    def _record(self, slot: ModelSlot, tokens: int) -> None:
        slot.tokens_today += tokens
        if self.store:
            try:
                self.store.quota_save(slot.id, self.day, slot.used_today, slot.tokens_today)
            except Exception as e:
                log.warning("quota save failed: %s", e)

    def _handle_error(self, slot: ModelSlot, e: Exception) -> None:
        msg = str(e)
        low = msg.lower()
        if "429" in msg or "resource_exhausted" in low or "quota" in low:
            if "perday" in low.replace(" ", "") or "per_day" in low or "daily" in low:
                slot.used_today = slot.rpd
            else:
                slot.cooldown_until = time.time() + 60
        elif "404" in msg or "not found" in low or "not supported" in low or "invalid model" in low:
            slot.disabled = True
        elif any(x in msg for x in ("500", "502", "503", "504")) or "unavailable" in low or "timeout" in low:
            slot.cooldown_until = time.time() + 20
        else:
            slot.cooldown_until = time.time() + 30
        log.warning("model %s error: %s", slot.id, msg[:200])

    def call(self, tier: str, prompt: str, json_out: bool = True, grounded: bool = False,
             max_output_tokens: int = 2500) -> LLMResult:
        est = estimate_tokens(prompt) + max_output_tokens
        json_retry_used = False
        for _ in range(8):
            pick = self._pick(tier, est)
            if not pick:
                raise QuotaExhausted(tier)
            wait, slot = pick
            if wait > self.max_wait:
                raise QuotaExhausted(f"{tier} (next slot in {wait:.0f}s)")
            if wait > 0:
                time.sleep(wait)
            now = time.time()
            slot.window.append((now, est))
            slot.used_today += 1
            try:
                text, sources, tokens = self.backend.generate(
                    slot.id, prompt, json_mode=json_out and slot.json_mode, grounded=grounded)
            except Exception as e:  # noqa: BLE001
                self._handle_error(slot, e)
                self._record(slot, 0)
                continue
            if tokens:
                slot.window[-1] = (now, tokens)
            self._record(slot, tokens or est)
            data = parse_json(text) if json_out else None
            if json_out and data is None:
                if json_retry_used:
                    raise ValueError(f"{slot.id} returned unparseable JSON")
                json_retry_used = True
                prompt += "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
                continue
            return LLMResult(text, data, slot.id, sources, tokens)
        raise QuotaExhausted(tier)

    def embed(self, texts: list[str], batch: int = 50) -> list[list[float]] | None:
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            est = sum(estimate_tokens(t) for t in chunk)
            done = False
            for _ in range(4):
                pick = self._pick("embed", est)
                if not pick or pick[0] > self.max_wait:
                    return None
                wait, slot = pick
                if wait > 0:
                    time.sleep(wait)
                slot.window.append((time.time(), est))
                slot.used_today += 1
                try:
                    vecs = self.backend.embed(slot.id, chunk)
                except Exception as e:  # noqa: BLE001
                    self._handle_error(slot, e)
                    self._record(slot, 0)
                    continue
                self._record(slot, est)
                out.extend(vecs)
                done = True
                break
            if not done:
                return None
        return out
