#!/usr/bin/env python3
"""Restore drill for a `wrangler d1 export` SQL dump.

Loads the dump into a scratch in-memory SQLite database, runs PRAGMA integrity_check and
foreign_key_check, confirms the core tables exist and reports row counts. Exit status is
non-zero when the dump does not restore cleanly, so the nightly backup workflow fails loudly
instead of archiving an unusable backup.

    python3 scripts/backup_restore_check.py basinmap-db.sql [--json report.json]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REQUIRED_TABLES = ("users", "observations", "media_intake", "observation_revisions", "publication_exports")


def _read_dump(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def restore_check(dump_path: Path, required: Sequence[str] = REQUIRED_TABLES) -> Dict[str, Any]:
    report: Dict[str, Any] = {"dump": str(dump_path), "bytes": dump_path.stat().st_size, "ok": False}
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.executescript(_read_dump(dump_path))
        except sqlite3.Error as exc:
            report["error"] = f"restore failed: {exc}"
            return report
        report["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        report["foreign_key_violations"] = len(fk)
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' ORDER BY name"
        )]
        tables: Dict[str, int] = {}
        for name in names:
            tables[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        report["tables"] = tables
        missing: List[str] = [t for t in required if t not in tables]
        report["missing_tables"] = missing
        report["ok"] = report["integrity_check"] == "ok" and not missing and not fk
        return report
    finally:
        conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", type=Path, help="SQL dump from `wrangler d1 export` (optionally .gz)")
    ap.add_argument("--json", type=Path, help="also write the report as JSON")
    args = ap.parse_args(argv)
    report = restore_check(args.dump)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
