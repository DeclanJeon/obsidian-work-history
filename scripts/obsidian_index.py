"""Derived Obsidian navigation views over published immutable event bundles.

Reads only published ``Work History/<slug>/<YYYY>/Events/<date>/<event_id>/``
bundles (see ``history.py`` / ``references/record-format.md``) and regenerates:

- ``Work History/<slug>/README.md``             project identity + date navigation
- ``Work History/<slug>/<YYYY>/README.md``      year navigation
- ``Work History/<slug>/<YYYY>/<YYYY-MM-DD>.md`` daily event index
- ``Work History/_index/projects.md``           global project navigation
- ``Work History/_index/projects.csv``          project identity → slug map
- ``Work History/_index/events.csv``            cross-project query index

Safety contract:

- Generated content lives only between the exact markers
  ``<!-- obsidian-work-history:managed:start -->`` /
  ``<!-- obsidian-work-history:managed:end -->``; unmanaged frontmatter and
  prose are preserved verbatim. Malformed marker pairs abort the whole run.
- Wikilinks are deduplicated by parsed destination (``.md`` suffix and the
  ``Work History/`` prefix normalized away), so an existing ``[[<day-note>]]``
  in user prose is never repeated by generated content and vice versa.
- All index writers serialize on ``_index/.index-write.lock``; the event scan
  happens inside the lock so the last writer sees every published bundle.
- Every write goes through plan → compare-hash → temporary file →
  ``os.replace``. If a target changed between plan and commit (ambiguous
  write), the event bundle is kept and the ENTIRE publication aborts with
  zero writes; the commit hash is also rechecked inside the temp staging
  window immediately before each ``os.replace``.
- Excluded artifacts (blank ``snapshot_path``/``sha256`` rows joined to
  ``event.json.excluded_artifacts[]`` by ``record_id``) render as explicit
  excluded/incomplete provenance; a blank row without matching exclusion
  metadata fails closed instead of rendering as a missing file.
"""
import argparse
import contextlib
import csv
import dataclasses
import hashlib
import io
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history  # noqa: E402  (Task 1 event core)

MANAGED_START = "<!-- obsidian-work-history:managed:start -->"
MANAGED_END = "<!-- obsidian-work-history:managed:end -->"
INDEX_DIRNAME = "_index"
LOCK_FILENAME = ".index-write.lock"
LOCK_TIMEOUT_SECONDS = 15.0
LOCK_STALE_SECONDS = 300.0
PROJECT_FIELDS = ["project", "slug", "event_count",
                  "first_local_date", "last_local_date"]
WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")
DATE_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}")
YEAR_FORMAT = re.compile(r"\d{4}")
DRIVE_FORMAT = re.compile(r"[A-Za-z]:")
UTF8_BOM = b"\xef\xbb\xbf"


class IndexConflict(Exception):
    """A target file changed between plan and commit; user content wins."""

    def __init__(self, conflicts, written=()):
        self.conflicts = [str(path) for path in conflicts]
        self.written = [str(path) for path in written]
        super().__init__(
            "index conflict; ambiguous writes aborted, user content "
            "preserved: " + ", ".join(self.conflicts))


@dataclasses.dataclass
class IndexUpdate:
    path: Path
    content: bytes
    previous_sha256: str | None
    vault: Path


# ---------- path and link safety ----------


def normalize_destination(dest):
    """Canonical wikilink identity: [[a/b]] == [[a/b.md]] ==
    [[Work History/a/b]]."""
    if dest.endswith(".md"):
        dest = dest[:-3]
    prefix = history.HISTORY_DIRNAME + "/"
    if dest.startswith(prefix):
        dest = dest[len(prefix):]
    return dest


def destinations(text):
    return {normalize_destination(d) for d in WIKILINK.findall(text)}


def display(text):
    """Single-line wikilink-safe display text."""
    cleaned = re.sub(r"[\[\]|]", " ", str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned or "(untitled)"


class LinkEmitter:
    """Emits each wikilink destination at most once per note.

    Destinations already present in preserved prose are seeded via
    ``taken``; a repeat renders plain display text, never a second link.
    """

    def __init__(self, taken=()):
        self.taken = {normalize_destination(d) for d in taken}

    def link(self, dest, alias):
        key = normalize_destination(dest)
        label = display(alias)
        if key in self.taken:
            return label
        self.taken.add(key)
        return f"[[{dest}|{label}]]"


def ensure_inside_vault(path, vault):
    """Return ``path`` only if every component below the vault is real —
    no symlink hops, no escape."""
    try:
        relative = Path(path).relative_to(vault)
    except ValueError as error:
        raise ValueError(f"path escapes vault: {path}") from error
    current = vault
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"refusing symlinked path: {current}")
    return current


