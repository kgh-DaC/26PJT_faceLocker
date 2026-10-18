# facedoor 아키텍처

라즈베리파이 5(Ubuntu 24.04) + USB 웹캠으로 등록된 얼굴을 인식해 **솔밸브 개방 신호 1개**와 확인용 LED·화면 표시를 출력하는 Python 프로그램.
지금은 하드웨어가 없으므로 Windows PC에서 mock 모드로 전체 흐름을 개발·검증하고, 설정 한 줄(`hardware.mode: pi`)로 실기로 전환한다.
인식 신뢰도(FAR/FRR/ROC/DET/EER/혼동행렬/점수분포/조건별 표)와 조명·마스크·안경·목도리 강건성을 재현 가능한 방식으로 평가한다.

세부 시그니처·스키마는 `docs/INTERFACES.md`가 정본이다. 이 문서는 구조와 **이유**를 다룬다.

(v1.3 설계, 미구현) 등록자마다 사물함을 배정하고 **그 사람의 사물함 출력 하나만** 여는 `lockers.mode=per_locker`를 §6.1과 INTERFACES §13에 정의했다. 기본값은 기존 단일 출력(`single`)이며 동작이 바뀌지 않는다.

---

## 1. 요구사항과 우선순위

| 우선순위 | 요구 | 설계 대응 |
|---|---|---|
| 1 안전 | 솔밸브가 열린 채 고착되지 않음 | 워치독(`max_open_sec`, 코드 절대상한 10초), 종료·예외·시그널 시 출력 OFF, 부팅 시 OFF. per_locker는 채널별 워치독 + 전 채널 OFF |
| 1 안전/보안 | (per_locker) 인식된 사람의 사물함만 열림, 다른 사물함은 절대 열리지 않음 | 앱(LockerMap)과 도어 컨트롤러 2중 검사, 채널 불변식 검사 후 FAULT, 동시 개방 상한 1, 오개방 감사 지표 0 기준 (§6.1) |
| 2 보안 | 타인에게 열리지 않음(FAR 우선) | 보수적 기본 임계값, 보정 결과는 표본 충분할 때만 임계값 하향 허용, 마진, N-of-M 투표, 품질 미달 HOLD |
| 3 편의 | 등록자는 조건이 달라도 열림(FRR) | 조건 태그별 다중 등록, 가림(occluded) 임계값 그룹, CLAHE 옵션(평가 후) |
| 4 이식성 | PC=개발, Pi5=실기 동일 코드 | 하드웨어 의존을 `hardware/`에 격리, 카메라 백엔드 분기, 설정 파일 전환 |
| 5 검증성 | "올바르게 평가되었는지" 사후 판단 | 이벤트 로그 `logs/events.csv` + 사후 라벨 → 같은 지표로 재계산, 리포트에 표본 수·합성 비중·CI |

---

## 2. 기술 스택 선택

| 영역 | 선택 | 대안 | 이유 |
|---|---|---|---|
| 언어 | Python 3.12 | | Ubuntu 24.04 기본 파이썬과 동일 |
| 검출 | OpenCV **YuNet** (`face_detection_yunet_2023mar.onnx`, MIT) | | 5점 랜드마크 제공, Pi5 CPU에서 실시간, OpenCV만으로 동작 |
| 임베딩(기본) | OpenCV **SFace** (`face_recognition_sface_2021dec.onnx`, Apache-2.0, 128차원) | | 추가 런타임 불필요, PC/Pi 동일 설치, 상업 이용 가능 라이선스 |
| 임베딩(선택) | **ArcFace** w600k_r50 (512차원) via `onnxruntime` 직접 로드 | `insightface` 패키지 | 정확도 더 높음. insightface 패키지는 Windows에서 C++ 빌드가 필요해 쓰지 않고 정렬을 자체 구현. 모델 파일은 InsightFace 비상업 라이선스라 사용자가 직접 배치 |
| GPIO | `gpiozero` + `lgpio` 핀 팩토리 | RPi.GPIO | Pi5의 GPIO는 RP1 칩이라 RPi.GPIO가 동작하지 않음 |
| PC GPIO | 순수 파이썬 mock(기본) / gpiozero `MockFactory` | | MockFactory로 Pi 코드 경로(`gpio_pi.py`)를 PC 테스트에서 실행 가능 |
| 설정 | YAML (`PyYAML`) | | 사람이 고치기 쉬움, 주석 가능 |
| 평가 그래프 | matplotlib (평가 extra에만) | | 런타임 의존성 최소화 |
| 테스트 | pytest | | |

**OpenCV 버전 주의**: YuNet 2023mar는 OpenCV ≥ 4.8 필요. Ubuntu 24.04 apt의 `python3-opencv`는 4.6이라 **사용 불가** → Pi에서도 pip `opencv-python>=4.9`를 쓴다(aarch64 휠 제공). GUI용 `libgl1`, `libglib2.0-0`는 apt로 설치.

---

## 3. 구성도

```
                     configs/default.yaml (+ configs/local.yaml, --set)
                                   │ load_config()
                                   ▼
┌────────────┐  Frame  ┌──────────────────────────── FramePipeline ───────────────────────────┐
│ Camera     │────────►│ Detector(YuNet) → 다중얼굴 정책 → Quality → Liveness(no-op)            │
│ OpenCV/File│         │      → Embedder(SFace|ArcFace, 정렬+L2) → Recognizer(코사인, ThresholdPolicy)│
└────────────┘         └───────────────────────────────┬───────────────────────────────────────┘
                                                       │ RecognitionResult (verdict, scores)
                     models/calibration.json ──► ThresholdPolicy      data/gallery ──► FaceDatabase
                                                       ▼
                                        VoteDecider (N-of-M, rearm, deny)
                                                       │ DecisionOutput (should_open 1회)
             ┌─────────────────────────────────────────┼───────────────────────────────┐
             ▼                                         ▼                               ▼
   hardware.HardwareBundle                    events.EventLogger                display.Display
   DoorController(솔밸브+녹색LED)              logs/events.csv                  오버레이/가상 LED
   StatusLeds(적/황)  ── pi | mock             (평가 재현 경로)                 (--no-display 시 Null)

   평가:  data/probe ─► evaluation.scoring (같은 FramePipeline 사용) ─► metrics ─► report.html
                                                                     └► calibrate ─► models/calibration.json
```

