# 레거시 검사와 마이그레이션 (`migrate.py`)

레거시 저장소는 두 가지 형태가 있으며 목적이 다르다. **`inspect`와 `dry-run`은 어디에도 쓰지 않는다.** `import`는 검토된 dry-run에 대한 명시적 `--approve`가 있어야만 볼트에 쓴다. 어떤 명령도 소스 루트를 변경하지 않는다 — 소스는 항상 바이트 단위로 동일하게 남는다.

## 두 레거시 형태의 분류

| 형태 | 모양 | 처리 |
|---|---|---|
| 글로벌 이벤트 저장소 (`~/.history`) | `events/<stem>/` 아래 `<stem>_records.csv`, `<stem>_links.csv`, `<stem>.md`, `<stem>_원문.md`, `artifacts/` | 유일한 번들 가져오기 소스 |
| 프로젝트 로컬 `.history/` | `events/` 아래 평탄한 `NNNNNN-*.md` 노트, `STATUS_SNAPSHOT.md`, `prompts/` | 읽기 전용 연속성 증거. 번들 가져오기 소스가 **아니며** dry-run/import는 오류로 거부한다 |

형태는 폴더 이름이 아니라 **내용**으로 판별한다. `events/` 아래 번들형 디렉터리가 있으면 글로벌 저장소, 평탄한 `.md` 노트/`STATUS_SNAPSHOT.md`/`prompts/`만 있으면 프로젝트 로컬 연속성이다. `.pending-*` 디렉터리는 어느 수준에서든 미게시 스테이징으로 보고되며 절대 가져오지 않는다.

## CLI

```text
python scripts/migrate.py inspect PATH

python scripts/migrate.py dry-run SOURCE_ROOT --map MAP.json [--vault PATH]

python scripts/migrate.py import SOURCE_ROOT --map MAP.json --approve
  [--vault PATH] [--timezone NAME | --utc-offset=±HH:MM]
```

- `inspect PATH`: 분류(`global_event_store` / `project_local_continuity` / `unrecognized`), 이벤트 디렉터리 목록, `.pending-*`, 손상된 폴더, 비이벤트 항목을 보고한다.
- `dry-run`: 이벤트별 프로젝트 매핑·slug, 매핑 누락(`unmapped`)과 고아 매핑 키(`orphan_map_entries`), event_id 충돌(`collisions`), 비밀 패턴 플래그(**값은 절대 출력하지 않음**), 제외 예정 아티팩트, 미지원 CSV 필드, 손상/불완전 폴더, pending 폴더를 보고한다. 소스와 볼트 모두 읽기 전용이다.
- `import`: 승인된 이벤트만 Task 1 번들 형식으로 원자적으로 게시한다. 성공/스킵/실패를 이벤트별로 사실대로 보고한다.

종료 코드: `0` 발견 사항 없음/모두 처리, `1` 부분 실패 또는 dry-run 발견 사항(이벤트별 `status`가 진실), `2` 하드 오류(인자, 읽을 수 없는 소스, 잘못된 소스 형태, vault 미해석).

## 프로젝트 매핑 (`--map`)

JSON 객체: 각 **상대 이벤트 디렉터리** → 정규 프로젝트 식별자.

```json
{
  "events/054535882370_2026-10-08_히스토리 스킬 요구사항_bc3591...": "PonsLink",
  "events/054556303621_2026-10-08_히스토리 스킬 최종 산출물_a946...": "second_brains"
}
```

한 소스 루트의 이벤트가 서로 다른 프로젝트에 매핑될 수 있다. 매핑 키에 `..`, 절대 경로, 드라이브 문자, 백슬래시가 있으면 거부한다. 값은 비어 있지 않은 프로젝트 식별자 문자열이어야 한다(`{"project": "..."}` 형태의 객체도 허용). 매핑되지 않은 이벤트는 가져오지 않고 `unmapped`로 보고한다.

## 보존과 무결성 규칙