def check_write_target(path, vault):
    """Refuse symlinked/escaping/directory targets before planning a write."""
    relative = ensure_inside_vault(path, vault)
    if relative.is_symlink():
        raise ValueError(f"refusing to write through symlink: {path}")
    if relative.is_dir():
        raise ValueError(f"ambiguous write target (is a directory): {path}")


def validate_snapshot_path(snapshot):
    if not snapshot:
        return
    pure = PurePosixPath(snapshot)
    if ("\\" in snapshot or pure.is_absolute() or snapshot.startswith("/")
            or ".." in pure.parts or DRIVE_FORMAT.match(snapshot)):
        raise ValueError(
            f"snapshot_path escapes the vault layout: {snapshot!r}")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# ---------- bundle reading ----------


def read_csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def read_bundles(vault, project_folder=None):
    """Read every published bundle; incomplete/corrupt bundles abort."""
    root = history.history_root(vault)
    events = []
    for folder in history.event_dirs(vault, project_folder):
        folder = ensure_inside_vault(folder, vault)
        records_path = folder / "records.csv"
        links_path = folder / "links.csv"
        json_path = folder / "event.json"
        for candidate in (records_path, links_path, json_path):
            if candidate.is_symlink():
                raise ValueError(f"refusing symlinked bundle file: {candidate}")
        if not (records_path.is_file() and links_path.is_file()):
            raise ValueError(f"incomplete event: {folder}")
        records = read_csv_rows(records_path)
        links = read_csv_rows(links_path)
        event_json = {}
        if json_path.is_file():
            try:
                event_json = json.loads(
                    json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"malformed event.json in bundle: {folder}") from error
        relative = folder.relative_to(root)
        parts = relative.parts
        if len(parts) != 5 or parts[2] != "Events":
            raise ValueError(f"unexpected event directory shape: {folder}")
        slug, year, _events_dir, date, event_id = parts
        if not (YEAR_FORMAT.fullmatch(year) and DATE_FORMAT.fullmatch(date)):
            raise ValueError(f"unexpected event directory shape: {folder}")
        if year != date[:4]:
            raise ValueError(
                f"year folder {year} disagrees with event date {date}: "
                f"{folder}")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError(
                f"impossible calendar date in event path: {folder}") from error
        event_rows = [row for row in records if row.get("role") == "event"]
        if len(event_rows) != 1:
            raise ValueError(
                f"event bundle must have exactly one event row: {folder}")
        meta = event_rows[0]
        if meta.get("event_id") and meta["event_id"] != event_id:
            raise ValueError(
                f"event_id mismatch between path and records.csv: {folder}")
        if meta.get("local_date") and meta["local_date"] != date:
            raise ValueError(
                f"local_date {meta['local_date']} disagrees with event "
                f"path {date}: {folder}")
        for row in records:
            validate_snapshot_path(row.get("snapshot_path") or "")
        exclusions = {}
        raw = event_json.get("excluded_artifacts")
        for item in raw or []:
            if isinstance(item, dict) and item.get("record_id"):
                exclusions[item["record_id"]] = item
        artifacts = [(row, exclusions.get(row.get("record_id")))
                     for row in records if row.get("role") != "event"]
        for row, exclusion in artifacts:
            if not (row.get("snapshot_path") or "") and exclusion is None:
                raise ValueError(
                    f"blank-snapshot row {row.get('record_id')} has no "
                    f"matching event.json excluded_artifacts entry: {folder}")
        events.append(dict(
            slug=slug, year=year, date=date, event_id=event_id,
            rel_bundle=folder.relative_to(vault).as_posix(),
            recorded_at=meta.get("recorded_at") or "",
            kind=meta.get("kind") or "", status=meta.get("status") or "",
            title=meta.get("title") or "",
            project=meta.get("project") or "",
            timezone=meta.get("timezone") or "",
            local_date=meta.get("local_date") or date,
            artifacts=artifacts, records=records, links=links))
    events.sort(key=lambda e: (e["recorded_at"], e["event_id"]))
    return events


def project_name(root, slug, events):
    marker = root / slug / "project.json"
    try:
        if marker.is_file() and not marker.is_symlink():
            stored = json.loads(marker.read_text(encoding="utf-8"))
            if stored.get("project"):
                return stored["project"]
    except (OSError, ValueError):
        pass
    if events:
        return events[0]["project"]
    return slug


