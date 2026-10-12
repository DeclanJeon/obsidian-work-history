"""Immutable project work-history events in an Obsidian vault.

Python 3.10+, standard library only. Events publish as complete bundles under
``<vault>/Work History/<project-slug>/<YYYY>/Events/<YYYY-MM-DD>/<event_id>/``
via a same-filesystem stage directory and one atomic rename.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata
import uuid

SCHEMA_VERSION = 1

RECORD_FIELDS = ["record_id", "event_id", "recorded_at", "title", "kind", "status",
                 "tool", "model", "version", "role", "source_path", "snapshot_path",
                 "sha256",
                 # Schema version 1 additions (appended; upstream fields unchanged).
                 "schema_version", "project", "source_key", "local_date", "timezone"]
LINK_FIELDS = ["source_id", "target_id", "relation"]

HISTORY_DIRNAME = "Work History"
DEFAULT_VAULT = Path("Documents") / "Obsidian Vault"
# Deterministic clock hook for tests; an ISO 8601 instant. Not a public feature.
FIXED_NOW_ENV = "OBSIDIAN_WORK_HISTORY_FIXED_NOW"


def now_utc():
    fixed = os.environ.get(FIXED_NOW_ENV)
    if fixed:
        try:
            instant = datetime.fromisoformat(fixed)
        except ValueError:
            raise ValueError(f"invalid {FIXED_NOW_ENV}: {fixed!r}")
        if instant.tzinfo is None:
            raise ValueError(f"{FIXED_NOW_ENV} must include a UTC offset: {fixed!r}")
        return instant.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def slug(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    if not value:
        raise ValueError("title must contain a usable filename character")
    return value[:60]


SECRET_RULES = [
    ("github-token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("api-token", re.compile(r"\b(?:sk|pk|api|key|token|secret|password)[-_][A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-token", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("database-url", re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|"
                               r"redis|amqps?)://[^\s'\"]*:[^\s'\"]*@[^\s'\"]+")),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
                               r"-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)),
]
PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def screen_text(text, source):
    """Redact recognizable secret values; fail closed on ambiguous content.

    Best-effort screening of supported text only — never claims exhaustive
    scanning. Returns (sanitized_text, redaction_count).
    """
    begins = len(PRIVATE_KEY_MARKER.findall(text))
    blocks = len(SECRET_RULES[-1][1].findall(text))
    if begins != blocks:
        raise ValueError(
            f"ambiguous secret-like content in {source}; record refused for review")
    sanitized, total = text, 0
    for _label, pattern in SECRET_RULES:
        def repl(match):
            prefix = match.group(1) if match.lastindex else ""
            return prefix + "[REDACTED]"
        sanitized, count = pattern.subn(repl, sanitized)
        total += count
    return sanitized, total


def looks_binary(raw):
    return b"\x00" in raw[:8192]

def project_slug(identity):
    """Stable, collision-resistant folder name for a canonical project identity."""
    canonical = unicodedata.normalize("NFKC", identity).strip()
    if not canonical:
        raise ValueError("project identity must not be empty")
    readable = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", canonical)
    readable = re.sub(r"\s+", " ", readable).strip(" .")
    if not readable:
        readable = "project"
    readable = readable[:48].strip(" .-")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def find_project_slug(history_root, identity):
    """Reuse the folder already recorded for this exact project identity."""
    canonical = unicodedata.normalize("NFKC", identity).strip()
    if not history_root.exists():
        return None
    for folder in sorted(history_root.iterdir()):
        marker = folder / "project.json"
        if not (folder.is_dir() and marker.is_file()):
            continue
        try:
            stored = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if stored.get("project") == canonical or stored.get("project") == identity:
            return folder.name
    return None


def local_zone(args):
    """Resolve the vault-local timezone. Default is a clearly labeled UTC date."""
    if getattr(args, "timezone", None):
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(args.timezone), args.timezone
        except Exception as error:
            raise ValueError(f"unknown timezone: {args.timezone}") from error
    offset = getattr(args, "utc_offset", None)
    if offset is not None:
        match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", offset)
        if not match:
            raise ValueError(f"--utc-offset must look like --utc-offset=+09:00, got {offset!r}")
        sign, hours, minutes = match.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        if sign == "-":
            delta = -delta
        label = f"{sign}{hours}:{minutes}"
        return timezone(delta, name=label), label
    return timezone.utc, "UTC"


def resolve_vault(explicit):
    """Resolve the vault; missing/unresolved vault means zero writes.

    Order: explicit --vault; OBSIDIAN_VAULT_PATH; the documented default, only
    if it already exists. There is deliberately no ~/.history fallback.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not explicit and env:
        candidates.append(Path(env).expanduser())
    if not explicit and not env:
        candidates.append(Path.home() / DEFAULT_VAULT)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved
        source = "--vault" if explicit else (
            "OBSIDIAN_VAULT_PATH" if env else "default vault path")
        raise ValueError(f"vault not found ({source}): {resolved}")
    raise ValueError("no vault resolved")


def history_root(vault):
    return vault / HISTORY_DIRNAME