- **ID/링크 보존**: 소스 `event_id`, `record_id`, `links.csv` 관계(`precedes`/`used`/`produced`)를 그대로 유지한다. `used`/`produced`는 이벤트 → 이 이벤트의 record_id여야 하고 `precedes`는 이 이벤트를 한쪽 끝으로 포함해야 한다. 알 수 없는 record_id를 가리키는 링크는 그 이벤트를 실패시킨다.
- **멱등성**: `source_key = legacy-history:sha256(source_root + rel_dir + event_id)`. 같은 `source_key`의 기존 번들은 `already_imported` 스킵. 다른 `source_key`로 같은 `event_id`가 존재하면 **충돌** — 절대 조용히 재매핑하지 않는다.
- **경로 안전**: `snapshot_path`는 소스 루트 아래 POSIX 상대 경로여야 한다. `..`, 절대/드라이브 경로, 백슬래시, 심링크 구성요소는 거부한다.
- **해시 검증**: CSV의 `sha256`이 소스 스냅샷 파일의 실제 다이제스트와 다르면 그 이벤트는 손상으로 실패한다. 빈 `snapshot_path` 행은 소스 다이제스트 검증이 불가능하므로 실패한다(업스트림은 항상 스냅샷을 쓴다).
- **날짜 경로**: `local_date`는 소스 `recorded_at`(UTC 인스턴트 보존)을 `--timezone`/`--utc-offset`으로 변환해 계산한다. 기본은 `UTC` 레이블 — 날짜 경계를 넘는 추측은 하지 않는다.
- **미지원 필드**: `records.csv`/`links.csv`의 알 수 없는 컬럼과 행 값은 `event.json.migration.unknown_fields`에 보존한다(`schema_version` 포함). 값이 행 수보다 짧으면 나머지는 `null`로 보존된다. 보존할 수 없는 손상은 그 이벤트를 실패시킨다 — 보고만 하고 버리지 않는다.

## 텍스트 검사와 바이너리 제외

- 주석 `<stem>.md`(→ `record.md`), 원문 `<stem>_원문.md`(→ `original.md`), UTF-8 아티팩트는 `history.screen_text`로 게시 전 검사한다. 인식된 비밀 값은 `[REDACTED]`로 치환한다. **모호한 내용(BEGIN/END 불일치 등)은 그 이벤트 전체를 실패시키고 아무것도 쓰지 않는다.**
- `records.csv`의 `sha256`은 **저장된**(치환된) 바이트 기준이다. 원본 소스 다이제스트는 `event.json.source_sha256`/`artifacts[].source_sha256`에 출처로 남는다.
- NUL 바이트를 포함하거나 UTF-8이 아닌 아티팩트는 바이트를 복사하지 않고 Task 1/2 호환 제외 레코드로 남긴다: `records.csv` 행(빈 `snapshot_path`/`sha256`, 소스 `record_id`/`role`/`source_path` 보존) + `event.json.excluded_artifacts[]`의 `{record_id, role, source_path, sha256(소스 다이제스트), reason}` + 기존 `used`/`produced` 링크 유지. 인덱서가 불완전 출처로 렌더링한다.

## 게시 원자성과 볼트 쓰기

- 이벤트당 완전한 번들을 대상 날짜 폴더 안의 `.pending-*` 스테이징 디렉터리에 완성한 뒤 `os.rename`으로 게시한다. 실패한 이벤트는 스테이징을 정리하고 볼트에 아무것도 남기지 않는다.
- 게시 성공한 뒤에만 `<slug>/project.json` 매핑 마커를 쓴다(한 번만; 기존 매핑 재사용).
- `import` 보고서의 `imported`/`already_imported`/`failed`와 이벤트별 `status`가 진실이다 — 부분 실패를 성공으로 보고하지 않는다.
- 마이그레이션은 Obsidian 뷰를 재생성하지 않는다. 가져오기 후 `python scripts/obsidian_index.py index --vault PATH`를 실행한다.
