#!/usr/bin/env bash
# The newsroom database (Oct 10 2026): everything the pipeline and the desk work on lives in a Postgres inside
# the GitHub Actions job (service container "postgres", port 5432), loaded from a saved copy at the start of
# the job and saved again at the end. Reads there cost nothing; Supabase keeps only the readers' tables and the
# run logs (nishpaksh/db.py READER_TABLES). Supabase's free egress (5 GB a month) was used ~10x over by the
# pipeline reading its own data back every hour.
#
# The copy is kept twice, ENCRYPTED (the repo is public and the copy holds outlets' article text):
#   1. the Actions cache (fast), key newsroom-<run>-<attempt>, at .newsroom/cache/
#   2. the `newsroom` branch (backup), one commit force-pushed, in parts of 90 MB
# Each copy carries a stamp (UTC time of the save + run id); restore takes the newer of the two.
#
#   newsroom.sh restore     load the newest copy into the local Postgres (fails if there is none: the job
#                           must never start from an empty newsroom and then save it over the real one)
#   newsroom.sh save        dump, check, encrypt, write the cache copy and push the branch copy
#   newsroom.sh seed URL    one time: copy the newsroom tables from Supabase (URL) into the local Postgres
#
# Needs NEWSROOM_KEY (secret), GH_TOKEN and GITHUB_REPOSITORY. Postgres tools run from the postgres:17 image,
# the same version as the service and as Supabase.
set -Eeuo pipefail

DIR="${NEWSROOM_DIR:-.newsroom}"
CACHE="$DIR/cache"
DB="newsroom"
LOCAL="postgresql://postgres:postgres@localhost:5432"
BRANCH="newsroom"
REMOTE="${NEWSROOM_REMOTE:-https://x-access-token:${GH_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"
# the newsroom tables (everything in public except READER_TABLES in nishpaksh/db.py)
TABLES=(feeds articles stories claims canonical story_pairs source_clusters published translations story_links quota_usage)
READERS=(profiles follows push_subscriptions notifications audio_requests audio_files recaps videos saved notify_state
         account_events runs diagnostics)

mkdir -p "$CACHE"
WORK="$(cd "$DIR" && pwd)"

if [ -n "${NEWSROOM_NATIVE:-}" ]; then   # tests on a machine with its own Postgres tools
  W="$WORK"
  pg() { PGPASSWORD=postgres "$@"; }
else                                     # a Postgres client tool from the postgres:17 image, $DIR mounted as /w
  W="/w"
  pg() { docker run --rm --network host -e PGPASSWORD=postgres -v "$WORK:/w" postgres:17 "$@"; }
fi
sql() { pg psql -X -q -v ON_ERROR_STOP=1 -t -A "$LOCAL/${2:-$DB}" -c "$1"; }

# every load and save, and any failure, is written to Supabase's diagnostics (kind 'newsroom') when
# NEWSROOM_LOG_URL is set, so the newsroom's health is visible outside the job; logging never fails the job
log_event() {   # event status counts stamp size
  if [ -n "${NEWSROOM_LOG_URL:-}" ]; then
    DATABASE_URL="$NEWSROOM_LOG_URL" python -m nishpaksh.newsroomlog "$@" >/dev/null 2>&1 || echo "(could not log $1)"
  fi
}
CMD="${1:-}"
trap 'log_event "$CMD" failed "line $LINENO: $BASH_COMMAND"' ERR
fail() { echo "$1"; trap - ERR; log_event "$CMD" refused "$1"; exit 1; }

need_key() {
  if [ -z "${NEWSROOM_KEY:-}" ]; then fail "NEWSROOM_KEY is not set (repository secret)"; fi
}
encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:NEWSROOM_KEY -in "$1" -out "$2"; }
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:NEWSROOM_KEY -in "$1" -out "$2"; }

wait_for_postgres() {
  for _ in $(seq 1 60); do
    if pg pg_isready -q -h localhost -p 5432 -U postgres; then return 0; fi
    sleep 2
  done
  fail "the local Postgres did not start"
}

fresh_db() {
  sql "drop database if exists $DB" postgres
  sql "create database $DB" postgres
  # roles the Supabase schema names in its row-level-security policies; the job connects as postgres
  # (a superuser), so the policies change nothing here, they only have to load
  for r in anon authenticated service_role nishpaksh_app; do
    sql "do \$\$ begin if not exists (select 1 from pg_roles where rolname = '$r') then create role $r nologin; end if; end \$\$" postgres
  done
}

counts() {   # "articles=16701 stories=7655 ..." for the main tables
  local out=""
  for t in articles stories claims canonical published; do
    out="$out $t=$(sql "select count(*) from $t")"
  done
  echo "${out# }"
}

check_counts() {   # a copy with no articles or no stories is never accepted
  local c; c="$(counts)"
  echo "newsroom rows: $c"
  case " $c " in
    *" articles=0 "*|*" stories=0 "*) fail "the newsroom copy is empty: refusing it" ;;
  esac
}

load_dump() {   # $1 = plain dump file inside $DIR
  fresh_db
  pg pg_restore --no-owner --no-acl --exit-on-error --single-transaction -d "$LOCAL/$DB" "$W/$1"
  check_counts
}