def write_csv(path, fields, rows):
    # BOM enables Korean text in Excel. csv handles embedded newlines and quotes.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def event_dirs(vault, project_folder=None):
    root = vault / HISTORY_DIRNAME
    if not root.exists():
        return
    if project_folder:
        projects = [root / project_folder]
    else:
        projects = sorted(p for p in root.iterdir()
                          if p.is_dir() and not p.name.startswith((".", "_")))
    for project in projects:
        for events in sorted(project.glob("*/Events/*")):
            if not events.is_dir():
                continue
            for folder in sorted(events.iterdir()):
                # .pending-* stage folders are not published events.
                if folder.is_dir() and not folder.name.startswith("."):
                    yield folder


def load_events(vault, project_folder=None):
    rows, links = [], []
    for folder in event_dirs(vault, project_folder):
        for name, target in (("records.csv", rows), ("links.csv", links)):
            file = folder / name
            if not file.is_file():
                raise ValueError(f"incomplete event: {folder}")
            with file.open(encoding="utf-8-sig", newline="") as stream:
                target.extend(csv.DictReader(stream))
    rows.sort(key=lambda row: (row["recorded_at"], row["event_id"], row["record_id"]))
    return rows, links


def known_event_ids(vault):
    return {folder.name for folder in event_dirs(vault)}


def vault_relative(path, vault):
    return path.relative_to(vault).as_posix()