# ---------- managed-block merge ----------


def merge_managed(existing, heading, render):
    """Return merged note text; raise ValueError on malformed markers."""
    if existing is None:
        body = render(LinkEmitter())
        return f"{heading}\n\n{MANAGED_START}\n{body}{MANAGED_END}\n"
    starts = existing.count(MANAGED_START)
    ends = existing.count(MANAGED_END)
    if starts == 0 and ends == 0:
        emitter = LinkEmitter(destinations(existing))
        body = render(emitter)
        base = existing if existing.endswith("\n") else existing + "\n"
        return f"{base}\n{MANAGED_START}\n{body}{MANAGED_END}\n"
    if starts != 1 or ends != 1:
        raise ValueError("malformed managed markers: refusing to merge")
    start = existing.find(MANAGED_START)
    end = existing.find(MANAGED_END)
    if end < start:
        raise ValueError("malformed managed markers: refusing to merge")
    prefix = existing[:start]
    suffix = existing[end + len(MANAGED_END):]
    emitter = LinkEmitter(destinations(prefix + suffix))
    body = render(emitter)
    return prefix + f"{MANAGED_START}\n{body}{MANAGED_END}" + suffix


# ---------- renderers ----------


def render_day(date, day_events, emitter):
    lines = [f"## Recorded events — {date}", ""]
    for event in day_events:
        record_dest = f"{event['rel_bundle']}/record"
        lines.append(
            f"- `{event['recorded_at']}` · {event['kind']} · "
            f"{event['status']} — {display(event['title'])} — "
            f"{emitter.link(record_dest, 'record')}")
        for row, exclusion in event["artifacts"]:
            role = row.get("role") or "artifact"
            name = display(Path(row.get("source_path") or
                                row.get("record_id") or "artifact").name)
            snapshot = row.get("snapshot_path") or ""
            if snapshot:
                lines.append(f"  - {role}: "
                             f"{emitter.link(snapshot, name)}")
            else:
                reason = (exclusion or {}).get("reason") or \
                    "no snapshot stored"
                digest = (exclusion or {}).get("sha256") or ""
                suffix = (f" — source sha256 `{digest[:16]}…`"
                          if digest else "")
                lines.append(f"  - excluded {role}: `{name}` — "
                             f"{reason}{suffix}")
    return "\n".join(lines) + "\n"


def render_year(slug, year, dates, emitter):
    lines = []
    for date in sorted(dates):
        events = dates[date]
        dest = f"{history.HISTORY_DIRNAME}/{slug}/{year}/{date}"
        lines.append(f"- {emitter.link(dest, date)} — "
                     f"{len(events)} event(s)")
    return "\n".join(lines) + "\n"


