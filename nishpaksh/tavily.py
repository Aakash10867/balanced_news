"""Tavily, on a strict budget: 1,000 free credits a month.

Two uses, in this order of value:
  extract  read pages our own fetcher is blocked from (1 credit per 5 pages read; failures free)
  search   find coverage when the free Google News search finds nothing usable (1 credit each)

Credits are booked in quota_usage (model "tavily", one row per UTC day) BEFORE a call is
made, so a crash can only over-count, never overspend. Today's allowance is what is left of
the month spread over the days left, never more than `daily_cap`: unspent credits roll
forward within the month, and an early burst cannot starve the end of the month.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import math
import re
import threading

import requests

from .config import tavily_api_key

log = logging.getLogger(__name__)
MONTHLY_CREDITS = 1000
SAFETY = 50             # never plan to use the last 50: our count and Tavily's may differ slightly
USAGE_KEY = "tavily"


def _norm_url(u: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower())
    return u.rstrip("/")


def clean_page_text(text: str) -> str:
    """Tavily returns the whole page: menus, 'img' placeholders, share buttons. Keep the prose:
    lines long enough to be sentences, in order, without repeats."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) < 40 or line in seen or line.lower().startswith(("img", "advertisement", "also read",
                                                                          "read more", "follow us", "share")):
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out)


class Tavily:
    def __init__(self, store, key: str | None = None, daily_cap: int = 40, session=None):
        self.store = store
        self.key = tavily_api_key() if key is None else key
        self.daily_cap = daily_cap
        self.http = session or requests
        self._lock = threading.Lock()
        self.spent_this_run = 0

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    # budget ------------------------------------------------------------------------------
    def _today(self) -> dt.date:
        return dt.datetime.now(dt.timezone.utc).date()

    def _used(self, day: dt.date) -> tuple[int, int]:
        """(used today, used this month)"""
        rows = self.store.quota_month(USAGE_KEY, day.strftime("%Y-%m"))
        return rows.get(day.isoformat(), 0), sum(rows.values())

    def allowance_today(self) -> int:
        day = self._today()
        used_today, used_month = self._used(day)
        days_left = calendar.monthrange(day.year, day.month)[1] - day.day + 1
        left = max(0, MONTHLY_CREDITS - SAFETY - used_month + used_today)
        per_day = min(self.daily_cap, math.floor(left / days_left))
        return max(0, per_day - used_today)

    def _book(self, credits: int) -> bool:
        with self._lock:
            if credits <= 0:
                return True
            if self.allowance_today() < credits:
                return False
            day = self._today()
            used_today, _ = self._used(day)
            self.store.quota_save(USAGE_KEY, day.isoformat(), used_today + credits, 0)
            self.spent_this_run += credits
            return True

    def _refund(self, credits: int) -> None:
        with self._lock:
            day = self._today()
            used_today, _ = self._used(day)
            self.store.quota_save(USAGE_KEY, day.isoformat(), max(0, used_today - credits), 0)
            self.spent_this_run -= credits

    def _post(self, path: str, body: dict) -> dict:
        r = self.http.post(f"https://api.tavily.com/{path}", json=body, timeout=90,
                           headers={"Authorization": f"Bearer {self.key}"})
        if r.status_code != 200:
            raise RuntimeError(f"tavily {path} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # calls -------------------------------------------------------------------------------
    def extract(self, urls: list[str]) -> dict[str, str]:
        """Page text for each URL Tavily could read. Books ceil(n/5) credits up front (the most
        the call can cost) and refunds what the failures did not use."""
        out: dict[str, str] = {}
        if not self.enabled:
            return out
        for i in range(0, len(urls), 20):
            chunk = urls[i:i + 20]
            booked = math.ceil(len(chunk) / 5)
            if not self._book(booked):
                log.info("tavily: extract budget for today is spent; %d pages left unread", len(urls) - i)
                break
            try:
                data = self._post("extract", {"urls": chunk, "extract_depth": "basic", "format": "text"})
            except Exception as e:  # noqa: BLE001
                log.warning("tavily extract failed: %s", str(e)[:200])
                self._refund(booked)
                continue
            by_norm = {_norm_url(u): u for u in chunk}
            for r in data.get("results") or []:
                text = clean_page_text(r.get("raw_content") or "")
                asked = by_norm.get(_norm_url(r.get("url") or ""), r.get("url"))
                if asked and text:
                    out[asked] = text
            # charged by what Tavily returned (it may rewrite a URL), never less
            used = math.ceil(len([r for r in data.get("results") or [] if r.get("raw_content")]) / 5)
            if used < booked:
                self._refund(booked - used)
        return out

    def search(self, query: str, days: int = 3, domains: list[str] | None = None, max_results: int = 10) -> list[dict]:
        """News search; returns [{url, title, published_date}]. One credit."""
        if not self.enabled or not self._book(1):
            return []
        body = {"query": query[:380], "topic": "news", "max_results": max_results,
                "start_date": (self._today() - dt.timedelta(days=days)).isoformat()}
        if domains:
            body["include_domains"] = domains
        try:
            data = self._post("search", body)
        except Exception as e:  # noqa: BLE001
            log.warning("tavily search failed: %s", str(e)[:200])
            self._refund(1)
            return []
        return [{"url": r.get("url"), "title": r.get("title") or "", "published_date": r.get("published_date")}
                for r in data.get("results") or [] if r.get("url")]
