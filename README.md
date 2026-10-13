# obsidian-work-history

AI 에이전트의 프로젝트 작업 이력, 대화에서 확인 가능한 요구사항, 실행 프롬프트·결과, 결정, 오류, 검증 및 버전 산출물을 **하나의 Obsidian 볼트**에 보존하는 Agent Skill과 Python CLI.

## 저장 모델

이벤트 번들이 유일한 원본이다. 날짜·프로젝트·전역 탐색 노트와 CSV는 번들에서 재생성되는 파생 뷰다.

```text
<Vault>/Work History/
  _index/
    projects.md
    projects.csv
    events.csv
  <project-slug>/
    project.json
    README.md
    <YYYY>/
      README.md
      <YYYY-MM-DD>.md
      Events/<YYYY-MM-DD>/<event_id>/
        record.md
        original.md
        records.csv
        links.csv
        event.json
        artifacts/
```

`recorded_at`은 UTC 인스턴트로 보존한다. 폴더의 날짜는 `--timezone` 또는 `--utc-offset`으로 계산한 볼트 로컬 날짜이며, 시간대가 없으면 `UTC`로 명확히 기록한다. 이벤트는 동일 파일시스템의 staging 디렉터리에서 완성한 뒤 원자적으로 게시한다.

## 설치 및 실행

Python 3.10 이상, 외부 패키지 없이 실행한다. 스킬을 쓰려면 이 저장소 전체(`SKILL.md`, `scripts/`, `references/`)를 호스트가 탐색하는 스킬 디렉터리의 `obsidian-work-history/` 아래에 둔다.

```text
python scripts/history.py record --vault <vault> --project "Example Project" \
  --title "초기 요구사항" --kind requirement --status received \
  --text-file request.md --tool omp
python scripts/history.py index --vault <vault>
python scripts/obsidian_index.py index --vault <vault>
```

첫 명령은 불변 이벤트 번들을 만들고 `event_id`를 JSON으로 반환한다. 후속 이벤트의 `--parent`에 그 ID를 전달해 프롬프트·결과·입력·산출물·재개 계보를 연결한다. 두 번째 명령은 CSV 스냅샷을, 세 번째는 프로젝트/연도/날짜 Markdown 뷰와 전역 탐색 인덱스를 갱신한다. 프로젝트/날짜 노트의 wikilink는 링크 라벨이 아니라 정규화한 목적지 기준으로 중복 제거된다.

## 레거시 이력 가져오기

새 기록은 Obsidian 볼트를 대상으로 한다. 과거의 `~/.history` 이벤트 저장소는 자동 이동하지 않는다. 가져오기는 명시적으로 인벤토리·dry-run을 검토하고 사용자가 요청한 뒤에만 `--approve`로 실행한다.

```text
python scripts/migrate.py inspect <legacy-path>
python scripts/migrate.py dry-run <legacy-root> --map <map.json> --vault <vault>
python scripts/migrate.py import <legacy-root> --map <map.json> --approve --vault <vault>
python scripts/obsidian_index.py index --vault <vault>
```

프로젝트 로컬 `.history/`는 관련 작업을 재개할 때 읽는 증거일 뿐, 가져오기 원본이나 쓰기 대상으로 취급하지 않는다. 세부 내용은 [마이그레이션 가이드](references/migration.md)를 따른다.

## 안전 및 한계

- 볼트 해석 순서: 명시적 `--vault`, `OBSIDIAN_VAULT_PATH`, 실제 존재가 확인된 기본값 `~/Documents/Obsidian Vault`. 해석 실패 시 어디에도 쓰지 않는다.
- 이벤트 작성과 인덱싱은 볼트 내부의 기존 심링크/Windows junction 경로를 거부한다.
- 알려진 텍스트 비밀 형식은 저장 전 가리고, 모호한 패턴은 해당 이벤트를 거부한다. 이는 완전한 비밀 스캔을 보장하지 않는다. 호출 전에도 비밀을 제외한다.
- 미검사 바이너리는 복사하지 않는다. 스냅샷 없는 출처 레코드와 `excluded_artifacts`로 누락이 아닌 제외임을 표시한다.
- 실패·중단은 새 상태 이벤트로 보존한다. 재개는 이전 `interrupted` 이벤트를 부모로 둔 새 `kind=resume` 이벤트이며 `resumed`라는 상태는 없다.
- 날짜/프로젝트 뷰는 생성기 소유 관리 블록만 갱신한다. 기존 프론트매터와 문구는 보존하고, 모호한 마커·경쟁 편집·경로 충돌이면 안전하게 중단한다.
- 이전 스킬이 만든 표시명 폴더는 이름이 프로젝트 정체성과 정확히 같고 `README.md`의 첫 제목이 `# <Project> — Work History`일 때만 재사용해 `project.json` 매핑으로 고정한다. 그 밖의 기존 폴더는 자동 이동·병합하지 않는다.
- 설치만으로 모든 대화를 자동 캡처하지 않는다. 이력은 감사용 출처 기록이지 QA, 승인, 수락의 증거가 아니다.

## 검증과 설계

```text
python -m unittest discover -s tests -v
```

테스트는 불변 이벤트·버전 바이트/해시·부모/아티팩트 관계·비밀 처리·바이너리 제외·볼트/경로 안전·legacy import/idempotency·UTC와 로컬 날짜 경계·동시 게시·wikilink 목적지 중복 제거·부분 실패를 검증한다.

시나리오 평가 절차와 synthetic fixture는 [evals/README.md](evals/README.md)에 있다. 실사용 Vault를 평가 입력으로 쓰지 않는다.

- [에이전트 절차](SKILL.md)
- [이벤트 형식과 CLI](references/record-format.md)
- [Obsidian 레이아웃 및 링크 규칙](references/obsidian-layout.md)
- [레거시 검사 및 마이그레이션](references/migration.md)
- [통합 설계와 QA 기록](docs/design.md)

이 저장소에는 `LICENSE`를 포함하지 않는다. 사용권이나 라이선스를 추정하지 않는다.