핵심 결정: **런타임과 평가가 같은 `FramePipeline`을 쓴다.** 평가 점수와 실제 문 앞 점수가 다른 코드에서 나오면 보정 임계값이 의미를 잃기 때문이다.

---

## 4. 런타임 루프와 상태

`app.FaceDoorApp.step()` (단일 스레드 메인 루프, 디스플레이도 메인 스레드):

1. `camera.read()` — 연속 실패 `read_retries`회 → `CameraError` → 문 닫고 종료(코드 3). systemd가 재시작.
2. `runtime.process_every_n` 프레임마다 `pipeline.process()`; 건너뛴 프레임은 표시만.
3. `decider.update(result)` → `should_open`이면 `door.open("face:<id>:<score>:<votes>/<window>")`.
4. `should_deny` → 적색 점멸. 판정 HOLD → 황색.
5. `door.status()` 변화 감지 → `door_close` 이벤트. `events.log_frame()`, `display.show()`.

하드웨어 상태 머신(`hardware/`, 스레드 타이머) — `close_mode: timer`(구 동작, 리밋 없음):

```
IDLE --open(reason)--> OPEN [솔밸브 ON + 녹색 ON, 타이머=min(duration, max_open_sec)]
OPEN --타이머 만료 | close()--> COOLDOWN [출력 OFF, cooldown_sec]
COOLDOWN --시간 경과--> IDLE
OPEN/COOLDOWN 중 open() --> 거부(False), 연장 없음
어떤 상태든 cleanup()/예외/SIGINT/SIGTERM/atexit --> 출력 OFF
```

**(v1.5, 미구현) 리밋센서 모드 `close_mode: limit`** (INTERFACES §15, 기본값으로 채택 예정): 각 도어에 리밋센서(눌림=닫힘, 안 눌림=열림). 얼굴은 "해제"에만 쓰고, "유지·재잠금"은 문이 실제로 열렸다 닫혔는지로만 결정한다(v1.4의 얼굴 유지 신호 방식 close-hold는 폐기).

```
LOCKED_CLOSED --얼굴 인식 open()--> UNLOCKED_WAITING [솔밸브 ON, 해제 시각 기록]
UNLOCKED_WAITING --리밋 해제 0.1초 연속--> OPEN [문 열림. 시간으로는 잠기지 않음]
UNLOCKED_WAITING --unlock_timeout_sec(10) 안에 문이 안 열림--> COOLDOWN [자동 재잠금, cause=unlock_timeout]
OPEN --리밋 눌림--> CLOSING_CONFIRM --눌림 close_confirm_sec(2) 연속--> COOLDOWN [잠금, cause=close_confirmed]
CLOSING_CONFIRM --리밋 다시 해제--> OPEN [카운터 리셋, 출력 변화 없음 → 채터링 없음]
(해제 중 어디서든) 해제 시점 + max_unlock_sec(30)--> 잠금 [cause=watchdog, 센서 값과 무관, 연장 불가]
(어디서든) 리밋 읽기 실패 sensor_fault_sec(1) 지속--> 즉시 잠금 + SENSOR_FAULT [해제 요청 거부]
LOCKED_CLOSED --솔밸브 OFF인데 리밋 해제--> LOCKED_OPEN [강제로 열림/센서 선 빠짐. 해제 요청 거부(door_open)]
COOLDOWN --cooldown_sec--> LOCKED_CLOSED(눌림) | LOCKED_OPEN(해제)
```

설계 이유:
- **해제 조건 비대칭**: 해제 = 얼굴 N-of-M 투표(엄격), 잠금 = 센서가 "닫힘"을 `close_confirm_sec` 연속으로 확인(디바운스). 문이 닫히는 순간 접점 튕김으로 솔밸브가 열렸다 닫혔다 하지 않는다.
- **"열림으로 오인해 해제 유지" 금지**: 해제 유지의 유일한 근거는 "리밋이 해제 상태로 읽힘"이므로, 그 근거가 약해지는 모든 경로를 잠금 쪽으로 닫는다. (1) 해제 요청 시 리밋을 동기 재읽기해 읽기 실패·이미 열림이면 해제 거부 — 시작부터 미연결이면 절대 안 열린다. (2) 해제 중 읽기 실패가 `sensor_fault_sec` 지속되면 즉시 잠금(마지막 유효 값을 믿고 유지하지 않음). (3) 해제 중 선이 빠져 "열림"으로 고정되는 경우는 센서만으로 구분할 수 없으므로 `max_unlock_sec` 워치독이 최종 방어선이다. (4) 권장 배선(NC 접점 + `limit_active_low: false`)이면 선이 빠져도 "닫힘"으로 읽혀 `unlock_timeout`/`close_confirmed` 경로로 잠긴다.
- **워치독은 fail-safe 방향**: `max_unlock_sec`는 "해제 시점" 기준이고 문을 여닫아도 연장되지 않으며, 초과하면 문이 열려 있어도 솔밸브를 끈다(문이 아직 열려 있으면 LOCKED_OPEN으로 닫힐 때까지 재해제 거부). 워치독은 (a) 채널별 독립 타이머 (b) 감시 스레드 (c) `status()` 지연 검사 세 곳에서 확인해 하나만 살아도 동작한다. 감시 스레드 예외는 전 채널 OFF + FAULT.
- **독립성**: 상태기계는 하드웨어 스레드가 `poll_once()`로 구동하므로 카메라·추론·GUI가 멈춰도 재잠금이 늦어지지 않는다. 앱은 해제만 요청하고 `drain_events()`로 전이를 받아 기록한다.
- 절대상한은 timer 모드 10초(코드 상한)와 limit 모드 120초(코드 상한, 기본 30초)로 분리된다. 문을 열고 물건을 넣고 닫는 시간이 필요하기 때문이며, **솔밸브가 연속 통전으로 30초를 견디는지는 사용자 확인 사항**이다(§14).

