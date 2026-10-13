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


def classify_source(path):
    """Classify a legacy store without modifying it.

    Returns global_event_store | project_local_continuity | unrecognized.
    """
    path = Path(path)
    if not path.is_dir():
        return "unrecognized"
    events = path / "events"
    if events.is_dir():
        has_bundle_dirs = any(e.is_dir() and not e.name.startswith(".")
                              for e in events.iterdir())
        has_notes = any(e.is_file() and e.suffix == ".md"
                        for e in events.iterdir())
        if has_bundle_dirs:
            return "global_event_store"
        if has_notes or (path / "STATUS_SNAPSHOT.md").is_file() \
                or (path / "prompts").is_dir():
            return "project_local_continuity"
    if (path / "STATUS_SNAPSHOT.md").is_file() or (path / "prompts").is_dir():
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
    if not events_root.is_dir():
        return events, pending, corrupt, non_event
    for entry in sorted(events_root.iterdir()):
        rel = entry.relative_to(root).as_posix()
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
        if current.is_symlink():
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


def read_csv_strict(path, rel):
    """Parse one source CSV. Raises EventProblem on unreadable/short rows."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            fields = reader.fieldnames or []
    except (OSError, UnicodeError, csv.Error) as error:
        raise EventProblem(f"unreadable {rel}: {error}") from error
    if any(None in row for row in rows):
        raise EventProblem(f"{rel} has ragged rows (extra unnamed fields)")
    return fields, rows


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
        if candidate.is_symlink():
            raise EventProblem(f"refusing symlinked file: {candidate}")

    fields, rows = read_csv_strict(records_path, "records.csv")
    link_fields, links = read_csv_strict(links_path, "links.csv")
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
    for row in rows:
        if row.get("event_id") != event_id:
            raise EventProblem(
                f"row {row.get('record_id')} event_id "
                f"{row.get('event_id')!r} disagrees with {event_id!r}")
        if not row.get("record_id"):
            raise EventProblem(f"row in {rel} has empty record_id")
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
        mapping[pure.as_posix()] = value
    return mapping


# ---------- vault side ----------


def vault_event_index(vault):
    """{event_id: (bundle_dir, source_key_or_None)} for published bundles."""
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
    print(json.dumps(result, ensure_ascii=False))
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
                    source_files=dict(
                        records=event["records_path"].name,
                        links=event["links_path"].name,
                        annotated=event["annotated_path"].name,
                        original=event["original_path"].name),
                    original_recorded_at=record["recorded_at"],
                    unknown_fields=event["unknown_fields"],
                    imported_at=datetime.now(timezone.utc).isoformat()))


def import_event(analysis, event, entry, vault, zone, zone_label):
    """Stage and atomically publish one migrated event bundle."""
    root = analysis["root"]
    record = event["event_row"]
    parents = [l["source_id"] for l in event["links"]
               if l["relation"] == "precedes"
               and l["target_id"] == event["event_id"]]
    recorded = datetime.fromisoformat(record["recorded_at"])
    local = recorded.astimezone(zone)
    local_date = f"{local:%Y-%m-%d}"
    year = f"{local:%Y}"
    folder_name = (history.find_project_slug(history.history_root(vault),
                                             entry["project"])
                   or history.project_slug(entry["project"]))
    project_folder = history.history_root(vault) / folder_name
    destination = (project_folder / year / "Events" / local_date
                   / event["event_id"])
    if destination.exists():
        raise EventProblem(f"destination already exists: {destination}")

    # Everything below happens in memory before ANY vault write: screening
    # failures, hash problems, and ambiguity leave the vault untouched.
    redactions = 0
    prepared = []
    excluded = []
    original_data = event["original_path"].read_bytes()
    original_text = original_data.decode("utf-8-sig")
    clean_text, count = history.screen_text(original_text,
                                            event["original_path"])
    redactions += count
    stored_original = clean_text.encode("utf-8")
    stored_original_sha = hashlib.sha256(stored_original).hexdigest()

    annotated_data = event["annotated_path"].read_bytes()
    annotated_text = annotated_data.decode("utf-8-sig")
    clean_annotated, count = history.screen_text(annotated_text,
                                                 event["annotated_path"])
    redactions += count

    snapshot_by_record = {row["record_id"]: (row, path, digest)
                          for row, path, digest in event["snapshots"]}
    stored_artifacts = []
    rows, links = [], []
    common = dict(schema_version=str(history.SCHEMA_VERSION),
                  project=entry["project"], source_key=entry["source_key"],
                  local_date=local_date, timezone=zone_label)
    for row in event["rows"]:
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
            clean, count = history.screen_text(text, source_file)
            redactions += count
            stored = clean.encode("utf-8")
            name = source_file.name
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
        excluded.append(dict(record_id=item["record_id"], role=item["role"],
                             source_path=item["source_path"],
                             sha256=item["sha256"], reason=item["reason"]))
    links = [dict(source_id=l["source_id"], target_id=l["target_id"],
                  relation=l["relation"]) for l in event["links"]]

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pending-",
                                  dir=destination.parent))
    try:
        (stage / "record.md").write_bytes(clean_annotated.encode("utf-8"))
        (stage / "original.md").write_bytes(stored_original)
        if prepared:
            (stage / "artifacts").mkdir()
            for name, data in prepared:
                (stage / "artifacts" / name).write_bytes(data)
        history.write_csv(stage / "records.csv", history.RECORD_FIELDS, rows)
        history.write_csv(stage / "links.csv", history.LINK_FIELDS, links)
        (stage / "event.json").write_text(json.dumps(
            build_event_json(event, entry, folder_name, zone_label,
                             local_date, stored_original_sha,
                             stored_artifacts, excluded, redactions,
                             parents),
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(stage, destination)
        marker = project_folder / "project.json"
        if not marker.exists():
            marker.write_text(json.dumps(
                dict(schema_version=history.SCHEMA_VERSION,
                     project=entry["project"], slug=folder_name),
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return dict(status="imported", destination=str(destination))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


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
    print(json.dumps(result, ensure_ascii=False))
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
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "dry-run":
            return cmd_dry_run(args)
        return cmd_import(args)
    except (ValueError, OSError, UnicodeError, csv.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
