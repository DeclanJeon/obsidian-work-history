# Skill evaluation protocol

All fixtures are synthetic and must be copied into isolated scratch directories before a run. Never point an eval at a real Obsidian vault or modify the fixture source. Run each prompt once without the skill and once with `SKILL.md` loaded; provide only that run's copied project/vault paths and environment facts. For the same-day cases, set `OBSIDIAN_WORK_HISTORY_FIXED_NOW=2026-10-13T12:00:00+00:00` in the isolated process so the existing date note is deterministic.

## Fixture setup

| Eval | Project input | Vault input | Environment |
|---|---|---|---|
| 1 — unresolved vault | `unknown-vault-project/` | none | Unset `OBSIDIAN_VAULT_PATH`; use an isolated home with no documented default vault. |
| 2 — interrupted resume | `legacy-project/` | copy `empty-vault/` | Set `OBSIDIAN_VAULT_PATH` to the copied empty vault; working directory is the copied project root. `outputs/demo_scene_animatic.mp4` is absent. |
| 3 — same-day preservation | `example-project/` | copy `same-day-vault/` | Explicitly select the copied vault; the source code file exists and `outputs/report.csv` does not. |
| 4 — partial index failure | none | copy `partial-index-vault/` | Project identity is `A`; existing mapping points to a `README.md/` directory, deliberately preventing view publication. |
| 5 — exact-target dedup | `example-project/` | copy `same-day-vault/` | Same fixed clock and vault setup as Eval 3; exact project/date destination already occurs in the project README. |

Eval 1 tests zero-write behavior. Evals 2–5 exercise the included CLI against isolated copies; compare file trees and inspect the exact target notes rather than relying only on the agent's narrative. Record published-event and generated-view state separately.

## Deterministic coverage

These permanent tests cover the non-LLM invariants used by the scenarios and the migration workflow:

- Exact wikilink destination deduplication: `test_existing_wikilink_destination_is_not_repeated`, `test_generated_content_dedupes_by_destination_not_line_text`, `test_reuses_legacy_project_folder_and_deduplicates_date_link`.
- UTC/local date boundary and project slug collision resistance: `test_utc_midnight_boundary_uses_vault_local_date`, `test_project_slug_is_stable_and_collision_resistant`.
- Legacy secret screening, unknown-field preservation, and binary exclusion: `test_import_secret_text_redacted_and_digest_preserved`, `test_import_preserves_unknown_fields_in_migration_metadata`, `test_import_binary_artifact_excluded_with_link`.
- Partial index publication and truthful state: `test_main_reports_partial_state_on_late_conflict`.
- Windows junction fallback and Vault write confinement: `test_windows_reparse_point_is_detected_without_is_junction`, `test_indexer_refuses_windows_junction_component`, `test_record_refuses_symlinked_work_history_root`, `test_index_refuses_symlinked_snapshot_root`.

Run the full suite from the repository root:

```text
python -m unittest discover -s tests -v
```

## Final paired evaluation results

All five prompts were rerun in fresh synthetic scratch copies, once without the skill and once with `SKILL.md`. Baseline runs did not load the skill or call its scripts. Skill runs used the actual CLI/indexer whenever a Vault resolved; Eval 1 correctly stopped before any CLI write. Scratch results stayed outside this repository.

| Eval | Baseline (no skill) | With `obsidian-work-history` |
|---|---|---|
| 1 — unresolved vault | Stopped with zero history writes; absent `src/theme.py` remained unverified. | Same safe stop; reported checked locations and marked the file user-reported/unverified. |
| 2 — interrupted resume | Wrote a note only in the isolated vault; preserved `.history` and did not verify the absent MP4. | Published a `received` event plus both indexes; preserved/cited the interrupted source and did not treat local IDs as Vault parents. |
| 3 — same-day preservation | Updated the existing date note; preserved prior content and avoided a second date note or absent CSV claim. | Reused the exact legacy project folder, published an event, and refreshed CSV/Obsidian indexes while preserving prior content. |
| 4 — partial index failure | Made no event/index writes because no work-note contents were supplied; left the README directory conflict unchanged. | Published the event and CSV snapshot; Obsidian view generation refused the injected README-directory conflict, preserved it, and reported stale/partial views with recovery guidance. |
| 5 — exact-target dedup | Added context to the existing date note; the project README destination remained exactly once. | Published the event and both indexes; retained exactly one normalized README destination and preserved existing date-note content. |

The Eval 4 view failure is the expected injected conflict, not a passing index refresh. The acceptance result is truthful separation of event/CSV publication from failed Obsidian views. Deterministic regression tests cover the underlying file and link invariants listed above.
