# obsidian-work-history

omp·Codex·Claude 등 파일 접근이 가능한 AI 에이전트의 제작 과정과 버전별 결과물을 **Obsidian 볼트**에 불변 이벤트로 보존하는 Agent Skill.

| 저장 대상 | 형식 |
|---|---|
| 요구사항·사용자 메시지·실행 프롬프트·응답·판단 근거·오류·검증 결과 | `original.md` 원문 스냅샷 + `record.md` 메타데이터 |
| 기록 ↔ 입력 자료 ↔ 산출물, 이전 단계 ↔ 다음 단계 | `records.csv` / `links.csv` + `event.json` |
| 원본 문서·코드·이미지·영상·데이터, 중간/최종 결과 | `artifacts/`의 원래 형식 파일 스냅샷 |


각 이벤트는 `<Vault>/Work History/<project-slug>/<YYYY>/Events/<YYYY-MM-DD>/<event_id>/` 아래 완성된 번들로 원자적으로 게시된다. `recorded_at`은 UTC, 날짜 경로는 볼트 로컬 시간대(`--timezone`/`--utc-offset=±HH:MM`, 미지정 시 명확한 `UTC`)를 사용한다. 텍스트는 저장 전 알려진 비밀 형식을 `[REDACTED]`로 치환하고 모호한 내용은 거부한다. 미검사 바이너리 아티팩트는 복사하지 않고 `excluded_artifacts` + 스냅샷 없는 `records.csv` 행과 `links.csv` 관계로 명시한다.

1. `--vault PATH`
2. `OBSIDIAN_VAULT_PATH`
3. `~/Documents/Obsidian Vault` — 존재할 때만

해석 실패 시 아무것도 쓰지 않는다. `~/.history` 폴백은 없으며 레거시 저장소는 명시적 가져오기 절차로만 다룬다.

## 설치

`SKILL.md`, `scripts/`, `references/`를 포함한 이 폴더 전체를 호스트가 탐색하는 스킬 디렉터리의 `obsidian-work-history/` 아래에 둔다.

## 직접 실행

Python 3.10 이상, 외부 의존성 없음.

```text
python scripts/history.py record --vault <볼트> --project "내 프로젝트" \
  --title "초기 요구사항" --kind requirement --status received \
  --text-file requirements.md --tool omp
python scripts/history.py index --vault <볼트>
```

반환 JSON의 `event_id`를 다음 호출의 `--parent`로 사용한다. 원문 MD·문서의 비밀값은 호출 전에 제거한다. 상세 스키마와 실패·재개 규칙은 [기록 형식](references/record-format.md), 에이전트 절차는 [스킬 본문](SKILL.md)을 참고한다. 통합 설계 근거는 [설계 문서](docs/design.md)에 있다.

## 확인된 검증

```text
python -m unittest discover -s tests -v
```

회귀 시나리오: 버전별 바이트·해시·출처 연결 보존, 잘못된 입력의 게시 거부, 동일 제목 충돌과 중단 staging 제외, 볼트 해석 순서와 `~` 확장, 누락 볼트의 무기록 실패, slug 안정성·충돌 저항, UTC 자정 경계의 볼트 로컬 날짜, `.pending-*` 미게시 제외, resume 링크 이벤트.

## 제한과 운영

- 스킬은 요청된 워크플로다. 설치만으로 모든 메시지가 자동 캡처되지는 않는다.
- 이벤트 번들은 관례상 불변이며 권한 기반 WORM 저장소는 아니다.
- `Work History/`는 개인정보·업무 원문을 포함할 수 있다. 공개 커밋·공유는 사용자가 결정한다.
- 라이선스: 두 원본 저장소에 라이선스 메타데이터가 없었으며 사용자 승인 전까지 LICENSE 파일을 추가하지 않는다.
