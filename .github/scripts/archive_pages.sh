#!/usr/bin/env bash
# Move published articles older than 3 days to the `archive` branch, then delete them from the database
# (nishpaksh/pagearchive.py). Only page files and the month index are checked out: the branch grows by
# ~50 small files a day and is never cloned whole. A story is deleted only after the push succeeded.
set -euo pipefail
REMOTE="${ARCHIVE_REMOTE:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
DIR="${ARCHIVE_DIR:-arch}"
rm -rf "$DIR"
if git ls-remote --exit-code --heads "$REMOTE" archive >/dev/null 2>&1; then
  git clone --quiet --depth 1 --filter=blob:none --sparse --branch archive "$REMOTE" "$DIR"
  git -C "$DIR" sparse-checkout set --no-cone '/index/' '/README.md'
else
  git init --quiet -b archive "$DIR"
  git -C "$DIR" remote add origin "$REMOTE"
  printf '%s\n' "# Nishpaksh archive" "" \
    "Every article as published on the site, moved here once it is three days old (never changed)." \
    "pages/<story_id>.json.gz: the article in English and Hindi. index/<YYYY-MM>.jsonl: one line per article." \
    > "$DIR/README.md"
fi
python -m nishpaksh.pagearchive export --dir "$DIR"
git -C "$DIR" add --sparse README.md
[ -d "$DIR/pages" ] && git -C "$DIR" add --sparse pages
[ -d "$DIR/index" ] && git -C "$DIR" add --sparse index
if git -C "$DIR" diff --cached --quiet; then
  echo "nothing new to archive"
else
  git -C "$DIR" -c user.name="nishpaksh-archive" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    commit --quiet -m "Archive articles $(date -u '+%Y-%m-%d %H:%M')"
  git -C "$DIR" push --quiet origin HEAD:archive
fi
# pages already on the branch (this push or an earlier one whose delete did not run) leave the database
python -m nishpaksh.pagearchive delete --dir "$DIR"