녹색 LED는 `DoorController`가 솔밸브와 **같은 전이 안에서** 구동한다. 그래서 "녹색이 켜져 있으면 신호도 나가고 있다"가 구조적으로 보장된다. 적색/황색은 `StatusLeds`가 담당한다.

투표기 상태(`decision.py`): `IDLE → VERIFYING → OPEN(1프레임) → REARM(rearm_sec) → IDLE`, 부수적으로 `HOLD`, `DENIED(1프레임)`. 규칙은 INTERFACES §3.9.

---

## 5. 인식 판정 정책

### 5.1 점수
- 모든 점수는 L2 정규화 임베딩의 **코사인 유사도** `[-1, 1]`, 수용 규칙 `score >= threshold`.
- 사람별 점수 = 그 사람 갤러리 샘플들과의 코사인 중 **최대값**(기본) 또는 상위 k 평균.
- 후보 인정: `best >= threshold` **그리고** `best - second(다른 사람) >= margin(0.05)`.

### 5.2 임계값 정책 (보안 우선)
- 코드에 임계값을 두지 않는다. 출처는 `models/calibration.json`(보정 결과)과 `configs/default.yaml`(보수적 기본값)뿐이다.
- 기본값(보정 없음): SFace `global 0.50 / occluded 0.55`, ArcFace `0.45 / 0.50`. OpenCV가 제시한 SFace 참고값(0.363)보다 의도적으로 높다(FRR을 희생하고 FAR을 낮춤).
- 바닥값: SFace 0.40, ArcFace 0.30. 어떤 보정 결과도 이 아래로 내려가지 못한다.
- 보정 결과 반영: impostor 표본이 `3/target_far`(목표 0.1%면 3000) 이상이고 실촬영 데이터일 때만(`reliable=true`) 보정값을 그대로 쓴다. 아니면 `max(보정값, 기본값)` — 보정은 임계값을 **올리는 방향으로만** 반영된다. 소규모 데이터에서 과적합된 낮은 임계값으로 문이 쉽게 열리는 것을 막기 위함이다.
- 보정 파일이 다른 임베더(`model_id` 불일치)로 만들어졌거나 손상되면 무시하고 기본값을 쓴다(경고 로그).
- 상세 규칙: INTERFACES §7.2.

### 5.3 가림(마스크·목도리) 대응과 트레이드오프

| 선택지 | FRR(마스크) | FAR 위험 | 채택 |
|---|---|---|---|
| A. 단일 임계값, 마스크 샘플 미등록 | 매우 높음 | 낮음 | ✗ |
| B. 마스크/목도리 태그 샘플 등록 + 단일 임계값 | 개선 | 낮음 | 1차 수단 |
| C. B + 가림 그룹 별도 임계값(occluded) | 더 개선 가능 | 눈 주변만 남아 타인 점수도 상승 → 증가 | 기본값은 **global보다 높게(0.55)**; 실측 보정으로만 하향 |
| D. 가림 매칭은 항상 거부(`occluded_policy: deny`) | 100% | 최저 | 고보안 현장 옵션 |
| E. 가림 시 2차 인증(PIN/카드) | 낮음 | 낮음 | 향후 확장 제안(인터페이스 없음) |

가림 여부는 별도 마스크 검출기 없이 **최고 점수 갤러리 샘플의 태그**(`mask`, `scarf`)로 판단한다. 가려진 얼굴은 가림 샘플과 가장 잘 맞는다는 관찰에 기반한 근사이며, 평가 리포트의 조건별 표로 타당성을 확인한다.
안경 착용/미착용은 둘 다 등록(`glasses`, `noglasses`)하는 것으로 대응하고 별도 그룹을 두지 않는다.
(v1.3) **모자(`hat`)는 global 그룹**으로 둔다: 이마·눈썹 일부만 가려 눈·코·입이 남으므로 특징 손실이 마스크보다 작고, 얼굴 아래쪽 가림(마스크·목도리)으로 보정하는 occluded 임계값에 성격이 다른 샘플을 섞으면 보정이 왜곡된다. 챙이 눈을 가리면 품질 게이트에서 HOLD된다. 이는 가정이므로 `hat` 평가 조건으로 측정하고, 모자 조건 FAR이 높으면 `recognition.occluded_tags`에 `hat`을 추가한다(설정만으로 전환). 마스크+모자 동시 착용은 더 엄격한 `mask`로 촬영. `other`(기타)도 global. 조명은 `lowlight`, `backlight` 태그 등록 + 품질 HOLD + CLAHE 옵션(A/B 평가 후에만 켬).

### 5.4 시간적 투표 (N-of-M)
- 기본 **3/5**: 최근 5개 처리 프레임 중 같은 사람 MATCH 3회 → OPEN. `window_timeout_sec 2.0`으로 오래된 프레임 제거.
- UNKNOWN 또는 다른 사람 MATCH가 끼면 창을 비운다. HOLD/NO_FACE는 슬롯만 차지(리셋 안 함) — 흔들림으로 품질 미달 프레임이 섞여도 정상 사용자가 계속 진행할 수 있게.
- OPEN 후 `rearm_sec 5.0` 동안 재판정하지 않음(`open_sec 3 + cooldown 2`).
- 연속 UNKNOWN 3회 → DENIED 이벤트(적색 점멸), 2초 내 재발생 없음.
- 효과: 프레임 단위 FAR이 p일 때 독립 가정 하에 3연속 근사 p³ 수준까지 낮아진다(실제로는 프레임 간 상관이 커서 그만큼 줄지 않음 — 평가 리포트는 보수적으로 **프레임 단위** 수치를 보고).

### 5.5 품질 게이트 (HOLD)
얼굴 짧은 변 ≥ 80px, 검출 점수 ≥ 0.90, 라플라시안 분산 ≥ 60, 밝기 50~210, |yaw 비율| ≤ 0.45, 가장자리 아님. 미달 시 점수 계산 없이 HOLD. 어둡거나 흐린 프레임의 노이즈 점수가 우연히 타인과 맞는 것과, 정상 사용자를 UNKNOWN으로 연속 거부하는 것을 함께 막는다. 저조도에서 HOLD 비율이 높은 것은 의도된 동작이다.

---

## 6. 하드웨어와 플랫폼 경계

