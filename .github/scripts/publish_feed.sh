#!/usr/bin/env bash
# Write the live articles as ready-made files (nishpaksh/feed.py) and replace the `feed` branch with
# them: one commit, force-pushed, so the branch never grows. The reading site (site/) reads them
# from GitHub's CDN and falls back to Supabase, so a failure here only costs speed.
set -euo pipefail
REMOTE="${FEED_REMOTE:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
DIR="${FEED_DIR:-feedout}"
rm -rf "$DIR"
git init --quiet -b feed "$DIR"
python -m nishpaksh.feed --dir "$DIR"
git -C "$DIR" add -A
git -C "$DIR" -c user.name="nishpaksh-feed" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "Feed $(date -u '+%Y-%m-%d %H:%M')"
git -C "$DIR" push --quiet --force "$REMOTE" HEAD:feed
echo "feed published"
