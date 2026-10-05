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
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
EMBED_DIMS = 256
PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific
PACE_SLACK_HOURS = 2   # how far ahead of an even spread a slot may run
OVERLOAD_STREAK = 3    # consecutive overload errors before a model is dropped for the run
TIER_OVERLOAD_STREAK = 10   # consecutive refusals across a whole tier before it is skipped for the run


IST = ZoneInfo("Asia/Kolkata")
# Indian news arrives on Indian hours: the day's allowance opens faster 07:00-23:00 IST
DAY_WEIGHT, NIGHT_WEIGHT = 1.5, 0.5


def _hour_weight(pacific_midnight: dt.datetime, h: int) -> float:
    ist_hour = (pacific_midnight + dt.timedelta(hours=h)).astimezone(IST).hour
    return DAY_WEIGHT if 7 <= ist_hour < 23 else NIGHT_WEIGHT


def pace_fraction(now: dt.datetime) -> float:
    """Share of the day's allowance open by `now` (Pacific): each hour of the quota day opens its
    weighted share, plus PACE_SLACK_HOURS of average hours as headroom."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    w = [_hour_weight(midnight, h) for h in range(24)]
    total = sum(w)
    done = sum(w[:now.hour]) + w[now.hour] * (now.minute / 60)
    return min(1.0, (done + PACE_SLACK_HOURS * total / 24) / total)


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


class CallFailed(RuntimeError):
    """Every attempt errored (overload, rate limit, bad reply): not the same as no quota left."""


def _error_kind(msg: str) -> str:
    low = msg.lower()
    for k, words in (("rate limit 429", ("429", "resource_exhausted")), ("overloaded 5xx", ("500", "502", "503", "504", "unavailable")),
                     ("timeout", ("timeout", "timed out", "deadline")), ("not found", ("404", "not found")),
                     ("bad request 400", ("400", "invalid_argument"))):
        if any(w in low for w in words):
            return k
    return low[:60]


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
    key: int = 0          # which API key (Google Cloud project) this slot spends
    fail_streak: int = 0  # consecutive overload errors in this run

    @property
    def usage_key(self) -> str:
        """Name under which usage is stored: key 1 keeps the plain model id."""
        return self.id if self.key == 0 else f"{self.id}@k{self.key + 1}"

    def wait_time(self, est_tokens: int, now: float, units: int = 1) -> float | None:
        """Seconds until this model can take the call; None if it cannot today. `units` is how many
        requests Google counts for the call (an embedding batch counts one per text)."""
        rpm, tpm = self.rpm_cap, self.tpm_cap
        if self.disabled or self.used_today + units > self.rpd or est_tokens > tpm or units > rpm:
            return None
        self.window = [(t, k) for t, k in self.window if now - t < 60]
        wait = max(0.0, self.cooldown_until - now)
        over = len(self.window) + units - rpm
        if over > 0:
            wait = max(wait, 60 - (now - self.window[over - 1][0]) + 1.0)
        used = sum(k for _, k in self.window)
        if used + est_tokens > tpm:
            remaining = used
            for t, k in self.window:
                remaining -= k
                if remaining + est_tokens <= tpm:
                    wait = max(wait, 60 - (now - t) + 1.0)
                    break
        return wait

    # Stay just under Google's per-minute limits (owner, Oct 5 2026): the dashboard showed Flash-Lite
    # at 16 of 15 requests a minute. Our minute and Google's are measured at different moments
    # (network, clock), so we aim one request (10% for large limits) and 10% of tokens below.
    @property
    def rpm_cap(self) -> int:
        return max(1, self.rpm - max(1, self.rpm // 10))

    @property
    def tpm_cap(self) -> int:
        return int(self.tpm * 0.9)


@dataclass
class LLMResult:
    text: str
    data: object
    model: str
    sources: list
    tokens: int


class GeminiBackend:
    """Thin wrapper over google-genai so tests can swap in a fake."""

    def __init__(self, api_key: str, timeout_s: int = 150):
        from google import genai
        from google.genai import types
        # without a timeout a stuck call to an overloaded model can hang the whole run
        # no hidden retries inside the SDK: every request Google sees is one the router counted
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=timeout_s * 1000, retry_options=types.HttpRetryOptions(attempts=1)))

    def list_models(self) -> list[str]:
        return [m.name.split("/")[-1] for m in self.client.models.list()]

    def generate(self, model: str, prompt: str, json_mode: bool, grounded: bool):
        from google.genai import types
        cfg: dict = {"temperature": 0.0,
                     "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
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
        """One vector per text, verified. A plain list of strings can be read by some embedding
        models as ONE multi-part input (one vector back, which was once copied to 487 articles),
        so each text is sent as its own Content and the reply is checked: exactly one vector per
        text, and no two different texts with an identical vector. A bad reply raises; it is never
        retried text by text here, because one call per text is what once spent a whole day's
        embedding quota in a single run. The router retries with smaller batches instead."""
        from google.genai import types
        cfg = types.EmbedContentConfig(output_dimensionality=EMBED_DIMS)
        contents = [types.Content(parts=[types.Part(text=t)]) for t in texts]
        r = self.client.models.embed_content(model=model, contents=contents, config=cfg)
        vecs = [[round(float(x), 5) for x in e.values] for e in (r.embeddings or [])]
        if not embeddings_look_valid(texts, vecs):
            raise ValueError(f"embedding reply failed validation ({len(vecs)} vectors for {len(texts)} texts)")
        return vecs


def embeddings_look_valid(texts: list[str], vecs: list[list[float]]) -> bool:
    if len(vecs) != len(texts) or any(not v for v in vecs):
        return False
    seen: dict[tuple, str] = {}
    for t, v in zip(texts, vecs):
        key = tuple(v[:16])
        if key in seen and seen[key] != t:
            return False
        seen[key] = t
    return True


def _norm(model_id: str) -> str:
    return re.sub(r"(-it|-preview.*|-latest|-\d{3,})$", "", model_id.lower())


class Router:
    def __init__(self, tiers_cfg: dict, backend, store=None, max_wait: float = 180.0, paced: bool | None = None):
        # One backend per API key. Each key is a separate Google Cloud project with its own
        # free-tier quota, so every (key, model) pair is its own slot with its own counters.
        self.backends = list(backend) if isinstance(backend, (list, tuple)) else [backend]
        self.backend = self.backends[0]
        self.store = store
        self.max_wait = max_wait
        import os
        # paced only against the real shared quota (the production database); tests opt in
        real = store is not None and not str(getattr(getattr(store, "engine", None), "url", "")).startswith("sqlite")
        self.paced = (real and os.environ.get("RUN_TRIGGER", "") != "backfill") if paced is None else paced
        self.day = quota_day()
        self._lock = threading.Lock()
        self.bad_keys: set[int] = set()
        from collections import Counter as _C
        self.error_log: _C = _C()   # "tier | model | kind" -> count, written to the run's stats
        # every request's outcome per model and key ("gemini-3.8-flash@k2" -> {"ok": 4, "overloaded 5xx": 9}),
        # so refusal rates can be compared by hour of day (owner, Oct 5 2026)
        self.call_log: dict[str, _C] = {}
        self.tier_streak: dict[str, int] = {}   # consecutive overload refusals per tier
        # One slot per (key, model), shared by every tier that lists the model, so a model used by
        # two tiers is never counted against two separate quotas. `keep` lets a tier stop using a
        # model while that many requests remain today, leaving them for the other tiers.
        self.slots: dict[tuple[int, str], ModelSlot] = {}
        self.tiers: dict[str, list[ModelSlot]] = {}
        self.keep: dict[tuple[str, int], int] = {}
        for name, models in tiers_cfg.items():
            self.tiers[name] = []
            for k in range(len(self.backends)):
                for m in models:
                    m = dict(m)
                    keep = int(m.pop("keep", 0))
                    if (k, m["id"]) not in self.slots:
                        self.slots[(k, m["id"])] = ModelSlot(**m, key=k)
                    slot = self.slots[(k, m["id"])]
                    self.tiers[name].append(slot)
                    self.keep[(name, id(slot))] = keep
        usage = store.quota_load(self.day) if store else {}
        for slot in self.all_slots():
            if slot.usage_key in usage:
                slot.used_today, slot.tokens_today = usage[slot.usage_key]

    def all_slots(self):
        seen = set()
        for slots in self.tiers.values():
            for s in slots:
                if id(s) not in seen:
                    seen.add(id(s))
                    yield s

    # model discovery -----------------------------------------------------------
    def resolve(self) -> None:
        """Map configured IDs to the IDs each key actually serves; disable missing ones."""
        for k, backend in enumerate(self.backends):
            try:
                available = backend.list_models()
            except Exception as e:  # listing failed: keep configured IDs and learn from 404s
                log.warning("model listing failed for key %d (%s); using configured IDs", k + 1, e)
                continue
            self._resolve_key(k, available)

    def _resolve_key(self, k: int, available: list[str]) -> None:
        avail_set = set(available)
        for slot in self.all_slots():
            if slot.key != k or slot.id in avail_set:
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
                log.info("model %s -> %s (key %d)", slot.id, best, k + 1)
                slot.id = best
                usage = self.store.quota_load(self.day).get(slot.usage_key) if self.store else None
                if usage:
                    slot.used_today, slot.tokens_today = usage
            else:
                log.warning("model %s not available on key %d; disabled", slot.id, k + 1)
                slot.disabled = True

    def _outcome(self, slot: ModelSlot, kind: str) -> None:
        """Count one request's outcome (caller holds the lock)."""
        from collections import Counter as _C
        self.call_log.setdefault(slot.usage_key, _C())[kind] += 1

    # budgets -------------------------------------------------------------------
    def pace_cap(self, tier: str, slot: ModelSlot) -> int:
        """Most requests this tier may have spent on this slot by now. The daily allowance (rpd
        minus what the tier leaves for others) opens up evenly over the Pacific quota day, plus
        PACE_SLACK_HOURS of headroom; whatever an hour does not use carries forward. Without it the
        whole day's Flash-Lite went in ~15 runs (Oct 2026), leaving the site dark until the reset.
        A run started with RUN_TRIGGER=backfill is not paced (deliberate catch-up)."""
        allowance = slot.rpd - self.keep.get((tier, id(slot)), 0)
        if not self.paced:
            return allowance
        return int(allowance * pace_fraction(self._now_pacific()) + 1e-6)

    def _now_pacific(self) -> dt.datetime:
        return dt.datetime.now(PACIFIC)

    def remaining_now(self, tier: str) -> int:
        """Requests this tier can still make in this run under the pacing curve."""
        return sum(max(0, self.pace_cap(tier, s) - s.used_today)
                   for s in self.tiers.get(tier, []) if not s.disabled)

    def remaining_today(self, tier: str, include_disabled: bool = False) -> int:
        return sum(max(0, s.rpd - s.used_today - self.keep.get((tier, id(s)), 0))
                   for s in self.tiers.get(tier, []) if include_disabled or not s.disabled)

    def per_run_budget(self, tier: str) -> int:
        """Spread what is left of today's quota over the hourly runs left today."""
        now = dt.datetime.now(PACIFIC)
        runs_left = max(1, 24 - now.hour)
        left = self.remaining_today(tier)
        return 0 if left == 0 else max(1, left // runs_left)

    # calls -----------------------------------------------------------------------
    def _pick(self, tier: str, est: int, units: int = 1):
        best = None
        now = time.time()
        for s in self.tiers.get(tier, []):
            if s.used_today + units > self.pace_cap(tier, s):
                continue
            w = s.wait_time(est, now, units)
            if w is not None and (best is None or w < best[0]):
                best = (w, s)
        return best

    def _record(self, slot: ModelSlot, tokens: int) -> None:
        slot.tokens_today += tokens
        if self.store:
            try:
                self.store.quota_save(slot.usage_key, self.day, slot.used_today, slot.tokens_today)
            except Exception as e:
                log.warning("quota save failed: %s", e)

    def _handle_error(self, slot: ModelSlot, e: Exception) -> None:
        msg = str(e)
        low = msg.lower()
        if ("api key not valid" in low or "api_key_invalid" in low or "api key expired" in low
                or "unauthenticated" in low or "service_disabled" in low
                or ("permission_denied" in low and "api key" in low)):
            # a bad key is bad for every model: stop using it for this run instead of retrying
            for s in self.all_slots():
                if s.key == slot.key:
                    s.disabled = True
            self.bad_keys.add(slot.key)
            log.error("API key %d rejected (%s); all its models disabled for this run", slot.key + 1, msg[:120])
            return
        if "429" in msg or "resource_exhausted" in low or "quota" in low:
            if "perday" in low.replace(" ", "") or "per_day" in low or "daily" in low:
                slot.used_today = slot.rpd
            else:
                slot.cooldown_until = time.time() + 60
        elif "404" in msg or "not found" in low or "not supported" in low or "invalid model" in low:
            slot.disabled = True
        elif any(x in msg for x in ("500", "502", "503", "504")) or "unavailable" in low or "deadline" in low \
                or "timeout" in low or "timed out" in low:
            slot.cooldown_until = time.time() + 45  # overloaded: let the other models in the tier work
            slot.fail_streak += 1
            if slot.fail_streak >= OVERLOAD_STREAK:
                # Gemma on the free tier fails most calls ("high demand"): Oct 4 2026, ~840 booked
                # Gemma requests gave 16 articles read. Each failure can hang until the timeout, so
                # after a streak the model is dropped for the rest of the run.
                slot.disabled = True
                log.warning("model %s (key %d) overloaded %d times in a row; off for this run",
                            slot.id, slot.key + 1, slot.fail_streak)
        else:
            slot.cooldown_until = time.time() + 30
        log.warning("model %s (key %d) error: %s", slot.id, slot.key + 1, msg[:200])

    def _reserve(self, tier: str, est: int, units: int = 1) -> ModelSlot:
        """Block until some model in the tier can take the call, then book it. Thread-safe."""
        while True:
            with self._lock:
                pick = self._pick(tier, est, units)
                if not pick:
                    raise QuotaExhausted(tier)
                wait, slot = pick
                if wait > self.max_wait:
                    raise QuotaExhausted(f"{tier} (next slot in {wait:.0f}s)")
                if wait <= 0:
                    t = time.time()
                    slot.window.append((t, est))
                    slot.window.extend((t, 0) for _ in range(units - 1))
                    slot.used_today += units
                    return slot
            time.sleep(min(wait, 5.0))

    def call(self, tier: str, prompt: str, json_out: bool = True, grounded: bool = False,
             max_output_tokens: int = 2500, max_attempts: int = 8) -> LLMResult:
        est = estimate_tokens(prompt) + max_output_tokens
        json_retry_used = False
        errors: list[str] = []
        for _ in range(max_attempts):
            if self.tier_streak.get(tier, 0) >= TIER_OVERLOAD_STREAK:
                # every model of this tier has been refusing for load (Oct 5 2026: the writer made
                # ~55 refused calls a run for hours): stop asking for the rest of this run
                raise CallFailed(f"{tier}: models overloaded this run ({self.tier_streak[tier]} refusals in a row)")
            try:
                slot = self._reserve(tier, est)
            except QuotaExhausted:
                if errors:   # the models failed, the quota did not run out: say so (Oct 2026 these were hidden)
                    raise CallFailed(f"{tier}: {len(errors)} failed attempts, last: {errors[-1]}") from None
                raise
            booked = slot.window[-1] if slot.window else None
            try:
                text, sources, tokens = self.backends[slot.key].generate(
                    slot.id, prompt, json_mode=json_out and slot.json_mode, grounded=grounded)
            except Exception as e:  # noqa: BLE001
                short = f"{slot.id}: {str(e)[:120]}"
                errors.append(short)
                kind = _error_kind(str(e))
                with self._lock:
                    self._handle_error(slot, e)
                    if kind in ("overloaded 5xx", "timeout") and slot.used_today < slot.rpd:
                        # the server refused for load: not a request served, so it does not use up
                        # the day's allowance (Oct 5 2026: 27 of 30 writer calls were 503 "high
                        # demand", and counting them throttled the writer by our own pacing)
                        slot.used_today = max(0, slot.used_today - 1)
                    self._record(slot, 0)
                    self.error_log[f"{tier} | {slot.id} | {kind}"] += 1
                    self._outcome(slot, kind)
                    if kind in ("overloaded 5xx", "timeout"):
                        self.tier_streak[tier] = self.tier_streak.get(tier, 0) + 1
                continue
            with self._lock:
                slot.fail_streak = 0
                self.tier_streak[tier] = 0
                self._outcome(slot, "ok")
                if tokens and booked in slot.window:
                    slot.window[slot.window.index(booked)] = (booked[0], tokens)
                self._record(slot, tokens or est)
            data = parse_json(text) if json_out else None
            if json_out and data is None:
                if json_retry_used:
                    raise ValueError(f"{slot.id} returned unparseable JSON")
                json_retry_used = True
                prompt += "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
                continue
            return LLMResult(text, data, slot.id, sources, tokens)
        if errors:
            raise CallFailed(f"{tier}: {len(errors)} failed attempts, last: {errors[-1]}")
        raise QuotaExhausted(tier)

    def embed(self, texts: list[str], batch: int = 25, max_requests: int | None = None) -> list[list[float]] | None:
        """Vectors for all texts, or None. Google counts every text in a batch as one request against
        the daily and per-minute limits (seen on real data: 26 batches exhausted a 1,000/day quota),
        so quota is booked per text. Never spends more than `max_requests` calls: a batch that fails
        validation is retried once in smaller pieces, never one text per call."""
        out: list[list[float]] = []
        spent = 0
        self.last_embed_spent = 0
        queue = [texts[i:i + batch] for i in range(0, len(texts), batch)]
        while queue:
            chunk = queue.pop(0)
            if max_requests is not None and spent >= max_requests:
                return None
            est = sum(estimate_tokens(t) for t in chunk)
            try:
                slot = self._reserve("embed", est, units=len(chunk))
            except QuotaExhausted:
                return None
            spent += 1
            self.last_embed_spent = spent
            try:
                vecs = self.backends[slot.key].embed(slot.id, chunk)
                if not embeddings_look_valid(chunk, vecs):
                    raise ValueError("embedding reply failed validation")
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    if "validation" not in str(e):
                        self._handle_error(slot, e)
                    self._record(slot, 0)
                    self._outcome(slot, "bad reply" if "validation" in str(e) else _error_kind(str(e)))
                log.warning("embedding batch of %d failed (%s)", len(chunk), str(e)[:150])
                if len(chunk) > 10 and "validation" in str(e):
                    step = max(10, len(chunk) // 4)  # smaller pieces, at most 4 more requests
                    queue[:0] = [chunk[i:i + step] for i in range(0, len(chunk), step)]
                    continue
                return None
            with self._lock:
                self._record(slot, est)
                self._outcome(slot, "ok")
            out.extend(vecs)
        return out
