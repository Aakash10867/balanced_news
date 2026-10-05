"""Paths, environment and tunable thresholds."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def load_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def normalize_db_url(url: str) -> str:
    """Pin the psycopg2 driver: SQLAlchemy 2.1 switched its default Postgres driver to psycopg 3."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return normalize_db_url(url)
    DATA_DIR.mkdir(exist_ok=True)
    return f"sqlite:///{DATA_DIR / 'nishpaksh.db'}"


def gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip()


def gemini_api_keys() -> list[str]:
    """Every configured key, each from its own Google Cloud project (GEMINI_API_KEY, _2, _3...).
    Duplicates are dropped: two copies of one key would double-count one quota."""
    keys = [gemini_api_key()] + [os.environ.get(f"GEMINI_API_KEY_{i}", "").strip() for i in range(2, 6)]
    return list(dict.fromkeys(k for k in keys if k))


def tavily_api_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


@dataclass(frozen=True)
class Settings:
    # ingestion
    max_article_age_hours: int = 48
    max_new_articles_per_run: int = 400
    max_new_per_feed: int = 30           # so a few prolific feeds cannot crowd out the rest
    max_article_chars: int = 7000
    min_full_text_chars: int = 400
    feed_disable_after_failures: int = 24
    # storage (Supabase free tier is 500 MB)
    vectors_retention_hours: int = 96        # embeddings + MinHash: only needed inside the 72 h windows
    unread_retention_days: int = 7           # articles never sent to a model (single-source stories)
    text_retention_days: int = 14            # full text of read articles; extracted claims are kept
    story_retention_days: int = 90           # whole stories: claims, verdicts, published pages
    storage_soft_limit_mb: int = 350         # above this, retention halves until back under

    # proactive search (discover.py) and Tavily (1,000 credits a month)
    search_stories_per_run: int = 10
    search_depth_share: float = 0.2      # share of searches spent on follow-ups to published stories
    search_every_hours: int = 6          # a story is searched again at most this often
    search_new_per_story: int = 3        # new outlets added per story per search
    tavily_extract_pages_per_run: int = 5
    tavily_searches_per_run: int = 1     # only when the free searches found nothing
    tavily_daily_cap: int = 31          # 31 x 31 days < 1,000 even if Tavily's month is not the calendar month

    # wire-copy detection (MinHash Jaccard on 5-word shingles)
    wire_jaccard: float = 0.45
    wire_window_hours: int = 72

    # story grouping
    story_window_hours: int = 72
    # Calibrated on 216 real article pairs labelled same / related / different event
    # (gemini-embedding-001, title + lead; tools/probe.py). "Different" pairs: 90% below 0.69.
    # "Related" (same saga, different event) overlaps "same" up to ~0.90, so the whole band between
    # goes to a model; only very close pairs (precision 0.94 at 0.92) join on similarity alone.
    story_join_cosine: float = 0.92     # mean similarity to the story's two closest articles: join
    story_core_cosine: float = 0.85     # ...and at least this close to the story's core article
    story_ask_cosine: float = 0.75      # from here up to join: ask a model "same specific event?"
    story_split_cosine: float = 0.72    # average-linkage cut when re-checking a story for separate events
    story_split_min_size: int = 4
    heal_per_run: int = 20
    embed_min_texts_per_run: int = 60      # Google counts each text; ~1,000 texts a day per key
    embed_reserve_per_run: int = 40        # kept back for each later run today (new arrivals)

    # extraction: only stories covered by >= 2 independent sources; at most this many articles each
    max_extract_per_story: int = 8

    # claim matching (TF-IDF within a story; the middle band goes to an LLM)
    claim_same_cosine: float = 0.75
    claim_candidate_cosine: float = 0.35
    match_batch_size: int = 25

    # perspectives
    omission_penalty: float = 0.25
    split_margin: float = 0.25
    global_min_sources: int = 6
    global_min_silhouette: float = 0.10

    # verdicts
    min_articles_to_verify: int = 2
    established_min_outlets: int = 3     # independent outlets (owner groups) that were actually read
    established_min_origins: int = 2     # independent origins (origins.py); the unattributed pool never counts
    established_after_hours: float = 6   # before this, an otherwise established statement is "developing"
    qualify_min_outlets: int = 3         # interim publishing rule while perspectives are unknown

    # run health checks
    health_max_story_articles: int = 80
    health_embed_batch: int = 25        # one grouping batch (stories.py embeds 25 texts per call)
    health_stall_runs: int = 3


SETTINGS = Settings()

# Evidence a source cites, and how much it counts toward independent support.
EVIDENCE_WEIGHT = {
    "fir": 3.0,
    "court_record": 3.0,
    "official_data": 3.0,
    "video": 3.0,
    "official_statement": 2.0,
    "named_witness": 1.5,
    "unnamed_source": 0.5,
    "none": 1.0,
}
# Only these can establish a claim as FALSE (or CONFIRMED). Official statements cannot:
# police and ministries are parties to many stories.
PRIMARY_EVIDENCE = {"fir", "court_record", "official_data", "video"}
