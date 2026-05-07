#!/usr/bin/env bash
# Restore / save the SQLite state DB on a dedicated orphan-style branch.
#
#   ./scripts/_ci_state.sh restore   # pulls bot-state:smart_money.db -> $DB_PATH
#   ./scripts/_ci_state.sh save      # force-pushes $DB_PATH -> bot-state branch
#
# Required env (provided automatically by GitHub Actions):
#   GITHUB_REPOSITORY   owner/repo
#   GH_TOKEN            a token with `contents: write` (use ${{ secrets.GITHUB_TOKEN }})
#
# Optional env:
#   STATE_BRANCH        default: bot-state
#   DB_PATH             default: data/smart_money.db
#   GITHUB_WORKSPACE    default: $PWD (set by Actions)

set -euo pipefail

ACTION="${1:-}"
STATE_BRANCH="${STATE_BRANCH:-bot-state}"
DB_PATH="${DB_PATH:-data/smart_money.db}"
WORKSPACE="${GITHUB_WORKSPACE:-$PWD}"

case "$ACTION" in
  restore)
    mkdir -p "$(dirname "$WORKSPACE/$DB_PATH")"
    if git ls-remote --exit-code origin "$STATE_BRANCH" >/dev/null 2>&1; then
      git fetch --depth=1 origin "$STATE_BRANCH"
      if git show "FETCH_HEAD:smart_money.db" > "$WORKSPACE/$DB_PATH" 2>/dev/null; then
        echo "restored $DB_PATH from origin/$STATE_BRANCH ($(stat -c%s "$WORKSPACE/$DB_PATH") bytes)"
      else
        rm -f "$WORKSPACE/$DB_PATH"
        echo "branch $STATE_BRANCH exists but has no smart_money.db; starting fresh"
      fi
    else
      echo "$STATE_BRANCH does not exist yet; starting fresh"
    fi
    ;;

  save)
    if [[ ! -f "$WORKSPACE/$DB_PATH" ]]; then
      echo "no DB at $DB_PATH; nothing to save"
      exit 0
    fi
    : "${GITHUB_REPOSITORY:?required}"
    : "${GH_TOKEN:?required}"

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    pushd "$tmp" >/dev/null

    git init -q -b "$STATE_BRANCH"
    git config user.email "smart-money-bot@users.noreply.github.com"
    git config user.name "smart-money-bot"

    cp "$WORKSPACE/$DB_PATH" smart_money.db
    git add smart_money.db
    git -c commit.gpgsign=false commit -q -m "state @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    git remote add origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
    git push --force origin "$STATE_BRANCH"

    popd >/dev/null
    echo "pushed $DB_PATH -> origin/$STATE_BRANCH"
    ;;

  *)
    echo "usage: $0 restore|save" >&2
    exit 2
    ;;
esac
