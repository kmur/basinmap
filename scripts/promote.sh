#!/usr/bin/env bash
# Promote what is on staging to production (basinmap.org).
#   bash scripts/promote.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain)" ]; then echo "Uncommitted changes; run scripts/ship.sh first."; exit 2; fi
git fetch -q origin
echo "Commits going to production:"; git log --oneline origin/main..origin/staging
read -r -p "Promote these to basinmap.org? [y/N] " ok
[ "$ok" = "y" ] || { echo "Cancelled."; exit 0; }
git checkout -q main && git pull -q --ff-only origin main
git merge -q --no-edit origin/staging
git push -q origin main
git checkout -q staging
echo "==> Pushed to main. Deploy: https://github.com/kmur/basinmap/actions"