| 항목 | Windows PC (개발) | Raspberry Pi 5 / Ubuntu 24.04 (실기) |
|---|---|---|
| `hardware.mode` | `mock` (기본) | `pi` (`configs/local.yaml`에 `hardware: {mode: pi}`) |
| GPIO | 순수 파이썬 mock 또는 gpiozero MockFactory | gpiozero + lgpio (`python3-lgpio` apt, venv `--system-site-packages`) |
| 카메라 | `CAP_DSHOW`, 인덱스 0 | `CAP_V4L2`, `/dev/video0` (`v4l2-ctl --list-devices`) |
| 화면 | cv2.imshow | 모니터 연결 시 imshow, systemd 서비스는 `--no-display` |
| 실행 | `python scripts/run.py` | `deploy/facedoor.service` (Restart=on-failure, ExecStopPost로 GPIO OFF) |

- `mode: pi`를 Pi가 아닌 곳에서 지정하면 **오류 종료**(조용한 mock 폴백 금지). `auto`일 때만 경고 후 mock.
- 핀(BCM): 솔밸브 17(물리 11), 녹 27(물리 13), 적 22(물리 15), 황 23(물리 16), 실내 버튼(선택) 미할당. 극성은 `solenoid_active_high`(릴레이 모듈이 active-low면 false).
- Pi5 RP1 gpiochip 번호는 커널에 따라 0 또는 4 → `gpio_chip: auto`.
- 솔밸브(12/24V 유도성)는 GPIO로 직접 구동 금지: 릴레이 또는 로직레벨 MOSFET + 플라이백 다이오드 + 외부 전원, GND 공통. 상세는 `deploy/WIRING.md`(hw 담당). 전원 차단 시 잠김/열림(fail-secure/fail-safe)은 현장 법규에 따라 사용자가 결정.

### 6.1 사물함별 출력 (`lockers.mode=per_locker`) — v1.3 설계, 미구현

요구: 등록자마다 사물함 번호를 배정하고 배정된 사물함만 연다. 계약 상세는 INTERFACES §13.

```
person.json.locker_id ──► LockerMap(시작 시 스냅샷) ──► resolve(decision.person_id)
                                                          │ allowed? 아니면 출력 없음 + 사유 기록
                                                          ▼
                         door.open(reason, locker_id=L) ──► 채널 표 [A01:BCM17, A02:BCM24, ...]
                                                          │ 재검사(미설정/busy/cooldown/동시상한/불변식)
                                                          ▼
                                             채널 L 하나만 ON + 공용 녹색 LED, 채널별 워치독
```

설계 결정과 이유:
- **채널 목록은 `hardware.locker_outputs`에 둔다**(사람 매핑·정책은 `lockers` 섹션). `hardware/`는 다른 섹션을 읽지 않는다는 경계를 유지하고, systemd `ExecStopPost --force-off`가 `hardware` 섹션만으로 전 채널을 끌 수 있게 하기 위함이다.
- **이중 스위치**: `lockers.mode: per_locker`와 비어 있지 않은 `locker_outputs`가 함께 있어야 하고, per_locker에서는 `solenoid_pin: null`이어야 한다. 한쪽만 바뀐 설정은 시작 단계에서 ConfigError — "단일 출력이 모든 사람에게 열리는" 상태로 조용히 남는 것을 막는다.
- **사람→사물함 매핑은 `person.json.locker_id`**(이미 존재). 갤러리와 함께 백업·삭제되어 사람 삭제 시 배정이 고아로 남지 않는다. 1인 1사물함으로 시작하고, 1인 다사물함은 열 사물함을 고르는 UI가 필요해 `locker_ids` 키만 예약했다.
- **공유는 기본 금지**: 같은 사물함이 두 사람에게 배정된 것은 대부분 입력 실수다. 금지 상태에서 공유가 발견되면 시작은 하되 해당 사물함 배정자 전원을 억제한다(다른 사물함 사용자까지 막지 않음). 실제 공유가 필요하면 `allow_shared: true` + CLI `--shared`.
- **2중 검사**: 앱은 사유를 이벤트에 남기기 위해 `LockerMap`으로 먼저 거르고, 도어 컨트롤러는 앱을 신뢰하지 않고 locker 존재·채널 상태·동시 개방 수·출력 불변식을 다시 검사한다. "OPEN 상태가 아닌 채널의 출력이 ON"이 관측되면 즉시 전 채널 OFF + FAULT(재시작 전까지 개방 금지). 엉뚱한 사물함이 열리는 것은 FRR보다 훨씬 나쁜 오류이므로 가용성보다 정지를 택한다.
- **동시 개방 상한 1(기본)**: 투표 rearm(5초)과 채널별 쿨다운이 있어 실사용에서 1로 충분하고, 전원 용량(솔밸브 돌입전류)과 오동작 시 피해 범위를 줄인다.
- **녹색 LED는 공용**: "어느 사물함이든 신호 출력 중"을 뜻한다. 사물함별 LED는 핀 예산을 두 배로 써서 보류(미결).
- **매핑은 시작 시 스냅샷**: 갤러리 임베딩도 시작 시 로드되므로 같은 규칙. 배정 변경·해제 후 서비스 재시작 필요(CLI가 안내).
- **감사 가능성**: `events.csv`에 `locker_id`, `open_lockers` 컬럼(끝에 추가, 하위호환), `hardware.log` 채널 행은 `locker=<id> pin=BCM<n>`으로 시작. 평가가 이 둘을 대조해 "올바른 사람인데 다른 사물함이 열림"(`wrong_locker_opens`)을 계산하고 0이 아니면 실패로 처리한다. 단, 로그는 소프트웨어 라우팅만 증명하므로 배선 매칭은 실물 확인(INTERFACES §13.9 시나리오 30)이 필수다.

