# QA 리포트: 리밋센서 잠금 제어 (INTERFACES v1.5 §13/§15/§16)

검증일 2026-10-17. 코드 수정 없음. 대상: hardware/(base, limits, gpio_mock, gpio_pi, factory), app.py, display.py, locker_panel.py.

## 1. pytest

`.venv/Scripts/python.exe -m pytest tests` -> **790 passed, 1 skipped** (30.7 s).
- skip 1건: `tests/test_admin_gui.py:348` "app.py does not take camera.lock yet" (§14.6, vision 미완 = 미검증).
- 검증 도중 eval-engineer 가 `evaluation/locker_audit.py` 를 편집 중이어서 (a) `test_locker_audit.py::test_scenario24...` 1건 실패(integrity 딕셔너리에 `chatter_reopens` 키 추가), (b) SyntaxError 수집 오류, (c) `test_eval_scripts.py` 3건 실패가 일시적으로 관측되었으나, 편집 종료 후 재실행에서 전부 통과. 담당(eval)은 최종 상태에서 `chatter_reopens` 가 INTERFACES §15.6 평가 절에 문서화돼 있는지 확인 요망.

## 2. 필수 확인 항목 결과

| # | 항목 | 결과 | 근거 |
|---|---|---|---|
| 1 | 해제 후 unlock_timeout_sec 내 미개방 시 재잠금 | 통과 | E2E 로그 `CLOSE unlock_timeout ... unlocked_for=3.016s door_open_for=0.000s`(설정 3 s). test_l1 (두 백엔드) |
| 2 | 열림 중 유지, close_confirm 연속 후 잠금, 중간 재개방 리셋 | 통과 | E2E: OPEN 1.0 s 유지 -> CLOSING_CONFIRM -> 0.5 s 후 `close_confirmed`. `base.py:1372-1378` 해제 샘플이 오면 OPEN 복귀, None 이면 prs_since 리셋. test_l3 (출력 전이 0회 확인) |
| 3 | max_unlock_sec 워치독이 센서와 무관 | 통과 | 직접 프로브: 문 열고닫기 1.5/0.5 s 반복 -> 해제 시점 기준 30.0 s 직후 OFF(P1). 센서 None 0.5 s/유효 0.05 s 교대 -> 30.3 s OFF(P2). `_tick_locked` 가 센서 분기 전에 `_due_locked` 를 먼저 검사(`base.py:1343`), 독립 `threading.Timer` + `status()` 지연 검사 병행. test_l4, l10 |
| 4 | 센서 실패/미연결 시 거부, 해제 중 sensor_fault_sec 후 잠금 | 통과 | `open()` 가 동기 재읽기(`base.py:1470`), None -> `sensor_fault`, 해제(열림) -> `door_open`. 시작부터 미연결(NO 배선) 시 LOCKED_OPEN 으로 출력 ON 0회. test_l6, l7, l8, l9 |
| 5 | Unknown/HOLD 에서 open 미호출 | 통과 | `app.py` 에서 `door.open` 호출 지점은 `_request_open` 하나이고 `decision.should_open is True` 분기 안에서만 호출. test_app: `test_unknown_person_never_opens`, `test_hold_never_opens`, `test_occluded_deny_policy_blocks`, `test_person_without_locker_never_opens` |
| 6 | 앱 예외/종료 시 모든 출력 OFF | 통과 | `step()` except 에서 `door.close` 가 최우선. `run()` finally -> `shutdown()` (door.close -> leds.off -> hardware.cleanup, 각각 개별 try). factory 에 atexit/SIGINT/SIGTERM 훅. test_app 의 카메라 실패/예외/KeyboardInterrupt/SIGINT/shutdown 멱등 |
| 7 | mock 앱 E2E | 통과 | 아래 3절 |
| 8 | 쿨다운 경유 LOCKED_CLOSED 안전성 | 부분 결함(낮음~중간) | 아래 H-1 |
| 9 | open() 거부 순서 vs §15.3 표 | 불일치 확인(문서 수정 필요) | 아래 H-2 |

## 3. E2E 결과 (7번)

`scripts/run.py` 는 `--no-display` 에서 리밋 키를 줄 수 없고, 등록 인물에 locker_id 가 없어(per_locker 는 locker 없는 인물을 거부 = 올바른 fail-closed 동작) 그대로는 해제가 안 된다. 그래서 스크래치패드의 드라이버(`scratchpad/e2e.py`)가 `app.main` 을 그대로 호출하되 `create_hardware` 를 감싸 mock 리밋을 별도 스레드로 토글했다. 설정: lockers.mode=single, unlock_timeout 3, close_confirm 0.5, max_unlock 8, cooldown 1, 영상은 gallery 의 me 사진 900프레임 AVI, 로그는 스크래치패드로 리다이렉트(프로젝트 logs 오염 없음).

