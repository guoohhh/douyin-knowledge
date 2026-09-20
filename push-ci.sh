#!/usr/bin/env bash
# Push the CI fix to integration/v1.
#
# Why this script exists: the agent sandbox has no git credentials (no helper, no ~/.ssh,
# no gh CLI) and `origin` is HTTPS, so `git push` there dies non-interactively. Run this
# from your own terminal, where your credentials live.
#
# Safe to re-run. It pushes one branch and never touches main.
set -euo pipefail

cd "$(dirname "$0")"

BRANCH=integration/v1
# HEAD as of the last agent pass. Four commits are unpushed: f54ee22 (P0-1, sidecar
# pagination), 16604f9 (P0-2, media acquisition), 9612a28 (P0-3, retroactive policy) and
# c5ce134 (P1-1, content-type triage). Remote is still at 293347a.
EXPECTED=c5ce134

echo "==> repo:   $(pwd)"
echo "==> branch: $(git branch --show-current)"

if [ "$(git branch --show-current)" != "$BRANCH" ]; then
  echo "ERROR: not on $BRANCH. Run: git checkout $BRANCH" >&2
  exit 1
fi

# Tracked files only: this script is itself untracked and must not trip its own check.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: working tree is dirty. Commit or stash first:" >&2
  git status --short >&2
  exit 1
fi

HEAD_SHORT=$(git rev-parse --short HEAD)
echo "==> HEAD:   $HEAD_SHORT"
if [ "$HEAD_SHORT" != "$EXPECTED" ]; then
  echo "NOTE: HEAD is $HEAD_SHORT, expected $EXPECTED."
  echo "      The branch moved since the agent last saw it. Review before pushing:"
  git log --oneline -3
  read -r -p "      Push anyway? [y/N] " reply
  [ "$reply" = "y" ] || { echo "aborted"; exit 1; }
fi

echo "==> pushing $BRANCH to origin"
git push origin "$BRANCH"

echo
echo "Done. Watch the three workflows here:"
echo "  https://github.com/guoohhh/douyin-knowledge/actions"
