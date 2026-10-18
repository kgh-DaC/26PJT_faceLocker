# QA 통합 검증 리포트 (vision + eval + hw 경계면)

- 일시: 2026-10-16, 검증자: qa-inspector
- 범위: `app.py`, `scripts/run.py`, `scripts/enroll.py`, `scripts/evaluate.py`, `recognizer.py`, `pipeline.py`, `hardware/factory.py`, `deploy/facedoor.service`, `docs/INTERFACES.md`
- 원칙: 결함은 고치지 않고 기록만 함. 심각도 순서: 안전 > 정확성 > 이식성 > 문서/스타일

## 0. 실행 결과 요약

| 항목 | 결과 | 근거 / 재현 |
|---|---|---|
| pytest 전체 | **통과 433 / 433** (15.4s) | `.venv/Scripts/python.exe -m pytest -q` |
| E2E 등록 | 통과 | `FACEDOOR_HOME=<tmp> python scripts/enroll.py --name lena --tag normal --images tests/assets/lena.jpg --no-display` → saved 1 (한글 경로 imread 우회 정상). messi5.jpg는 `too_small`로 거부(정상 품질 게이트) |
| E2E 인식→OPEN | 통과 | 밝기 변형 6프레임 폴더로 `run.py --source <dir> --no-display` → frame 2에서 `event=open`, `door_signal=1`, reason `face:lena:1.0000:3/3`, 종료 시 `CLOSE shutdown` → `CLEANUP outputs OFF`, exit 0 |
| E2E Unknown | 통과 | astronaut 6프레임 → 전부 UNKNOWN(점수 약 -0.1), 3번째에서 `deny`, OPEN 0회, door_signal 항상 0 |
| E2E 카메라 실패 | 통과 | 없는 경로 `--source` → exit 3(CAMERA), 출력 OFF + CLEANUP |
| E2E SIGTERM | 통과(fail-closed), 종료 코드는 §B-1 참조 | 루프 중 `signal.raise_signal(SIGTERM)` → 핸들러가 `CLEANUP outputs OFF` → `stop` 이벤트 door_signal=0 → exit **130** |
| E2E evaluate | 통과 | split_by=image: cal 6 / test 6, 누수 probe 1개(gallery 원본과 동일한 lena.jpg) 자동 제외+경고, PNG 5종·metrics.json·scores.csv·per_condition.csv·report.html·config_snapshot.yaml·calibration_candidate.json 생성. `--write-calibration`이 없으면 calibration.json 미생성 확인, 있으면 임시 루트의 models/에만 생성. `--no-calibrate`, 기본 split_by=person(사람 1명 → cal 0, 경고 후 현재 임계값으로 평가) 모두 exit 0 |
| 지표 손계산 | 통과 | test genuine 3 / impostor 3, 오수용 0 → FAR 0, FRR 0, far_upper95 = 3/3 = 1.0 (Rule of three) 일치 |
| 프로젝트 오염 | 없음 | 모든 E2E는 scratchpad 아래 `FACEDOOR_HOME`에서 실행했고 끝난 뒤 삭제함. 프로젝트의 `data/`, `logs/`, `reports/`, `models/calibration.json`은 여전히 없음 |

미검증: 실기(Pi5/lgpio/V4L2)는 이 PC에서 확인할 수 없음. Windows `os.kill(SIGTERM)`은 핸들러를 거치지 않으므로 SIGTERM은 프로세스 내부 `raise_signal`로만 확인함.

## 1. 경계면 검증

### 1.1 `pipeline.recognizer.policy` 대입 (evaluate.py:156)
- `Recognizer.__init__`이 `self.policy = policy`로 public 속성으로 저장한다(recognizer.py:111).
- `identify()`는 호출할 때마다 `pol = self.policy`를 읽는다(recognizer.py:145). 캐시하지 않으므로 다시 대입하면 바로 반영된다. `score_all()`은 정책과 무관하다.
- E2E 로그에서도 다시 대입한 뒤 정책 source가 `default`에서 `max(calibration,default)`로 바뀐 것을 확인했다(전역 기본값을 0.6으로 올린 실행).
- **결론: 동작은 정상.** 다만 계약 문서(§3.7)에는 `policy`와 `db` 속성이 없다. 테스트도 없어서 "다시 대입하면 identify에 반영된다"는 사실을 보증하는 장치가 없다 → C-2.

