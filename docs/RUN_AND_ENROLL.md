# 등록과 실행 (vision 사용법)

계약은 `docs/INTERFACES.md` §3.12, §3.13, §9. 모든 명령은 프로젝트 루트에서 `.venv` 파이썬으로 실행한다.

## 1. 모델 준비
```
python scripts/download_models.py          # models/ 에 YuNet, SFace + sha256 확인
```

## 2. 등록 `scripts/enroll.py`
한 사람당 조건 태그별로 여러 장을 등록한다(조건 다양성이 조명/마스크/안경/목도리 강건성의 1차 수단).
```
python scripts/enroll.py --name alice --tag normal --display-name 앨리스   # 웹캠, 포즈 안내(정면/좌/우/위/아래)
python scripts/enroll.py --name alice --tag mask
python scripts/enroll.py --name alice --tag glasses --images D:/photos/alice_glasses   # 폴더 또는 glob
python scripts/enroll.py --name alice --tag lowlight --count 6 --no-display
python scripts/enroll.py --name alice --prune-outliers      # 이상치 샘플 삭제(없으면 보고만)
python scripts/enroll.py --reembed                          # 임베더/모델 변경 후 갤러리 재임베딩
```
- `--count N`: 라이브는 태그당 총 샘플 수(포즈에 나눠 배분), `--images`는 사용할 파일 수 상한.
- 품질 미달(작음/흐림/어두움/가장자리), 얼굴 없음/여러 개, 직전 샘플과 중복은 자동 거부되고 이유별 개수가 출력된다.
- 종료 코드: 0 정상, 2 인자/설정 오류, 4 모델, 6 갤러리(임베더 불일치 → `--reembed`).

## 3. 실행 `scripts/run.py` (= `python -m facedoor`, `pip install -e .` 후)
```
python scripts/run.py                                       # 웹캠 0, PC는 mock GPIO, 화면 표시
python scripts/run.py --no-display --tag mask               # 헤드리스, events.csv condition_tag=mask
python scripts/run.py --source data/probe/normal/alice --no-display --max-frames 50   # 녹화/이미지 폴더 재생
python scripts/run.py --set hardware.mode=pi --no-display   # Pi 실기 (보통은 configs/local.yaml)
```
- 화면: 박스(초록 MATCH 이름+점수/임계값, 빨강 Unknown, 노랑 HOLD 이유), 우상단 DOOR SIGNAL ON/OFF, 가상 LED G/R/A, 상태(OPEN/DENIED/HOLD/...), FPS, 임계값 출처. `q`/ESC 종료.
- 판정: 최근 5 처리 프레임 중 같은 사람 MATCH 3회 → `door.open("face:<id>:<score>:<votes>/<window>")` 1회, 이후 `rearm_sec` 동안 재판정 없음.
- 로그: `logs/events.csv`(33컬럼, 평가용), `logs/hardware.log`(출력 전이), `logs/facedoor.log`.
- 종료 코드(`ExitCode`): 0 정상/입력 끝/q, 2 설정, 3 카메라, 4 모델, 5 하드웨어, 6 갤러리, 130 SIGINT/SIGTERM.
  어떤 경로든 종료 전에 문 닫기 → LED 끄기 → `hardware.cleanup()`이 실행된다.
- `runtime.max_fps`는 실시간 카메라에만 적용(파일 재생은 `camera.file_fps`).
- `lockers.require_locker_id=true`면 `person.json`에 `locker_id`가 없는 사람은 인식돼도 열지 않는다(로그 message에 기록).

### 3.1 인식 실행창과 사물함 패널 (v1.5, INTERFACES §15.4·§16·§17)
- `display.backend`: `auto`(tkinter 가능하면 `TkDisplay` 한글 창, 아니면 경고 후 영문 OpenCV 창) / `tk`(불가 시 오류 종료 1) / `opencv` / `--no-display`는 창 없음.
- `TkDisplay` 레이아웃: 왼쪽 웹캠(얼굴 사각형만), 오른쪽 위에서부터 사물함 패널(큰 배지 열림/닫힘/불명 = 리밋센서, 아래에 잠김/해제·카운트다운·센서 오류), 영상 아래 상태줄(인식 문구·임계값 출처·FPS). 글꼴은 `display.font_families`(Malgun Gothic → NanumGothic → Noto Sans CJK KR; Windows 한글판의 "맑은 고딕" 이름도 인식, Pi는 `sudo apt install fonts-nanum`).
- 시뮬레이션(mock이고 `limits.is_simulated`일 때만): 패널 클릭 또는 키 `1`~`4` = 해당 사물함 문 열기/닫기 토글, 키 `5`~`8` = 사물함 1~4 센서 읽기 실패 토글. 실기에서는 클릭·키·안내 줄 모두 무시된다. OpenCV 폴백에서는 키만 동작한다.
- 앱 동작: 인식 OPEN → 승자의 사물함에 `door.open(reason, locker_id=L)`. 거부(`door_open`/`sensor_fault`/`busy`/`cooldown`)는 events.csv `message`의 `(rejected by door: <code>)`와 상태줄 문구로 표시. 매 `step()`에서 `door.drain_events()`를 `door_opened`/`door_close`(close_cause)/`door_forced`/`door_sensor` 행으로 기록하고, events.csv 37번째 컬럼 `locker_states`에 사물함 상태를 남긴다.
- `logs/door_state.json`(schema_version 2): 상태 변화 또는 0.5초마다 갱신. 종료 시 모든 행을 `UNKNOWN`으로 한 번 더 써서 관리 화면이 "닫힘"으로 오인하지 않게 한다. 읽기는 `facedoor.locker_panel.read_state()`.

## 4. 테스트
```
.venv/Scripts/python.exe -m pytest            # 카메라/GPIO/네트워크 불필요. models/ 없으면 models 테스트는 skip
```