관측 타임라인(초): LOCKED_CLOSED -> 0.27 UNLOCKED_WAITING -> (리밋 열림) 0.94 OPEN -> (리밋 닫힘) 1.95 CLOSING_CONFIRM -> 2.47 COOLDOWN(`close_confirmed`, unlocked_for=2.188 s) -> 3.45 LOCKED_CLOSED -> 5.31 재해제 -> 문 안 열림 -> 8.31 `unlock_timeout` -> 종료 시 CLEANUP outputs OFF, exit 0.
events.csv 37컬럼, 행: `open`, `door_opened`, `door_close(close_confirmed)`, `open`, `door_close(unlock_timeout)`, `stop`. hardware.log 에 STATE/CLOSE(cause=, unlocked_for=, door_open_for=) 확인.

## 4. 결함·권고 (담당별, 심각도순)

### hw-engineer

**H-1 (중간~낮음, 안전) 쿨다운 만료 시 close_confirm 없이 LOCKED_CLOSED 로 전이** (`base.py:1405-1408` COOLDOWN 분기, `base.py:1233-1238` `_settle_locked`, `_refresh_locked`)
- 재현: 해제 -> 문 열림 -> 워치독(30 s) 발동 -> COOLDOWN 2 s -> 만료 시점 직전에 문을 열어둔 채 마지막 1~2 샘플(20~40 ms)만 눌림 -> 즉시 `LOCKED_CLOSED` 가 되고 `open()` 이 **True**. 같은 상황에서 쿨다운 만료 전부터 열려 있으면 `LOCKED_OPEN`(거부)이어서, 접점 바운스/문을 스치는 순간 하나로 "닫힘 확정"이 우회된다. 프로브: `scratchpad/probe.py` 의 P3 redo2.
- 평가: 경로별로 다르다. (a) `close_confirmed` 경유는 이미 close_confirm_sec 를 채웠으므로 안전, (b) `unlock_timeout` 경유는 문이 열린 적이 없으므로 무해, (c) **`watchdog`/`sensor_fault` 경유**는 문이 열려 있었던 경우라 close_confirm 이 필요하다. 영향은 "문이 실제로는 열려 있는데 얼굴 인식만 되면 다시 해제"(R1 의도상 허용되는 새 해제)까지라 즉시 위험은 낮지만, §15.3 의 "LOCKED_OPEN 은 눌림이 close_confirm_sec 연속이어야 해제" 불변조건과 다르다.
- 권고: `_after_off_locked` 에서 cause 가 watchdog/sensor_fault 이거나 `door_open_at` 이 있었으면 COOLDOWN 만료 후 `LOCKED_CLOSED` 는 `prs_since` 가 `close_confirm_sec` 이상일 때만, 아니면 `LOCKED_OPEN`. 혹은 정책으로 현행 유지를 확정하고 §15.3 R9 에 명시. 테스트 추가(L4 확장: 쿨다운 만료 시점 1샘플 눌림이 LOCKED_CLOSED 를 만들지 않음).

**H-2 (문서 불일치, 낮음) `open()` 거부 순서가 §15.3 표와 다름** (`base.py:1455-1475`)
- 코드 순서: shutdown -> (per_locker: fault, no_locker_id, unknown_locker) -> **busy** -> sensor_fault(상태) -> sensor_fault(재읽기 None) -> door_open -> cooldown -> concurrency_limit -> invariant. 표(§15.3)는 b/c sensor_fault -> d door_open -> **e busy** -> f cooldown.
- 실제 차이(프로브 P4/P4b): 해제 중(ON 상태)에 센서가 None 이거나 문이 열려 읽혀도 코드는 `busy`, 표는 `sensor_fault`/`door_open`. 코드가 더 타당하다(OPEN 은 정의상 문이 열린 상태이므로 `door_open` 거부가 의미 없고, ON 상태에서 재읽기를 하면 로그만 늘어남). 안전상 문제 없음(어느 쪽이든 거부, 출력 변화 0).
- 권고: INTERFACES §15.3 표를 코드 순서(busy 를 b 앞으로)로 수정하고, 앱의 알림 매핑(`app.py` tmpl)과 일치함을 명시. 코드는 수정하지 않아도 됨. 단, ON 상태에서 센서가 None 인 경우 사용자에게는 `busy` 알림이 나가므로 센서 오류 표시는 sensor_fault_sec 후 패널에서 확인한다는 점을 문서화.

**H-3 (낮음, 표시) `_settle_locked` 가 `last_sample is None` 이면 LOCKED_CLOSED 로 처리** (`base.py:1236`)
- 지연 갱신(`_refresh_locked`, `open()/close()/status()` 경유)에서 마지막 샘플이 None 이면 패널이 "닫힘"으로 보일 수 있다. `open()` 이 재읽기로 거부하므로 안전하지만 §15.1 "불명은 어느 쪽 판단에도 쓰지 않는다"와 어긋난다. None 이면 쿨다운을 유지하거나 센서 상태 판정을 poll 에 맡길 것을 권고.

