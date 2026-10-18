---
name: gpio-door-control
description: 라즈베리파이5 GPIO로 솔밸브/도어 개방 신호와 확인용 LED를 제어하고, PC에서는 mock으로 시뮬레이션하며, Ubuntu 24.04 배포(systemd, 설치 스크립트, 배선 문서)까지 구성하는 방법. GPIO·릴레이·LED·핀 매핑·안전 타임아웃·라즈베리파이 이식 작업 시 사용한다.
---

# GPIO 도어 제어와 배포

## 설계 원칙
하드웨어 접근은 `src/facedoor/hardware/` 한 곳에 가둔다. 이유: 실제 장치가 없는 지금 PC에서 전체 흐름을 검증하고, 나중에 설정 한 줄(`hardware.mode: pi`)로 전환하기 위해서다.

```python
class DoorController(Protocol):
    def open(self, reason: str, duration: float | None = None) -> None: ...
    def close(self) -> None: ...
    def status(self) -> DoorStatus: ...   # is_open, opened_at, remaining
    def cleanup(self) -> None: ...
```

## 구현 지침
- **Pi5**: `gpiozero.DigitalOutputDevice(pin, active_high=cfg, initial_value=False)`와 `LED`. Pi5의 GPIO는 RP1 칩이라 `RPi.GPIO`가 동작하지 않으므로 `lgpio` 핀 팩토리(`GPIOZERO_PIN_FACTORY=lgpio`)를 쓴다. import는 `factory.py` 안에서 지연시킨다(Windows에서 import 오류 방지).
- **Mock**: `gpiozero.pins.mock.MockFactory`를 쓰거나 순수 Python 상태 객체로 구현한다. 상태 변화를 `logs/hardware.log`와 화면 오버레이(가상 LED/솔밸브 아이콘)에 낸다. 이것이 "신호가 나가고 있는지 확인"하는 현재 단계의 수단이다.
- **상태 머신**: IDLE → OPEN(타이머) → COOLDOWN → IDLE. 솔밸브 출력과 LED(녹)를 같은 전이에서 구동한다. DENIED는 적색 LED 짧게 점멸, HOLD는 황색.
- **안전**: ① `max_open_sec` 워치독(기본 5초, 설정 상한 초과 불가) ② `atexit`/SIGINT/SIGTERM에서 `cleanup()`으로 출력 OFF ③ 부팅·예외 시 초기값 OFF ④ 연속 개방 쿨다운 ⑤ 카메라/모델 오류 시 즉시 `close()`. 솔밸브가 열린 채 고착되는 것이 가장 큰 물리 위험이기 때문이다.
- **설정 예**:
```yaml
hardware:
  mode: auto          # auto|pi|mock
  solenoid_pin: 17    # BCM
  led_green_pin: 27
  led_red_pin: 22
  led_amber_pin: 23
  active_high: true
  open_sec: 3.0
  max_open_sec: 5.0
  cooldown_sec: 2.0
```

## 배선 문서 (`deploy/WIRING.md`에 반드시 포함)
- GPIO는 3.3V 신호 전용, 솔밸브(12/24V)는 직접 구동 금지 → 릴레이 모듈 또는 로직레벨 MOSFET(예: IRLZ44N) + 외부 전원.
- 유도성 부하: 플라이백 다이오드(1N4007 등) 역방향 병렬 필수.
- LED는 220~330Ω 저항 직렬, GND 공통 연결(외부 전원 GND와 Pi GND 공유).
- 릴레이 모듈이 active-low인 경우가 많으므로 `active_high`를 확인하고, 처음에는 솔밸브 대신 LED로 시험한다.
- 도어 잠금 용도라면 전원 차단 시 잠김(fail-secure)/열림(fail-safe) 중 어느 쪽인지 법규·안전 요건에 맞게 사용자가 결정한다고 명시한다.

## Ubuntu 24.04 배포
- `deploy/install_rpi.sh`: `apt install python3-venv python3-lgpio python3-opencv v4l-utils`, venv 생성(`--system-site-packages` 여부 문서화), `pip install -r requirements-pi.txt`, 모델 다운로드, 사용자를 `gpio`/`video` 그룹에 추가.
- `deploy/facedoor.service`: `Restart=on-failure`, `ExecStopPost`로 GPIO OFF 보장, 헤드리스 기본.
- 점검 명령: `v4l2-ctl --list-devices`, `gpioinfo`, `python scripts/hw_selftest.py`(LED 순차 점멸 + 솔밸브 짧은 펄스는 `--allow-solenoid` 지정 시만).
- PC→Pi 이식 체크리스트를 `docs/PORTING.md`에 둔다(카메라 인덱스, 백엔드, 핀, 극성, 권한, 성능: Pi5는 CPU 추론이므로 FPS 측정 후 해상도/프레임 스킵 조정).

## 테스트
mock 모드에서: open 후 타임아웃 자동 close, 예외 중 출력 OFF, 쿨다운 중 재요청 무시, `max_open_sec` 초과 요청 클램프, cleanup 후 상태 OFF.
