# 기록 형식과 CLI

스키마 버전: **1**. 이벤트 번들은 불변의 원본 기록이며 날짜별 Obsidian 노트와 인덱스는 파생 뷰다.

## 원문 MD 작성

아래 항목 중 실제 존재하는 항목을 채운다. 전문은 중략 없이 옮긴다. 사용자 메시지와 AI가 재작성한 실행 프롬프트를 구분한다. 도구 응답이 길면 별도 MD로 저장하고 `--document`로 등록한다.

```markdown
# 기록 제목

## 원래 발생 시각
unknown 또는 확인된 ISO 8601 시각

## 사용자 요구사항 / 메시지 원문
실제로 받은 메시지 전문

## 실행 프롬프트 원문
실제로 전달한 프롬프트 전문

## 실행 설정과 출처
호스트, 모델, 매개변수, 입력 파일, 외부 작업 ID 및 URL

## 공개된 판단과 변경 이유
선택한 방식과 이유, 이전 버전과의 차이

## 응답 / 도구 결과 원문
관찰한 결과 또는 오류 전문

## 검증과 남은 일
실제 실행한 검증 결과, 누락된 데이터와 이유
```

이 MD는 도구가 바이트 그대로 `original.md`에 보존한다. 별도의 주석 `record.md`에는 메타데이터·부모 ID와 내용을 넣는다. CLI는 비밀값을 자동 탐지하거나 지우지 않는다. **호출 전에 원문과 첨부 파일에서 비밀값을 제외한다.**

## CLI

```text
python scripts/history.py record
  --project IDENTITY --title TEXT --kind KIND --status STATUS --text-file FILE
  [--vault PATH] [--timezone NAME | --utc-offset ±HH:MM]
  [--tool TEXT] [--model TEXT] [--version TEXT]
  [--parent EVENT_ID ...] [--input FILE ...] [--document FILE ...] [--output FILE ...]

python scripts/history.py index [--vault PATH] [--project IDENTITY]
```

반복 옵션은 `--parent ID1 --parent ID2`처럼 옵션 자체를 반복한다. 경로에 공백이 있으면 따옴표로 감싼다. 종류와 상태의 허용값은 `record --help`로 조회한다.

## 저장소(Vault) 해석

새 기록의 대상은 Obsidian 볼트다. 해석 순서:

1. `--vault PATH` (`~` 확장)
2. `OBSIDIAN_VAULT_PATH`
3. 문서화된 기본값 `~/Documents/Obsidian Vault` — **디렉터리가 실제 존재할 때만**

해석에 실패하면 **아무것도 쓰지 않고** 종료 코드 2로 오류를 보고한다. `~/.history`·프로젝트 디렉터리·추측 경로로의 폴백은 없다. 레거시 저장소는 별도의 명시적 가져오기 절차로만 읽는다.

## 이벤트 메타데이터 (schema_version 1)

- `project`(필수): 프로젝트의 정규 식별자. 폴더명으로는 Unicode NFKC 정규화, 경로 금지 문자 치환, 가독 부분 48자 제한, 정규 식별자 SHA-256 앞 10자리 접미사 규칙의 **slug**를 쓴다. 동일 식별자는 볼트에 기록된 매핑(`<slug>/project.json`)을 재사용하며 표시 이름이 바뀌어도 기존 매핑을 바꾸지 않는다.
- `source_key`: 이벤트의 안정된 출처 키. 새 기록에서는 `event_id`와 동일하다.
- `recorded_at`: UTC ISO 8601 기록 시각(마이크로초). 전체 시간순 정렬 기준.
- `local_date`/`timezone`: 볼트 로컬 날짜와 그 기준 시간대. `--timezone`은 IANA 이름(예: `Asia/Seoul`), `--utc-offset`은 고정 오프셋(예: `+09:00`, `-04:00`)을 받는다. 둘 다 없으면 명확히 `UTC`로 표기된 UTC 날짜를 쓰며 시간대를 추측하지 않는다.
- `kind`: `resume`을 포함한 기존 열거값 유지. 재개는 새 상태가 아니라 이전 `interrupted` 이벤트에 `precedes`로 연결된 `kind=resume` + 유효 상태(`started`/`completed`/`failed`)의 새 이벤트다.
- `--input`/`--document`/`--output`, `--version`, `--parent` 의미는 업스트림과 동일하다.