**H-4 (정보) 독립 워치독 Timer 도 `self._lock` 을 잡는다** (`base.py:1180`). 폴링 스레드가 락을 쥔 채 `limits.read` 에서 멈추면(실기 gpiozero 에서는 드묾) Timer·cleanup 모두 대기한다. R8 의 "스레드 정지는 독립 타이머가 처리" 주장이 이 경우엔 성립하지 않는다. 실기 소크 테스트로 확인하거나 `limits.read` 를 락 밖에서 수행하는 구조를 검토. 또 `unlock_timeout` 은 폴링 스레드에서만 집행되고(Timer 는 max_unlock 전용) 스레드가 죽으면 R8 로 FAULT OFF 되므로 스펙 범위 내이다.

**H-5 (정보, 사용자 확인 필요) 기본값이 위험한 배선 쪽** — `default.yaml` 은 `limit_active_low: true`(NO 접점)이고 선 빠짐은 "열림"으로 읽혀 해제 후 도중 단선 시 최대 `max_unlock_sec`(30 s)까지 유지된다. 설계상 인지된 사항이며 WARNING 이 나온다. 실배선은 NC + `limit_active_low: false` 권장(WIRING.md 와 일치 확인).

### vision-engineer

**V-1 (낮음, 안전 무관) `camera.lock` 미연동** — `tests/test_admin_gui.py:348` skip 으로 표시된 §14.6 통합 미완. 관리 화면과 인식 앱이 동시에 카메라를 열 수 있다. 이번 범위 밖이지만 "미검증"으로 남긴다.

**V-2 (정보)** 어떤 예외든 `door.close()` 가 모든 사물함을 즉시 재잠금한다(`app.py` step except). 표시 계층의 일시 오류(예: `display.show` 예외)로도 열려 있는 모든 문이 잠기므로 fail-closed 로는 맞다. 다만 문이 열려 있는 사물함은 close_confirm 없이 COOLDOWN 이 된다는 점을 인지할 것(H-1 과 같은 경로).

**V-3 (정보)** 도어 이벤트는 `_process_frame` 안에서만 drain 된다. 카메라 read 가 예외를 내는 프레임에서는 다음 정상 프레임 또는 shutdown 시점까지 events.csv 행이 지연된다(손실 아님, shutdown 에서 최종 drain 확인). 판정 영향 없음.

앱 쪽 안전 로직(해제 단일 호출 지점, rejected 메시지, 시뮬레이션 키는 `limits.is_simulated` 일 때만, 최종 door_state.json 을 UNKNOWN 으로 기록)은 문제 없음. 인식 화면의 키/클릭 시뮬레이션은 `_sim` 이 False 이면 핸들러 자체를 None 으로 넘겨 실기에서 센서를 속일 경로가 없다.

### eval-engineer

결함 없음(최종 상태에서 전부 통과). 위 1절의 일시적 실패는 편집 중 상태였고, `chatter_reopens` 지표의 문서화 여부만 확인 요망. `watchdog_overruns`/`on_while_closed_overtime` 등 §15.6 의 평가 지표 구현은 이번 범위에서 코드 검토하지 않았다(미검증).

## 5. 이식성

- Pi 전용 import(`lgpio`, `gpiozero`)는 모두 함수 내부 지연 import(`gpio_pi.py:59,98,326`, `limits.py` 의 PiLimitInputs 생성자). 모듈 최상단 import 없음.
- 카메라 백엔드 분기 `camera.py:44-48` (CAP_DSHOW / CAP_V4L2) 확인. Windows 에서 mock 전 흐름(E2E) 구동 확인.
- 비ASCII 프로젝트 경로(`PJT5_얼굴인식`)에서 `cv2.imread` 가 실패한다는 점을 확인했다(내 보조 스크립트에서 발생, 프로젝트는 imageio 모듈로 우회). 앱 로그 콘솔 출력의 경로가 cp949 에서 깨져 보이나 기능 영향 없음.
- 실기 미검증: 실제 GPIO 극성·풀업, lgpio 핀 점유, 단선 동작.

## 6. 재현 명령

- 전체 테스트: `.venv/Scripts/python.exe -m pytest tests`
- E2E: `.venv/Scripts/python.exe <scratchpad>/e2e.py <scratchpad>` (스크래치패드: `C:\Users\ghkim\AppData\Local\Temp\claude\C--Users-ghkim-Downloads-PJT5-----\4b12b689-3d95-44f1-b8d8-4cc2b056ba0f\scratchpad`)
- 경계 프로브(P1~P5, H-1 재현): 같은 폴더의 `probe.py`
