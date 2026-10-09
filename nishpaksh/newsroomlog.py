"""A line in Supabase's `diagnostics` (kind 'newsroom') for each load and save of the newsroom copy, so its
health can be read from outside the job (GitHub's job logs cannot be downloaded from the sandbox).

    python -m nishpaksh.newsroomlog save ok "articles=16701 stories=7655" "2026-10-10T01:05:00Z run 1-1" 41M
"""
from __future__ import annotations

import os
import sys

from .config import database_url
from .db import Store, diagnostics, insert, utcnow


def main() -> None:
    event, status, counts, stamp, size = (sys.argv[1:] + [""] * 5)[:5]
    store = Store(database_url(), readers_url="")
    store.exec(insert(diagnostics).values(kind="newsroom", created_at=utcnow(), report={
        "event": event, "status": status, "counts": counts, "stamp": stamp, "size": size,
        "run": f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}",
        "workflow": os.environ.get("GITHUB_WORKFLOW", "")}))


if __name__ == "__main__":
    main()
