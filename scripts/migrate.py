"""Read-only inspection, dry-run, and approved import of legacy history stores.

Two legacy shapes are deliberately separated:

- Global event store (``~/.history``): versioned ``events/<stem>/`` event
  bundles written by the upstream ``history.py`` — the only shape accepted
  as an import source.
- Project-local ``.history/``: continuity evidence (flat event notes,
  ``STATUS_SNAPSHOT.md``, ``prompts/``) — inspectable read-only, NEVER an
  event-bundle import source.

``inspect`` and ``dry-run`` write nothing anywhere. ``import`` additionally
requires ``--approve`` and an explicit JSON map from each relative event
directory to its canonical project identity. Source roots are never
modified. Text is screened with ``history.screen_text`` before publishing;
ambiguous text blocks that event. Binary/unsupported artifacts are excluded
as first-class linked metadata rows (blank snapshot/sha256 in records.csv
plus ``event.json.excluded_artifacts``) — bytes are never copied. Unknown
source CSV columns are preserved in ``event.json.migration.unknown_fields``
or the event fails.

Idempotency: ``source_key = legacy-history:sha256(source_root + rel_dir +
event_id)``. A matching existing bundle is an idempotent skip; the same
event_id under a different source_key is a conflict and is never remapped.

Exit codes: 0 clean, 1 soft findings/partial failure (per-event statuses
are the truth), 2 hard error (arguments, unreadable source, wrong shape).
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history  # noqa: E402  (Task 1 event core)

MIGRATION_SCHEMA_VERSION = 1
UPSTREAM_FIELDS = ["record_id", "event_id", "recorded_at", "title", "kind",
                   "status", "tool", "model", "version", "role",
                   "source_path", "snapshot_path", "sha256"]
REQUIRED_FIELDS = set(UPSTREAM_FIELDS)
DRIVE_FORMAT = re.compile(r"[A-Za-z]:")


class EventProblem(Exception):
    """One event fails validation/screening; the rest may still proceed."""


# ---------- source layout ----------


def is_link_or_junction(path):
    """Recognize symlinks and Windows junctions without traversing them."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    if os.name == "nt":
        try:
            return bool(path.lstat().st_file_attributes & 0x400)
        except (AttributeError, OSError):
            return False
    return False


def classify_source(path):
    """Classify a legacy store without modifying it.

    Project-local continuity markers dominate; an ``events/`` directory of
    event-bundle dirs (or an empty/pending-only one, the global store's
    on-disk shape) classifies as a global event store; anything else is
    unrecognized.

    Returns global_event_store | project_local_continuity | unrecognized.
    """
    path = Path(path)
    if not path.is_dir():
        return "unrecognized"
    events = path / "events"
    has_local_markers = ((path / "STATUS_SNAPSHOT.md").is_file()
                         or (path / "prompts").is_dir())
    if is_link_or_junction(events):
        return "unrecognized"
    if events.is_dir():
        entries = list(events.iterdir())
        has_bundle_dirs = any(e.is_dir() and not is_link_or_junction(e)
                              and not e.name.startswith(".")
                              for e in entries)
        has_notes = any(e.is_file() and not is_link_or_junction(e)
                        and e.suffix == ".md" for e in entries)
        if has_bundle_dirs and not has_notes and not has_local_markers:
            return "global_event_store"
        if has_notes or has_local_markers:
            return "project_local_continuity"
        if not has_notes:
            # Empty or pending-only events/: the global event store's
            # resting shape (a fresh --root has an empty events/).
            return "global_event_store"
    if has_local_markers:
        return "project_local_continuity"
    return "unrecognized"


def scan_source(root):
    """Inventory a global event store. Read-only.

    Returns (events, pending, corrupt, non_event_entries) where events is
    ``[(rel_dir, Path)]`` for non-pending dirs with exactly one each of
    ``*_records.csv``/``*_links.csv``/annotated ``.md``/``*_원문.md``, and
    corrupt is ``{rel_dir: reason}`` for malformed event dirs.
    """
    root = Path(root).resolve()
    events_root = root / "events"
    events, pending, corrupt, non_event = [], [], {}, []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name.startswith(".pending-"):
            pending.append(entry.relative_to(root).as_posix())
    if is_link_or_junction(events_root):
        corrupt["events"] = "symlinked events directory rejected"
        return events, pending, corrupt, non_event
    if not events_root.is_dir():
        return events, pending, corrupt, non_event
    for entry in sorted(events_root.iterdir()):
        rel = entry.relative_to(root).as_posix()
        if is_link_or_junction(entry):
            if entry.name.startswith(".pending-"):
                pending.append(rel)
            else:
                corrupt[rel] = "symlinked event entry rejected"
            continue
        if not entry.is_dir():
            non_event.append(rel)
            continue
        if entry.name.startswith("."):
            pending.append(rel)
            continue
        expected = {
            "records": list(entry.glob("*_records.csv")),
            "links": list(entry.glob("*_links.csv")),
            "original": list(entry.glob("*_원문.md")),
        }
        expected["annotated"] = [
            p for p in entry.glob("*.md")
            if not p.name.endswith("_원문.md")]
        problems = []
        for key in ("records", "links", "annotated", "original"):
            if len(expected[key]) != 1:
                problems.append(
                    f"{key}: expected exactly 1 file, found "
                    f"{len(expected[key])}")
        if problems:
            corrupt[rel] = "; ".join(problems)
        else:
            events.append((rel, entry))
    return events, pending, corrupt, non_event