restore() {
  need_key
  wait_for_postgres
  local cache_stamp="" branch_stamp="" pick=""
  if [ -s "$CACHE/newsroom.dump.enc" ] && [ -s "$CACHE/stamp" ]; then cache_stamp="$(cat "$CACHE/stamp")"; fi
  rm -rf "$DIR/branch"
  if git clone --quiet --depth 1 --branch "$BRANCH" "$REMOTE" "$DIR/branch" 2>/dev/null && [ -s "$DIR/branch/stamp" ]; then
    branch_stamp="$(cat "$DIR/branch/stamp")"
  fi
  echo "cache copy:  ${cache_stamp:-none}"
  echo "branch copy: ${branch_stamp:-none}"
  if [ -z "$cache_stamp" ] && [ -z "$branch_stamp" ]; then
    fail "no saved newsroom copy: stopping (run the newsroom-seed workflow once)"
  fi
  # the stamp starts with an ISO time, so the newer copy sorts last
  if [ -n "$cache_stamp" ] && { [ -z "$branch_stamp" ] || [[ ! "$branch_stamp" > "$cache_stamp" ]]; }; then
    pick="cache"; cp "$CACHE/newsroom.dump.enc" "$DIR/in.enc"
  else
    pick="branch"; cat "$DIR"/branch/newsroom.dump.enc.part-* > "$DIR/in.enc"
  fi
  echo "restoring the $pick copy"
  decrypt "$DIR/in.enc" "$DIR/in.dump"
  rm -f "$DIR/in.enc"
  load_dump in.dump
  rm -f "$DIR/in.dump"
  rm -rf "$DIR/branch"
  date -u +%s > "$DIR/restored"     # save refuses to run without this mark
  log_event restore "ok ($pick)" "$(counts)" "$([ "$pick" = cache ] && echo "$cache_stamp" || echo "$branch_stamp")"
}

save() {
  need_key
  if [ ! -f "$DIR/restored" ]; then fail "this job never loaded a newsroom copy: not saving"; fi
  check_counts
  local tabs=()
  for t in "${TABLES[@]}"; do tabs+=(-t "public.$t"); done      # the newsroom tables only (with their sequences)
  pg pg_dump --no-owner --no-acl -Fc -Z 6 "${tabs[@]}" -f $W/out.dump "$LOCAL/$DB"
  # the dump must hold the main tables before it replaces anything
  local listing; listing="$(pg pg_restore -l $W/out.dump)"
  for t in articles stories claims canonical published; do
    if ! grep -q "TABLE DATA public $t " <<<"$listing"; then
      fail "the new dump has no data for $t: not saving"
    fi
  done
  local stamp; stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ) run ${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
  encrypt "$DIR/out.dump" "$DIR/out.enc"
  rm -f "$DIR/out.dump"
  echo "newsroom copy: $(du -h "$DIR/out.enc" | cut -f1), stamp $stamp"
  # 1. the cache copy (the workflow's actions/cache/save step stores $CACHE after this)
  mv "$DIR/out.enc" "$CACHE/newsroom.dump.enc"
  echo "$stamp" > "$CACHE/stamp"
  # 2. the branch copy: one commit, force-pushed, so the branch never grows
  rm -rf "$DIR/push"; mkdir -p "$DIR/push"
  split -b 90m -d -a 3 "$CACHE/newsroom.dump.enc" "$DIR/push/newsroom.dump.enc.part-"
  echo "$stamp" > "$DIR/push/stamp"
  cat > "$DIR/push/README.md" <<'EOF'
Encrypted copy of the Nishpaksh newsroom database (.github/scripts/newsroom.sh). Not for reading.
EOF
  git init --quiet -b "$BRANCH" "$DIR/push"
  git -C "$DIR/push" add -A
  git -C "$DIR/push" -c user.name="nishpaksh-newsroom" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    commit --quiet -m "Newsroom $stamp"
  local ok=""
  for attempt in 1 2 3; do
    if git -C "$DIR/push" push --quiet --force "$REMOTE" "HEAD:$BRANCH"; then ok=1; break; fi
    echo "push failed (try $attempt)"; sleep 10
  done
  rm -rf "$DIR/push"
  if [ -z "$ok" ]; then fail "the backup branch could not be pushed"; fi
  log_event save ok "$(counts)" "$stamp" "$(du -h "$CACHE/newsroom.dump.enc" | cut -f1)"
  echo "newsroom saved"
}

seed() {   # $1 = Supabase database URL
  need_key
  wait_for_postgres
  local src="$1" tabs=()
  # pg_dump needs one session for the whole dump: Supabase's pooler gives that on 5432 (session mode), not
  # on 6543 (transaction mode)
  src="${src/:6543\//:5432\/}"
  for t in "${TABLES[@]}"; do tabs+=(-t "public.$t"); done
  local where; where="$(sed -E 's#^[a-z+]+://[^@]*@##; s#\?.*$##' <<<"$src")"   # host:port/db, no credentials
  echo "copying from $where"
  if ! pg pg_dump --no-owner --no-acl -Fc -Z 6 "${tabs[@]}" -f $W/seed.dump "$src" 2> "$DIR/seed.err"; then
    cat "$DIR/seed.err"
    fail "pg_dump from $where failed: $(tail -c 600 "$DIR/seed.err" | tr '\n' ' ')"
  fi
  load_dump seed.dump
  rm -f "$DIR/seed.dump"
  date -u +%s > "$DIR/restored"
  log_event seed ok "$(counts)"
}

case "${1:-}" in
  restore) restore ;;
  save) save ;;
  seed) seed "${2:?the Supabase database URL}" ;;
  counts) counts ;;
  *) echo "usage: newsroom.sh restore|save|seed URL|counts"; exit 2 ;;
esac
