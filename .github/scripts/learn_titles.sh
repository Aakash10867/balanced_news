#!/usr/bin/env bash
# Titles learned from the outlets (nishpaksh/learn.py): append what has the evidence to
# config/titles_learned.yaml and commit that one file to the branch the job runs on. The hand-written
# config/titles.yaml is never touched. A push by the job's own token starts no other workflow. If the push is
# refused (a protected branch), the step fails harmlessly (it runs with continue-on-error): nothing else depends on it.
set -euo pipefail
BRANCH="${GITHUB_REF_NAME:-main}"
REMOTE="${LEARN_REMOTE:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
FILE="config/titles_learned.yaml"
# start from the branch as it is now (the owner may have struck a title out since this job started)
git -c user.name="nishpaksh-titles" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  pull --quiet --rebase --autostash "$REMOTE" "$BRANCH" || echo "could not update the branch; learning on the checkout"
python -m nishpaksh.learn --file "$FILE"
if git diff --quiet -- "$FILE" && [ -z "$(git ls-files --others --exclude-standard -- "$FILE")" ]; then
  echo "no new titles"
  exit 0
fi
git add "$FILE"
git -c user.name="nishpaksh-titles" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "Learned titles $(date -u '+%Y-%m-%d %H:%M')"
git push --quiet "$REMOTE" "HEAD:${BRANCH}" || {
  git -c user.name="nishpaksh-titles" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    pull --quiet --rebase "$REMOTE" "$BRANCH"
  git push --quiet "$REMOTE" "HEAD:${BRANCH}"
}
echo "learned titles pushed"
