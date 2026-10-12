import csv
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "obsidian_index.py"
HISTORY_SCRIPT = REPO / "scripts" / "history.py"
FIXED_NOW_ENV = "OBSIDIAN_WORK_HISTORY_FIXED_NOW"
MANAGED_START = "<!-- obsidian-work-history:managed:start -->"
MANAGED_END = "<!-- obsidian-work-history:managed:end -->"
WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")

sys.path.insert(0, str(REPO / "scripts"))
import history  # noqa: E402  (Task 1 module; provides schema constants)


def load_index_module():
    spec = importlib.util.spec_from_file_location("obsidian_index", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ObsidianIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.text = self.base / "message.md"
        self.text.write_bytes("recorded text\n".encode("utf-8"))

    # --- subprocess helpers ---

    def run_cli(self, script, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(script), *map(str, args)],
                                capture_output=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def record(self, *args, project="프로젝트A", title="기록 제목",
               kind="execution", status="completed", ok=True):
        return self.run_cli(HISTORY_SCRIPT, "record", "--vault", self.vault,
                            "--project", project, "--title", title,
                            "--kind", kind, "--status", status,
                            "--text-file", self.text, *args, ok=ok)

    def view(self, *args, ok=True, env=None):
        return self.run_cli(SCRIPT, "index", "--vault", self.vault, *args,
                            ok=ok, env=env)

    # --- vault helpers ---

    def root(self):
        return self.vault / "Work History"

    def slug_of(self, project):
        slug = history.find_project_slug(self.root(), project)
        self.assertIsNotNone(slug, project)
        return slug

    def day_note(self, slug, date):
        return self.root() / slug / date[:4] / f"{date}.md"

    def read_csv(self, path):
        with open(path, encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def write_bundle(self, slug, date, event_id, rows, links,
                     event_json=None, year=None):
        """Craft a published event bundle by hand (no record CLI)."""
        folder = (self.root() / slug / (year or date[:4]) / "Events"
                  / date / event_id)
        folder.mkdir(parents=True)
        history.write_csv(folder / "records.csv", history.RECORD_FIELDS, rows)
        history.write_csv(folder / "links.csv", history.LINK_FIELDS, links)
        (folder / "record.md").write_text("# record\n", encoding="utf-8")
        if event_json is not None:
            (folder / "event.json").write_text(
                json.dumps(event_json, ensure_ascii=False), encoding="utf-8")
        return folder

    def base_row(self, project, event_id, **overrides):
        row = dict.fromkeys(history.RECORD_FIELDS, "")
        row.update(record_id=event_id, event_id=event_id,
                   recorded_at="2026-10-13T12:00:00+00:00",
                   title="title", kind="execution", status="completed",
                   role="event", source_path=str(self.text),
                   snapshot_path="", sha256="",
                   schema_version="1", project=project, source_key=event_id,
                   local_date="2026-10-13", timezone="UTC")
        row.update(overrides)
        return row

    def destinations(self, text):
        """Parsed wikilink destinations, normalized: [[a/b]] and [[a/b.md]]
        resolve to the same note, so the .md suffix is stripped."""
        return [dest[:-3] if dest.endswith(".md") else dest
                for dest in WIKILINK.findall(text)]

    def assert_no_duplicate_destinations(self, path):
        text = Path(path).read_text(encoding="utf-8")
        destinations = self.destinations(text)
        self.assertEqual(len(destinations), len(set(destinations)),
                         f"duplicate wikilink destinations in {path}: "
                         f"{sorted(destinations)}")

    # --- Required scenario: one project/day note ---

    def test_day_note_layout_links_and_managed_markers(self):
        first = self.record(title="first", status="started")
        second = self.record(title="second", status="completed")
        self.view()
        slug = self.slug_of("프로젝트A")
        note_path = self.day_note(slug, first["local_date"])
        self.assertTrue(note_path.is_file())
        text = note_path.read_text(encoding="utf-8")
        self.assertEqual(text.count(MANAGED_START), 1, text)
        self.assertEqual(text.count(MANAGED_END), 1, text)
        self.assertLess(text.index(MANAGED_START), text.index(MANAGED_END))
        # Both events are linked to their immutable record.md bundles.
        for result in (first, second):
            event_dir = Path(result["event_path"])
            expected_dest = (event_dir / "record").relative_to(self.vault).as_posix()
            self.assertIn(f"[[{expected_dest}", text)
        for fragment in ("first", "second", "started", "completed"):
            self.assertIn(fragment, text)
        # The UTC instant is shown, not a recomputed local time.
        self.assertIn(first["recorded_at"][11:16], text)
        self.assert_no_duplicate_destinations(note_path)
        # Project + year navigation notes exist and link the day note.
        readme = self.root() / slug / "README.md"
        year_readme = (self.root() / slug / first["local_date"][:4]
                       / "README.md")
        self.assertTrue(readme.is_file())
        self.assertTrue(year_readme.is_file())
        day_dest = note_path.relative_to(self.vault).as_posix()[:-3]
        for nav in (readme, year_readme):
            nav_text = nav.read_text(encoding="utf-8")
            self.assertIn(f"[[{day_dest}", nav_text)
            self.assert_no_duplicate_destinations(nav)

    # --- Required scenario: unmanaged frontmatter/prose preserved ---

    def test_unmanaged_frontmatter_and_prose_are_preserved(self):
        first = self.record()
        slug = self.slug_of("프로젝트A")
        note_path = self.day_note(slug, first["local_date"])
        note_path.parent.mkdir(parents=True, exist_ok=True)
        prior = ("---\ntype: work-history\ndate: 2026-10-13\n"
                 "tags: [work-history, custom]\n---\n\n"
                 "## Summary\nHandwritten prose stays.\n\n"
                 "## Decisions\n- kept the alias\n")
        note_path.write_text(prior, encoding="utf-8")
        self.view()
        text = note_path.read_text(encoding="utf-8")
        self.assertIn(prior.rstrip("\n"), text)
        self.assertEqual(text.count(MANAGED_START), 1)
        self.assertIn("기록 제목", text)
        # A second event regenerates only the managed block; prose survives.
        second = self.record(title="second")
        self.view()
        text = note_path.read_text(encoding="utf-8")
        self.assertIn(prior.rstrip("\n"), text)
        self.assertIn("second", text)
        self.assertEqual(text.count(MANAGED_START), 1, text)
        self.assertEqual(text.count(MANAGED_END), 1, text)

    # --- Required scenario: malformed markers fail safely ---

    def test_malformed_markers_fail_closed_without_writes(self):
        first = self.record()
        slug = self.slug_of("프로젝트A")
        note_path = self.day_note(slug, first["local_date"])
        note_path.parent.mkdir(parents=True, exist_ok=True)
        for broken in (
            f"prose\n{MANAGED_START}\nunterminated block\n",
            f"prose\n{MANAGED_END}\ndangling end\n",
            f"{MANAGED_START}\na\n{MANAGED_END}\nb\n"
            f"{MANAGED_START}\nc\n{MANAGED_END}\n",
            f"{MANAGED_END}\nwrong order\n{MANAGED_START}\n",
        ):
            note_path.write_text(broken, encoding="utf-8")
            error = self.view(ok=False)
            self.assertIn("error", error)
            self.assertEqual(note_path.read_text(encoding="utf-8"), broken)
        # The event bundle itself is never touched.
        self.assertTrue((Path(first["event_path"]) / "records.csv").is_file())
        self.assertTrue((Path(first["event_path"]) / "event.json").is_file())

    # --- Required scenario: hash conflict aborts that update ---

    def test_changed_file_conflict_aborts_entire_publication(self):
        self.record()
        self.view()
        module = load_index_module()
        # Simulate a non-cooperating external writer: the note changes between
        # the index plan and its commit.
        events_csv = self.root() / "_index" / "events.csv"
        events_csv.unlink()
        plan = module.plan_updates(self.vault)
        day_update = next(u for u in plan
                          if Path(u.path).name.endswith(".md")
                          and u.path.parent.name.isdigit())
        # Pre-commit state of every planned target (absent → None).
        before = {}
        for update in plan:
            before[update.path] = (update.path.read_bytes()
                                   if update.path.is_file() else None)
        external = (Path(day_update.path).read_text(encoding="utf-8")
                    + "\nexternal edit\n")
        Path(day_update.path).write_text(external, encoding="utf-8")
        before[day_update.path] = Path(day_update.path).read_bytes()
        with self.assertRaises(module.IndexConflict):
            module.commit_updates(plan)
        # Any target conflict aborts the WHOLE publication: every target
        # keeps its pre-commit bytes — including events.csv, which stays
        # absent — and the event bundle is preserved untouched.
        for path, snapshot in before.items():
            current = path.read_bytes() if path.is_file() else None
            self.assertEqual(current, snapshot, f"index wrote over {path}")
        self.assertFalse(events_csv.exists())
        event_dir = next((self.root()).rglob("records.csv"))
        self.assertTrue(event_dir.is_file())

    # --- Required scenario: destination-based wikilink deduplication ---
    # The real defect: an existing [[<day-note>]] target in project-note
    # prose, then generated content repeating the SAME destination under
    # different surrounding prose. Identity is the destination, not the line.

    def test_existing_wikilink_destination_is_not_repeated(self):
        result = self.record(title="linked-day")
        slug = self.slug_of("프로젝트A")
        date = result["local_date"]
        note_path = self.day_note(slug, date)
        day_dest = note_path.relative_to(self.vault).as_posix()[:-3]
        readme = self.root() / slug / "README.md"
        readme.parent.mkdir(parents=True, exist_ok=True)
        prior = ("# Project history\n\n## Recent activity\n"
                 f"- earlier session: [[{day_dest}|{date}]] fixed the build\n")
        readme.write_text(prior, encoding="utf-8")
        self.view()
        text = readme.read_text(encoding="utf-8")
        # Old prose kept verbatim; the destination still occurs exactly once.
        self.assertIn("earlier session:", text)
        self.assertIn("fixed the build", text)
        self.assertEqual(self.destinations(text).count(day_dest), 1, text)
        # New managed prose (event listings) is present without repeating
        # the [[<day-note>]] destination.
        self.assertIn("linked-day", text)
        self.assert_no_duplicate_destinations(readme)

    def test_generated_content_dedupes_by_destination_not_line_text(self):
        # Two different records (different surrounding prose) share one
        # snapshot destination: the destination must be linked once.
        slug = "craft-" + "a" * 10
        date = "2026-10-13"
        event_id = "e" * 32
        bundle = f"Work History/{slug}/2026/Events/{date}/{event_id}"
        shared = bundle + "/artifacts/shared.md"
        rows = [self.base_row("craft", event_id,
                              snapshot_path=f"{bundle}/original.md",
                              sha256="0" * 64),
                self.base_row("craft", event_id, record_id="r1",
                              role="input", source_path="/tmp/a.md",
                              snapshot_path=shared, sha256="1" * 64,
                              title="input one"),
                self.base_row("craft", event_id, record_id="r2",
                              role="document", source_path="/tmp/b.md",
                              snapshot_path=shared, sha256="1" * 64,
                              title="document two")]
        links = [dict(source_id=event_id, target_id="r1", relation="used"),
                 dict(source_id=event_id, target_id="r2", relation="used")]
        self.write_bundle(slug, date, event_id, rows, links,
                          event_json={"event_id": event_id, "schema_version": 1,
                                    "project": "craft", "project_slug": slug,
                                    "recorded_at": "2026-10-13T12:00:00+00:00",
                                    "local_date": date, "timezone": "UTC",
                                    "title": "title", "kind": "execution",
                                    "status": "completed", "parents": [],
                                    "excluded_artifacts": [],
                                    "artifacts": []})
        self.view()
        note_path = self.day_note(slug, date)
        text = note_path.read_text(encoding="utf-8")
        self.assertEqual(self.destinations(text).count(shared[:-3]), 1, text)
        # Both records' distinct prose is preserved around the single link.
        self.assertIn("a.md", text)
        self.assertIn("b.md", text)
        self.assert_no_duplicate_destinations(note_path)

    # --- Required scenario: excluded artifact provenance ---

    def test_excluded_artifact_is_incomplete_provenance_not_missing_file(self):
        binary = self.base / "blob.bin"
        payload = b"\x00\x01EXCLUDEDBYTES\xff" * 4
        binary.write_bytes(payload)
        result = self.record("--document", binary)
        self.view()  # must NOT fail as a missing-file error
        slug = self.slug_of("프로젝트A")
        note_path = self.day_note(slug, result["local_date"])
        text = note_path.read_text(encoding="utf-8")
        event = json.loads((Path(result["event_path"]) / "event.json")
                           .read_text(encoding="utf-8"))
        exclusion = event["excluded_artifacts"][0]
        # Rendered explicitly as excluded/incomplete provenance with the
        # reason and the source digest — never a link to a nonexistent file.
        self.assertIn("excluded", text.lower())
        self.assertIn(exclusion["sha256"][:16], text)
        excluded_lines = [line for line in text.splitlines()
                          if "exclud" in line.lower()]
        self.assertTrue(excluded_lines, text)
        for line in excluded_lines:
            self.assertNotIn("[[", line)
        self.assertIn(Path(exclusion["source_path"]).name, text)
        # The events.csv row keeps blank snapshot_path/sha256.
        rows = self.read_csv(self.root() / "_index" / "events.csv")
        row = next(r for r in rows
                   if r["record_id"] == exclusion["record_id"])
        self.assertEqual(row["snapshot_path"], "")
        self.assertEqual(row["sha256"], "")
        self.assert_no_duplicate_destinations(note_path)

    # --- Required scenario: two concurrent event writers ---

    def test_concurrent_publish_and_index_retain_every_event(self):
        results, errors = {}, {}

        def writer(name):
            try:
                outcome = self.record(title=f"event-{name}")
                self.view()
                results[name] = outcome
            except Exception as error:  # noqa: BLE001 — reported via assert
                errors[name] = error

        threads = [threading.Thread(target=writer, args=(name,))
                   for name in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse(errors, errors)
        self.assertEqual(set(results), {"one", "two"})
        slug = self.slug_of("프로젝트A")
        date = results["one"]["local_date"]
        note_path = self.day_note(slug, date)
        text = note_path.read_text(encoding="utf-8")
        for name, outcome in results.items():
            self.assertIn(f"event-{name}", text,
                          "index lost a concurrently published event")
        rows = self.read_csv(self.root() / "_index" / "events.csv")
        self.assertEqual(
            {r["event_id"] for r in rows if r["role"] == "event"},
            {results["one"]["event_id"], results["two"]["event_id"]})
        self.assert_no_duplicate_destinations(note_path)

    # --- Required scenario: UTC/local date boundary ---

    def test_timezone_boundary_indexes_under_vault_local_date(self):
        env = dict(os.environ, **{FIXED_NOW_ENV: "2026-10-14T01:30:00+00:00"})
        result = self.run_cli(HISTORY_SCRIPT, "record", "--vault", self.vault,
                              "--project", "boundary", "--title", "boundary",
                              "--kind", "requirement", "--status", "received",
                              "--text-file", self.text,
                              "--timezone", "America/New_York", env=env)
        self.assertEqual(result["local_date"], "2026-10-13")
        self.assertTrue(result["recorded_at"].startswith("2026-10-14T01:30:00"))
        self.view()
        slug = self.slug_of("boundary")
        local_note = self.day_note(slug, "2026-10-13")
        utc_note = self.day_note(slug, "2026-10-14")
        self.assertTrue(local_note.is_file())
        self.assertFalse(utc_note.exists())
        text = local_note.read_text(encoding="utf-8")
        self.assertIn("boundary", text)
        # UTC instant preserved verbatim in CSV; no re-derived date.
        rows = self.read_csv(self.root() / "_index" / "events.csv")
        event_row = next(r for r in rows
                         if r["event_id"] == result["event_id"])
        self.assertEqual(event_row["recorded_at"], result["recorded_at"])
        self.assertEqual(event_row["local_date"], "2026-10-13")

    # --- Global index outputs ---

    def test_global_index_files_cover_all_projects(self):
        first = self.record(project="Alpha", title="alpha work")
        second = self.record(project="Beta", title="beta work")
        self.view()
        slug_a, slug_b = self.slug_of("Alpha"), self.slug_of("Beta")
        index_dir = self.root() / "_index"
        projects_md = (index_dir / "projects.md").read_text(encoding="utf-8")
        self.assertIn(f"[[Work History/{slug_a}/README", projects_md)
        self.assertIn(f"[[Work History/{slug_b}/README", projects_md)
        # Destinations resolve to the actual project README notes.
        for link in self.destinations(projects_md):
            self.assertTrue((self.vault / f"{link}.md").is_file(), link)
        self.assert_no_duplicate_destinations(index_dir / "projects.md")
        projects = {row["project"]: row for row in
                    self.read_csv(index_dir / "projects.csv")}
        self.assertEqual(projects["Alpha"]["slug"], slug_a)
        self.assertEqual(projects["Beta"]["slug"], slug_b)
        events = {row["event_id"] for row in
                  self.read_csv(index_dir / "events.csv")
                  if row["role"] == "event"}
        self.assertEqual(events, {first["event_id"], second["event_id"]})

    def test_project_scope_limits_regeneration(self):
        self.record(project="Alpha", title="alpha")
        beta = self.record(project="Beta", title="beta")
        self.view("--project", "Beta")
        slug_a, slug_b = self.slug_of("Alpha"), self.slug_of("Beta")
        self.assertTrue((self.root() / slug_b / "README.md").is_file())
        self.assertTrue(self.day_note(slug_b, beta["local_date"]).is_file())
        self.assertFalse((self.root() / slug_a / "README.md").exists())
        # Global files still cover every published project.
        projects = {row["project"] for row in
                    self.read_csv(self.root() / "_index" / "projects.csv")}
        self.assertEqual(projects, {"Alpha", "Beta"})

    # --- Required scenarios: path/symlink escape refused ---

    def test_snapshot_path_escape_is_refused(self):
        slug = "evil-" + "b" * 10
        date = "2026-10-13"
        event_id = "f" * 32
        bundle = f"Work History/{slug}/2026/Events/{date}/{event_id}"
        rows = [self.base_row("evil", event_id,
                              snapshot_path=f"{bundle}/original.md",
                              sha256="0" * 64),
                self.base_row("evil", event_id, record_id="r1", role="output",
                              source_path="/tmp/out.md",
                              snapshot_path="../../escape.md",
                              sha256="1" * 64)]
        links = [dict(source_id=event_id, target_id="r1", relation="produced")]
        self.write_bundle(slug, date, event_id, rows, links,
                          event_json={"event_id": event_id, "schema_version": 1,
                                    "project": "evil", "project_slug": slug,
                                    "recorded_at": "2026-10-13T12:00:00+00:00",
                                    "local_date": date, "timezone": "UTC",
                                    "title": "title", "kind": "execution",
                                    "status": "completed", "parents": [],
                                    "excluded_artifacts": [],
                                    "artifacts": []})
        self.view(ok=False)
        self.assertFalse((self.base / "escape.md").exists())
        self.assertFalse((self.vault.parent / "escape.md").exists())

    def test_symlinked_note_target_is_refused(self):
        result = self.record()
        slug = self.slug_of("프로젝트A")
        note_path = self.day_note(slug, result["local_date"])
        note_path.parent.mkdir(parents=True, exist_ok=True)
        outside = self.base / "outside.md"
        outside.write_text("outside content\n", encoding="utf-8")
        try:
            note_path.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable on this platform: {error}")
        self.view(ok=False)
        # Nothing was written through the symlink.
        self.assertEqual(outside.read_text(encoding="utf-8"),
                         "outside content\n")
        self.assertTrue(note_path.is_symlink())

    # --- Review: race between commit check and atomic replace ---

    def test_race_during_atomic_write_rechecks_before_replace(self):
        self.record()
        self.view()
        module = load_index_module()
        events_csv = self.root() / "_index" / "events.csv"
        events_csv.unlink()
        plan = module.plan_updates(self.vault)
        raced = next(u for u in plan if u.path == events_csv)
        original = module.atomic_write

        def racing_write(path, *args, **kwargs):
            if path == raced.path:
                # External edit lands after commit's first hash check but
                # inside staging — before os.replace.
                Path(path).write_text("raced\n", encoding="utf-8")
            return original(path, *args, **kwargs)

        module.atomic_write = racing_write
        self.addCleanup(setattr, module, "atomic_write", original)
        with self.assertRaises(module.IndexConflict):
            module.commit_updates(plan)
        # The external bytes win; the second compare before replace refused.
        self.assertEqual(events_csv.read_text(encoding="utf-8"), "raced\n")

    # --- Review: lock owner safety ---

    def test_live_lock_is_never_treated_as_stale(self):
        self.record()
        module = load_index_module()
        module.LOCK_TIMEOUT_SECONDS = 0.4
        lock_path = (self.root() / "_index" / module.LOCK_FILENAME)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # An ancient lock file that belongs to a LIVE process (us) must be
        # refused, never unlinked as "stale".
        lock_path.write_text(json.dumps({"pid": os.getpid()}),
                             encoding="utf-8")
        old = time.time() - 86400
        os.utime(lock_path, (old, old))
        with self.assertRaises(ValueError):
            with module.index_lock(self.vault):
                pass
        self.assertTrue(lock_path.is_file(), "live lock must not be unlinked")
        self.assertIn(str(os.getpid()),
                      lock_path.read_text(encoding="utf-8"))

    def test_index_lock_release_never_unlinks_another_owner(self):
        self.record()
        module = load_index_module()
        lock_path = (self.root() / "_index" / module.LOCK_FILENAME)
        with module.index_lock(self.vault):
            self.assertTrue(lock_path.is_file())
            # A second owner's lock replaces ours while we hold the context.
            lock_path.write_text(json.dumps({"pid": 424242, "owner": "other"}),
                                 encoding="utf-8")
        # Our exit must not delete the replacement lock.
        self.assertTrue(lock_path.is_file())
        self.assertIn("other", lock_path.read_text(encoding="utf-8"))
    def test_stale_dead_owner_lock_is_not_automatically_reclaimed(self):
        self.record()
        module = load_index_module()
        module.LOCK_TIMEOUT_SECONDS = 0
        lock_path = (self.root() / "_index" / module.LOCK_FILENAME)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # An ancient lock whose owner process is provably dead must STILL
        # not be unlinked automatically: byte-compare + unlink cannot be
        # made atomic, so a racy reaper could delete a newer owner's lock.
        # Orphan locks require manual cleanup instead.
        stale = json.dumps({"token": "dead-owner", "pid": 424242})
        lock_path.write_text(stale, encoding="utf-8")
        old = time.time() - 86400
        os.utime(lock_path, (old, old))
        with mock.patch.object(module, "_pid_alive", return_value=False):
            with self.assertRaises(ValueError):
                with module.index_lock(self.vault):
                    pass
        self.assertEqual(lock_path.read_text(encoding="utf-8"), stale)


    # --- Review: event path/date validation ---

    def test_year_folder_must_match_event_local_date(self):
        slug = "craft-" + "y" * 10
        date = "2026-10-13"
        event_id = "9" * 32
        bundle = f"Work History/{slug}/1999/Events/{date}/{event_id}"
        rows = [self.base_row("craft", event_id,
                              snapshot_path=f"{bundle}/original.md",
                              sha256="0" * 64)]
        self.write_bundle(slug, date, event_id, rows, [],
                          event_json={"event_id": event_id,
                                    "schema_version": 1,
                                    "project": "craft", "project_slug": slug,
                                    "recorded_at": "2026-10-13T12:00:00+00:00",
                                    "local_date": date, "timezone": "UTC",
                                    "title": "t", "kind": "execution",
                                    "status": "completed", "parents": [],
                                    "excluded_artifacts": [], "artifacts": []},
                          year="1999")
        error = self.view(ok=False)
        self.assertIn("error", error)
        self.assertFalse((self.root() / "_index" / "events.csv").exists())
        self.assertFalse(self.day_note(slug, date).exists())

    def test_impossible_local_date_is_refused(self):
        slug = "craft-" + "z" * 10
        date = "2026-02-30"  # impossible calendar date
        event_id = "8" * 32
        bundle = f"Work History/{slug}/2026/Events/{date}/{event_id}"
        rows = [self.base_row("craft", event_id, local_date=date,
                              snapshot_path=f"{bundle}/original.md",
                              sha256="0" * 64)]
        self.write_bundle(slug, date, event_id, rows, [],
                          event_json={"event_id": event_id,
                                    "schema_version": 1,
                                    "project": "craft", "project_slug": slug,
                                    "recorded_at": "2026-02-28T12:00:00+00:00",
                                    "local_date": date, "timezone": "UTC",
                                    "title": "t", "kind": "execution",
                                    "status": "completed", "parents": [],
                                    "excluded_artifacts": [], "artifacts": []})
        error = self.view(ok=False)
        self.assertIn("error", error)
        self.assertFalse(self.day_note(slug, date).exists())
        self.assertFalse((self.root() / "_index" / "events.csv").exists())

    # --- Review: exclusion rows require exclusion metadata ---

    def test_blank_snapshot_row_without_exclusion_metadata_fails_closed(self):
        slug = "craft-" + "x" * 10
        date = "2026-10-13"
        event_id = "7" * 32
        bundle = f"Work History/{slug}/2026/Events/{date}/{event_id}"

        def rows_for(eid):
            return [self.base_row("craft", eid,
                                  snapshot_path=f"{bundle}/original.md",
                                  sha256="0" * 64),
                    self.base_row("craft", eid, record_id="r1",
                                  role="document", source_path="/tmp/doc.bin",
                                  snapshot_path="", sha256="")]

        def links_for(eid):
            return [dict(source_id=eid, target_id="r1", relation="used")]

        base_json = {"schema_version": 1, "project": "craft",
                     "project_slug": slug,
                     "recorded_at": "2026-10-13T12:00:00+00:00",
                     "local_date": date, "timezone": "UTC", "title": "t",
                     "kind": "execution", "status": "completed",
                     "parents": [], "artifacts": []}
        # Variant 1: excluded_artifacts entry missing entirely.
        self.write_bundle(slug, date, event_id, rows_for(event_id),
                          links_for(event_id),
                          event_json={**base_json, "event_id": event_id,
                                      "excluded_artifacts": []})
        error = self.view(ok=False)
        self.assertIn("error", error)
        self.assertFalse((self.root() / "_index" / "events.csv").exists())
        self.assertFalse(self.day_note(slug, date).exists())
        # Variant 2: no event.json at all.
        self.write_bundle(slug, date, "6" * 32, rows_for("6" * 32),
                          links_for("6" * 32), event_json=None)
        self.view(ok=False)
        self.assertFalse((self.root() / "_index" / "events.csv").exists())
        # Variant 3: mismatched record_id in excluded_artifacts.
        self.write_bundle(slug, date, "5" * 32, rows_for("5" * 32),
                          links_for("5" * 32),
                          event_json={**base_json, "event_id": "5" * 32,
                                      "excluded_artifacts": [
                                          {"record_id": "someone-else",
                                           "role": "document",
                                           "source_path": "/tmp/doc.bin",
                                           "sha256": "2" * 64,
                                           "reason": "x"}]})
        self.view(ok=False)
        self.assertFalse((self.root() / "_index" / "events.csv").exists())
        self.assertFalse(self.day_note(slug, date).exists())

    # --- Incomplete bundles fail safely ---

    def test_incomplete_event_bundle_fails_index(self):
        self.record()
        broken = (self.root() / "x-project" / "2026" / "Events"
                  / "2026-10-13" / ("c" * 32))
        broken.mkdir(parents=True)
        (broken / "record.md").write_text("partial\n", encoding="utf-8")
        # records.csv missing → published-but-incomplete; index must fail
        # rather than silently skip or fabricate rows.
        self.view(ok=False)


if __name__ == "__main__":
    unittest.main()