def render_project(slug, year_map, emitter):
    lines = []
    for year in sorted(year_map):
        year_dest = (f"{history.HISTORY_DIRNAME}/{slug}/{year}/README")
        lines.append(f"### {emitter.link(year_dest, year)}")
        lines.append("")
        for date in sorted(year_map[year]):
            events = year_map[year][date]
            dest = f"{history.HISTORY_DIRNAME}/{slug}/{year}/{date}"
            titles = "; ".join(display(e["title"]) for e in events)
            lines.append(f"- {emitter.link(dest, date)} — "
                         f"{len(events)} event(s): {titles}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_projects(projects, emitter):
    lines = []
    for entry in projects:
        span = (entry["first"] if entry["first"] == entry["last"]
                else f"{entry['first']} → {entry['last']}")
        dest = (history.HISTORY_DIRNAME + "/" + entry["slug"] + "/README")
        lines.append(
            f"- {emitter.link(dest, entry['project'])}"
            f" — slug `{entry['slug']}` · {entry['count']} event(s) · {span}")
    return "\n".join(lines) + "\n"


def csv_bytes(fields, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return UTF8_BOM + buffer.getvalue().encode("utf-8")


# ---------- planning ----------


def plan_note(vault, path, heading, render):
    """Plan one managed note update.

    Unchanged files stay in the plan: the hash check happens at commit so an
    in-flight external edit still conflicts instead of being overwritten.
    """
    check_write_target(path, vault)
    existing = path.read_bytes() if path.is_file() else None
    previous = sha256_bytes(existing) if existing is not None else None
    text = existing.decode("utf-8") if existing is not None else None
    merged = merge_managed(text, heading, render)
    return IndexUpdate(path=path, content=merged.encode("utf-8"),
                       previous_sha256=previous, vault=vault)


def plan_generated_file(vault, path, content):
    """Plan a fully-generated file (CSVs); same conflict discipline."""
    check_write_target(path, vault)
    existing = path.read_bytes() if path.is_file() else None
    previous = sha256_bytes(existing) if existing is not None else None
    return IndexUpdate(path=path, content=content,
                       previous_sha256=previous, vault=vault)


def _plan(vault, project_folder=None):
    vault = Path(vault)
    root = history.history_root(vault)
    scoped = read_bundles(vault, project_folder)
    bundles = scoped if project_folder is None else read_bundles(vault)
    updates = []

    by_day = {}
    for event in scoped:
        by_day.setdefault((event["slug"], event["year"], event["date"]),
                          []).append(event)

    # Daily notes.
    for (slug, year, date), day_events in sorted(by_day.items()):
        day_events.sort(key=lambda e: (e["recorded_at"], e["event_id"]))
        path = root / slug / year / f"{date}.md"
        update = plan_note(
            vault, path, f"# {date}",
            lambda emitter, d=date, ev=day_events:
                render_day(d, ev, emitter))
        updates.append(update)

    # Year navigation notes.
    by_year = {}
    for (slug, year, date), day_events in by_day.items():
        by_year.setdefault((slug, year), {})[date] = day_events
    for (slug, year), dates in sorted(by_year.items()):
        path = root / slug / year / "README.md"
        update = plan_note(
            vault, path, f"# {year} — work history",
            lambda emitter, s=slug, y=year, d=dates:
                render_year(s, y, d, emitter))
        updates.append(update)

    # Project README navigation.
    by_slug = {}
    for (slug, year, date), day_events in by_day.items():
        by_slug.setdefault(slug, {}).setdefault(year, {})[date] = day_events
    for slug, year_map in sorted(by_slug.items()):
        scoped_events = [e for days in (d for y in year_map.values()
                                        for d in y.values()) for e in days]
        name = project_name(root, slug, scoped_events)
        path = root / slug / "README.md"
        update = plan_note(
            vault, path, f"# {display(name)} — work history",
            lambda emitter, s=slug, y=year_map:
                render_project(s, y, emitter))
        updates.append(update)

    # Global navigation always covers every published project.
    index_dir = root / INDEX_DIRNAME
    grouped = {}
    for event in bundles:
        grouped.setdefault(event["slug"], []).append(event)
    projects = []
    for slug, slug_events in sorted(grouped.items()):
        dates = sorted(e["local_date"] for e in slug_events)
        projects.append(dict(
            project=project_name(root, slug, slug_events), slug=slug,
            count=len({e["event_id"] for e in slug_events}),
            first=dates[0] if dates else "", last=dates[-1] if dates else ""))
    updates.append(plan_note(
        vault, index_dir / "projects.md", "# Work history projects",
        lambda emitter, p=projects: render_projects(p, emitter)))
    project_rows = [dict(project=p["project"], slug=p["slug"],
                         event_count=p["count"],
                         first_local_date=p["first"],
                         last_local_date=p["last"]) for p in projects]
    updates.append(plan_generated_file(
        vault, index_dir / "projects.csv",
        csv_bytes(PROJECT_FIELDS, project_rows)))
    all_rows = [row for event in bundles for row in event["records"]]
    all_rows.sort(key=lambda r: (r.get("recorded_at") or "",
                                 r.get("event_id") or "",
                                 r.get("record_id") or ""))
    updates.append(plan_generated_file(
        vault, index_dir / "events.csv",
        csv_bytes(history.RECORD_FIELDS, all_rows)))

    stats = dict(events=len({e["event_id"] for e in bundles}),
                 projects=len(grouped),
                 projects_md=str(index_dir / "projects.md"),
                 projects_csv=str(index_dir / "projects.csv"),
                 events_csv=str(index_dir / "events.csv"))
    return updates, stats


def plan_updates(vault, project_folder=None):
    """Plan all view updates without writing anything."""
    updates, _stats = _plan(vault, project_folder)
    return updates


# ---------- committing ----------


def atomic_write(path, content, expected_sha256):
    """Write via same-directory temp file + os.replace (atomic).

    Re-checks the target hash immediately before replacing: a concurrent
    write landing mid-staging raises IndexConflict instead of overwriting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-index-", suffix=".part",
                                     dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        current = path.read_bytes() if path.is_file() else None
        current_sha = sha256_bytes(current) if current is not None else None
        if current_sha != expected_sha256:
            raise IndexConflict([path])
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def commit_updates(updates):
    """All-or-nothing commit: preflight every target hash, then write.

    Phase 1 validates all targets before any write — any conflict aborts the
    entire publication with zero writes. Phase 2 rechecks each hash inside
    atomic_write immediately before os.replace; a race detected there raises
    IndexConflict carrying the targets already written (partial state is
    surfaced, never silently overwritten).
    """
    pending = []
    for update in updates:
        check_write_target(update.path, update.vault)
        current = (update.path.read_bytes()
                   if update.path.is_file() else None)
        if current == update.content:
            continue
        current_sha = sha256_bytes(current) if current is not None else None
        if current_sha != update.previous_sha256:
            raise IndexConflict([update.path])
        pending.append(update)
    written = []
    for update in pending:
        try:
            atomic_write(update.path, update.content,
                         update.previous_sha256)
        except IndexConflict as conflict:
            conflict.written = [str(path) for path in written]
            raise
        written.append(update.path)
    return written

def _pid_alive(pid):
    """Best-effort liveness probe for the recorded lock owner.

    Anything uncertain answers True (alive) so a lock is never reclaimed on a
    guess — a stale lock then requires manual cleanup rather than risking two
    concurrent index writers.
    """
    if not isinstance(pid, int) or pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            still_active = 259
            try:
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == still_active
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True



@contextlib.contextmanager
def index_lock(vault):
    """Serialize index writers; the event scan runs while holding it.

    Conservative ownership: each holder writes a unique token+PID. An
    existing lock is reclaimed ONLY when its recorded owner process is
    demonstrably dead AND the lock is older than LOCK_STALE_SECONDS; an
    unreadable/malformed lock or a live-owner lock is never unlinked — the
    caller waits or fails, so cleanup stays manual rather than risking two
    writers. Release unlinks the file only while its on-disk bytes still
    match the token this context wrote.
    """
    lock_path = (history.history_root(vault) / INDEX_DIRNAME / LOCK_FILENAME)
    # Validate ancestors before creating anything: never follow a symlinked
    # "Work History" or "_index" into another directory.
    ensure_inside_vault(lock_path, vault)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    check_write_target(lock_path, vault)
    token = uuid.uuid4().hex
    mine = json.dumps({"token": token, "pid": os.getpid(),
                       "acquired": time.time()}).encode("utf-8")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            fd = None
        if fd is not None:
            break
        try:
            holder_bytes = lock_path.read_bytes()
            holder = json.loads(holder_bytes.decode("utf-8"))
            age = time.time() - lock_path.stat().st_mtime
        except (OSError, ValueError):
            holder, age = None, 0.0
        holder_pid = holder.get("pid") if isinstance(holder, dict) else None
        # Reclaim only a demonstrably dead, stale owner. A live PID is never
        # unlinked; unreadable/malformed locks are never unlinked either.
        if (isinstance(holder, dict) and isinstance(holder_pid, int)
                and not _pid_alive(holder_pid) and age > LOCK_STALE_SECONDS):
            try:
                # Token-compare before unlink: never remove a lock that was
                # replaced since we read it.
                if lock_path.read_bytes() == holder_bytes:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
        if time.monotonic() > deadline:
            raise ValueError(f"index lock is held: {lock_path}")
        time.sleep(0.05)
    try:
        os.write(fd, mine)
    finally:
        os.close(fd)
    try:
        yield
    finally:
        try:
            # Release only our own lock: if another owner's file replaced
            # it, our bytes no longer match and we leave it alone.
            if lock_path.read_bytes() == mine:
                lock_path.unlink()
        except OSError:
            pass

def rebuild(vault, project_folder=None):
    """Lock → plan (event scan inside the lock) → atomic commit."""
    with index_lock(vault):
        updates, stats = _plan(vault, project_folder)
        written = commit_updates(updates)
    stats["updated"] = [str(path) for path in written]
    return stats


# ---------- CLI ----------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser(
        "index",
        help="Regenerate managed Obsidian notes and _index CSV/Markdown")
    history.add_vault_arguments(index)
    index.add_argument("--project",
                       help="Regenerate notes for one project identity only; "
                            "_index outputs still cover every project")
    args = parser.parse_args()
    try:
        vault = history.resolve_vault(args.vault)
        project_folder = None
        if args.project:
            project_folder = history.find_project_slug(
                history.history_root(vault), args.project)
            if project_folder is None:
                raise ValueError(f"unknown project: {args.project}")
        result = rebuild(vault, project_folder)
        result["updated"] = [
            Path(path).relative_to(vault).as_posix()
            for path in result["updated"]]
    except (IndexConflict, ValueError, OSError, UnicodeError,
            csv.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
