"""Pruebas del orquestador (scripts/process_latest.py) sin red: cadena de pendientes, manifest e indice."""
import json, os, sys, tempfile, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import process_latest as pl  # noqa: E402
import sde_delta as s  # noqa: E402


def fake_meta(chain):
    """chain: {build: build_anterior}; devuelve un sustituto de changelog_meta."""
    return lambda build, _dir: ({"lastBuildNumber": chain[build], "releaseDate": f"2025-01-{build % 28 + 1:02d}T00:00:00Z"}
                                if build in chain else None)


def touch(path, data="{}"):
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def result(errors=0, complete=True, tables=None):
    tables = tables if tables is not None else [
        {"table": "types", "added": 1, "removed": 0, "changed": 2, "schema_ops": {"add_path": 1}, "flags": [],
         "renamed_from": None},
        {"table": "same", "added": 0, "removed": 0, "changed": 0, "schema_ops": {}, "flags": [], "renamed_from": None}]
    return {"complete": complete, "tables": tables, "tables_compared": len(tables), "delta_lines": 4,
            "verification": {"errors": errors, "info": 0, "error_details": [{"table": "x", "id": 1, "issue": "i"}] * errors}}


class PendingBuilds(unittest.TestCase):
    CHAIN = {10: 9, 9: 8, 8: 7, 7: 6}   # 10 <- 9 <- 8 <- 7 <- 6

    def pending(self, latest, done, first):
        with mock.patch.object(pl, "changelog_meta", fake_meta(self.CHAIN)):
            return pl.pending_builds(latest, done, first, "unused")

    def test_all_pending_in_ascending_order(self):
        self.assertEqual(self.pending(10, set(), 7), [(7, 6), (8, 7), (9, 8), (10, 9)])

    def test_stops_at_an_already_processed_build(self):
        self.assertEqual(self.pending(10, {8, 7}, 7), [(9, 8), (10, 9)])

    def test_nothing_pending_when_latest_is_done(self):
        self.assertEqual(self.pending(10, {10}, 7), [])

    def test_stops_at_the_first_build(self):
        self.assertEqual(self.pending(10, set(), 9), [(9, 8), (10, 9)])

    def test_broken_chain_is_an_error_not_a_skip(self):
        with self.assertRaises(s.DeltaError):
            self.pending(10, set(), 5)   # 6 no tiene changelog: no se puede saltar y seguir


class Manifest(unittest.TestCase):
    def build(self, res, verify_all=False):
        with tempfile.TemporaryDirectory() as d:
            touch(os.path.join(d, "delta.jsonl.gz"), b"x" * 10)
            m = pl.write_manifest(d, 42, 41, "2025-01-01T00:00:00Z", res, verify_all)
            with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
                on_disk = f.read()
        return m, on_disk

    def test_ok_manifest_counts_only_tables_with_changes(self):
        m, _ = self.build(result())
        self.assertEqual(m["verification"]["status"], "ok")
        self.assertEqual(list(m["tables"]), ["types"])
        self.assertEqual(m["totals"], {"tablesCompared": 2, "tablesChanged": 1, "added": 1, "removed": 0, "changed": 2,
                                       "schemaOps": 1, "deltaLines": 4})
        self.assertEqual(m["files"]["delta.jsonl.gz"]["bytes"], 10)
        self.assertEqual(m["formatVersion"], 1)
        self.assertIn("CCP hf.", m["notice"])

    def test_mismatches_publish_with_warn(self):
        self.assertEqual(self.build(result(errors=2))[0]["verification"]["status"], "warn")

    def test_incomplete_changelog_is_warn(self):
        self.assertEqual(self.build(result(complete=False))[0]["verification"]["status"], "warn")

    def test_verify_all_mode_is_recorded(self):
        self.assertEqual(self.build(result(), verify_all=True)[0]["verification"]["mode"], "verify-all")

    def test_manifest_is_deterministic(self):
        self.assertEqual(self.build(result())[1], self.build(result())[1])   # sin fechas de ejecucion


class IndexAndChangelog(unittest.TestCase):
    def test_processed_builds_ignores_tmp_and_incomplete_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            for name, has_manifest in (("100", True), ("101", False), ("102.tmp", True), ("notes", True)):
                os.makedirs(os.path.join(d, name))
                if has_manifest:
                    touch(os.path.join(d, name, "manifest.json"))
            self.assertEqual(pl.processed_builds(d), {100})

    def test_index_and_changelog_are_generated_from_manifests(self):
        with tempfile.TemporaryDirectory() as d:
            manifests = []
            for b, last, errors in ((42, 41, 0), (43, 42, 1)):
                bd = os.path.join(d, str(b))
                os.makedirs(bd)
                touch(os.path.join(bd, "delta.jsonl.gz"), b"x")
                manifests.append(pl.write_manifest(bd, b, last, "2025-01-0%dT00:00:00Z" % (b - 41), result(errors), False))
            self.assertEqual(pl.processed_builds(d), {42, 43})
            loaded = pl.load_manifests(d)
            pl.write_index(d, loaded)
            with open(os.path.join(d, "index.json"), encoding="utf-8") as f:
                index = json.load(f)
            self.assertEqual((index["firstBuild"], index["latestBuild"]), (42, 43))
            self.assertEqual([(e["build"], e["lastBuild"], e["verification"]) for e in index["builds"]],
                             [(42, 41, "ok"), (43, 42, "warn")])
            path = os.path.join(d, "CHANGELOG.md")
            pl.write_changelog(path, loaded)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertLess(text.index("Build 43"), text.index("Build 42"))   # el mas reciente primero
            self.assertIn("**Verification: warn**", text)
            self.assertIn("`types`: +1 -0 ~2 [schema: add_path x1]", text)


if __name__ == "__main__":
    unittest.main()
