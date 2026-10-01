"""Check every feed in config/feeds.yaml and report how many items it returns.

    python -m nishpaksh.tools.check_feeds
"""
from nishpaksh.config import load_yaml
from nishpaksh.ingest import fetch_article, fetch_feed


def main() -> None:
    for f in load_yaml("feeds.yaml")["feeds"]:
        try:
            entries = fetch_feed(f)
            sample = fetch_article(entries[0]["url"]) if entries else None
            body = len((sample or {}).get("text") or "")
            print(f"OK   {f['name']:22s} {len(entries):3d} items; first article text {body} chars")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {f['name']:22s} {str(e)[:90]}")


if __name__ == "__main__":
    main()
