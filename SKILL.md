---
name: obsidian-work-history
description: "Use when recording, organizing, finding, or resuming AI project work in an Obsidian vault, including requirements, prompts, decisions, tool results, failures, validation, versioned artifacts, and delivery history."
compatibility: "Python 3.10+ standard library for the included CLI; file and command access required."
---

# Obsidian Work History

## Purpose and boundaries

Keep one traceable project history in the Obsidian vault: immutable event bundles are the source of truth; project, year, and date notes are generated navigation. A history entry is evidence, not proof of QA, approval, or acceptance.

Use this skill when the user asks to log/archive work, resume an interrupted operation, find prior work, or when a substantial project task reaches a meaningful checkpoint. Skip greetings, trivial chat, and unrelated Q&A. Installation does not capture every conversation automatically.

Record only information available in the current session or explicitly supplied sources. Never collect hidden reasoning, system/developer instructions, credentials, or unavailable content. Label each claim as observed, user-reported, inferred, intended, or verified. A user statement alone is not verification.

## 1. Resolve the vault before writing

Use this order:

1. Vault path explicitly supplied by the user (`--vault`).
2. `OBSIDIAN_VAULT_PATH`.
3. The documented default `~/Documents/Obsidian Vault`, only if it exists.

If no vault resolves, stop before creating any file. Report the locations checked and missing configuration. Never fall back to `~/.history`, the project directory, a guessed path, or another user's hard-coded path. State the resolved destination before recording.

Read vault-root instructions and relevant templates. Inspect `Work History/` for an existing project identity before recording. The CLI reuses a `project.json` mapping and can adopt the prior skill's exact display-name folder only when its README heading is `# <Project> — Work History`; other unmarked folders are not silently remapped. Do not rename or overwrite legacy notes. If the folder identity is ambiguous, report the conflict instead of creating a competing project history.

## 2. Capture an event and its provenance

Resolve the vault first, then write a UTF-8 temporary Markdown source containing the accessible user request, relevant execution prompt and options, observed response/tool results, decisions, errors, validation, and known gaps. Preserve exact source text only when available, relevant, permitted by vault rules, and screened for secrets. Distinguish the user's request from any prompt rewritten for a generation tool. See [record-format.md](references/record-format.md) for the source template and schema.

Create the event with `scripts/history.py`:

```text
python <skill-path>/scripts/history.py record --vault <vault> \
  --project "<canonical project identity>" \
  --title "<short factual title>" --kind requirement --status received \
  --text-file <temporary-source.md> --tool <observed tool>
```

Use `record --help` for the supported `kind`/`status` values and optional `--model`, `--version`, `--input`, `--document`, `--output`, and repeated `--parent` flags.

- Record a meaningful step as its own event. Use the returned `event_id` as `--parent` on the next event to preserve sequence and provenance.
- Attach existing inputs/documents with `--input`/`--document`. Attach an output with `--output` only after confirming the file exists. When recording outputs, provide the actual version with `--version`; never invent version numbers.
- Use observed tool/model values; leave unknown values blank. The CLI redacts supported secret patterns and refuses ambiguous text, but this is not a complete secret scanner. Screen the content before calling it.
- Never copy unscanned binary data. The CLI records an explicit exclusion and source digest without claiming that bytes were preserved.
- Preserve interruptions. A resumed operation is a new `kind=resume` event with a valid status (`started`, `completed`, or `failed`) and the prior `interrupted` event as parent. `resumed` is not a valid status. Record completion only with observed evidence.
- If the only evidence of a resumed attempt or completion is the user's statement, do not invent a started/completed resume event. Record that statement as received/user-reported, link it to the interrupted event, and leave the operation unverified until observable evidence is available.
- If the vault timezone is documented, pass `--timezone` (IANA) or `--utc-offset=±HH:MM`. Otherwise keep the CLI's explicitly labeled `UTC` date; do not guess across midnight.

## 3. Generate the project/date history

After publishing the event or a batch of events, refresh the event CSV snapshot and Obsidian views:

```text
python <skill-path>/scripts/history.py index --vault <vault>
python <skill-path>/scripts/obsidian_index.py index --vault <vault>
```

`history.py index` writes query snapshots. `obsidian_index.py index` generates the global project index, per-project and per-year navigation, and one date note for each project/date. See [obsidian-layout.md](references/obsidian-layout.md).

The generated project and date notes are views, not the canonical record. Do not edit their managed blocks by hand. The indexer preserves unmanaged frontmatter/prose, serializes writers, and aborts on ambiguous markers, path conflicts, or concurrent edits. Re-run it only after inspecting the reported conflict and current files.

Wikilink identity is its normalized destination, not its alias or surrounding bullet text. The indexer checks preserved prose and managed content together; if a destination already exists, it emits context without another link. For any vault-convention-required manual backlink outside generated views, scan the entire target note for the normalized destination before adding it. Do not add a second link because the new activity text differs.

## 4. Read or continue legacy history

Treat `~/.history/` and project-local `.history/` as read-only unless the user explicitly requests migration.

- Project-local `.history/` is continuity evidence only. Read only the relevant operation's status and events, cite the source paths in the new event, preserve the interrupted state, and do not append, delete, or bulk-import it. Its local event IDs are not valid `--parent` values unless a matching event bundle already exists in this vault.
- `~/.history/` event bundles can be imported only after the user requests migration, `inspect` and a mapped `dry-run` are reviewed, and the user explicitly approves import. `import` requires `--approve`; never infer approval from a general logging request.
- Never import `.pending-*` staging directories. Unmapped, corrupt, secret-bearing, or unsupported records remain unimported and are reported.

Read [migration.md](references/migration.md) before running legacy commands. A successful import does not rebuild views; run `obsidian_index.py index` afterward.

## 5. Verify and report honestly

Before reporting completion:

1. Confirm the returned event ID, event path, project identity, local date/timezone, parent IDs, and attached/excluded artifacts.
2. Confirm both index commands' results. Re-read/list the generated paths; verify project/date placement, preserved prose, and no duplicate wikilink destinations.
3. If event publication succeeded but index generation failed, report the event as published and the views as stale/partial. Name the exact files written, conflicts, and recovery action. Do not claim “fully logged.”
4. Report the actual vault path, event/index paths, versioned outputs, exclusions, redactions, missing evidence, and blockers. Do not claim that a history entry proves successful QA or acceptance.

Snapshots are conventionally immutable, not protected by WORM storage. The indexer’s managed-block and conflict guarantees are documented in [obsidian-layout.md](references/obsidian-layout.md).