def record(args):
    vault = resolve_vault(args.vault)
    zone, zone_label = local_zone(args)
    text_path = Path(args.text_file).resolve(strict=True)
    raw_bytes = text_path.read_bytes()
    text, redactions = screen_text(raw_bytes.decode("utf-8-sig"), text_path)
    stored_bytes = text.encode("utf-8")
    files, excluded = [], []
    prepared = []
    for role in ("input", "document", "output"):
        for source in getattr(args, role):
            path = Path(source).resolve(strict=True)
            if not path.is_file():
                raise ValueError(f"artifact must be a regular file: {path}")
            files.append((role, path))
    if args.output and not args.version:
        raise ValueError("--version is required when recording outputs")
    for role, path in files:
        source_bytes = path.read_bytes()
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        if looks_binary(source_bytes):
            excluded.append(dict(role=role, source_path=str(path),
                                 sha256=source_sha,
                                 reason="binary artifact excluded: no approved local "
                                        "scanner; bytes not copied"))
            continue
        try:
            artifact_text = source_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            excluded.append(dict(role=role, source_path=str(path),
                                 sha256=source_sha,
                                 reason="artifact is not supported UTF-8 text; "
                                        "bytes not copied"))
            continue
        artifact_text, count = screen_text(artifact_text, path)
        redactions += count
        prepared.append((role, path, source_sha,
                         artifact_text.encode("utf-8")))

    root = history_root(vault)
    folder_name = find_project_slug(root, args.project) or project_slug(args.project)
    event_id = uuid.uuid4().hex
    recorded_at = now_utc()
    local = recorded_at.astimezone(zone)
    local_date = f"{local:%Y-%m-%d}"
    project_folder = root / folder_name
    destination = project_folder / f"{local:%Y}" / "Events" / local_date / event_id

    for parent in dict.fromkeys(args.parent):
        if parent not in known_event_ids(vault):
            raise ValueError(f"unknown parent event: {parent}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=destination.parent))
    try:
        rows, links, artifacts = [], [], []
        common = dict(recorded_at=recorded_at.isoformat(), title=args.title,
                      kind=args.kind, status=args.status, tool=args.tool,
                      model=args.model, version=args.version,
                      schema_version=str(SCHEMA_VERSION), project=args.project,
                      source_key=event_id, local_date=local_date, timezone=zone_label)
        # Preserve only the sanitized text; secret-bearing source bytes are
        # never staged or published.
        (stage / "original.md").write_bytes(stored_bytes)
        metadata = {
            "event_id": event_id, "source_key": event_id,
            "schema_version": SCHEMA_VERSION,
            "project": args.project, "project_slug": folder_name,
            "recorded_at": recorded_at.isoformat(),
            "local_date": local_date, "timezone": zone_label,
            "title": args.title, "kind": args.kind, "status": args.status,
            "tool": args.tool, "model": args.model, "version": args.version,
        }
        lines = [f"- {key}: {json.dumps(value, ensure_ascii=False)}"
                 for key, value in metadata.items()]
        note = (f"\n- redactions: {redactions}"
                f"\n- excluded_artifacts: {len(excluded)}")
        (stage / "record.md").write_text(
            "# 작업 기록\n\n" + "\n".join(lines) + note +
            f"\n- parents: {json.dumps(args.parent)}\n"
            f"- exact_text_snapshot: original.md\n\n## 기록 내용\n\n{text}",
            encoding="utf-8")
        rows.append(dict(common, event_id=event_id, record_id=event_id, role="event",
                         source_path=str(text_path),
                         snapshot_path=vault_relative(destination / "original.md", vault),
                         sha256=hashlib.sha256(stored_bytes).hexdigest()))
        for parent in dict.fromkeys(args.parent):
            links.append(dict(source_id=parent, target_id=event_id, relation="precedes"))
        if prepared:
            (stage / "artifacts").mkdir()
        for role, source, source_sha, sanitized in prepared:
            artifact_id = uuid.uuid4().hex
            # Include the source extension; stored bytes are sanitized UTF-8.
            name = f"{event_id}_{role}_{slug(source.stem)}_{artifact_id}{source.suffix}"
            target = stage / "artifacts" / name
            target.write_bytes(sanitized)
            stored_sha = hashlib.sha256(sanitized).hexdigest()
            rows.append(dict(common, event_id=event_id, record_id=artifact_id,
                             role=role, source_path=str(source),
                             snapshot_path=vault_relative(
                                 destination / "artifacts" / name, vault),
                             sha256=stored_sha))
            artifacts.append(dict(record_id=artifact_id, role=role, name=name,
                                  source_path=str(source),
                                  sha256=stored_sha,
                                  source_sha256=source_sha))
            links.append(dict(source_id=event_id, target_id=artifact_id,
                              relation="produced" if role == "output" else "used"))
        write_csv(stage / "records.csv", RECORD_FIELDS, rows)
        write_csv(stage / "links.csv", LINK_FIELDS, links)
        (stage / "event.json").write_text(json.dumps(
            dict(schema_version=SCHEMA_VERSION, event_id=event_id,
                 source_key=event_id, project=args.project, project_slug=folder_name,
                 recorded_at=recorded_at.isoformat(), local_date=local_date,
                 timezone=zone_label, title=args.title, kind=args.kind,
                 status=args.status, tool=args.tool, model=args.model,
                 version=args.version, parents=args.parent,
                 source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                 redactions=redactions, excluded_artifacts=excluded,
                 artifacts=artifacts),
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Record the identity→folder mapping once; never silently remap later.
        marker = project_folder / "project.json"
        if not marker.exists():
            project_folder.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(
                dict(schema_version=SCHEMA_VERSION, project=args.project,
                     slug=folder_name), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        # Publish only complete events. Unique IDs avoid shared append files.
        os.rename(stage, destination)
        return dict(event_id=event_id, source_key=event_id,
                    event_path=str(destination),
                    record_path=str(destination / "record.md"),
                    recorded_at=recorded_at.isoformat(),
                    local_date=local_date, timezone=zone_label,
                    project=args.project, project_slug=folder_name,
                    records=len(rows))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def index(args):
    vault = resolve_vault(args.vault)
    folder = find_project_slug(history_root(vault), args.project) if args.project else None
    if args.project and folder is None:
        raise ValueError(f"unknown project: {args.project}")
    rows, links = load_events(vault, folder)
    stem = f"{now_utc():%H%M%S%f_%Y-%m-%d}_전체-히스토리_{uuid.uuid4().hex}"
    target_root = vault / HISTORY_DIRNAME / "_index" / "snapshots"
    target_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pending-index-", dir=target_root))
    try:
        write_csv(stage / (stem + "_records.csv"), RECORD_FIELDS, rows)
        write_csv(stage / (stem + "_links.csv"), LINK_FIELDS, links)
        destination = target_root / stem
        os.rename(stage, destination)
        return dict(records_csv=str(destination / (stem + "_records.csv")),
                    links_csv=str(destination / (stem + "_links.csv")),
                    records=len(rows), links=len(links))
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def add_vault_arguments(parser):
    parser.add_argument("--vault",
                        help="Obsidian vault root (else OBSIDIAN_VAULT_PATH, else "
                             "the documented default vault if it exists)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("record", help="Snapshot text and files as an immutable event")
    add_vault_arguments(add)
    add.add_argument("--project", required=True,
                     help="Canonical project identity; mapped to a stable slug folder")
    add.add_argument("--timezone", help="IANA vault-local timezone, e.g. Asia/Seoul")
    add.add_argument("--utc-offset", dest="utc_offset",
                     help="Fixed vault-local UTC offset; use = syntax, e.g. "
                          "--utc-offset=+09:00 or --utc-offset=-04:00")
    add.add_argument("--title", required=True)
    add.add_argument("--kind", required=True, choices=["requirement", "prompt", "response", "decision",
                                                       "execution", "artifact", "validation", "feedback", "delivery", "resume"])
    add.add_argument("--status", required=True, choices=["received", "started", "completed", "failed", "interrupted"])
    add.add_argument("--text-file", required=True)
    for option in ("tool", "model", "version"):
        add.add_argument("--" + option, default="")
    for option in ("parent", "input", "document", "output"):
        add.add_argument("--" + option, action="append", default=[])
    lookup = commands.add_parser("index", help="Write a new consolidated CSV snapshot")
    add_vault_arguments(lookup)
    lookup.add_argument("--project", help="Limit the snapshot to one project identity")
    args = parser.parse_args()
    try:
        result = record(args) if args.command == "record" else index(args)
    except (ValueError, OSError, UnicodeError, csv.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