배선 안전(per_locker):
- 사물함 N개 = 릴레이/MOSFET N채널, 솔밸브마다 플라이백 다이오드, 외부 전원 용량 ≥ `max_concurrent_open × 정격전류 × 1.5` + 코일 전류.
- 다채널 릴레이 보드는 active-low가 흔하다 → 채널별 `active_high`. 부팅 중(앱 시작 전) 핀 기본 풀업/풀다운 때문에 릴레이가 순간 동작할 수 있으므로 솔밸브 전원을 분리한 채 부팅 시험을 한다. 사물함 핀은 BCM ≥ 9(기본 풀다운)를 권장하고, 예약 핀(0,1,2,3,14,15)은 금지.
- **확정: 사물함 4개, locker_id `"1"`~`"4"`.** 제안 핀: 1=BCM24(물리18), 2=BCM25(물리22), 3=BCM16(물리36), 4=BCM26(물리37) — 기존 17/27/22/23과 충돌 없음, 모두 부팅 풀다운 영역. 극성 기본 active-high(3.3V에서 확실히 켜지고 꺼지는 MOSFET/High-trigger 모듈 권장), active-low 보드면 채널별 `active_high: false`. 극성을 틀리면 시작하자마자 4개가 모두 열리므로 첫 시험은 솔밸브 전원을 분리하고 한다. 상한 16채널.
- **기존 BCM17 단일 출력(하위호환)**: `single`(기본)에서는 지금과 완전히 같다. `per_locker`에서는 `solenoid_pin: null`이 필수이고 BCM17은 claim·구동·force-off 하지 않는다(입력 상태, 기본 풀다운). 기존 정문 릴레이 배선은 분리하거나 사물함 채널로 명시해 재사용한다.

### 6.2 한글 관리 화면 — v1.3 설계, 미구현 (INTERFACES §14)

- **목적**: CLI 없이 사용자 등록·제거·사물함 배정·인식 보강 촬영. 모든 문구 한글(`facedoor/texts.py`, 인식 화면·CLI와 공용).
- **실행 즉시 촬영하지 않는다**: PIN → 메인 메뉴 → 버튼을 눌러야 카메라를 연다. 끝나면 즉시 닫는다.
- **tkinter + OpenCV, PIL 없음**: `cv2.putText`는 한글 불가 → 영상에는 색 사각형만 그리고 한글은 tkinter 위젯으로. 프레임은 `cv2.imencode(".ppm")` → `tk.PhotoImage(data=...)`로 넘긴다(Tk 내장 형식이라 추가 의존성 없음). 카메라·추론은 워커 스레드, 위젯은 메인 스레드(`after` 폴링 + 크기 1 큐). Pi는 `python3-tk`, `fonts-nanum` apt 설치 필요.
- **업무 로직 분리**: `gui/service.py`의 `AdminService`가 CLI와 같은 `Enroller`/`FaceDatabase`/`lockers.check_assignment`를 쓴다. GUI 전용 규칙을 따로 두지 않아 CLI와 GUI 결과가 어긋나지 않는다.
- **카메라 충돌**: 웹캠은 한 프로세스만 확실히 쓸 수 있다. `runlock.RunLock`(OS 파일 잠금, 프로세스가 죽으면 자동 해제)으로 인식 프로그램과 관리 화면 촬영을 상호 배제하고, 갤러리 쓰기도 `gallery.lock`으로 직렬화한다. 인식 프로그램 실행 중에는 촬영 버튼을 비활성화하고 "먼저 종료하세요"를 한글로 안내.
- **반영 시점**: 인식 프로그램은 시작 시 갤러리·배정을 읽으므로 변경은 재시작 후 반영. 특히 **제거한 사람이 재시작 전까지 계속 인식될 수 있음**을 완료 대화상자에서 강조한다. 관리 화면은 sudo가 필요한 서비스 재시작을 직접 하지 않는다(권한 상승 방지).
- **이름 정책**: `person_id`는 ASCII 일련번호(`u0001` 자동 제안), 한글은 `display_name`. 폴더명·OpenCV 경로·화면 라벨 문제(§7)를 피하고, 로마자 자동 변환의 혼란과 동명이인 충돌을 없앤다. 동명이인은 경고 후 허용.
- **접근 보호**: §12.1 예약분을 살려 관리자 PIN(PBKDF2-SHA256, 연속 실패 잠금, 잠금 상태 파일로 재시작 우회 방지)을 관리 화면에만 적용. CLI·파일 접근은 여전히 OS 권한이 최종 경계.
- **신규 등록 실패 시 롤백**: 저장된 샘플이 `min_register_samples` 미만이거나 취소되면 그 사람 폴더를 지워 "반쯤 등록된 사람"이 남지 않게 한다. 보강 촬영은 이미 저장된 샘플을 유지한다.
- **저조도/역광 보강도 품질 기준을 낮추지 않는다**(보안 우선). 너무 어두우면 거부하고 조명 안내.

### 6.3 한글 인식 화면과 사물함 상태 패널 — v1.4 설계, 미구현 (INTERFACES §16·§17)

- **인식 화면도 한글**: `cv2.putText`는 한글을 못 그리므로 인식 화면을 `TkDisplay`(tkinter 창: 왼쪽 영상, 오른쪽 한글 패널)로 바꾼다. Pillow+TTF 방식은 의존성·폰트 경로 관리가 늘어 기각했다. 관리 화면과 같은 기술·폰트(`python3-tk`, `fonts-nanum`)·문구 모듈(`facedoor/texts.py`)을 공유한다. tk를 못 쓰는 환경은 `display.backend: auto`에서 ASCII `OpenCVDisplay`로 경고 후 폴백, `tk`로 강제했는데 실패하면 오류 종료.
- **인식 창 레이아웃(v1.5)**: 왼쪽 웹캠 영상, 오른쪽 위에서부터 사물함 패널 4개(각각 큰 배지로 **열림/닫힘**, 리밋센서 기준), 영상 아래 한 줄 상태줄(인식 문구·FPS). 패널 보조 문구는 잠김 / 잠금 해제됨·문을 여세요(재잠금 카운트다운) / 해제 유지 / 닫힘 확인 중 / 센서 오류 / 잠긴 상태에서 열림(강제 개방 의심). 상세는 INTERFACES §16.2·§17.1.
- **PC mock 시뮬레이션**: 가상 리밋을 패널 클릭 또는 키 1~4(문 열기/닫기), 5~8(센서 오류 주입)로 조작해 실기 없이 전체 흐름(해제→열림→닫힘 확인→잠금, 타임아웃, 워치독, 센서 오류)을 확인한다. 실기에서는 비활성이라 키보드로 센서를 속일 수 없다. 가상 리밋도 실제와 같은 상태기계 코드를 통과한다(INTERFACES §15.4).
- **사물함 1~4 패널 소유자 표시**: 해제된 동안 연 사람 이름(`locker_owner: when_open`). 관리 화면은 별도 프로세스라 앱이 쓰는 `logs/door_state.json`을 읽고, 오래되었거나 인식 프로그램이 꺼져 있으면 "상태 알 수 없음"으로 표시한다(거짓 "잠김" 금지).
- **한글화 경계**: 사람이 읽는 화면·CLI 출력은 한글, 기계가 읽는 로그·CSV 컬럼·JSON 키·Enum·옵션 이름은 영문 유지.