## 저장 구조

```text
<Vault>/Work History/
  <project-slug>/
    project.json                         # {"schema_version", "project", "slug"}
    <YYYY>/
      Events/
        <YYYY-MM-DD>/                    # 볼트 로컬 날짜
          <event_id>/
            record.md                    # 주석 메타데이터 + 내용
            original.md                  # 원문 바이트 스냅샷
            records.csv
            links.csv
            event.json                   # 기계 판독용 이벤트 메타데이터
            artifacts/
              <event_id>_<role>_<원래제목>_<파일ID>.<원래확장자>
  _index/
    snapshots/<stem>_records.csv         # index 명령의 조회용 스냅샷
    snapshots/<stem>_links.csv
```

이벤트는 대상 날짜 폴더 안의 `.pending-*` 임시 디렉터리에 완성한 뒤 같은 파일시스템 내 rename으로 게시한다. 게시 실패나 중단으로 남은 `.pending-*`는 미게시 기록이며 `index`에 포함되지 않고 자동 완료·삭제하지 않는다. `snapshot_path`는 볼트 루트 기준 POSIX 상대 경로다.

`index`는 게시 완료 이벤트를 모아 `Work History/_index/snapshots/` 아래 새 CSV 스냅샷을 원자적으로 쓴다. 병렬 작업이 진행 중이면 이후 게시 이벤트는 빠질 수 있으므로 납품 직전 모든 writer가 끝난 뒤 다시 실행한다. 날짜별/프로젝트 Obsidian 뷰 생성은 별도 단계가 담당한다.

## CSV 스키마 (schema_version 1)

CSV는 UTF-8 BOM, 표준 CSV quoting을 사용한다. 업스트림 13개 컬럼을 그대로 유지하고 끝에 5개 컬럼을 추가했다.

### records.csv

| 컬럼 | 의미 |
|---|---|
| record_id | 이벤트 또는 파일의 고유 ID |
| event_id | 파일이 속한 이벤트 ID |
| recorded_at | UTC ISO 8601 기록 시각 |
| title | 이벤트 원래 제목 |
| kind | 요구사항·프롬프트·응답·실행·산출물·resume 등의 종류 |
| status | received / started / completed / failed / interrupted |
| tool, model | 실제 확인한 도구와 모델; 모르면 빈 값 |
| version | 명시한 결과물 버전; 비산출물 이벤트는 빈 값 가능 |
| role | event / input / document / output |
| source_path | 원문 MD 또는 원본 파일의 당시 절대 경로 |
| snapshot_path | 보존 파일의 볼트 상대 POSIX 경로 |
| sha256 | 실제 보존한 파일 바이트의 SHA-256 |
| schema_version | 행 스키마 버전 (`1`) |
| project | 정규 프로젝트 식별자 |
| source_key | 이벤트 출처 키 (새 기록에서는 event_id와 동일) |
| local_date | 볼트 로컬 날짜 YYYY-MM-DD |
| timezone | local_date 산출에 쓴 시간대 (`UTC`, IANA 이름 또는 오프셋) |

이벤트 행의 `snapshot_path`와 해시는 `record.md`가 아니라 `original.md`를 가리킨다.

### links.csv

| 컬럼 | 의미 |
|---|---|
| source_id | 관계 출발 ID |
| target_id | 관계 도착 ID |
| relation | precedes: 이전 이벤트 → 다음 이벤트; used: 이벤트 → 입력/문서; produced: 이벤트 → 산출물 |

## 조회·보안

CSV를 스프레드시트에서 열 때 외부 입력에 `=`, `+`, `-`, `@`로 시작하는 값이 있으면 수식으로 해석될 수 있다. 원문 CSV를 보존하면서 가져오기 옵션에서 **모든 컬럼을 텍스트로 지정**한다. 비밀 자료를 담지 않고 외부 전송 없이 로컬 저장한다. 볼트 내 `Work History/`의 공개 Git 커밋·공유 여부는 사용자가 결정한다.
