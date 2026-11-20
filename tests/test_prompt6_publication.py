#!/usr/bin/env python3
"""Stage 6 tests: Darwin Core Archive publication pipeline, GBIF validator/crawl/reconciliation,
Worker/Python archive parity, and the D1 backup restore drill."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backend_engine  # noqa: E402
import backup_restore_check  # noqa: E402
import export_dwca  # noqa: E402
from test_prompt3_backend_and_dwca import build_test_jpeg_with_exif, http_call  # noqa: E402
from test_prompt5_auth_review import _node_supports_type_stripping  # noqa: E402

DATASET_KEY = "11111111-2222-3333-4444-555555555555"
UAT = "https://api.gbif-uat.org/v1"


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeGbifApi:
    def __init__(self) -> None:
        self.calls = []
        self.indexed = []

    def __call__(self, req, timeout=0):
        url = req.full_url
        self.calls.append((req.get_method(), url, req.get_header("Authorization"), req.data))
        if url == export_dwca.VALIDATOR_URL and req.get_method() == "POST":
            return FakeResp(json.dumps({"key": "val-1", "status": "SUBMITTED"}).encode())
        if url.startswith(export_dwca.VALIDATOR_URL + "/"):
            return FakeResp(json.dumps({"key": "val-1", "status": "FINISHED", "indexeable": True}).encode())
        if url.endswith(f"/dataset/{DATASET_KEY}/crawl"):
            return FakeResp(b"")
        if "/occurrence/search?" in url:
            return FakeResp(json.dumps({"endOfRecords": True, "results": self.indexed}).encode())
        raise AssertionError(f"unexpected call {url}")


class TestPythonPublicationPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.r2 = tmp / "public-r2"
        (self.r2 / "overlays").mkdir(parents=True)
        self.backend = backend_engine.BasinmapBackend(tmp / "db.sqlite3", self.r2, tmp / "private-r2")
        self.contrib = {"Authorization": "Bearer dev-contributor-orcid", "X-Turnstile-Token": "ok", "Content-Type": "application/json"}
        self.reviewer = {"Authorization": "Bearer dev-reviewer-orcid", "Content-Type": "application/json"}
        self.api = FakeGbifApi()
        self.env = mock.patch.dict(os.environ, {"GBIF_USER": "basinmap-bot", "GBIF_PWD": "bot-password"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.backend.close()
        self.tmp.cleanup()

    def call(self, method, path, headers=None, body=None):
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        st, _, resp = http_call(self.backend, self.r2, method, path, headers=headers, body=raw)
        return st, json.loads(resp.decode()) if resp else None

    def submit(self, key, **over):
        payload = {"scientificName": "Oncorhynchus kisutch", "eventDate": "2026-11-10", "decimalLatitude": 44.05,
                   "decimalLongitude": -123.08, "coordinateUncertaintyInMeters": 25, "license": "CC-BY 4.0",
                   "licenseAccepted": True, **over}
        st, res = self.call("POST", "/api/observations", {**self.contrib, "Idempotency-Key": key}, payload)
        self.assertEqual(st, 201)
        return res["observation"]["occurrence_id"]

    def run_pipeline(self, force=False):
        return export_dwca.run_publication(
            self.backend.conn, self.r2, "test", public_base_url="https://staging.basinmap.org/api/r2",
            dataset_key=DATASET_KEY, api_base=UAT, force=force, urlopen=self.api,
            rebuild_overlay=self.backend.rebuild_approved_overlay,
        )

    def status_of(self, oid):
        return self.backend.conn.execute(
            "SELECT publication_status, gbif_occurrence_key, updated_at FROM observations WHERE occurrence_id = ?", (oid,)
        ).fetchone()

    def test_full_lifecycle(self) -> None:
        a = self.submit("a")
        b = self.submit("b", scientificName="Castor canadensis", license="CC0 1.0")
        c = self.submit("c", scientificName="Ardea herodias")
        st, _ = self.call("PUT", f"/api/uploads/{quote(a)}", {"Authorization": "Bearer dev-contributor-orcid"},
                          build_test_jpeg_with_exif(44, 3, 123, 5))
        self.assertEqual(st, 201)
        for oid in (a, b):
            self.assertEqual(self.call("POST", f"/api/review/{quote(oid)}/approve", self.reviewer, {"notes": "ok"})[0], 200)
        updated_before = self.status_of(a)["updated_at"]

        run1 = self.run_pipeline()
        exp = run1["export"]
        self.assertTrue(exp["changed"])
        self.assertEqual((exp["record_count"], exp["media_count"], exp["newly_exported"]), (2, 1, 2))
        self.assertTrue(exp["validation"]["valid"])
        self.assertEqual(run1["validator"], "submitted")
        self.assertEqual(run1["crawl"], "requested")
        post = next(c for c in self.api.calls if c[0] == "POST" and c[1] == export_dwca.VALIDATOR_URL)
        self.assertTrue(post[2].startswith("Basic "))
        self.assertIn(b'name="file"', post[3])
        self.assertIn(b'name="validationRequest"', post[3])
        self.assertTrue((self.r2 / exp["archive_snapshot_r2_key"]).is_file())
        status = json.loads((self.r2 / "exports" / "status.json").read_text())
        self.assertEqual(status["approved_records_by_publication_status"], {"exported": 2})
        self.assertEqual(self.status_of(a)["publication_status"], "exported")
        self.assertEqual(self.status_of(a)["updated_at"], updated_before, "status changes must not alter dwc:modified")
        self.assertEqual(self.status_of(c)["publication_status"], "not_exported")

        run2 = self.run_pipeline()
        self.assertFalse(run2["export"]["changed"])
        self.assertEqual(run2["validations"], [{"export_id": exp["export_id"], "status": "finished"}])
        self.assertTrue(run2["status"]["latest_export"]["gbif_validator"]["indexeable"])

        self.api.indexed = [{"key": 4400000123, "occurrenceID": a}, {"key": 1, "occurrenceID": "urn:other:1"}]
        run3 = self.run_pipeline(force=True)
        self.assertEqual(run3["reconcile"], {"checked": 2, "newly_indexed": 1, "complete": True})
        self.assertEqual(tuple(self.status_of(a))[:2], ("indexed_by_gbif", 4400000123))
        self.assertEqual(run3["export"]["newly_exported"], 0)

        overlay = json.loads((self.r2 / "overlays" / "approved.geojson").read_text())
        self.assertEqual(sorted(f["id"] for f in overlay["features"]), sorted([a, b]))
        (self.r2 / "releases" / "v9").mkdir(parents=True)
        (self.r2 / "releases" / "manifest.json").write_text(
            json.dumps({"layers": {"snapshot_own_occurrence_ids": "/releases/v9/own.json"}}))
        (self.r2 / "releases" / "v9" / "own.json").write_text(json.dumps([a]))
        self.call("POST", f"/api/review/{quote(b)}/needs_changes", self.reviewer, {"notes": "check id"})
        overlay = json.loads((self.r2 / "overlays" / "approved.geojson").read_text())
        self.assertEqual(overlay["features"], [], "A is in the live snapshot; B is no longer approved")

        run4 = self.run_pipeline()
        self.assertTrue(run4["export"]["changed"])
        self.assertEqual((run4["export"]["record_count"], run4["export"]["withdrawn_count"]), (1, 1))
        self.assertEqual(self.status_of(b)["publication_status"], "not_exported")
        note = self.backend.conn.execute(
            "SELECT notes FROM observation_revisions WHERE occurrence_id = ? ORDER BY revision_number DESC LIMIT 1", (b,)
        ).fetchone()[0]
        self.assertTrue(note.startswith("Withdrawn: omitted from DwC-A"))
        runs = self.backend.conn.execute("SELECT COUNT(*), SUM(error IS NOT NULL) FROM publication_runs").fetchone()
        self.assertEqual(tuple(runs), (4, 0))

    def test_crawl_without_registry_rights_and_missing_credentials(self) -> None:
        a = self.submit("a")
        self.call("POST", f"/api/review/{quote(a)}/approve", self.reviewer, {})

        def forbidden(req, timeout=0):
            if req.full_url.endswith("/crawl"):
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
            return self.api(req, timeout)

        res = export_dwca.run_publication(self.backend.conn, self.r2, "test", dataset_key=DATASET_KEY, api_base=UAT, urlopen=forbidden)
        self.assertEqual(res["crawl"], "not_authorized")
        with mock.patch.dict(os.environ, {"GBIF_USER": "", "GBIF_PWD": ""}):
            res2 = export_dwca.run_publication(self.backend.conn, self.r2, "test", force=True, urlopen=self.api)
        self.assertEqual((res2["validator"], res2["crawl"]), ("not_submitted", "no_dataset_key"))
        with self.assertRaises(RuntimeError), mock.patch.dict(os.environ, {"GBIF_USER": ""}):
            export_dwca.submit_to_gbif_validator(self.r2 / "exports" / "dwca-latest.zip")

    def test_empty_archive_is_not_sent_to_gbif(self) -> None:
        res = self.run_pipeline()
        self.assertTrue(res["export"]["changed"])
        self.assertEqual(res["export"]["record_count"], 0)
        self.assertEqual((res["validator"], res["crawl"]), ("skipped_empty", "skipped_empty"))
        self.assertEqual([c for c in self.api.calls if c[0] == "POST"], [], "no validator upload or crawl request")
        eml = (self.r2 / "exports" / "eml.xml").read_text()
        self.assertEqual(eml.count("<positionName>Data Steward</positionName>"), 3)
        self.assertEqual(eml.count("<surName>Murch</surName>"), 3)

    def test_dev_server_admin_endpoints(self) -> None:
        self.assertEqual(self.call("POST", "/api/admin/publication/run", self.reviewer, {})[0], 403)
        self.backend.conn.execute("UPDATE users SET role = 'admin' WHERE id = 'usr-review-1'")
        self.backend.conn.commit()
        with mock.patch.dict(os.environ, {"GBIF_USER": "", "GBIF_PWD": ""}):
            st, res = self.call("POST", "/api/admin/publication/run", self.reviewer, {"force": True})
        self.assertEqual((st, res["export"]["changed"]), (200, True))
        st, res = self.call("GET", "/api/admin/publication/status", self.reviewer)
        self.assertEqual((st, len(res["runs"]), len(res["exports"])), (200, 1, 1))


@unittest.skipUnless(_node_supports_type_stripping(), "Node >= 22.18 required to execute the Worker TypeScript")
class TestWorkerArchiveParity(unittest.TestCase):
    def test_worker_archive_is_valid_and_matches_python_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "worker-dwca.zip"
            proc = subprocess.run(
                [shutil.which("node") or "node", "--test", "--disable-warning=ExperimentalWarning",
                 "--test-name-pattern", "publication pipeline", "tests/worker_harness.test.mts"],
                cwd=ROOT_DIR, capture_output=True, text=True, timeout=300, env={**os.environ, "DWCA_OUT": str(out)},
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout[-4000:] + proc.stderr[-2000:])
            self.assertTrue(out.is_file())
            res = export_dwca.validate_dwca_zip(out)
            self.assertEqual((res["occurrence_rows"], res["multimedia_rows"]), (2, 1))
            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(zf.read("meta.xml").decode(), export_dwca._build_meta_xml())
                occ_header = zf.read("occurrence.txt").decode().splitlines()[0]
                self.assertEqual(occ_header, "\t".join(c for c, _ in export_dwca.OCCURRENCE_FIELDS))
                eml = zf.read("eml.xml").decode()
                self.assertTrue(re.search(r"<pubDate>\d{4}-\d{2}-\d{2}</pubDate>", eml))


class TestBackupRestoreDrill(unittest.TestCase):
    def _dump(self, path: Path, conn) -> None:
        with path.open("w", encoding="utf-8") as f:
            for line in conn.iterdump():
                f.write(f"{line}\n")

    def test_restore_drill_on_backend_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            backend = backend_engine.BasinmapBackend(tmp / "db.sqlite3", tmp / "r2", tmp / "p")
            dump = tmp / "basinmap-db.sql"
            self._dump(dump, backend.conn)
            backend.close()
            report = backup_restore_check.restore_check(dump)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["integrity_check"], "ok")
            self.assertGreaterEqual(report["tables"]["users"], 2)
            for table in backup_restore_check.REQUIRED_TABLES:
                self.assertIn(table, report["tables"])

    def test_restore_drill_rejects_truncated_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "bad.sql"
            dump.write_text("CREATE TABLE users (id TEXT);\nINSERT INTO users VALUES ('x');\n")
            report = backup_restore_check.restore_check(dump)
            self.assertFalse(report["ok"])
            self.assertIn("observations", report["missing_tables"])


if __name__ == "__main__":
    unittest.main()
