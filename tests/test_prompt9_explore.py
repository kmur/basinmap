#!/usr/bin/env python3
"""Stage 9: run the frontend search / species-key / dataset-catalog unit tests (tests/explore.test.mts)
and check the built bundle wires the explore module into the app."""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_dist  # noqa: E402
from test_prompt5_auth_review import _node_supports_type_stripping  # noqa: E402


@unittest.skipUnless(_node_supports_type_stripping(), "Node >= 22.18 required to run the TypeScript tests")
class TestExploreModule(unittest.TestCase):
    def test_explore_unit_tests_pass(self) -> None:
        proc = subprocess.run(
            [shutil.which("node") or "node", "--test", "--test-reporter=tap", "--disable-warning=ExperimentalWarning",
             "tests/explore.test.mts"],
            cwd=ROOT_DIR, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stdout[-4000:] + proc.stderr[-2000:])
        self.assertRegex(proc.stdout, r"(#|ℹ) fail 0")


class TestExploreBundle(unittest.TestCase):
    def test_bundle_contains_explore_and_stage9_ui(self) -> None:
        built = build_dist.build_dist()
        self.assertIn("explore_js", built)
        app_js = built["app_js"].read_text(encoding="utf-8")
        self.assertIn('from "./explore.js"', app_js)
        html = built["index"].read_text(encoding="utf-8")
        for element_id in ("taxon-search", "taxon-results", "species-key-card", "select-dataset", "select-basemap"):
            self.assertIn(f'id="{element_id}"', html)


if __name__ == "__main__":
    unittest.main()