def resolve_source_snapshot(root, snapshot_path):
    """Resolve a records.csv snapshot_path safely beneath the source root.

    Rejects traversal, absolute/drive paths, backslashes, and any symlinked
    component. Returns the real file path or raises EventProblem.
    """
    if "\\" in snapshot_path:
        raise EventProblem(
            f"snapshot_path is not a POSIX relative path: {snapshot_path!r}")
    pure = PurePosixPath(snapshot_path)
    if (pure.is_absolute() or snapshot_path.startswith("/")
            or ".." in pure.parts or DRIVE_FORMAT.match(snapshot_path)):
        raise EventProblem(
            f"snapshot_path escapes the source root: {snapshot_path!r}")
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if is_link_or_junction(current):
            raise EventProblem(f"refusing symlinked source path: {current}")
    if not candidate.is_file():
        raise EventProblem(f"snapshot file missing: {snapshot_path!r}")
    return candidate


def secret_report(text):
    """Count recognizable secret hits and ambiguity without exposing values."""
    hits = 0
    for _label, pattern in history.SECRET_RULES:
        hits += len(pattern.findall(text))
    begins = len(history.PRIVATE_KEY_MARKER.findall(text))
    blocks = len(history.SECRET_RULES[-1][1].findall(text))
    return hits, begins != blocks


def screen_metadata_text(value, label):
    """Redact supported metadata text without leaking scanner diagnostics."""
    try:
        return history.screen_text(value, label)
    except ValueError as error:
        raise EventProblem(
            f"ambiguous secret-like metadata in {label}; event blocked") from error


def require_clean_identifier(value, label):
    cleaned, _count = screen_metadata_text(value, label)
    if cleaned != value:
        raise EventProblem(
            f"secret-like content in structural field {label}; "
            "cannot redact without changing IDs")


def validate_event_id(event_id):
    """Reject IDs that cannot safely occupy exactly one destination component."""
    forbidden = '<>:"|?*'
    component = event_id.strip(" .")
    device_name = component.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"}
    is_device_number = bool(re.fullmatch(r"(?:COM|LPT)[1-9]", device_name))
    if (not event_id or event_id in (".", "..")
            or "/" in event_id or "\\" in event_id
            or DRIVE_FORMAT.match(event_id)
            or any(ord(char) < 32 or char in forbidden
                   for char in event_id)
            or component != event_id
            or device_name in reserved or is_device_number):
        raise EventProblem("event_id is not a safe single path component")


def sanitize_unknown_fields(fields):
    """Preserve unknown CSV columns and values, redacting only values."""
    result = {}
    redactions = 0
    for column_key, values_key in (("columns", "values"),
                                   ("link_columns", "link_values")):
        columns = []
        for column in fields[column_key]:
            cleaned, count = screen_metadata_text(
                column, "unknown CSV column name")
            if cleaned != column:
                raise EventProblem(
                    "secret-like content in unknown CSV column name")
            columns.append(cleaned)
            redactions += count
        result[column_key] = columns
        clean_values = {}
        for column, values in fields[values_key].items():
            clean_column, count = screen_metadata_text(
                column, "unknown CSV column name")
            if clean_column != column:
                raise EventProblem(
                    "secret-like content in unknown CSV column name")
            redactions += count
            cleaned_values = []
            for value in values:
                if value is None:
                    cleaned_values.append(None)
                    continue
                cleaned, count = screen_metadata_text(
                    value, f"unknown CSV value in {column}")
                cleaned_values.append(cleaned)
                redactions += count
            clean_values[clean_column] = cleaned_values
        result[values_key] = clean_values
    return result, redactions


def scrub_report(value):
    """Redact known secrets from every user-facing JSON string."""
    if isinstance(value, str):
        try:
            return history.screen_text(value, "migration report")[0]
        except ValueError:
            return "[REDACTED: ambiguous secret-like content]"
    if isinstance(value, list):
        return [scrub_report(item) for item in value]
    if isinstance(value, dict):
        return {scrub_report(key): scrub_report(item)
                for key, item in value.items()}
    return value


def print_json(value, *, file=None):
    print(json.dumps(scrub_report(value), ensure_ascii=False), file=file)