### 6.4 리밋센서 배선과 핀 — v1.5 설계, 미구현 (INTERFACES §15)

- 리밋 입력(BCM, 내부 풀업, 3.3V 입력만): 사물함 1~4 = BCM 5/6/12/13 (물리 29/31/32/33), single 정문 = BCM 5. 기존 솔밸브 24/25/16/26(single 17), LED 27/22/23과 겹치지 않고 예약 핀(0~3, 14, 15)을 피한다. 각 리밋은 입력 핀 ↔ COM, 반대 단자 ↔ GND(가까운 GND: 물리 30, 34).
- **접점 선택이 안전성을 좌우한다**: NO 접점(눌림=LOW)은 선이 빠지면 "열림"으로 읽혀 위험한 쪽이다. NC 접점 + `limit_active_low: false`는 선이 빠지면 "닫힘"으로 읽혀 잠금 쪽으로 안전하다. 설계는 두 배선 모두 동작하게 하되(`door_open` 거부와 워치독이 보강), NC 배선을 권장하고 기본값은 일반적인 NO 배선(`true`)으로 둔다 — 사용자의 실제 리밋 모듈(접점 종류, 내장 풀업 여부) 확인 후 확정.
- 리밋 케이블이 솔밸브 전원선과 나란히 가면 노이즈로 오판할 수 있다: 꼬임/차폐, 필요 시 10 nF 커패시터 병렬. 소프트웨어 디바운스(`open_confirm_sec`/`close_confirm_sec`)가 있어 접점 바운스에는 둔감하다.
- 해제 시 솔밸브는 문이 열려 있는 내내 통전되므로(최대 `max_unlock_sec`) 솔밸브의 연속 통전 정격·발열을 확인해야 한다. 정격이 짧으면 `max_unlock_sec`를 줄이고 사용 방식(열면 바로 닫기)을 안내한다.

---

## 7. Windows 한글 경로 함정

작업 폴더 `PJT5_얼굴인식`에 한글이 있다. Windows에서 `cv2.imread/imwrite`와 `cv2.FaceDetectorYN.create(path)`, `cv2.FaceRecognizerSF.create(path)`는 비ASCII 경로에서 실패한다. 그래서:
- 이미지 입출력은 `facedoor.imageio.imread/imwrite`(`np.fromfile`+`imdecode`, `imencode`+`tofile`)만 쓴다.
- 모델 로드는 `imageio.ascii_safe_path()`로 ASCII 캐시 경로에 복사한 뒤 넘긴다.
- `person_id`는 ASCII로 제한(`cv2.putText`가 한글을 못 그림). 한글 이름은 `person.json`의 `display_name`.

---

## 8. 위조(스푸핑) 위험 — 명시적 한계

**이 시스템은 기본 상태에서 사진·휴대폰 화면·영상 재생으로 열릴 수 있다.** 2D RGB 웹캠 + 얼굴 임베딩만으로는 실물과 사진을 구분하지 못한다. 물리 출입 제어이므로 이 한계를 README와 리포트에 숨기지 않는다.

- 인터페이스: `liveness.LivenessChecker.check(frame, det) -> LivenessResult(is_live: bool|None, score, method)`. 기본 구현 `NoopLiveness`는 `is_live=None`.
- `liveness.required: true`로 바꾸면 `is_live is True`가 아닌 프레임은 HOLD → 실제 검사기가 붙기 전에는 문이 열리지 않는다(fail-closed 확인용).
- 확장 후보: 눈 깜빡임/머리 움직임 챌린지(약함), IR 카메라 + IR 조명(중간), 깊이 센서(강함), 2차 인증(PIN/카드).

---

## 9. fail-closed 목록

