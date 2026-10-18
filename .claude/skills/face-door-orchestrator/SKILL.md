---
name: face-door-orchestrator
description: 라즈베리파이5/우분투24 얼굴인식 도어(솔밸브) 제어 시스템 개발 전체를 조율하는 오케스트레이터. 얼굴인식 도어 프로그램 개발, 등록/인식/GPIO/LED/평가 모듈 구현, 하드웨어 연결 준비, 인식률 평가 리포트 작성 요청 시 반드시 사용한다. 다시 실행, 재실행, 업데이트, 수정, 보완, 일부만 다시, 이전 결과 개선, 임계값/성능 개선, 마스크·안경·조명 대응 개선 요청에도 사용한다.
---

# 얼굴인식 도어 시스템 오케스트레이터

**실행 모드: 혼합** — 1단계 서브에이전트, 2단계 지속형 에이전트 협업, 3단계 QA 점진 검증. 처리 목록이 고정된 대량 작업이 없어 `Workflow`는 쓰지 않는다.

## 0단계: 컨텍스트 확인
- `src/facedoor/`, `docs/ARCHITECTURE.md`, `_workspace/`가 없다 → 처음부터 실행.
- 있고 부분 수정 요청(예: "마스크 인식률 개선", "LED 핀 변경") → 해당 에이전트만 재호출. 담당: 인식 로직=`vision-engineer`, GPIO/배포=`hw-engineer`, 지표/리포트/임계값=`eval-engineer`, 구조=`face-architect`.
- `_workspace/`가 있고 새 요구사항 → `_workspace_<타임스탬프>/`로 이동 후 새로 실행.

## 1단계: 설계 (서브에이전트)
`Agent(subagent_type: "face-architect")`에 사용자 요구를 전달한다. 산출: `docs/ARCHITECTURE.md`, `docs/INTERFACES.md`, `configs/default.yaml`, 프로젝트 뼈대(`pyproject.toml`/`requirements*.txt`, 패키지 디렉터리). 이 계약이 있어야 2단계 병렬 구현이 맞물린다. 완료 후 요약을 사용자에게 보여 주고 핵심 선택(백엔드, 핀, 임계값 정책)이 의도에 맞는지 확인받는다.

## 2단계: 병렬 구현 (지속형 에이전트)
한 메시지에서 병렬 실행:
- `Agent(name: "vision", subagent_type: "vision-engineer", prompt: 계약 문서 경로 + 담당 모듈)`
- `Agent(name: "hw", subagent_type: "hw-engineer", ...)`
- `Agent(name: "eval", subagent_type: "eval-engineer", ...)`

`TaskCreate`로 의존 관계를 등록한다: hw의 `DoorController` 인터페이스 → vision의 `decision` 연결; vision의 점수 노출 → eval의 `scripts/evaluate.py`. 인터페이스 이견은 `SendMessage`로 조율하고 `face-architect`가 최종 판단한다.

## 3단계: 점진 QA
모듈 하나가 끝날 때마다 `qa-inspector`(subagent_type, 모든 도구 사용 가능)를 호출해 경계면·안전·이식성을 검증한다. 전체 완료 후 통합 시나리오를 한 번 더 돌린다:
1. mock 모드 + `FileCamera`로 등록 → 인식 → 솔밸브 신호/LED 상태 로그 확인.
2. Unknown·HOLD에서 open 미호출 확인.
3. `scripts/evaluate.py`가 샘플 데이터로 리포트를 생성.
결함은 담당 에이전트에게 돌려보내 재검증한다(최대 2회, 이후 사용자에게 보고).

## 4단계: 보고
- 사용자에게: 실행 방법(PC mock / Pi 실기), 평가 리포트 위치, 미검증 항목(실카메라, 실GPIO, 실데이터), 위조 방어 한계, 배선 안전 주의.
- 사용자가 직접 해야 하는 일: 본인 얼굴/조건별 사진 촬영, Pi 배선, `hardware.mode: pi` 전환.

## 데이터 전달
- 계약·설계: 파일(`docs/`, `configs/`). 진행 상태: `TaskCreate/TaskUpdate`. 에이전트 간 협의: `SendMessage`. QA 결과: `_workspace/qa_<module>_report.md`. `_workspace/`는 보존한다.

## 오류 처리
- 에이전트 실패 → 1회 재시도, 재실패 시 해당 모듈 없이 진행하고 보고서에 누락 명시.
- 응답 없는 지속형 에이전트 → `SendMessage`로 상태 확인, 불가하면 같은 유형을 새 이름으로 실행하고 맥락을 프롬프트로 전달.
- 패키지 설치/모델 다운로드 실패 → 추측으로 채우지 않고 "미검증"으로 기록, 사용자에게 수동 명령 안내.
- 개인정보: 얼굴 이미지/임베딩은 `data/`에만 저장하고 `.gitignore`에 포함한다.

## 테스트 시나리오
- **정상**: "등록자 1명 + 합성 조건 probe로 평가 리포트까지" → 설계 → 3모듈 병렬 → QA 통과 → mock에서 OPEN 로그와 `reports/*/report.html` 생성.
- **오류**: 카메라 열기 실패 시 출력 OFF 유지·명확한 오류 종료 / `mode: pi`를 PC에서 지정 시 오류 종료 → QA가 테스트로 확인.
- **후속**: "마스크 FRR이 높아" → eval이 조건별 표로 원인 확인 → vision이 마스크 태그 등록·임계값 분리 → eval 재평가로 전후 비교.
