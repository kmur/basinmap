#!/usr/bin/env bash
# Run a SQL query against the live D1 database.
#   bash scripts/d1.sh staging "SELECT COUNT(*) FROM observations"
#   bash scripts/d1.sh production "SELECT * FROM publication_runs ORDER BY started_at DESC LIMIT 3"
set -euo pipefail
case "${1:-}" in
  staging) db=basinmap-db-staging ;;
  production) db=basinmap-db ;;
  *) echo 'Usage: bash scripts/d1.sh staging|production "SQL"'; exit 2 ;;
esac
sql="${2:?SQL query required}"
# Run outside the repo so wrangler looks the database up by name (wrangler.jsonc holds placeholder ids).
cd /tmp && npx --yes wrangler@4 d1 execute "$db" --remote --command "$sql"