| 상황 | 동작 |
|---|---|
| 카메라 열기 실패 / 연속 읽기 실패 | 출력 OFF, `error` 이벤트, 종료 코드 3 |
| 모델 파일 없음/체크섬 불일치/로드 실패 | 시작 거부, 종료 코드 4 (`download_models.py` 안내) |
| 갤러리 비어 있음 / 임베더 불일치 | 시작 거부, 종료 코드 6 (`enroll.py --reembed` 안내) |
| calibration 없음/손상/불일치 | 보수적 기본 임계값으로 동작, 경고 |
| 품질 미달, 다중 얼굴(hold 정책), liveness 실패 | HOLD, open 호출 없음 |
| `step()` 내 임의 예외 | `door.close()` 후 기록, 치명적이면 종료 |
| 프로세스 종료(SIGINT/SIGTERM/atexit) | `HardwareBundle.cleanup()` → 출력 OFF, 종료 코드 130 (systemd `SuccessExitStatus=130 143`, INTERFACES §10) |
| 인식됐지만 locker_id 없음(`lockers.require_locker_id=true`) | `door.open()` 호출 안 함, `event=open` 행에 억제 message (INTERFACES §12.2) |
| (per_locker, v1.3) locker_id 없음/형식 오류/설정에 없는 사물함/금지된 공유 | 어떤 출력도 내지 않음, `event=open` 행에 사유 message와 `locker_id` (INTERFACES §13.4) |
| (per_locker) 같은 사물함 OPEN·COOLDOWN 중, 동시 개방 상한 도달 | 도어가 거부(`last_reject`), 연장 없음 |
| (per_locker) OPEN이 아닌 채널의 출력 ON 관측 | 전 채널 OFF, FAULT, 재시작 전까지 개방 금지 |
| (per_locker) 채널 하나라도 GPIO claim 실패 | 잡은 핀 OFF·해제, 종료 코드 5 (일부 채널로 동작 금지) |
| (v1.5 limit) 해제했지만 `unlock_timeout_sec` 안에 문이 안 열림 | 자동 재잠금 cause=unlock_timeout, 이후 cooldown |
| (v1.5 limit) 문이 열려 있는 동안 | 해제 유지. 리밋 눌림이 `close_confirm_sec` 연속일 때만 잠금(중간에 해제되면 카운터 리셋) |
| (v1.5 limit) `max_unlock_sec` 도달 | 센서 값과 무관하게 솔밸브 OFF cause=watchdog, 문이 열려 있으면 LOCKED_OPEN (닫히기 전 재해제 거부) |
| (v1.5 limit) 리밋 읽기 실패/미연결 | 해제 요청 거부(`sensor_fault`/`door_open`), 해제 중이면 `sensor_fault_sec` 후 즉시 잠금. 마지막 유효 값으로 유지하지 않음 |
| (v1.5 limit) 시작 시 모든 리밋 읽기 실패 (pi) | 시작 거부, 종료 코드 5. 일부 채널만 실패면 그 채널만 SENSOR_FAULT |
| (v1.5 limit) 하드웨어 감시 스레드 예외·정지 | 예외: 전 채널 OFF + FAULT. 정지: 독립 워치독 타이머가 `max_unlock_sec`에 OFF |
| (v1.5 limit) 예외·종료·시그널·카메라 실패 | 대기 없이 즉시 전 채널 OFF (cause=shutdown) |
| (v1.4) 관리 화면이 도어 상태를 모름(인식 프로그램 꺼짐/상태 파일 오래됨) | "상태 알 수 없음" 표시, "잠김"으로 추정 표시 금지 |
| open 중 프로세스 정지(hang) | 하드웨어 워치독 타이머가 `max_open_sec`(limit 모드는 `max_unlock_sec`) 후 OFF (별도 스레드). systemd `ExecStopPost`가 추가로 핀 OFF |
| 과도한 duration 요청 | `max_open_sec`로 클램프, 설정도 10초 초과 불가 (limit 모드는 duration 무시, `max_unlock_sec` ≤ 120초 코드 상한) |
| `mode: pi`인데 Pi 아님/lgpio 없음 | 오류 종료 코드 5 |

---

## 10. 데이터·로그·개인정보

- `data/gallery/<person_id>/<tag>_<n>.{npy,jpg,json}` — 임베딩, 재임베딩용 비정렬 얼굴 crop, 메타(원본 sha256). `data/gallery/_meta.json`의 `model_id`로 임베더 호환성을 검사한다.
- `data/probe/<condition>/<person_id>/*` 시험 이미지, `data/probe/_impostor/<id>/*` 미등록자, `syn_*` 합성 조건.
- `logs/events.csv` 판정 로그(33컬럼, INTERFACES §5), `logs/hardware.log` 출력 전이, `logs/facedoor.log` 앱 로그.
- `reports/<timestamp>/` 평가 산출물, `models/calibration.json` 보정 결과.
- 얼굴 이미지·임베딩은 개인정보 → `data/`, `logs/`, `reports/`, `models/*.onnx`는 `.gitignore`. 스냅샷 저장은 기본 꺼짐.

---

## 11. 평가 설계 요약 (상세: `robustness-evaluation` 스킬, INTERFACES §8)

- gallery/probe 누수 검사(sha256), probe를 사람 단위로 보정/평가 50:50 분할(seed 42), 평가셋은 보정 임계값으로 한 번만 채점.
- 보정용 impostor 점수 = probe별 "본인 제외 최고 점수"(open-set, 실제 문 개방 위험과 동일한 정의). `_impostor`가 없으면 등록자 교차 비교만 사용하고 "낙관적"이라고 리포트에 명시.
- 1:1 검증 곡선(ROC/DET/EER)과 시스템 지표(FAR, misID, FRR(system/matcher), HOLD율)를 분리 보고. 조건별 표 필수, 부트스트랩 95% CI, FA=0이면 Rule of three 상한.
- 운영 로그 평가: `events.csv`에 사후 라벨(`scripts/label_events.py`) → 같은 지표 재계산.
- 합성 조건(`syn_*`)은 실촬영과 분리 표기, 합성만으로 만든 보정은 `reliable=false`.
- (v1.3 per_locker) 사물함 감사 지표: `wrong_locker_opens`(올바른 사람인데 다른 사물함 개방), `extra_channel_on`, `unassigned_opens`, `watchdog_overruns`는 **0이어야 하는 중대 오류**, 라벨이 있으면 `misid_locker_opens`(다른 사람 사물함 개방)를 FAR/misID와 함께 보고 (INTERFACES §13.8).

---

## 12. 성능 목표 (Pi5, 미측정 — 실측 후 갱신)

640x480 입력, YuNet + SFace CPU 기준 목표 ≥ 8 FPS 처리. 부족하면 `detector.input_scale 0.5`, `runtime.process_every_n 2`, 해상도 축소 순으로 조정. 평가 리포트의 `latency_ms`(p50/p95)로 확인.

---

## 13. 모듈 담당

| 담당 | 범위 |
|---|---|
| face-architect | `docs/ARCHITECTURE.md`, `docs/INTERFACES.md`, `configs/default.yaml`, `errors.py`, 프로젝트 뼈대 |
| vision-engineer | `config, datatypes, imageio, camera, detector, quality, embedder, liveness, database, recognizer, pipeline, decision, display, events, enroll, app, __main__`, (v1.3) `lockers.py`, `adminpin.py`, `runlock.py`, `gui/*`, `scripts/{download_models,enroll,run,assign_locker,admin_gui}.py`, 해당 테스트 |
| hw-engineer | `hardware/{base,gpio_pi,gpio_mock,factory}.py`, `deploy/{install_rpi.sh,facedoor.service,WIRING.md}`, `docs/PORTING.md`, `scripts/hw_selftest.py`, `tests/test_hardware.py` |
| eval-engineer | `calibration.py`, `evaluation/{dataset,augment,scoring,metrics,calibrate,report}.py`, (v1.3) `evaluation/locker_audit.py`, `scripts/{evaluate,make_synthetic_probes,label_events}.py`, 해당 테스트 |

