import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "history.py"
FIXED_NOW_ENV = "OBSIDIAN_WORK_HISTORY_FIXED_NOW"


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.text = self.base / "message.md"
        self.text.write_bytes('요구사항: "한글, 쉼표"\r\n두 번째 줄\r\n'.encode("utf-8"))

    def cli(self, *args, ok=True, env=None, cwd=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                capture_output=True, encoding="utf-8", env=env, cwd=cwd)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def record(self, *args, ok=True):
        return self.cli("record", "--vault", self.vault, "--project", "테스트 프로젝트",
                        "--title", '한글, "제목"', "--kind", "artifact", "--status", "completed",
                        "--text-file", self.text, *args, ok=ok)

    def index(self, *args):
        result = self.cli("index", "--vault", self.vault, *args)
        with open(result["records_csv"], encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        with open(result["links_csv"], encoding="utf-8-sig", newline="") as stream:
            links = list(csv.DictReader(stream))
        return rows, links

    # --- New privacy/binary tests (Stage A: expected RED before Stage B) ---

    def test_known_secret_text_is_redacted_everywhere(self):
        bearer = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0."
                  "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        github = "ghp_" + "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
        self.text.write_text(
            f"요청: Authorization: Bearer {bearer}\ntoken={github}\n", encoding="utf-8")
        result = self.record()
        event_dir = Path(result["event_path"])
        for file in event_dir.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                for secret in (bearer, github):
                    self.assertNotIn(secret.encode(), data, file)
        stored = (event_dir / "original.md").read_bytes()
        self.assertNotEqual(stored, self.text.read_bytes())
        self.assertIn(b"REDACTED", stored)
        # The published sha256 describes the stored (redacted) bytes.
        with open(event_dir / "records.csv", encoding="utf-8-sig", newline="") as stream:
            row = next(r for r in csv.DictReader(stream) if r["role"] == "event")
        self.assertEqual(row["sha256"], hashlib.sha256(stored).hexdigest())

    def test_secret_bearing_document_artifact_is_not_copied(self):
        secret = "sk-" + "x" * 48
        doc = self.base / "notes.md"
        doc.write_text(f"API key: {secret}\n", encoding="utf-8")
        result = self.record("--document", doc)
        event_dir = Path(result["event_path"])
        for file in event_dir.rglob("*"):
            if file.is_file():
                self.assertNotIn(secret.encode(), file.read_bytes(), file)

    def test_unscanned_binary_is_excluded_not_copied(self):
        binary = self.base / "blob.bin"
        payload = b"\x00\x01MAGICBYTES\xff\xfe" * 4
        binary.write_bytes(payload)
        result = self.record("--document", binary)
        event_dir = Path(result["event_path"])
        digest = hashlib.sha256(payload).hexdigest()
        # No published file contains the binary bytes or its snapshot.
        for file in event_dir.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                self.assertNotIn(b"MAGICBYTES", data, file)
                self.assertNotEqual(hashlib.sha256(data).hexdigest(), digest, file)
        # The exclusion is surfaced truthfully: source digest recorded from the
        # source read, role/path kept, reason stated — never claimed as copied.
        event = json.loads((event_dir / "event.json").read_text(encoding="utf-8"))
        exclusions = event.get("excluded_artifacts", [])
        self.assertTrue(any(e["source_path"] == str(binary.resolve())
                            and e["sha256"] == digest and e["reason"]
                            for e in exclusions), exclusions)
        with open(event_dir / "records.csv", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertFalse(any(str(binary) in r["source_path"] and r["snapshot_path"]
                             for r in rows))

    # --- Ported upstream behavior ---

    def test_versions_preserve_original_bytes_and_relations(self):
        output = self.base / "result.bin"
        output.write_bytes(b"\x00first\xff")
        first = self.record("--output", output, "--version", "v0001")
        output.write_bytes(b"\x00second\xfe")
        second = self.record("--output", output, "--version", "v0002",
                             "--parent", first["event_id"], "--document", self.text)
        output.unlink()
        rows, links = self.index()
        outputs = {r["version"]: r for r in rows if r["role"] == "output"}
        for version, expected in (("v0001", b"\x00first\xff"), ("v0002", b"\x00second\xfe")):
            row = outputs[version]
            self.assertEqual((self.vault / row["snapshot_path"]).read_bytes(), expected)
            self.assertEqual(row["sha256"], hashlib.sha256(expected).hexdigest())
        for row in rows:
            self.assertEqual(row["title"], '한글, "제목"')
            if row["role"] == "event":
                self.assertEqual((self.vault / row["snapshot_path"]).read_bytes(),
                                 self.text.read_bytes())
        self.assertIn(dict(source_id=first["event_id"], target_id=second["event_id"],
                           relation="precedes"), links)
        self.assertEqual({r["record_id"] for r in rows},
                         {first["event_id"], second["event_id"]} |
                         {link["target_id"] for link in links if link["relation"] != "precedes"})

    def test_invalid_records_publish_nothing(self):
        output = self.base / "result.md"
        output.write_text("output", encoding="utf-8")
        cases = [("--output", output), ("--parent", "nonexistent"),
                 ("--output", self.base / "missing", "--version", "v0001"),
                 ("--document", self.base)]
        history_root = self.vault / "Work History"
        for args in cases:
            self.record(*args, ok=False)
            self.assertFalse(history_root.exists())
        self.text.write_bytes(b"\xff")
        self.record(ok=False)
        self.assertFalse(history_root.exists())

    def test_same_title_and_interrupted_stage_do_not_lose_records(self):
        first = self.record()
        second = self.record()
        pending = Path(first["event_path"]).parent / ".pending-crashed"
        pending.mkdir()
        (pending / "unfinished.md").write_text("unfinished", encoding="utf-8")
        rows, _ = self.index()
        self.assertEqual({r["event_id"] for r in rows},
                         {first["event_id"], second["event_id"]})
        self.assertTrue((pending / "unfinished.md").is_file())
        self.assertNotEqual(first["event_path"], second["event_path"])

    def test_explicit_vault_expands_tilde_and_beats_env(self):
        home = self.base / "different-user"
        (home / "custom-vault").mkdir(parents=True)
        environment = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                           OBSIDIAN_VAULT_PATH=str(self.base / "env-vault-missing"))
        result = self.cli("record", "--vault", "~/custom-vault", "--project", "proj",
                          "--title", "override", "--kind", "requirement", "--status",
                          "received", "--text-file", self.text, env=environment)
        self.assertTrue(Path(result["event_path"]).is_relative_to(
            (home / "custom-vault").resolve()))
        self.assertFalse((self.base / "env-vault-missing").exists())

    # --- New unified-behavior tests ---

    def test_missing_vault_writes_nothing(self):
        missing = self.base / "no-such-vault"
        self.cli("record", "--vault", missing, "--project", "p", "--title", "t",
                 "--kind", "requirement", "--status", "received",
                 "--text-file", self.text, ok=False)
        self.assertFalse(missing.exists())
        # With no explicit vault and no env/default vault, nothing is written and
        # no .history or Work History fallback is created in the working directory.
        work = self.base / "work"
        work.mkdir()
        env = dict(os.environ)
        env.pop("OBSIDIAN_VAULT_PATH", None)
        env["HOME"] = str(self.base / "no-home")
        env["USERPROFILE"] = str(self.base / "no-home")
        self.cli("record", "--title", "t", "--kind", "requirement", "--status",
                 "received", "--project", "p", "--text-file", self.text,
                 env=env, cwd=work, ok=False)
        self.assertFalse((work / ".history").exists())
        self.assertFalse((work / "Work History").exists())
        self.assertFalse((self.base / "no-home" / ".history").exists())

    def test_vault_resolution_order_and_no_home_fallback(self):
        # OBSIDIAN_VAULT_PATH is used when --vault is absent.
        env_vault = self.base / "env-vault"
        env_vault.mkdir()
        environment = dict(os.environ, OBSIDIAN_VAULT_PATH=str(env_vault),
                           HOME=str(self.base), USERPROFILE=str(self.base))
        result = self.cli("record", "--project", "p", "--title", "env",
                          "--kind", "requirement", "--status", "received",
                          "--text-file", self.text, env=environment)
        self.assertTrue(Path(result["event_path"]).is_relative_to(env_vault.resolve()))
        # Documented default is used only when it exists.
        default_vault = self.base / "Documents" / "Obsidian Vault"
        default_vault.mkdir(parents=True)
        environment.pop("OBSIDIAN_VAULT_PATH")
        result = self.cli("record", "--project", "p", "--title", "default",
                          "--kind", "requirement", "--status", "received",
                          "--text-file", self.text, env=environment)
        self.assertTrue(Path(result["event_path"]).is_relative_to(default_vault.resolve()))
        # Never falls back to ~/.history.
        self.assertFalse((self.base / ".history").exists())

    def test_event_bundle_layout_and_metadata(self):
        result = self.record("--timezone", "Asia/Seoul")
        event_path = Path(result["event_path"])
        # Work History/<slug>/<YYYY>/Events/<YYYY-MM-DD>/<event_id>/
        self.assertEqual(event_path.name, result["event_id"])
        rel = event_path.relative_to(self.vault)
        self.assertEqual(rel.parts[0], "Work History")
        self.assertEqual(rel.parts[2], result["local_date"][:4])
        self.assertEqual(rel.parts[3], "Events")
        self.assertEqual(rel.parts[4], result["local_date"])
        for name in ("record.md", "original.md", "records.csv", "links.csv", "event.json"):
            self.assertTrue((event_path / name).is_file(), name)
        event = json.loads((event_path / "event.json").read_text(encoding="utf-8"))
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["event_id"], result["event_id"])
        self.assertEqual(event["source_key"], result["event_id"])
        self.assertEqual(event["project"], "테스트 프로젝트")
        self.assertEqual(event["project_slug"], rel.parts[1])
        self.assertEqual(event["local_date"], result["local_date"])
        self.assertEqual(event["timezone"], "Asia/Seoul")
        self.assertEqual(event["recorded_at"], result["recorded_at"])
        self.assertEqual(event["parents"], [])
        self.assertEqual(event["original_sha256"],
                         hashlib.sha256(self.text.read_bytes()).hexdigest())
        # Slug carries a fixed-length sha-256 suffix of the canonical identity.
        suffix = rel.parts[1].rsplit("-", 1)[-1]
        self.assertEqual(len(suffix), 10)
        int(suffix, 16)
        with open(event_path / "records.csv", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["project"], "테스트 프로젝트")
        self.assertEqual(rows[0]["schema_version"], "1")
        self.assertEqual(rows[0]["source_key"], result["event_id"])
        self.assertEqual(rows[0]["local_date"], result["local_date"])
        self.assertEqual(rows[0]["timezone"], "Asia/Seoul")

    def test_project_slug_is_stable_and_collision_resistant(self):
        first = self.record("--project", "My Project!")
        second = self.record("--project", "My Project?")
        slug_a = Path(first["event_path"]).relative_to(self.vault).parts[1]
        slug_b = Path(second["event_path"]).relative_to(self.vault).parts[1]
        self.assertNotEqual(slug_a, slug_b)
        again = self.record("--project", "My Project!")
        self.assertEqual(Path(again["event_path"]).relative_to(self.vault).parts[1], slug_a)

    def test_utc_midnight_boundary_uses_vault_local_date(self):
        env = dict(os.environ, **{FIXED_NOW_ENV: "2026-10-14T01:30:00+00:00"})
        # 2026-10-14T01:30Z is still 2026-10-13 21:30 in New York (EDT): the UTC
        # instant is preserved while the vault-local date differs from UTC date.
        result = self.cli("record", "--vault", self.vault, "--project", "p",
                          "--title", "boundary", "--kind", "requirement",
                          "--status", "received", "--text-file", self.text,
                          "--timezone", "America/New_York", env=env)
        self.assertEqual(result["local_date"], "2026-10-13")
        self.assertTrue(result["recorded_at"].startswith("2026-10-14T01:30:00"))
        rel = Path(result["event_path"]).relative_to(self.vault)
        self.assertEqual(rel.parts[2], "2026")
        self.assertEqual(rel.parts[4], "2026-10-13")
        # Fixed offset alternative produces the same local date.
        result = self.cli("record", "--vault", self.vault, "--project", "p",
                          "--title", "boundary2", "--kind", "requirement",
                          "--status", "received", "--text-file", self.text,
                          "--utc-offset=-04:00", env=env)
        self.assertEqual(result["local_date"], "2026-10-13")
        self.assertEqual(result["timezone"], "-04:00")
        # Default (unconfigured) timezone is a clearly labeled UTC date.
        result = self.cli("record", "--vault", self.vault, "--project", "p",
                          "--title", "utc", "--kind", "requirement", "--status",
                          "received", "--text-file", self.text, env=env)
        self.assertEqual(result["timezone"], "UTC")
        self.assertEqual(result["local_date"], "2026-10-14")

    def test_atomic_publish_and_staged_leftovers_are_not_indexed(self):
        # A failed stage leaves only a .pending-* folder; it is neither published
        # as an event nor included in the index, and later records still publish.
        events_root = (self.vault / "Work History" / "p-x" / "2026" / "Events"
                       / "2026-10-13")
        events_root.mkdir(parents=True)
        stage = events_root / ".pending-deadbeef"
        stage.mkdir()
        (stage / "record.md").write_text("partial", encoding="utf-8")
        published = self.record()
        rows, _ = self.index()
        self.assertEqual({r["event_id"] for r in rows}, {published["event_id"]})
        self.assertTrue((stage / "record.md").is_file())
        # The published bundle is complete.
        for name in ("record.md", "original.md", "records.csv", "links.csv", "event.json"):
            self.assertTrue((Path(published["event_path"]) / name).is_file(), name)

    def test_parent_must_exist_and_links_map(self):
        self.record("--parent", "0" * 32, ok=False)
        parent = self.record()
        child = self.record("--parent", parent["event_id"])
        event = json.loads((Path(child["event_path"]) / "event.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(event["parents"], [parent["event_id"]])
        rows, links = self.index()
        self.assertIn(dict(source_id=parent["event_id"], target_id=child["event_id"],
                           relation="precedes"), links)

    def test_event_id_uniqueness_and_source_key(self):
        first = self.record()
        second = self.record()
        self.assertNotEqual(first["event_id"], second["event_id"])
        for result in (first, second):
            event = json.loads((Path(result["event_path"]) / "event.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(event["source_key"], result["event_id"])

    def test_resume_is_linked_event_not_new_status(self):
        parent = self.cli("record", "--vault", self.vault, "--project", "p",
                          "--title", "op", "--kind", "execution", "--status",
                          "interrupted", "--text-file", self.text)
        resumed = self.cli("record", "--vault", self.vault, "--project", "p",
                           "--title", "op", "--kind", "resume", "--status",
                           "started", "--text-file", self.text,
                           "--parent", parent["event_id"])
        _, links = self.index()
        self.assertIn(dict(source_id=parent["event_id"], target_id=resumed["event_id"],
                           relation="precedes"), links)


if __name__ == "__main__":
    unittest.main()