### 1.2 `door.open` 호출 위치
- `src/`·`scripts/` 전체에서 `door.open(` 호출은 `app.py:197` 한 곳뿐이다. `decision.should_open is True` 분기 안에 있고, `processed` 프레임이고, `person_id`가 있고, locker 검사를 통과한 경우에만 호출된다.
- 건너뛴 프레임(`process_every_n`)에서는 `should_open/should_deny`를 False로 덮어쓴다(app.py:183).
- `should_open`은 MATCH 투표로만 True가 된다(decision 규칙 6. `test_never_opens_without_match`, `test_unknown_person_never_opens`, `test_hold_never_opens`로 증명).

### 1.3 fail-closed
- `step()`에서 일어나는 모든 `Exception`은 `_safe_close_door()`를 먼저 부르고 그다음에 error 이벤트를 남긴다. `FaceDoorError`이면 해당 ExitCode로 종료하고, 그 밖의 예외는 10회 연속이면 종료한다.
- `KeyboardInterrupt`/`SystemExit`(시그널)는 `run()`에서 잡아 문을 닫고 130으로 끝낸다. `finally: shutdown()` 순서는 door.close → leds.off → hardware.cleanup → stop 로그 → display/camera close이며 멱등이다. `main()`의 `finally: hardware.cleanup()`이 이중 보장한다.
- 하드웨어는 파이프라인/카메라보다 먼저 만들어지고, atexit·SIGINT·SIGTERM에 cleanup이 등록된다.

## 2. 결함 및 권고 (심각도순)

### A. 안전

**A-1 (낮음-중간) 비치명 예외 뒤에도 투표 창이 유지됨** — `src/facedoor/app.py:148-169`
- `step()`의 비치명 예외 경로(예: cv2.error)는 문을 닫고 루프를 계속 돌지만 `self.decider.reset()`은 부르지 않는다. 그래서 MATCH, MATCH, 예외, MATCH로 이어지면 예외 전후의 표가 합쳐져 OPEN이 될 수 있다(window_timeout 2s 안이라면).
- 오개방 위험은 낮다(모두 같은 사람의 MATCH여야 함). 하지만 "오류 뒤에는 처음부터 다시 판정한다"는 원칙에 어긋난다.
- 권고(vision): except 블록에서 `decider.reset()`을 호출하고, 테스트 하나를 추가한다.

### B. 정확성 / 운영

**B-1 (중간) SIGTERM 종료 코드 130과 systemd 유닛 불일치** — `src/facedoor/app.py:251-253`, `deploy/facedoor.service`
- SIGTERM이 오면 factory 핸들러가 `SystemExit(143)`을 던지고, app이 이를 `ExitCode.INTERRUPTED(130)`으로 바꾼다. 안전 면에서는 정상이다(출력 OFF 확인).
- 그런데 유닛에 `SuccessExitStatus`가 없다. 그래서 `systemctl stop`을 할 때마다 서비스가 종료 코드 130으로 끝나 `failed` 상태로 남는다. `Restart=on-failure`는 명시적 stop에는 재시작하지 않지만, 상태 표시와 모니터링이 매번 실패로 보인다.
- 권고: **코드는 유지하고 문서와 배포 파일을 고친다.**
  - INTERFACES §10에 "SIGINT/SIGTERM → INTERRUPTED(130)"을 명시한다.
  - `facedoor.service`에 `SuccessExitStatus=130 143`을 추가한다(hw 담당).
  - ExitCode에 143을 따로 추가할 필요는 없다. 원인은 `stop` 이벤트 message에 `terminated (143)`으로 이미 남는다.

**B-2 (낮음-중간) 억제/거부된 개방도 `event=open`으로 기록됨** — `src/facedoor/events.py:126-127`, `app.py:192-199`
- locker 때문에 억제되었거나(door.open을 부르지 않음) 문이 busy/cooldown이라 거부된 경우에도 행의 `event`는 `open`이다. 이때 `door_signal`은 0 또는 직전 값이다.
- events.csv에서 `event=open`의 개수를 세면 실제 개방 횟수보다 많아진다.
- 권고: 문서 §5에 "`open`은 *결정* 이벤트이며 실제 출력은 `door_signal`과 hardware.log로 판단한다"고 명시한다. 또는 `open_suppressed` 이벤트를 추가한다(계약 변경 → architect 승인 필요). 최소한 문서는 고칠 것.

