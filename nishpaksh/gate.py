"""Should a scheduled run go ahead?

GitHub runs scheduled workflows on a best-effort basis: at busy times they are delayed by
hours or dropped. The workflow is therefore triggered every 15 minutes, and this gate lets
a trigger through only if no run has started in the last `--min-gap` minutes. Every hour
gets a run even if most triggers are dropped, and runs never pile up.

    python -m nishpaksh.gate --min-gap 50     # prints go=true or go=false
"""
from __future__ import annotations

import argparse
import datetime as dt

from sqlalchemy import func

from .config import database_url
from .db import Store, runs, select, utcnow


def should_run(store: Store, min_gap_minutes: float, now: dt.datetime | None = None) -> tuple[bool, str]:
    now = now or utcnow()
    last = store.one(select(func.max(runs.c.started_at).label("t")))
    last_start = last["t"] if last else None
    if last_start is None:
        return True, "no previous run"
    gap = (now - last_start).total_seconds() / 60
    if gap < min_gap_minutes:
        return False, f"last run started {gap:.0f} min ago"
    return True, f"last run started {gap:.0f} min ago"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--min-gap", type=float, default=50)
    a = p.parse_args()
    store = Store(database_url())
    store.init()
    go, why = should_run(store, a.min_gap)
    print(f"go={'true' if go else 'false'}")
    print(f"reason={why}")


if __name__ == "__main__":
    main()