(v1.3) per_locker 구현 순서: hw `base.py`의 `LockerOutput`/`HardwareConfig` 확장·`DoorStatus` 필드·Protocol 확장 → vision `config.py` 교차 검증·`lockers.py` → vision `app.py`/`events.py`/`display.py`/CLI, hw 컨트롤러 채널화 → eval `locker_audit.py`. `default.yaml`의 주석 처리된 새 키는 config/hardware 파서가 그 키를 받아들이게 된 **뒤에** 주석 해제한다(현재 코드는 알 수 없는 키를 ConfigError로 거부).

의존 순서: `errors`(완료) → hw `base.py`(HardwareConfig/DoorStatus/LedSnapshot) 와 vision `datatypes.py`를 가장 먼저 작성해 다른 쪽이 import할 수 있게 한다. eval의 `calibration.py`는 vision `recognizer.ThresholdPolicy`보다 먼저.

---

## 14. 미결 사항 / 향후

- 실제 카메라·GPIO·사용자 얼굴 데이터로 미검증. Pi5 FPS 미측정.
- 모델 sha256은 아직 고정되지 않음(TOFU: 최초 다운로드 시 `models/models.lock.json`에 기록). 확인 후 `default.yaml`에 고정 권장.
- 실내측 수동 개방 버튼(`exit_button_pin`)은 인터페이스만 존재, 기본 비활성.
- 2차 인증, 실제 liveness 검사기는 범위 밖.
- 관리자 PIN(`admin`)은 예약·미구현. 사물함(`lockers`)은 `mode=single`·`require_locker_id`·`enroll.py --locker-id`(패턴 검증) 구현, `mode=per_locker`는 v1.3 계약(INTERFACES §13)만 정의되고 코드는 미구현이라 여전히 ConfigError로 거부.
- per_locker: 사물함 4개(`"1"`~`"4"`)는 확정. 사용자 확인 필요(INTERFACES §13.11): 실제 핀(제안 BCM24/25/16/26), 릴레이 보드 극성(제안 active-high), 솔밸브 전압·전류와 정전 시 잠김 방식, 공유 필요 여부, 사물함별 LED, 정문+사물함 혼합, 동시 개방 수.
- 리밋센서(INTERFACES §15, 사용자 확인 필요): 리밋 접점 종류(NO/NC)와 모듈 극성·풀업 유무, 제안 핀 BCM 5/6/12/13, `unlock_timeout_sec`(제안 10초)·`close_confirm_sec`(제안 2초)·`max_unlock_sec`(제안 30초, 코드 상한 120초) 값, 솔밸브 연속 통전 정격, 사물함 문이 닫힐 때 솔밸브 래치/볼트 방식(해제 중 문 닫으면 바로 걸리는지 — 바로 걸리면 `close_confirm_sec` 중 이미 잠긴 상태), 센서 오류 알림 방식(현재는 패널 표시·로그만).
- 관리 화면(INTERFACES §14.11): PIN 자릿수(제안 6), 서비스 재시작 버튼(제안: 안 함).

---

## 15. 변경 이력

| 날짜 | 변경 | 작성 |
|---|---|---|
| 2026-10-16 | 최초 작성: 스택(YuNet+SFace, ArcFace 선택), 핀 BCM 17/27/22/23, 임계값 정책(보수적 기본값 + 보정은 상향만/충분 표본 시 하향), 3/5 투표, 이벤트 로그 33컬럼, calibration.json v1 | face-architect |
| 2026-10-16 | QA 통합 리포트 반영: §9 fail-closed 표에 시그널 종료 코드 130·locker 억제 추가, §14에 admin/lockers 상태 기록. 상세 계약은 INTERFACES v1.1(§12 신설) | face-architect |
| 2026-10-16 | vision 수정 반영: §14 lockers 상태 갱신(per_locker ConfigError, --locker-id 구현). 상세는 INTERFACES v1.2 | face-architect |
| 2026-10-16 | 사물함별 출력(per_locker) 설계 추가(코드 미구현): 서두, §1 요구 행, §6.1 신설(채널을 hardware 섹션에 두는 이유, 이중 스위치, 공유 기본 금지, 2중 검사·불변식 FAULT, 동시 개방 1, 배선 안전, 핀 제안), §9 fail-closed 행, §11 감사 지표, §13 담당·구현 순서, §14 미결. 상세는 INTERFACES v1.3 §13 | face-architect |
| 2026-10-16 | 사용자 결정 반영: 사물함 4개(`"1"`~`"4"`) 확정·핀 BCM24/25/16/26 제안·극성 기본·BCM17 하위호환(§6.1); §6.2 한글 관리 화면 신설(tkinter·PIL 없음·runlock·PIN·이름 정책·롤백); §5.3 `hat` global 그룹 결정; §13·§14 갱신. 상세는 INTERFACES v1.3 §12.1/§13/§14 | face-architect |
| 2026-10-16 | v1.4 설계(미구현): §4 닫힘 유지(close-hold) 비대칭 히스테리시스와 10초 절대상한 관계, §6.3 한글 인식 화면(TkDisplay)·사물함 상태 패널·한글화 경계, §9 fail-closed 행. 상세는 INTERFACES v1.4 §15~§17 | face-architect |
| 2026-10-17 | v1.5 설계(미구현): 리밋센서 기반 도어 제어 — §4 limit 모드 상태기계(UNLOCKED_WAITING/OPEN/CLOSING_CONFIRM/COOLDOWN/LOCKED_OPEN/SENSOR_FAULT)와 fail-safe 워치독 설계 이유(v1.4 close-hold 폐기), §6.3 인식 창 레이아웃(왼쪽 영상·오른쪽 사물함 패널 4개)·mock 시뮬레이션, §6.4 리밋 배선·핀(BCM 5/6/12/13)·NC 권장, §9 fail-closed 행 교체, §14 확인 사항. 상세는 INTERFACES v1.5 §15~§17 | face-architect |
