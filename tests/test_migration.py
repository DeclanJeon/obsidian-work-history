import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "migrate.py"
HISTORY_SCRIPT = REPO / "scripts" / "history.py"
INDEX_SCRIPT = REPO / "scripts" / "obsidian_index.py"

sys.path.insert(0, str(REPO / "scripts"))
import history  # noqa: E402  (Task 1 module; schema constants + screening)


def load_migrate_module():
    spec = importlib.util.spec_from_file_location("migrate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UPSTREAM_FIELDS = ["record_id", "event_id", "recorded_at", "title", "kind",
                   "status", "tool", "model", "version", "role",
                   "source_path", "snapshot_path", "sha256"]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        # Legacy global event store (upstream history.py --root shape).
        self.source = self.base / "legacy" / ".history"
        self.source.mkdir(parents=True)
        (self.source / "events").mkdir()
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.map_file = self.base / "projects.json"

    # --- CLI helpers ---

    def cli(self, *args, ok=None):
        """ok=True → rc 0; ok=False → rc 2; ok=1 → rc 1 (soft findings)."""
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                capture_output=True, encoding="utf-8")
        if ok is True:
            expect = 0
        elif ok is False:
            expect = 2
        else:
            expect = ok if isinstance(ok, int) else 0
        self.assertEqual(result.returncode, expect,
                         f"stdout={result.stdout}\nstderr={result.stderr}")
        stream = result.stdout if expect == 0 else result.stderr
        if expect == 1:
            stream = result.stdout or result.stderr
        return json.loads(stream)

    def inspect(self, path=None, ok=True):
        return self.cli("inspect", path or self.source, ok=ok)

    def dry_run(self, ok=True, extra=()):
        return self.cli("dry-run", self.source, "--vault", self.vault,
                        "--map", self.map_file, *extra, ok=ok)

    def migrate_import(self, ok=True, extra=()):
        return self.cli("import", self.source, "--vault", self.vault,
                        "--map", self.map_file, "--approve", *extra, ok=ok)

    def write_map(self, mapping):
        self.map_file.write_text(json.dumps(mapping, ensure_ascii=False),
                                 encoding="utf-8")
        return self.map_file

    # --- legacy fixture builders (synthetic only) ---

    def add_event(self, stem, event_id, recorded_at, *,
                  title="레거시 기록", kind="execution", status="completed",
                  tool="omp", model="", version="", role_rows=(),
                  original=b"legacy original text\n",
                  annotated=None, links=(), artifacts=(),
                  extra_fields=None, parents=()):
        """Write a complete upstream-format event dir under events/.

        ``artifacts`` are ``(role, name, bytes, source_path)`` tuples; the
        record_id is recovered from the upstream artifact filename suffix.
        ``extra_fields`` maps column -> per-row values; a scalar applies to
        the first (event) row.
        """
        folder = self.source / "events" / stem
        folder.mkdir(parents=True)
        if annotated is None:
            annotated = (f"# 작업 기록\n\n- event_id: {event_id}\n"
                         f"- parents: {json.dumps(parents)}\n\n"
                         "## 기록 내용\n\n" +
                         original.decode("utf-8-sig")).encode("utf-8")
        raw_name = stem + "_원문.md"
        (folder / raw_name).write_bytes(original)
        (folder / (stem + ".md")).write_bytes(annotated)
        common = dict(recorded_at=recorded_at, title=title, kind=kind,
                      status=status, tool=tool, model=model, version=version)
        rows = [dict(common, record_id=event_id, event_id=event_id,
                     role="event", source_path=str(self.base / "in.md"),
                     snapshot_path=f"events/{stem}/{raw_name}",
                     sha256=sha256(original))]
        artifact_dir = folder / "artifacts"
        for role, name, data, source_path in artifacts:
            artifact_dir.mkdir(exist_ok=True)
            (artifact_dir / name).write_bytes(data)
            record_id = name.rsplit("_", 1)[-1].split(".")[0] or name
            rows.append(dict(common, record_id=record_id,
                             event_id=event_id, role=role,
                             source_path=source_path,
                             snapshot_path=f"events/{stem}/artifacts/{name}",
                             sha256=sha256(data)))
        for row in role_rows:
            rows.append(dict(common, **row))
        extra = dict(extra_fields or {})
        for column, values in extra.items():
            if not isinstance(values, (list, tuple)):
                values = [values]
            for index, value in enumerate(values):
                if index < len(rows):
                    rows[index][column] = value
        fields = list(UPSTREAM_FIELDS) + list(extra.keys())
        self.write_csv(folder / (stem + "_records.csv"), fields, rows)
        self.write_csv(folder / (stem + "_links.csv"),
                       history.LINK_FIELDS, list(links))
        return folder

    @staticmethod
    def write_csv(path, fields, rows):
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def read_csv(path):
        with open(path, encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def tree_digest(root):
        """(relpath, sha256-or-'dir') for every entry — source immutability."""
        digest = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            digest.append((rel, "dir" if path.is_dir()
                           else sha256(path.read_bytes())))
        return digest

    def bundle_dir(self, event_id):
        """Find the published vault bundle for an event_id."""
        root = self.vault / "Work History"
        matches = list(root.glob(f"*/*/Events/*/{event_id}"))
        self.assertEqual(len(matches), 1,
                         f"expected one bundle for {event_id}: {matches}")
        return matches[0]

    # --- inspect classification ---

    def test_inspect_classifies_global_event_store(self):
        before = self.tree_digest(self.source)
        self.add_event("120000000001_2026-10-01_alpha_aaaa",
                       "aaaa", "2026-10-01T12:00:00.000001+00:00")
        result = self.inspect()
        self.assertEqual(result["classification"], "global_event_store")
        self.assertEqual(result["events"], 1)
        self.assertEqual(self.tree_digest(self.source), before)

    def test_inspect_classifies_project_local_continuity(self):
        local = self.base / "proj" / ".history"
        (local / "events").mkdir(parents=True)
        (local / "prompts").mkdir()
        (local / "STATUS_SNAPSHOT.md").write_text(
            '{"project_root": "x", "operations": {}}', encoding="utf-8")
        (local / "events" / "000001-20261012T033117-op-started.md"
            ).write_text("---\nevent: started\n---\n", encoding="utf-8")
        result = self.inspect(local)
        self.assertEqual(result["classification"],
                         "project_local_continuity")
        # Read-only continuity evidence is never a bundle-import source.
        for command in ("dry-run", "import"):
            args = [command, local, "--vault", self.vault]
            if command == "import":
                args += ["--map", self.map_file, "--approve"]
            else:
                args += ["--map", self.map_file]
            outcome = self.cli(*args, ok=False)
            self.assertIn("error", outcome)
        self.assertFalse((self.vault / "Work History").exists())

    def test_inspect_reports_pending_and_non_event_entries(self):
        self.add_event("120000000002_2026-10-01_beta_bbbb",
                       "bbbb", "2026-10-01T12:00:00.000002+00:00")
        (self.source / "events" / ".pending-half").mkdir()
        (self.source / ".pending-root").mkdir()
        (self.source / "events" / "stray.txt").write_text("x")
        result = self.inspect()
        self.assertEqual(result["events"], 1)
        self.assertIn("events/.pending-half", result["pending"])
        self.assertIn(".pending-root", result["pending"])
        self.assertTrue(result["non_event_entries"])

    def test_inspect_missing_source_is_hard_error(self):
        self.cli("inspect", self.base / "absent", ok=False)

    def test_inspect_unrecognized_layout(self):
        other = self.base / "random"
        other.mkdir()
        (other / "file.txt").write_text("hi")
        result = self.inspect(other)
        self.assertEqual(result["classification"], "unrecognized")

    # --- dry-run ---

    def test_dry_run_requires_map(self):
        self.add_event("120000000003_2026-10-01_gamma_cccc",
                       "cccc", "2026-10-01T12:00:00.000003+00:00")
        self.cli("dry-run", self.source, "--vault", self.vault, ok=False)
        self.assertFalse((self.vault / "Work History").exists())

    def test_dry_run_reports_mapping_and_collisions_without_writes(self):
        e1 = self.add_event("120000000004_2026-10-01_delta_dddd",
                            "dddd", "2026-10-01T12:00:00.000004+00:00")
        self.add_event("120000000005_2026-10-01_eps_eeee",
                       "eeee", "2026-10-01T12:00:00.000005+00:00")
        self.write_map({"events/120000000004_2026-10-01_delta_dddd": "ProjectA",
                        "events/120000000005_2026-10-01_eps_eeee": "ProjectB"})
        before = self.tree_digest(self.source)
        result = self.dry_run()
        self.assertFalse((self.vault / "Work History").exists())
        self.assertEqual(self.tree_digest(self.source), before)
        rel1 = "events/120000000004_2026-10-01_delta_dddd"
        rel2 = "events/120000000005_2026-10-01_eps_eeee"
        mapping = {e["event_dir"]: e for e in result["events"]}
        self.assertEqual(mapping[rel1]["project"], "ProjectA")
        self.assertEqual(mapping[rel2]["project"], "ProjectB")
        # Same map can target different projects from one source root.
        self.assertNotEqual(mapping[rel1]["project_slug"],
                            mapping[rel2]["project_slug"])

    def test_dry_run_reports_unmapped_and_orphan_map_entries(self):
        self.add_event("120000000006_2026-10-01_zeta_ffff",
                       "ffff", "2026-10-01T12:00:00.000006+00:00")
        self.write_map({"events/other_dir": "ProjectA"})
        result = self.dry_run(ok=1)
        rel = "events/120000000006_2026-10-01_zeta_ffff"
        self.assertIn(rel, result["unmapped"])
        self.assertIn("events/other_dir", result["orphan_map_entries"])
        self.assertFalse((self.vault / "Work History").exists())

    def test_dry_run_flags_secret_and_binary_without_values(self):
        secret = "ghp_" + "a" * 40
        self.add_event("120000000007_2026-10-01_eta_gggg",
                       "gggg", "2026-10-01T12:00:00.000007+00:00",
                       original=f"token is {secret}\n".encode("utf-8"),
                       artifacts=[("input",
                                   "evt_input_blob_abc123.bin",
                                   b"\x00\x01\x02binary", "c:/tmp/blob.bin")])
        self.write_map(
            {"events/120000000007_2026-10-01_eta_gggg": "P"})
        result = self.dry_run(ok=1)
        event = result["events"][0]
        self.assertTrue(event["secret_flag"])
        self.assertTrue(event["excluded_artifacts"])
        # Never expose the secret value anywhere in the report.
        self.assertNotIn(secret, json.dumps(result))

    def test_dry_run_reports_corrupt_and_unknown_fields(self):
        stem = "120000000008_2026-10-01_th_hhhh"
        folder = self.source / "events" / stem
        folder.mkdir(parents=True)
        (folder / (stem + "_원문.md")).write_text("x", encoding="utf-8")
        # missing records/links → corrupt
        self.write_map({f"events/{stem}": "P"})
        result = self.dry_run(ok=1)
        self.assertIn(f"events/{stem}", result["corrupt"])
        # Unknown CSV fields are preserved-or-fail, reported by dry-run.
        stem2 = "120000000009_2026-10-01_io_iiii"
        self.add_event(stem2, "iiii", "2026-10-01T12:00:00.000009+00:00",
                       extra_fields={"custom_col": ["custom_val"]},
                       )
        self.write_map({f"events/{stem}": "P", f"events/{stem2}": "P"})
        result = self.dry_run(ok=1)
        event = {e["event_dir"]: e for e in result["events"]}[
            f"events/{stem2}"]
        self.assertIn("custom_col", event["unknown_fields"])

    def test_dry_run_rejects_traversal_in_map(self):
        self.write_map({"../outside": "P"})
        self.cli("dry-run", self.source, "--vault", self.vault,
                 "--map", self.map_file, ok=False)

    # --- import ---

    def test_import_requires_approve(self):
        self.add_event("120000000010_2026-10-01_ka_jjjj",
                       "jjjj", "2026-10-01T12:00:00.000010+00:00")
        self.write_map({"events/120000000010_2026-10-01_ka_jjjj": "P"})
        self.cli("import", self.source, "--vault", self.vault,
                 "--map", self.map_file, ok=False)
        self.assertFalse((self.vault / "Work History").exists())

    def test_clean_import_preserves_ids_links_and_layout(self):
        stem = "120000000011_2026-10-01_la_kkkk"
        self.add_event(
            stem, "kkkk", "2026-10-01T23:30:00.000011+00:00",
            artifacts=[("input", f"{stem}_input_src_aid1.md", b"in bytes\n",
                        "c:/tmp/src.md"),
                       ("output", f"{stem}_output_res_aod2.txt",
                        b"out bytes\n", "c:/tmp/res.txt")],
            links=[dict(source_id="kkkk", target_id="aid1",
                        relation="used"),
                   dict(source_id="kkkk", target_id="aod2",
                        relation="produced")])
        before = self.tree_digest(self.source)
        self.write_map({f"events/{stem}": "내 프로젝트"})
        result = self.migrate_import(extra=("--utc-offset=+09:00",))
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.tree_digest(self.source), before)
        bundle = self.bundle_dir("kkkk")
        expected = (self.vault / "Work History" /
                    history.project_slug("내 프로젝트") /
                    "2026" / "Events" / "2026-10-02" / "kkkk")
        self.assertEqual(bundle, expected)
        for name in ("record.md", "original.md", "records.csv",
                     "links.csv", "event.json"):
            self.assertTrue((bundle / name).is_file(), name)
        event = json.loads((bundle / "event.json").read_text("utf-8"))
        self.assertEqual(event["event_id"], "kkkk")
        self.assertEqual(event["project"], "내 프로젝트")
        self.assertEqual(event["local_date"], "2026-10-02")
        self.assertEqual(event["timezone"], "+09:00")
        self.assertEqual(event["recorded_at"],
                         "2026-10-01T23:30:00.000011+00:00")
        rows = self.read_csv(bundle / "records.csv")
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["record_id"] for r in rows},
                         {"kkkk", "aid1", "aod2"})
        links = self.read_csv(bundle / "links.csv")
        self.assertIn(dict(source_id="kkkk", target_id="aid1",
                           relation="used"), links)
        self.assertIn(dict(source_id="kkkk", target_id="aod2",
                           relation="produced"), links)
        # Clean artifact bytes and stored-byte hashes are preserved.
        artifacts = bundle / "artifacts"
        stored = {p.name: p.read_bytes() for p in artifacts.iterdir()}
        self.assertIn(b"in bytes\n", stored.values())
        self.assertIn(b"out bytes\n", stored.values())
        row = next(r for r in rows if r["record_id"] == "aid1")
        self.assertEqual(row["sha256"], sha256(b"in bytes\n"))
        self.assertEqual(row["source_key"], event["source_key"])
        self.assertEqual(row["schema_version"], "1")
        self.assertEqual(row["local_date"], "2026-10-02")
        # The indexer accepts the migrated bundle (Task 2 contract).
        view = subprocess.run(
            [sys.executable, str(INDEX_SCRIPT), "index",
             "--vault", str(self.vault)],
            capture_output=True, encoding="utf-8")
        self.assertEqual(view.returncode, 0, view.stderr)

    def test_import_is_idempotent_and_reports_conflicts(self):
        stem = "120000000012_2026-10-01_mu_llll"
        self.add_event(stem, "llll", "2026-10-01T12:00:00.000012+00:00")
        self.write_map({f"events/{stem}": "P"})
        first = self.migrate_import()
        self.assertEqual(first["imported"], 1)
        second = self.migrate_import()
        self.assertEqual(second["already_imported"], 1)
        self.assertEqual(second["imported"], 0)
        # Conflicting event_id from a different source dir never remaps.
        stem2 = "120000000013_2026-10-01_nu_llll"
        self.add_event(stem2, "llll",
                       "2026-10-01T12:00:00.000013+00:00")
        self.write_map({f"events/{stem}": "P", f"events/{stem2}": "P"})
        third = self.migrate_import(ok=1)
        self.assertEqual(third["already_imported"], 1)
        conflict = [e for e in third["events"] if e["event_dir"]
                    .endswith(stem2)]
        self.assertEqual(conflict[0]["status"], "conflict")
        self.assertEqual(conflict[0]["event_id"], "llll")

    def test_import_secret_text_redacted_and_digest_preserved(self):
        secret = "ghp_" + "b" * 40
        original = f"call with {secret}\n".encode("utf-8")
        stem = "120000000014_2026-10-01_xi_mmmm"
        self.add_event(stem, "mmmm", "2026-10-01T12:00:00.000014+00:00",
                       original=original)
        self.write_map({f"events/{stem}": "P"})
        self.migrate_import()
        bundle = self.bundle_dir("mmmm")
        stored = (bundle / "original.md").read_bytes()
        self.assertNotIn(secret.encode(), stored)
        self.assertIn(b"REDACTED", stored)
        rows = self.read_csv(bundle / "records.csv")
        row = next(r for r in rows if r["role"] == "event")
        # records.csv sha256 covers stored (sanitized) bytes.
        self.assertEqual(row["sha256"], sha256(stored))
        event = json.loads((bundle / "event.json").read_text("utf-8"))
        # Original source digest stays as provenance.
        self.assertEqual(event["source_sha256"], sha256(original))
        self.assertGreaterEqual(event["redactions"], 1)

    def test_import_ambiguous_secret_blocks_event(self):
        stem = "120000000015_2026-10-01_om_nnnn"
        self.add_event(stem, "nnnn", "2026-10-01T12:00:00.000015+00:00",
                       original=b"-----BEGIN RSA PRIVATE KEY-----\nMII\n")
        self.write_map({f"events/{stem}": "P"})
        result = self.migrate_import(ok=1)
        self.assertEqual(result["imported"], 0)
        event = result["events"][0]
        self.assertEqual(event["status"], "failed")
        self.assertFalse((self.vault / "Work History").exists())

    def test_import_binary_artifact_excluded_with_link(self):
        data = b"\x00\x89PNG\r\n\x1a\nbinarybytes"
        name = "evt_output_pic_deadbeef01.png"
        stem = "120000000016_2026-10-01_pi_oooo"
        self.add_event(stem, "oooo", "2026-10-01T12:00:00.000016+00:00",
                       artifacts=[("output", name, data, "c:/tmp/pic.png")],
                       links=[dict(source_id="oooo",
                                   target_id="deadbeef01",
                                   relation="produced")],
                       version="v1")
        self.write_map({f"events/{stem}": "P"})
        result = self.migrate_import()
        self.assertEqual(result["imported"], 1)
        bundle = self.bundle_dir("oooo")
        self.assertFalse((bundle / "artifacts" / name).exists())
        rows = self.read_csv(bundle / "records.csv")
        row = next(r for r in rows if r["record_id"] == "deadbeef01")
        self.assertEqual(row["snapshot_path"], "")
        self.assertEqual(row["sha256"], "")
        event = json.loads((bundle / "event.json").read_text("utf-8"))
        [exclusion] = event["excluded_artifacts"]
        self.assertEqual(exclusion["record_id"], "deadbeef01")
        self.assertEqual(exclusion["role"], "output")
        self.assertEqual(exclusion["source_path"], "c:/tmp/pic.png")
        self.assertEqual(exclusion["sha256"], sha256(data))
        self.assertTrue(exclusion["reason"])
        links = self.read_csv(bundle / "links.csv")
        self.assertIn(dict(source_id="oooo", target_id="deadbeef01",
                           relation="produced"), links)
        # Task 2 indexer renders exclusion without failing.
        view = subprocess.run(
            [sys.executable, str(INDEX_SCRIPT), "index",
             "--vault", str(self.vault)],
            capture_output=True, encoding="utf-8")
        self.assertEqual(view.returncode, 0, view.stderr)

    def test_import_preserves_unknown_fields_in_migration_metadata(self):
        stem = "120000000017_2026-10-01_ro_pppp"
        self.add_event(stem, "pppp", "2026-10-01T12:00:00.000017+00:00",
                       extra_fields={"custom_col": ["kept"]})
        self.write_map({f"events/{stem}": "P"})
        self.migrate_import()
        bundle = self.bundle_dir("pppp")
        event = json.loads((bundle / "event.json").read_text("utf-8"))
        self.assertIn("migration", event)
        unknown = event["migration"]["unknown_fields"]
        self.assertIn("custom_col", unknown["columns"])
        self.assertEqual(unknown["values"]["custom_col"], ["kept"])

    def test_import_rejects_corrupt_event(self):
        stem = "120000000018_2026-10-01_si_qqqq"
        self.add_event(stem, "qqqq", "2026-10-01T12:00:00.000018+00:00")
        # Corrupt: break a required link target and a record hash.
        records = self.source / "events" / stem / f"{stem}_records.csv"
        rows = self.read_csv(records)
        rows[0]["sha256"] = "0" * 64
        self.write_csv(records, UPSTREAM_FIELDS, rows)
        self.write_map({f"events/{stem}": "P"})
        result = self.migrate_import(ok=1)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["events"][0]["status"], "failed")
        self.assertFalse((self.vault / "Work History").exists())

    def test_import_rejects_missing_link_target(self):
        stem = "120000000019_2026-10-01_ti_rrrr"
        self.add_event(stem, "rrrr", "2026-10-01T12:00:00.000019+00:00",
                       links=[dict(source_id="rrrr",
                                   target_id="ghost",
                                   relation="used")])
        self.write_map({f"events/{stem}": "P"})
        result = self.migrate_import(ok=1)
        self.assertEqual(result["events"][0]["status"], "failed")

    def test_import_pending_never_imported(self):
        (self.source / "events" / ".pending-half" / "x").mkdir(parents=True)
        self.add_event("120000000020_2026-10-01_ui_ssss",
                       "ssss", "2026-10-01T12:00:00.000020+00:00")
        self.write_map({"events/120000000020_2026-10-01_ui_ssss": "P"})
        result = self.migrate_import()
        self.assertEqual(result["imported"], 1)
        self.assertIn("events/.pending-half", result["pending"])

    def test_import_never_writes_source(self):
        stem = "120000000021_2026-10-01_vi_tttt"
        self.add_event(stem, "tttt", "2026-10-01T12:00:00.000021+00:00")
        self.write_map({f"events/{stem}": "P"})
        before = self.tree_digest(self.source)
        self.migrate_import()
        self.assertEqual(self.tree_digest(self.source), before)

    def test_import_rejects_traversal_snapshot_path(self):
        stem = "120000000022_2026-10-01_wi_uuuu"
        self.add_event(stem, "uuuu", "2026-10-01T12:00:00.000022+00:00")
        records = self.source / "events" / stem / f"{stem}_records.csv"
        rows = self.read_csv(records)
        rows[0]["snapshot_path"] = "../outside/escape.md"
        self.write_csv(records, UPSTREAM_FIELDS, rows)
        self.write_map({f"events/{stem}": "P"})
        result = self.migrate_import(ok=1)
        self.assertEqual(result["events"][0]["status"], "failed")

    def test_import_mixed_results_reported_truthfully(self):
        good = "120000000023_2026-10-01_xi_vvvv"
        bad = "120000000024_2026-10-01_yi_wwww"
        self.add_event(good, "vvvv", "2026-10-01T12:00:00.000023+00:00")
        self.add_event(bad, "wwww", "2026-10-01T12:00:00.000024+00:00",
                       original=b"-----BEGIN RSA PRIVATE KEY-----\nMII\n")
        self.write_map({f"events/{good}": "A", f"events/{bad}": "B"})
        result = self.migrate_import(ok=1)
        self.assertEqual(result["imported"], 1)
        statuses = {e["event_dir"]: e["status"] for e in result["events"]}
        self.assertEqual(statuses[f"events/{good}"], "imported")
        self.assertEqual(statuses[f"events/{bad}"], "failed")
        # Only the good event landed; failed one left nothing behind.
        self.bundle_dir("vvvv")
        root = self.vault / "Work History"
        self.assertFalse(list(root.glob(f"*/*/Events/*/wwww")))
        self.assertFalse(list(root.rglob(".pending-*")))


if __name__ == "__main__":
    unittest.main()