**B-3 (낮음) OPEN reason의 `<window>`가 설정 M이 아니라 현재 창 길이** — `decision.py:121`
- E2E에서 `face:lena:1.0000:3/3`이 나왔다(window_frames=5). `best_score`도 그 프레임 점수(0.9961)가 아니라 창 안의 최고값(1.0000)이다.
- §3.13/§5는 예시로 `2/5`를 보여 줘서 해석이 모호하다.
- 권고: 문서에 "votes/현재 창 길이, best_score = 창 안 해당 인물 최고 점수"로 정의를 명시한다(코드 유지).

**B-4 (낮음) 가려진 그룹 임계값을 global로 올렸을 때 source 라벨** — `recognizer.py:84`
- occluded 보정이 없는데 global로 올려진 경우 `source_for("occluded")`가 global의 source(`max(calibration,default)`)를 그대로 쓴다. 실제로는 occluded 보정값을 쓰지 않았는데도 그렇게 표시되어 오해를 부른다.
- 권고: 출처를 `global` 등으로 구분하는 표기를 검토한다(§5 `threshold_source`의 허용 값과 함께 문서화해야 하므로 우선순위는 낮음).

### C. 이식성

**C-1 (중간) Pi에서 프로젝트 경로가 비ASCII면 systemd 아래에서 모델 로드 실패** — `src/facedoor/imageio.py:77-96`
- `ascii_safe_path()`는 OS와 무관하게 경로가 비ASCII이면 모델/동영상을 `~/.cache/facedoor`로 복사한다.
- Linux의 OpenCV는 UTF-8 경로를 그대로 처리하므로 Pi에서는 이 우회가 필요 없다. 오히려 `facedoor.service`의 `ProtectHome=read-only` 때문에 `mkdir`에서 `PermissionError`가 난다.
- 이 예외는 `FaceDoorError`가 아니라서 `main()`이 잡지 못하고 traceback과 함께 exit 1로 끝난다. 하드웨어 cleanup은 `finally`에서 실행되므로 안전은 유지되지만, `Restart=on-failure` 때문에 재시작 루프에 빠진다.
- 현재 `install_rpi.sh`는 저장소 위치를 그대로 ROOT로 쓰므로, 폴더 이름 `PJT5_얼굴인식`을 그대로 복사하면 이 문제가 발생한다. PORTING.md:68은 "코드는 어느 쪽이든 동작"이라고 쓰고 있어 사실과 다르다.
- 권고(vision): `ascii_safe_path`를 `sys.platform.startswith("win")`일 때만 적용한다. 또는 OSError를 `ModelError`로 바꿔 던진다. 문서도 함께 수정한다.

**C-2 (확인됨, 결함 아님)** Pi 전용 import(`gpiozero`, `lgpio`)는 모두 함수/생성자 안의 지연 import다(gpio_pi.py:37,52,87,91,241, factory.py:196). 카메라 백엔드는 auto일 때 win→CAP_DSHOW, linux→CAP_V4L2로 분기한다(camera.py:43-46). 경로는 pathlib을 사용하고, 하드코딩된 `\\`·드라이브 문자는 Windows 전용 분기 안(imageio.py:61-62, 88)에만 있다. 로그 파일은 `newline="\n"`로 쓴다.

### D. 문서 / 계약 불일치

**D-1 (중간) INTERFACES §12가 없음** — `configs/default.yaml:158,164`, `config.py:189,199`, `database.py:4`가 "INTERFACES §12"를 참조하지만 문서는 §11(변경 이력)에서 끝난다.
- `admin` 섹션: 설정과 검증만 있고 이를 사용하는 코드가 전혀 없다(PIN 메뉴 미구현).
- `lockers` 섹션: `require_locker_id`만 app에 반영되어 있다(인식된 사람이라도 locker_id가 없으면 열지 않음. 제한만 추가하는 방향이라 안전함). 하지만 다음이 빠져 있다.
  - `enroll.py`에 `--locker-id` 옵션이 없다. 현재는 person.json을 직접 편집하는 방법밖에 없어서, `require_locker_id=true`로 설정하면 모든 사람이 열 수 없게 된다.
  - `locker_id_pattern`으로 검증하는 곳이 없다.
  - `mode=per_locker`가 검증을 통과하지만 미구현이어서 조용히 single로 동작한다.
