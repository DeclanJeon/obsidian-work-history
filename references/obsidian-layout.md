# Obsidian 뷰 생성 (`obsidian_index.py`)

이벤트 번들(불변 원본)만 읽어 날짜·프로젝트·전역 Obsidian 뷰를 다시 생성한다. 뷰는 언제든 전체를 재생성할 수 있는 **파생 데이터**다. 원본은 각 이벤트 번들이며 뷰를 편집해 기록을 바꾸지 않는다.

## CLI

```text
python scripts/obsidian_index.py index [--vault PATH] [--project IDENTITY]
```

- 성공 시 stdout에 JSON(`events`, `projects`, `updated[]`, `_index` 경로). 실패 시 stderr에 `{"error": ...}`와 종료 코드 2.
- `--project`는 그 프로젝트의 노트만 재생성한다. `_index/` 전역 파일은 항상 **모든** 프로젝트를 반영한다.
- 볼트 해석은 `history.py`와 동일(`--vault` → `OBSIDIAN_VAULT_PATH` → 존재하는 문서화된 기본값). 해석 실패 시 아무것도 쓰지 않는다.

## 생성되는 파일

```text
<Vault>/Work History/
  <slug>/
    README.md                    # 프로젝트 정체 + 연도/날짜 탐색
    <YYYY>/
      README.md                  # 연도별 날짜 목록
      <YYYY-MM-DD>.md            # 그 날짜의 이벤트 목록
      Events/<YYYY-MM-DD>/<event_id>/   # 불변 이벤트 번들(읽기 전용)
  _index/
    projects.md                  # 전역 프로젝트 탐색
    projects.csv                 # project, slug, event_count, first/last_local_date
    events.csv                   # 전 이벤트 records.csv 행 통합(RECORD_FIELDS 그대로)
    .index-write.lock            # 생성 중에만 존재하는 직렬화 락
```

날짜 노트의 각 이벤트 항목은 `recorded_at`(UTC 그대로)과 `local_date`(볼트 로컬 날짜)를 표시한다. 날짜 경계 근처 이벤트는 UTC 날짜가 아니라 번들에 기록된 `local_date` 아래에 놓이며 시간대를 다시 계산하지 않는다.

## 관리 블록(managed block) 계약

모든 생성 노트는 정확한 마커로 생성 구간을 표시한다:

```markdown
<!-- obsidian-work-history:managed:start -->
(재생성되는 목록)
<!-- obsidian-work-history:managed:end -->
```

- 마커가 없으면 기존 내용(프론트매터·본문)을 그대로 두고 파일 끝에 관리 블록 **하나**를 추가한다.
- 마커가 정확히 한 쌍이면 그 사이만 교체하고 바깥 텍스트는 바이트 그대로 보존한다.
- 마커가 엇갈리거나(`end`가 `start`보다 앞), 한쪽만 있거나, 두 쌍 이상이면 **모호함으로 간주해 그 노트를 건드리지 않고 실행 전체를 실패 처리**한다.

## 위키링크 대상 중복 제거

Obsidian에서 링크의 정체는 `[[대상]]`이지 주변 문장이 아니다. 병합된 노트 전체(기존 보존 문구 + 새 관리 블록)에서 대상을 정규화해(끝의 `.md`와 `Work History/` 접두어 제거) 같은 대상은 **최대 한 번만** 링크한다. 이미 보존 문구에 있는 대상이면 생성 항목은 링크 대신 평문 라벨만 남긴다 — 새 문구는 살리되 목적지는 반복하지 않는다.

## 실패 안전

- 모든 인덱스 쓰기는 `Work History/_index/.index-write.lock`의 배타 락 아래에서 계획(scan 포함)→커밋한다. 마지막으로 실행한 작성자는 그 시점까지 게시된 모든 이벤트를 본다 — 동시 `record`+인덱스에서 이벤트가 빠지지 않는다.
- 락 소유권: 각 홀더는 고유 토큰+PID를 락 파일에 기록한다. 기존 락은 **절대 자동 회수하지 않는다** — 비교·삭제를 원자적으로 만들 수 없어 경쟁 회수가 새 소유자의 락을 지울 수 있으므로, 소유자가 죽었더라도 대기하다가 시간 제한 후 실패한다. 고아 락은 소유 프로세스가 없음을 확인한 뒤 **수동으로만** 삭제한다(오류 메시지에 기록된 소유자 PID와 경과 시간을 보고한다). 해제 시에는 파일 바이트가 우리가 쓴 토큰과 같을 때만 삭제한다.
- 커밋은 전부 아니면 전무다: 모든 대상의 SHA-256을 쓰기 전에 먼저 전부 비교(preflight)하고 하나라도 다르면 **아무것도 쓰지 않고** `IndexConflict`로 실패한다. `atomic_write`는 임시 파일을 쓴 뒤 `os.replace` 직전에 해시를 **다시** 비교해 그 사이의 경쟁 쓰기도 잡는다 — 감지되면 그 대상은 덮지 않고 실행을 멈춘다. 충돌 시 CLI 오류 JSON은 `error`와 함께 볼트 상대 경로의 `conflicts`(거부된 대상)·`written`(이미 게시된 대상) 목록을 보고해 부분 상태를 정확히 설명할 수 있게 한다.
- 실제 쓰기는 같은 디렉터리 임시 파일 + `os.replace`(원자 교체)다. `.pending-*`·`.tmp-index-*`는 어떤 노트에도 나타나지 않는다.
- 심링크된 경로(볼트 아래 모든 구성요소와 대상 파일 자체), `..`/절대/드라이브 문자를 가진 `snapshot_path`, 불완전 번들(`records.csv`/`links.csv` 누락), 연도 폴더와 `local_date` 연도 불일치, 존재하지 않는 달력 날짜(예: `2026-02-30`), `event_id`와 경로의 불일치는 모두 실패 폐쇄다.

## 제외 아티팩트(불완전 출처) 렌더링

`records.csv`에서 `snapshot_path`·`sha256`이 빈 **비-이벤트 행**은 저장 스냅샷이 없는 제외 아티팩트다. `record_id`로 `event.json.excluded_artifacts[]`와 **일대일**로 대응해야 하며, 항목의 `role`·`source_path`·소스 `sha256` 다이제스트·비어 있지 않은 `reason`이 행과 모두 일치해야 한다. 다음은 모두 실패 폐쇄다(인덱스 출력 없음): 일치 항목 부재, `event.json` 부재, `record_id`/`role`/`source_path` 불일치, `sha256`/`reason` 누락·빈 값, 행의 CSV `sha256`가 빈 값이 아님, 스냅샷이 있는 행에 제외 메타데이터가 붙음, `excluded_artifacts`가 리스트가 아니거나 항목이 dict가 아님, 레코드 행 없는 제외 항목. 일치하면 날짜 노트에 링크 없이 `- excluded <role>: \`파일명\` — <사유> — source sha256 <접두>` 형태로 명시한다. `events.csv`의 해당 행도 빈 `snapshot_path`/`sha256`를 유지한다. **파일이 없다는 오류가 아니라, 바이트가 보존되지 않았다는 출처 정보다.**