def read_csv_strict(path, rel):
    """Parse and hash the exact source bytes used for CSV metadata."""
    try:
        raw = path.read_bytes()
        stream = io.StringIO(raw.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = reader.fieldnames or []
    except (OSError, UnicodeError, csv.Error) as error:
        raise EventProblem(f"unreadable {rel}: {error}") from error
    if not fields or any(not field for field in fields):
        raise EventProblem(f"{rel} has an empty CSV field name")
    if len(set(fields)) != len(fields):
        raise EventProblem(f"{rel} has duplicate CSV field names")
    if any(None in row for row in rows):
        raise EventProblem(f"{rel} has ragged rows (extra unnamed fields)")
    return fields, rows, hashlib.sha256(raw).hexdigest()


def analyze_event(root, rel, folder):
    """Validate one event dir and gather its dry-run facts. Read-only."""
    stem = folder.name
    records_path = next(folder.glob("*_records.csv"))
    links_path = next(folder.glob("*_links.csv"))
    annotated_path = next(p for p in folder.glob("*.md")
                          if not p.name.endswith("_원문.md"))
    original_path = next(folder.glob("*_원문.md"))
    for candidate in (records_path, links_path, annotated_path,
                      original_path):
        if is_link_or_junction(candidate):
            raise EventProblem(f"refusing symlinked file: {candidate}")

    fields, rows, records_digest = read_csv_strict(records_path, "records.csv")
    link_fields, links, links_digest = read_csv_strict(links_path, "links.csv")
    missing = REQUIRED_FIELDS - set(fields)
    if missing:
        raise EventProblem(f"records.csv missing fields: {sorted(missing)}")
    for required in ("source_id", "target_id", "relation"):
        if required not in link_fields:
            raise EventProblem(f"links.csv missing field: {required}")
    if not rows:
        raise EventProblem("records.csv has no rows")

    event_rows = [r for r in rows if r.get("role") == "event"]
    if len(event_rows) != 1:
        raise EventProblem(
            f"records.csv must have exactly one event row, "
            f"found {len(event_rows)}")
    event_id = event_rows[0].get("event_id") or ""
    if not event_id:
        raise EventProblem("event row has empty event_id")
    validate_event_id(event_id)
    for row in rows:
        if row.get("event_id") != event_id:
            raise EventProblem(
                f"row {row.get('record_id')} event_id "
                f"{row.get('event_id')!r} disagrees with {event_id!r}")
        if not row.get("record_id"):
            raise EventProblem(f"row in {rel} has empty record_id")
        require_clean_identifier(row["record_id"], "record_id")
        require_clean_identifier(row["event_id"], "event_id")
    record_ids = {r["record_id"] for r in rows}
    if len(record_ids) != len(rows):
        raise EventProblem(f"duplicate record_id values in {rel}")

    recorded_at = event_rows[0].get("recorded_at") or ""
    try:
        parsed = datetime.fromisoformat(recorded_at)
    except ValueError as error:
        raise EventProblem(
            f"unparseable recorded_at {recorded_at!r}") from error
    if parsed.tzinfo is None:
        raise EventProblem(f"recorded_at has no timezone: {recorded_at!r}")

    unknown_record_cols = [c for c in fields if c not in REQUIRED_FIELDS]
    unknown_link_cols = [c for c in link_fields
                         if c not in history.LINK_FIELDS]
    unknown_values = {c: [row.get(c) for row in rows]
                      for c in unknown_record_cols}
    unknown_link_values = {c: [row.get(c) for row in links]
                           for c in unknown_link_cols}

    missing_targets = []
    for link in links:
        for key in ("source_id", "target_id", "relation"):
            if not (link.get(key) or ""):
                raise EventProblem(f"links.csv row has empty {key}")
            require_clean_identifier(link[key], f"links.csv {key}")
        source_id, target_id = link["source_id"], link["target_id"]
        relation = link["relation"]
        if relation in ("used", "produced"):
            if source_id != event_id or target_id not in record_ids:
                missing_targets.append(f"{source_id} -> {target_id}")
        elif relation == "precedes":
            if source_id != event_id and target_id != event_id:
                missing_targets.append(f"{source_id} -> {target_id}")
        elif source_id not in record_ids and target_id not in record_ids:
            # Unknown relation wholly outside this event's record graph.
            missing_targets.append(f"{source_id} -> {target_id}")
    if missing_targets:
        raise EventProblem(
            f"links.csv targets unknown record IDs: {missing_targets}")

    secret_hits = 0
    ambiguous = False
    for value in (rel, *(path.name for path in
                        (records_path, links_path, annotated_path,
                         original_path)), *fields, *link_fields):
        hits, bad = secret_report(value)
        secret_hits += hits
        ambiguous = ambiguous or bad
    for row in rows:
        for value in row.values():
            if value:
                hits, bad = secret_report(value)
                secret_hits += hits
                ambiguous = ambiguous or bad
    for link in links:
        for value in link.values():
            if value:
                hits, bad = secret_report(value)
                secret_hits += hits
                ambiguous = ambiguous or bad
    for text_path, label in ((annotated_path, stem + ".md"),
                             (original_path, stem + "_원문.md")):
        data = text_path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise EventProblem(
                f"{label} is not supported UTF-8 text") from error
        hits, bad = secret_report(text)
        secret_hits += hits
        ambiguous = ambiguous or bad

    exclusions = []
    snapshots = []
    for row in rows:
        snapshot = row.get("snapshot_path") or ""
        if not snapshot:
            # A blank snapshot row is provenance-only. The Task-2 index
            # contract requires a matching exclusion entry carrying a
            # source digest; upstream never emits such rows and we cannot
            # fabricate a digest, so the event must fail — never silently
            # import unverifiable provenance.
            raise EventProblem(
                f"row {row['record_id']} has no snapshot_path; upstream "
                f"records always snapshot — cannot verify provenance")
        if not (row.get("sha256") or ""):
            raise EventProblem(
                f"row {row['record_id']} has snapshot_path but no sha256")
        source_file = resolve_source_snapshot(root, snapshot)
        if row.get("role") == "event" and source_file != original_path:
            raise EventProblem(
                f"event row snapshot {snapshot!r} is not the event's "
                f"*_원문.md original snapshot")
        data = source_file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        claimed = row.get("sha256") or ""
        if claimed != digest:
            raise EventProblem(
                f"sha256 mismatch for {snapshot}: csv={claimed} "
                f"actual={digest}")
        if history.looks_binary(data):
            exclusions.append(dict(record_id=row["record_id"],
                                   role=row.get("role") or "",
                                   source_path=row.get("source_path") or "",
                                   sha256=digest,
                                   reason="binary artifact: no approved "
                                          "local scanner; bytes not copied"))
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            exclusions.append(dict(record_id=row["record_id"],
                                   role=row.get("role") or "",
                                   source_path=row.get("source_path") or "",
                                   sha256=digest,
                                   reason="artifact is not supported UTF-8 "
                                          "text; bytes not copied"))
            continue
        hits, bad = secret_report(text)
        secret_hits += hits
        ambiguous = ambiguous or bad
        snapshots.append((row, source_file, digest))

    return dict(
        rel=rel, folder=folder, event_id=event_id, rows=rows, links=links,
        event_row=event_rows[0], recorded_at=recorded_at,
        records_path=records_path, links_path=links_path,
        source_files_sha256=dict(records=records_digest, links=links_digest),
        annotated_path=annotated_path, original_path=original_path,
        unknown_fields=dict(columns=unknown_record_cols,
                            values=unknown_values,
                            link_columns=unknown_link_cols,
                            link_values=unknown_link_values),
        secret_flag=bool(secret_hits), secret_count=secret_hits,
        ambiguous=ambiguous, excluded_artifacts=exclusions,
        snapshots=snapshots)


def source_key(root, rel, event_id):
    material = "\x1f".join([str(Path(root).resolve()), rel, event_id])
    return "legacy-history:" + hashlib.sha256(
        material.encode("utf-8")).hexdigest()


# ---------- mapping ----------


def load_map(map_path):
    """Load the explicit event-dir -> canonical project identity map."""
    path = Path(map_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read project map {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("project map must be a JSON object "
                         "{relative_event_dir: project_identity}")
    mapping = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ValueError("project map keys must be non-empty "
                             "relative paths")
        pure = PurePosixPath(key)
        if ("\\" in key or pure.is_absolute() or key.startswith("/")
                or ".." in pure.parts or DRIVE_FORMAT.match(key)):
            raise ValueError(f"project map key escapes the source root: {key!r}")
        if isinstance(value, dict):
            value = value.get("project")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"project map value for {key!r} must be a "
                             f"non-empty project identity string")
        try:
            clean_value, _count = history.screen_text(
                value, "project identity")
        except ValueError as error:
            raise ValueError(
                "project identity has ambiguous secret-like content") from error
        if clean_value != value:
            raise ValueError(
                "project identity contains secret-like content; choose a "
                "non-secret canonical name")
        mapping[pure.as_posix()] = value
    return mapping


# ---------- vault side ----------


def assert_safe_history_tree(vault):
    """Refuse vault history paths that would traverse symlinks or junctions."""
    root = history.history_root(vault)
    if is_link_or_junction(root):
        raise ValueError("Work History root must not be a symlink or junction")
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError("Work History path exists but is not a directory")
    for project in root.iterdir():
        if is_link_or_junction(project):
            raise ValueError(f"symlinked Work History entry rejected: {project.name}")
        marker = project / "project.json"
        if is_link_or_junction(marker):
            raise ValueError("symlinked project.json marker rejected")
        if not project.is_dir():
            continue
        for year in project.iterdir():
            if is_link_or_junction(year):
                raise ValueError(f"symlinked project entry rejected: {year.name}")
            if not year.is_dir():
                continue
            events = year / "Events"
            if is_link_or_junction(events):
                raise ValueError("symlinked Events directory rejected")
            if not events.is_dir():
                continue
            for day in events.iterdir():
                if is_link_or_junction(day):
                    raise ValueError(f"symlinked event day rejected: {day.name}")
                if not day.is_dir():
                    continue
                for event in day.iterdir():
                    if is_link_or_junction(event):
                        raise ValueError(
                            f"symlinked event bundle rejected: {event.name}")
                    if not event.is_dir():
                        continue
                    for name in ("event.json", "records.csv", "links.csv",
                                 "record.md", "original.md"):
                        if is_link_or_junction(event / name):
                            raise ValueError(
                                f"symlinked event file rejected: {name}")
                    artifacts = event / "artifacts"
                    if is_link_or_junction(artifacts):
                        raise ValueError("symlinked artifacts directory rejected")
                    if artifacts.is_dir():
                        for artifact in artifacts.iterdir():
                            if is_link_or_junction(artifact):
                                raise ValueError(
                                    "symlinked artifact file rejected")


def vault_event_index(vault):
    """{event_id: (bundle_dir, source_key_or_None)} for published bundles."""
    assert_safe_history_tree(vault)
    found = {}
    root = history.history_root(vault)
    if not root.exists():
        return found
    for folder in history.event_dirs(vault):
        event_id = folder.name
        key = None
        json_path = folder / "event.json"
        if json_path.is_file():
            try:
                key = json.loads(
                    json_path.read_text(encoding="utf-8")).get("source_key")
            except (OSError, ValueError):
                key = None
        if event_id in found:
            raise ValueError(f"duplicate event_id in vault history: {event_id}")
        found[event_id] = (folder, key)
    return found




# ---------- shared analysis pass ----------


def analyze_source(source_root):
    """Full read-only pass used by dry-run and import.

    Returns items: every non-pending event dir exactly once — either
    ``event`` (validated analysis) or ``error`` (fails on import and is
    also listed in ``corrupt``).
    """
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValueError(f"source root not found: {root}")
    classification = classify_source(root)
    if classification != "global_event_store":
        raise ValueError(
            f"source is {classification}, not a global event store "
            f"(project-local .history is read-only continuity evidence)")
    event_dirs, pending, corrupt, non_event = scan_source(root)
    items = []
    for rel, folder in event_dirs:
        try:
            items.append(dict(rel=rel, error=None,
                              event=analyze_event(root, rel, folder)))
        except EventProblem as error:
            corrupt[rel] = str(error)
            items.append(dict(rel=rel, error=str(error), event=None))
    for rel in corrupt:
        if not any(i["rel"] == rel for i in items):
            items.append(dict(rel=rel, error=corrupt[rel], event=None))
    return dict(root=root, items=items, pending=pending,
                corrupt=corrupt, non_event_entries=non_event)


# ---------- commands ----------


def cmd_inspect(args):
    path = Path(args.path)
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    classification = classify_source(path)
    result = dict(path=str(path.resolve()), classification=classification)
    if classification == "global_event_store":
        events, pending, corrupt, non_event = scan_source(path.resolve())
        result.update(events=len(events),
                      event_dirs=[rel for rel, _ in events],
                      pending=pending, corrupt=corrupt,
                      non_event_entries=non_event)
    elif classification == "project_local_continuity":
        events = path / "events"
        notes = [e.name for e in events.iterdir()
                 if e.is_file()] if events.is_dir() else []
        result.update(events=0, pending=[], corrupt=[],
                      non_event_entries=[], notes=notes,
                      note="read-only continuity evidence; never an "
                           "event-bundle import source")
    else:
        result.update(events=0, pending=[], corrupt=[],
                      non_event_entries=[])
    return result


def collision_check(analysis, mapping, vault):
    """Build one report entry per event dir; detect idempotent re-imports
    and event_id collisions.

    Returns (entries, unmapped, orphan_map_entries, collisions).
    """
    existing = vault_event_index(vault)
    claimed = {}  # event_id -> source_key claimed earlier in this run
    entries, unmapped, collisions = [], [], []
    for item in analysis["items"]:
        rel = item["rel"]
        if item["error"] is not None:
            entries.append(dict(event_dir=rel, event_id=None,
                                status="failed",
                                reason=item["error"]))
            continue
        event = item["event"]
        event_id = event["event_id"]
        key = source_key(analysis["root"], rel, event_id)
        entry = dict(
            event_dir=rel, event_id=event_id, source_key=key,
            secret_flag=event["secret_flag"],
            ambiguous=event["ambiguous"],
            excluded_artifacts=[dict(record_id=e["record_id"],
                                     role=e["role"],
                                     reason=e["reason"])
                                for e in event["excluded_artifacts"]],
            unknown_fields=sorted(
                event["unknown_fields"]["columns"]
                + event["unknown_fields"]["link_columns"]))
        project = mapping.get(rel)
        if project is None:
            entry.update(status="unmapped", project=None,
                         project_slug=None)
            unmapped.append(rel)
            entries.append(entry)
            continue
        entry.update(project=project,
                     project_slug=history.project_slug(project))
        if event_id in claimed and claimed[event_id] != key:
            entry["status"] = "conflict"
            collisions.append(dict(event_id=event_id, event_dir=rel,
                                   reason="duplicate source event_id "
                                          "under a different source_key"))
        elif event_id in existing and existing[event_id][1] != key:
            entry["status"] = "conflict"
            collisions.append(dict(event_id=event_id, event_dir=rel,
                                   reason="event_id exists in the vault "
                                          "with a different source_key; "
                                          "never silently remapped"))
        elif event_id in existing or event_id in claimed:
            entry["status"] = "already_imported"
        else:
            entry["status"] = "ready"
        claimed[event_id] = key
        entries.append(entry)
    present = {item["rel"] for item in analysis["items"]}
    orphan = sorted(k for k in mapping if k not in present)
    return entries, unmapped, orphan, collisions


def cmd_dry_run(args):
    if not getattr(args, "map", None):
        raise ValueError("dry-run requires an explicit --map JSON file")
    analysis = analyze_source(args.source)
    mapping = load_map(args.map)
    vault = history.resolve_vault(args.vault)
    entries, unmapped, orphan, collisions = collision_check(
        analysis, mapping, vault)
    result = dict(source_root=str(analysis["root"]),
                  classification="global_event_store", events=entries,
                  unmapped=unmapped, orphan_map_entries=orphan,
                  corrupt=analysis["corrupt"], pending=analysis["pending"],
                  non_event_entries=analysis["non_event_entries"],
                  collisions=collisions)
    soft = (unmapped or orphan or analysis["corrupt"] or collisions
            or any(e.get("secret_flag") or e.get("ambiguous")
                   or e.get("excluded_artifacts") for e in entries))
    print_json(result)
    return 1 if soft else 0


def build_event_json(event, entry, folder_name, zone_label, local_date,
                     stored_original_sha, stored_artifacts, excluded,
                     redactions, parents):
    record = event["event_row"]
    return dict(schema_version=history.SCHEMA_VERSION,
                event_id=event["event_id"], source_key=entry["source_key"],
                project=entry["project"], project_slug=folder_name,
                recorded_at=record["recorded_at"], local_date=local_date,
                timezone=zone_label, title=record.get("title") or "",
                kind=record.get("kind") or "",
                status=record.get("status") or "",
                tool=record.get("tool") or "",
                model=record.get("model") or "",
                version=record.get("version") or "",
                parents=parents,
                source_sha256=event["event_row"]["sha256"],
                redactions=redactions,
                excluded_artifacts=excluded, artifacts=stored_artifacts,
                migration=dict(
                    schema_version=MIGRATION_SCHEMA_VERSION,
                    source_event_dir=event["rel"],
                    source_files=event["source_files_safe"],
                    source_files_sha256=event["source_files_sha256"],
                    unknown_fields=event["unknown_fields"],
                    imported_at=datetime.now(timezone.utc).isoformat()))


def import_event(analysis, event, entry, vault, zone, zone_label):
    """Stage and atomically publish one migrated event bundle."""
    record = event["event_row"]
    parents = [l["source_id"] for l in event["links"]
               if l["relation"] == "precedes"
               and l["target_id"] == event["event_id"]]
    recorded = datetime.fromisoformat(record["recorded_at"])
    local = recorded.astimezone(zone)
    local_date = f"{local:%Y-%m-%d}"
    year = f"{local:%Y}"
    assert_safe_history_tree(vault)
    folder_name = (history.find_project_slug(history.history_root(vault),
                                             entry["project"])
                   or history.project_slug(entry["project"]))
    project_folder = history.history_root(vault) / folder_name
    destination = (project_folder / year / "Events" / local_date
                   / event["event_id"])
    history_root = history.history_root(vault)
    for candidate in (history_root, project_folder,
                      project_folder / year, project_folder / year / "Events",
                      destination.parent, destination):
        if is_link_or_junction(candidate):
            raise EventProblem("refusing symlinked or junctioned vault path")
    if destination.exists():
        raise EventProblem(f"destination already exists: {destination}")

    # Everything below happens in memory before ANY vault write: screening
    # failures, hash problems, and ambiguity leave the vault untouched.
    redactions = 0
    prepared = []
    excluded = []
    original_data = event["original_path"].read_bytes()
    original_text = original_data.decode("utf-8-sig")
    clean_text, count = screen_metadata_text(
        original_text, event["original_path"].name)
    redactions += count
    stored_original = clean_text.encode("utf-8")
    stored_original_sha = hashlib.sha256(stored_original).hexdigest()

    annotated_data = event["annotated_path"].read_bytes()
    annotated_text = annotated_data.decode("utf-8-sig")
    clean_annotated, count = screen_metadata_text(
        annotated_text, event["annotated_path"].name)
    redactions += count

    screened_rows = {}
    for row in event["rows"]:
        screened = dict(row)
        for field in ("title", "kind", "status", "tool", "model",
                      "version", "role", "source_path"):
            cleaned, count = screen_metadata_text(
                row.get(field) or "", f"record {row['record_id']} {field}")
            screened[field] = cleaned
            redactions += count
        screened_rows[row["record_id"]] = screened
    unknown_fields, count = sanitize_unknown_fields(event["unknown_fields"])
    redactions += count

    safe_rel, count = screen_metadata_text(
        event["rel"], "source event directory")
    redactions += count
    safe_source_files = {}
    for field, source_path in (
            ("records", event["records_path"]),
            ("links", event["links_path"]),
            ("annotated", event["annotated_path"]),
            ("original", event["original_path"])):
        safe_name, count = screen_metadata_text(
            source_path.name, f"source {field} filename")
        safe_source_files[field] = safe_name
        redactions += count

    safe_event = dict(event)
    safe_event.update(
        event_row=screened_rows[event["event_id"]],
        rows=[screened_rows[row["record_id"]] for row in event["rows"]],
        unknown_fields=unknown_fields,
        rel=safe_rel,
        source_files_safe=safe_source_files)

    snapshot_by_record = {row["record_id"]: (row, path, digest)
                          for row, path, digest in event["snapshots"]}
    stored_artifacts = []
    rows, links = [], []
    common = dict(schema_version=str(history.SCHEMA_VERSION),
                  project=entry["project"], source_key=entry["source_key"],
                  local_date=local_date, timezone=zone_label)
    for index, row in enumerate(safe_event["rows"], start=1):
        new_row = {field: row.get(field) or "" for field in UPSTREAM_FIELDS}
        new_row.update(common)
        if row.get("role") == "event":
            new_row["snapshot_path"] = history.vault_relative(
                destination / "original.md", vault)
            new_row["sha256"] = stored_original_sha
        elif any(e["record_id"] == row["record_id"]
                 for e in event["excluded_artifacts"]):
            # Exclusion rows carry no stored bytes: blank snapshot/sha256.
            new_row["snapshot_path"] = ""
            new_row["sha256"] = ""
        else:
            _row, source_file, _digest = snapshot_by_record[row["record_id"]]
            data = source_file.read_bytes()
            text = data.decode("utf-8-sig")
            clean, count = screen_metadata_text(text, source_file.name)
            redactions += count
            stored = clean.encode("utf-8")
            safe_name, count = screen_metadata_text(
                source_file.name, "artifact filename")
            redactions += count
            stem = history.slug(Path(safe_name).stem)
            suffix = Path(safe_name).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
                suffix = ""
            record_hash = hashlib.sha256(
                row["record_id"].encode("utf-8")).hexdigest()
            name = f"{index:04d}-{record_hash}-{stem}{suffix}"
            new_row["snapshot_path"] = history.vault_relative(
                destination / "artifacts" / name, vault)
            new_row["sha256"] = hashlib.sha256(stored).hexdigest()
            prepared.append((name, stored))
            stored_artifacts.append(dict(
                record_id=row["record_id"], role=row.get("role") or "",
                name=name, source_path=row.get("source_path") or "",
                sha256=new_row["sha256"],
                source_sha256=row.get("sha256") or ""))
        rows.append(new_row)
    for item in event["excluded_artifacts"]:
        row = screened_rows[item["record_id"]]
        excluded.append(dict(record_id=item["record_id"],
                             role=row.get("role") or "",
                             source_path=row.get("source_path") or "",
                             sha256=item["sha256"], reason=item["reason"]))
    links = [dict(source_id=l["source_id"], target_id=l["target_id"],
                  relation=l["relation"]) for l in event["links"]]

    missing_parents = []
    current = destination.parent
    while current != vault and not current.exists():
        missing_parents.append(current)
        current = current.parent
    stage = None
    renamed = False
    published = False
    marker = project_folder / "project.json"
    marker_created = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".pending-",
                                      dir=destination.parent))
        (stage / "record.md").write_bytes(clean_annotated.encode("utf-8"))
        (stage / "original.md").write_bytes(stored_original)
        if prepared:
            (stage / "artifacts").mkdir()
            for name, data in prepared:
                (stage / "artifacts" / name).write_bytes(data)
        history.write_csv(stage / "records.csv", history.RECORD_FIELDS, rows)
        history.write_csv(stage / "links.csv", history.LINK_FIELDS, links)
        (stage / "event.json").write_text(json.dumps(
            build_event_json(safe_event, entry, folder_name, zone_label,
                             local_date, stored_original_sha,
                             stored_artifacts, excluded, redactions,
                             parents),
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(stage, destination)
        stage = None
        renamed = True
        if not marker.exists():
            try:
                with marker.open("x", encoding="utf-8") as stream:
                    marker_created = True
                    stream.write(json.dumps(
                        dict(schema_version=history.SCHEMA_VERSION,
                             project=entry["project"], slug=folder_name),
                        ensure_ascii=False, indent=2) + "\n")
            except FileExistsError:
                existing = json.loads(marker.read_text(encoding="utf-8"))
                if existing.get("project") != entry["project"]:
                    raise EventProblem(
                        "project marker changed during publication")
        else:
            try:
                existing = json.loads(
                    marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise EventProblem(
                    "existing project marker cannot be verified") from error
            if existing.get("project") != entry["project"]:
                raise EventProblem(
                    "existing project marker conflicts with the mapping")
        published = True
        return dict(status="imported", destination=str(destination))
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        if not published:
            if marker_created and marker.exists():
                marker.unlink()
            if renamed and destination.exists():
                shutil.rmtree(destination)
            for parent in missing_parents:
                try:
                    parent.rmdir()
                except OSError:
                    pass



def cmd_import(args):
    if not args.approve:
        raise ValueError("import requires explicit --approve after "
                         "reviewing a dry-run report")
    if not getattr(args, "map", None):
        raise ValueError("import requires an explicit --map JSON file")
    analysis = analyze_source(args.source)
    mapping = load_map(args.map)
    vault = history.resolve_vault(args.vault)
    zone, zone_label = history.local_zone(args)
    entries, unmapped, _orphan, collisions = collision_check(
        analysis, mapping, vault)
    imported = already = failed = 0
    results = []
    by_rel = {item["rel"]: item["event"] for item in analysis["items"]}
    for entry in entries:
        status = entry["status"]
        if status == "already_imported":
            already += 1
            results.append(entry)
            continue
        if status in ("conflict", "unmapped", "failed"):
            failed += 1
            results.append(entry)
            continue
        event = by_rel[entry["event_dir"]]
        if event["ambiguous"]:
            entry.update(status="failed",
                         reason="ambiguous secret-like content; event "
                                "blocked for review")
            failed += 1
            results.append(entry)
            continue
        try:
            outcome = import_event(analysis, event, entry, vault,
                                   zone, zone_label)
            entry.update(outcome)
            imported += 1
        except (EventProblem, OSError, ValueError, UnicodeError) as error:
            entry.update(status="failed", reason=str(error))
            failed += 1
        results.append(entry)
    result = dict(source_root=str(analysis["root"]), imported=imported,
                  already_imported=already, failed=failed,
                  events=results, unmapped=unmapped,
                  corrupt=analysis["corrupt"], pending=analysis["pending"],
                  collisions=collisions)
    print_json(result)
    return 1 if (failed or unmapped or collisions
                 or analysis["corrupt"]) else 0


def add_vault_arguments(parser):
    parser.add_argument("--vault",
                        help="Obsidian vault root (else OBSIDIAN_VAULT_PATH, "
                             "else the documented default vault if it "
                             "exists)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser(
        "inspect",
        help="Classify and inventory a legacy store; strictly read-only")
    inspect.add_argument("path", help="Candidate legacy store directory")

    dry = commands.add_parser(
        "dry-run",
        help="Read-only migration preview: mapping, collisions, secret "
             "flags, unknown fields, corrupt and pending folders")
    dry.add_argument("source", help="Global legacy event store root "
                                    "(e.g. ~/.history)")
    add_vault_arguments(dry)
    dry.add_argument("--map", dest="map",
                     help="JSON file mapping each relative event dir to "
                          "its canonical project identity (required)")

    importer = commands.add_parser(
        "import",
        help="Publish approved legacy events into the vault; requires "
             "--approve")
    importer.add_argument("source", help="Global legacy event store root")
    add_vault_arguments(importer)
    importer.add_argument("--map", dest="map",
                          help="JSON file mapping each relative event dir "
                               "to its canonical project identity (required)")
    importer.add_argument("--approve", action="store_true",
                          help="Required: confirms the reviewed dry-run")
    importer.add_argument("--timezone",
                          help="IANA vault-local timezone, e.g. Asia/Seoul")
    importer.add_argument("--utc-offset", dest="utc_offset",
                          help="Fixed vault-local UTC offset; use = syntax, "
                               "e.g. --utc-offset=+09:00 or "
                               "--utc-offset=-04:00")

    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = cmd_inspect(args)
            print_json(result)
            return 0
        if args.command == "dry-run":
            return cmd_dry_run(args)
        return cmd_import(args)
    except (ValueError, OSError, UnicodeError, csv.Error) as error:
        print_json({"error": str(error)}, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
