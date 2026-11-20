#!/usr/bin/env python3
"""Stage 5 tests: auth/session behaviour, submission rules, reviewer queue, private media.

Two layers:
  * TestWorkerHarness runs tests/worker_harness.test.mts, which executes the real
    src/worker/index.ts under Node (D1 via node:sqlite, in-memory R2, mocked ORCID/Turnstile).
  * TestLocalBackendStage5 checks the same routes on scripts/dev_server.py + backend_engine.py,
    which must stay behaviourally synchronized with the Worker (AGENTS.md invariant 4).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend_engine  # noqa: E402
from test_prompt3_backend_and_dwca import build_test_jpeg_with_exif, http_call  # noqa: E402


def _node_supports_type_stripping() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"v(\d+)\.(\d+)", out)
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return major > 22 or (major == 22 and minor >= 18)


class TestWorkerHarness(unittest.TestCase):
    @unittest.skipUnless(_node_supports_type_stripping(), "Node >= 22.18 required to execute the Worker TypeScript")
    def test_worker_executes_auth_submission_and_review_flows(self) -> None:
        proc = subprocess.run(
            [shutil.which("node") or "node", "--test", "--disable-warning=ExperimentalWarning", "tests/worker_harness.test.mts"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout[-6000:] + proc.stderr[-3000:])
        self.assertRegex(proc.stdout, r"# fail 0")


class TestLocalBackendStage5(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.public_r2 = tmp / "public-r2"
        (self.public_r2 / "overlays").mkdir(parents=True)
        (self.public_r2 / "overlays" / "approved.geojson").write_text('{"type":"FeatureCollection","features":[]}\n')
        self.db_path = tmp / "t.sqlite3"
        self.backend = backend_engine.BasinmapBackend(self.db_path, self.public_r2, tmp / "private-r2", enforce_turnstile=True)
        self.contrib = {"Authorization": "Bearer dev-contributor-orcid", "X-Turnstile-Token": "ok", "Content-Type": "application/json"}
        self.reviewer = {"Authorization": "Bearer dev-reviewer-orcid", "X-Turnstile-Token": "ok", "Content-Type": "application/json"}

    def tearDown(self) -> None:
        self.backend.close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, headers=None, body=None):
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        status, hdrs, resp = http_call(self.backend, self.public_r2, method, path, headers=headers, body=raw)
        try:
            return status, hdrs, json.loads(resp.decode()) if resp else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, hdrs, resp

    def submit(self, key: str, **overrides):
        payload = {
            "scientificName": "Oncorhynchus kisutch",
            "eventDate": "2026-11-10",
            "decimalLatitude": 44.05,
            "decimalLongitude": -123.08,
            "coordinateUncertaintyInMeters": 25,
            "locality": "McKenzie River",
            "notes": "Spawning pair",
            "license": "CC-BY 4.0",
            "licenseAccepted": True,
        }
        payload.update(overrides)
        return self.call("POST", "/api/observations", {**self.contrib, "Idempotency-Key": key}, payload)

    def test_migrations_are_tracked_and_reapplied_safely(self) -> None:
        self.backend.close()
        self.backend = backend_engine.BasinmapBackend(self.db_path, self.public_r2, Path(self.tmp.name) / "private-r2")
        names = [r[0] for r in self.backend.conn.execute("SELECT name FROM _local_migrations ORDER BY name")]
        self.assertEqual(names, sorted(p.name for p in (ROOT_DIR / "migrations").glob("*.sql")))
        self.assertIn("0002_auth_sessions.sql", names)
        cols = {r[1] for r in self.backend.conn.execute("PRAGMA table_info(observations)")}
        self.assertIn("license_accepted_at", cols)

    def test_config_and_auth_routes_in_development(self) -> None:
        st, _, cfg = self.call("GET", "/api/config")
        self.assertEqual(st, 200)
        self.assertTrue(cfg["auth"]["dev_tokens_enabled"])
        self.assertFalse(cfg["auth"]["orcid_login_enabled"])
        self.assertEqual(cfg["max_media_per_observation"], 10)
        st, _, login = self.call("GET", "/api/auth/login")
        self.assertEqual((st, login["code"]), (503, "orcid_not_configured"))
        st, _, me = self.call("GET", "/api/auth/me", self.reviewer)
        self.assertEqual(me["user"]["role"], "reviewer")
        self.assertEqual(me["auth_method"], "dev-bearer")
        self.assertNotIn("email", me["user"])

    def test_submission_rules(self) -> None:
        st, _, err = self.submit("k0", licenseAccepted=False)
        self.assertEqual((st, err["code"]), (400, "license_not_accepted"))
        st, _, err = self.submit("k0", eventDate="2099-01-01")
        self.assertEqual((st, err["code"]), (400, "invalid_date"))
        st, _, err = self.submit("k0", coordinateUncertaintyInMeters=0)
        self.assertEqual((st, err["code"]), (400, "invalid_uncertainty"))
        st, _, ok = self.submit("k1", recordedByID="https://orcid.org/9999-9999-9999-9999")
        self.assertEqual(st, 201)
        obs = ok["observation"]
        self.assertEqual(obs["recorded_by_id"], "https://orcid.org/0000-0002-1825-0097")
        self.assertIsNotNone(obs["license_accepted_at"])
        self.assertEqual(obs["verbatim"]["notes"], "Spawning pair")
        # Replay without a Turnstile token still returns the saved record.
        st, _, replay = self.call(
            "POST", "/api/observations", {**self.contrib, "Idempotency-Key": "k1", "X-Turnstile-Token": ""}, {"license": "x"}
        )
        self.assertEqual(st, 200)
        self.assertTrue(replay["idempotent_replay"])
        # Same key from another account -> 409, never the other user's record.
        st, _, _ = self.call("POST", "/api/observations", {**self.reviewer, "Idempotency-Key": "k1"}, {"license": "x"})
        self.assertEqual(st, 409)
        st, _, mine = self.call("GET", "/api/observations/mine", self.contrib)
        self.assertEqual([o["occurrence_id"] for o in mine["observations"]], [obs["occurrence_id"]])
        self.assertEqual(mine["observations"][0]["media_count"], 0)

    def test_review_queue_private_media_and_reidentification(self) -> None:
        _, _, created = self.submit("k-rq")
        occ = created["observation"]["occurrence_id"]
        near = build_test_jpeg_with_exif(44, 3, 123, 5)
        far = build_test_jpeg_with_exif(44, 25, 123, 5)
        ids = []
        for img in (near, far):
            st, _, res = self.call("PUT", f"/api/uploads/{quote(occ)}", {"Authorization": "Bearer dev-contributor-orcid"}, img)
            self.assertEqual(st, 201)
            ids.append(res["media_id"])

        st, _, _ = self.call("GET", "/api/review/pending", self.contrib)
        self.assertEqual(st, 403)
        st, _, queue = self.call("GET", "/api/review/pending?status=submitted&limit=10", self.reviewer)
        self.assertEqual(st, 200)
        item = queue["observations"][0]
        self.assertEqual(item["occurrence_id"], occ)
        self.assertAlmostEqual(item["verbatim"]["latitude"], 44.05)
        self.assertEqual(item["submitter"]["orcid_id"], "https://orcid.org/0000-0002-1825-0097")
        self.assertLess(item["media"][0]["exif_offset_m"], 300)
        self.assertFalse(item["media"][0]["exif_mismatch"])
        self.assertTrue(item["media"][1]["exif_mismatch"])
        self.assertEqual(item["media"][0]["url"], f"/api/media/{ids[0]}")
        st, _, _ = self.call("GET", "/api/review/pending?status=bogus", self.reviewer)
        self.assertEqual(st, 400)

        st, hdrs, img = self.call("GET", f"/api/media/{ids[0]}", self.reviewer)
        self.assertEqual((st, hdrs["cache-control"]), (200, "private, no-store"))
        self.assertEqual(img, near)
        st, _, _ = self.call("GET", f"/api/media/{ids[0]}")
        self.assertEqual(st, 401)

        st, _, patched = self.call(
            "PATCH", f"/api/observations/{quote(occ)}", self.reviewer, {"scientificName": "Oncorhynchus tshawytscha"}
        )
        self.assertEqual(st, 200)
        self.assertEqual(patched["observation"]["identified_by_id"], "https://orcid.org/0000-0001-5109-3700")
        self.assertEqual(patched["observation"]["review_status"], "submitted")

        st, _, appr = self.call("POST", f"/api/review/{quote(occ)}/approve", self.reviewer, {"notes": "ok"})
        self.assertEqual((st, appr["review_status"], appr["occurrence_id"]), (200, "approved", occ))
        self.assertEqual(appr["observation"]["identified_by_id"], "https://orcid.org/0000-0001-5109-3700")

    def test_media_limit_per_observation(self) -> None:
        _, _, created = self.submit("k-lim")
        occ = quote(created["observation"]["occurrence_id"])
        img = build_test_jpeg_with_exif(44, 3, 123, 5)
        for _ in range(10):
            self.assertEqual(self.call("PUT", f"/api/uploads/{occ}", {"Authorization": "Bearer dev-contributor-orcid"}, img)[0], 201)
        self.assertEqual(self.call("PUT", f"/api/uploads/{occ}", {"Authorization": "Bearer dev-contributor-orcid"}, img)[0], 409)


if __name__ == "__main__":
    unittest.main()