- **권고: 문서와 코드 둘 다 고친다.**
  1. architect가 §12 "관리자 PIN과 사물함(locker)"를 신설하고 §11 이력을 갱신한다. 상태는 admin=예약(미구현), lockers=single만 지원으로 둔다. person.json의 `locker_id` 필드, `FaceDatabase.person_info/set_person_info/locker_id/display_name`, 억제 시 로그 형식도 여기에 정의한다.
  2. §2 AppConfig 목록에 `admin`, `lockers`(선택 섹션)를 추가한다.
  3. 코드(vision): `lockers.mode=per_locker`이면 ConfigError로 막는다(미구현 기능이 켜진 것처럼 보이지 않게). `enroll.py --locker-id`를 추가하고 pattern으로 검증한다. 이것이 없으면 `require_locker_id`를 실제로 쓸 수 없다.
  4. admin: 구현 계획이 없다면 default.yaml 주석을 "예약됨, 미구현"으로 바꾼다.

**D-2 (낮음) `--count` 의미** — `scripts/enroll.py:9,122`
- 라이브 모드에서 `spp = ceil(count/len(poses))`이므로 실제 저장 목표는 `len(poses) * spp`다. 예를 들어 `--count 3`이고 포즈가 5개이면 5장이 저장된다. "total samples"라는 설명과 다르다. `--images` 모드에서는 "최대 파일 수"를 뜻하므로 의미가 두 가지다.
- 권고: §9에 "live: 최소 N, 포즈 수의 배수로 올림. --images: 사용할 최대 파일 수"라고 명시한다(코드 유지). 정확히 N장을 원하면 vision이 마지막 포즈에서 잘라 내도록 고친다(선택).

**D-3 (낮음) 시그니처/옵션 차이 (문서 갱신 권고, 코드 유지)**
- §3.7 `Recognizer`: public 속성 `db`, `policy`(다시 대입 가능)와 `refresh()`가 문서에 없다. evaluate.py와 app.py가 이것들에 의존하므로 문서에 넣는다. 이와 함께 "policy를 다시 대입하면 다음 identify부터 반영된다"를 고정하는 테스트를 추가할 것을 권고한다(vision/eval).
- §3.8 `from_config`: 실제로는 `calibration` 인자를 생략하면 calibration 파일을 로드하고(`_AUTO`), 명시적으로 None을 주면 기본값만 쓴다. `allow_empty_gallery` 인자도 있다. 문서는 `= None` 기본값으로 적혀 있어 의미가 반대로 읽힌다. **eval 쪽 오용 위험이 있으므로 우선 갱신할 것.**
- §8 `calibrate(..., *, data=)`, `scoring.records_from_events`, `summary.compute_metrics`, `verification_scores/openset_scores(include_hold=)`가 문서에 없다.
- §9: `evaluate.py --no-calibrate --config --set`, `enroll.py --display-name`, `hw_selftest.py --force-off --hardware-log`가 빠져 있다.
- §3.13 `FaceDoorApp(..., clock=)`이 추가되어 있다.

## 3. 재현 방법 (임시 루트)

```bash
S=<scratch>; P=<project>
mkdir -p $S/e2e/configs $S/e2e/models; cp $P/configs/default.yaml $S/e2e/configs/; cp $P/models/* $S/e2e/models/
FACEDOOR_HOME=$S/e2e $P/.venv/Scripts/python.exe $P/scripts/enroll.py --name lena --tag normal --images $P/tests/assets/lena.jpg --no-display
FACEDOOR_HOME=$S/e2e $P/.venv/Scripts/python.exe $P/scripts/run.py --source <lena 변형 폴더> --no-display   # exit 0, event=open 1회
FACEDOOR_HOME=$S/e2e $P/.venv/Scripts/python.exe $P/scripts/evaluate.py --set evaluation.split_by=image   # data/probe/normal/lena, data/probe/_impostor/astro 준비 후
```

## 4. 담당별 조치 목록
- vision: A-1, C-1, D-1(per_locker 차단, enroll --locker-id), D-2(선택), Recognizer.policy 다시 대입 테스트
- hw: B-1(`SuccessExitStatus=130 143`)
- eval: D-3에서 from_config 시맨틱을 확인(현재 사용은 올바름)
- architect: §12 신설, §2/§3.7/§3.8/§5/§8/§9/§10 갱신, §11 이력
