#!/usr/bin/env bash
# One-command "test, commit and push to staging".
#   bash scripts/ship.sh "Describe the change"
set -euo pipefail
cd "$(dirname "$0")/.."
msg="${1:-}"
if [ -z "$msg" ]; then echo 'Usage: bash scripts/ship.sh "commit message"'; exit 2; fi
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "staging" ]; then echo "You are on '$branch'. Run: git checkout staging"; exit 2; fi

echo "==> Building and testing (same checks as GitHub Actions)"
python3 scripts/build_dist.py >/dev/null
python3 -m unittest discover -s data-pipeline/tests >/dev/null 2>&1 || { echo "data-pipeline tests FAILED:"; python3 -m unittest discover -s data-pipeline/tests 2>&1 | tail -30; exit 1; }
python3 -m unittest discover -s tests >/dev/null 2>&1 || { echo "tests FAILED:"; python3 -m unittest discover -s tests 2>&1 | tail -30; exit 1; }
npx --no-install tsc --noEmit
echo "    all checks passed"

if [ -z "$(git status --porcelain)" ]; then echo "Nothing to commit."; exit 0; fi
echo "==> Changes:"; git status --short
git add -A
git commit -q -m "$msg"
git push -q origin staging
echo "==> Pushed to staging. Deploy: https://github.com/kmur/basinmap/actions"
