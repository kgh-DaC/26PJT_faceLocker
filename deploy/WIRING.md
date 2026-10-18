# 배선 가이드 — Raspberry Pi 5 ↔ 솔밸브 릴레이/MOSFET + LED 3개 (+ 사물함 4채널)

> 상태: **실기 미검증.** PC에서 작성했고 Pi 5·릴레이 모듈·솔밸브 실물로 확인하지 않았다. 처음 연결할 때는 반드시 아래 "시험 순서"를 따른다.
> 핀 번호와 극성의 정본은 `configs/default.yaml`의 `hardware` 섹션이다(사물함 채널은 `configs/local.yaml`의 `hardware.locker_outputs`). 핀/극성을 바꾸면 이 문서도 함께 고친다.
> 1~6절은 단일 출력(`lockers.mode: single`, BCM17 솔밸브 1개), **7절은 사물함별 출력(`per_locker`, 사물함 4개)**, **8절은 리밋센서(문 열림/닫힘) 입력과 잠금 제어(`close_mode: limit`)** 이다. `configs/default.yaml`의 기본은 per_locker + limit이다.

## 1. 핀 배정 (BCM 번호 기준, 40핀 헤더)

| 기능 | 설정 키 | BCM | 물리 핀 | 방향 | 비고 |
|---|---|---|---|---|---|
| 솔밸브 개방 신호 → 릴레이/MOSFET 입력 | `solenoid_pin` | GPIO17 | 11 | 출력 | 극성 `solenoid_active_high` |
| 녹색 LED (개방 신호 출력 중) | `led_green_pin` | GPIO27 | 13 | 출력 | 솔밸브 신호와 같은 전이에서 켜고 끔 |
| 적색 LED (거부) | `led_red_pin` | GPIO22 | 15 | 출력 | 거부 시 3회 점멸 |
| 황색 LED (판정 보류/처리 중) | `led_amber_pin` | GPIO23 | 16 | 출력 | |
| 리밋센서 입력 (single 모드, 8절) | `limit_switch_pin` | GPIO5 (예) | 29 | 입력(내부 풀업) | 리밋 → GND(물리 30). per_locker는 7.1의 `limit_pin` |
| 실내 수동 개방 버튼 (선택) | `exit_button_pin` | 미할당 | — | 입력(내부 풀업) | 버튼 → GND |
| GND | — | — | 6, 9, 14, 20, 25, 30, 34, 39 | — | 외부 전원 GND와 **공통** 연결 |
| 3.3V | — | — | 1, 17 | — | 로직 전원 전용, 부하 구동 금지 |
| 5V | — | — | 2, 4 | — | 릴레이 모듈 VCC 용(모듈 사양 확인) |

## 2. 전기적 한계 (반드시 지킬 것)

- Pi 5 GPIO는 **3.3V 로직 전용**. 5V 이상을 GPIO 핀에 넣으면 RP1이 손상된다. 5V 신호를 읽어야 하면 레벨 시프터/분압기를 쓴다.
- 핀당 출력 전류는 **수 mA 수준으로 설계**한다(RP1 기본 구동 강도 기준 수 mA, 절대 16mA 초과 금지, 전체 합계도 작게). LED는 전류 제한 저항 필수, 그 외 부하는 직접 구동하지 않는다.
- **솔밸브(12/24V, 유도성 부하)는 GPIO로 절대 직접 구동하지 않는다.** 릴레이 모듈 또는 로직레벨 MOSFET + 외부 전원을 쓴다.
- 솔밸브 코일에는 **플라이백 다이오드(1N4007 등)를 코일과 역방향 병렬**로 단다(캐소드 → +V 쪽). 차단 순간의 역기전력이 MOSFET/릴레이 접점을 망가뜨리고 노이즈로 Pi를 리셋시킬 수 있다.
- 솔밸브 전원(12/24V)과 Pi 전원(USB-C 5V/5A)은 분리하고, **GND만 공통**으로 묶는다(옵토 절연 릴레이 모듈의 JD-VCC 분리 사용 시 제외 — 모듈 설명서 따름).

## 3. 회로

### 3.1 방법 A — 로직레벨 N-MOSFET 로우사이드 스위치 (권장: 극성 단순, active-high)

```
             +12V/24V (솔밸브 전원)
                 │
         ┌───────┴───────┐
         │               │
     [솔밸브 코일]     ─┴─  D1 1N4007 (캐소드가 +V 쪽)
         │             ─┬─
         └───────┬───────┘
                 │ Drain
GPIO17 ──[R1 100Ω]──┬── Gate   Q1 IRLZ44N (또는 AO3400 등 Vgs(th) < 2V, 3.3V에서 완전 도통하는 로직레벨 MOSFET)
                    │          Source
                [R2 100kΩ]      │
                    │           │
GND(Pi) ────────────┴───────────┴──── GND(외부 전원)   ← 공통 GND
```

- R2(게이트 풀다운 10k~100kΩ): 부팅 중이나 프로그램 종료 후 GPIO가 입력(하이 임피던스)으로 바뀌어도 MOSFET이 **꺼진 상태**를 유지하게 한다.
- 설정: `solenoid_active_high: true` (기본값).
- IRLZ44N은 Vgs=3.3V에서 Rds(on)이 데이터시트 보장 조건(4~5V) 밖이다. 솔밸브 전류가 1A를 넘으면 3.3V 구동이 보장된 MOSFET(예: AO3400, IRLML6344)이나 게이트 드라이버/MOSFET 모듈을 쓴다.

### 3.2 방법 B — 릴레이 모듈 (옵토 절연 1채널)

```
Pi 5V(물리 2) ─── 모듈 VCC         모듈 COM ───── +12/24V
Pi GND        ─── 모듈 GND         모듈 NO  ───── 솔밸브(+)    솔밸브(-) ──── 전원 GND
GPIO17        ─── 모듈 IN          솔밸브 양단에 D1 1N4007 역병렬 (모듈에 없으면 추가)
```

- 시중 릴레이 모듈은 **active-LOW(IN=LOW일 때 릴레이 ON)** 가 많다 → `solenoid_active_high: false`.
- 3.3V GPIO로 5V 모듈 입력을 확실히 켜는지 확인한다(옵토 LED 전류). 안 되면 트랜지스터 버퍼나 3.3V 호환 모듈을 쓴다.
- **부팅 시 오동작 주의**: BCM17 등 GPIO9~27은 전원 투입 직후 **내부 풀다운(LOW)** 상태다. active-LOW 모듈은 부팅 중(프로그램 시작 전) **릴레이가 켜져 문이 열릴 수 있다.**
  대책 중 하나를 반드시 적용한다: (1) IN 단자에 외부 풀업(10kΩ → 3.3V)이 있는 모듈을 쓰거나 추가, (2) active-HIGH 모듈 또는 방법 A 사용, (3) 기본 풀업인 GPIO0~8 사용(I2C/SPI와 충돌 검토 필요, 핀 변경 시 이 문서와 설정 갱신).
  같은 이유로 프로그램 종료 후 라인이 해제되어 입력으로 돌아가는 경우에도 외부 풀업/풀다운이 "꺼짐"을 보장해야 한다.

### 3.3 LED (3개 동일)

```
GPIO27 ──[R 330Ω]──►|── GND     (녹색)
GPIO22 ──[R 330Ω]──►|── GND     (적색)
GPIO23 ──[R 330Ω]──►|── GND     (황색)
```

- 220~330Ω 직렬 저항 (3.3V, 일반 LED 기준 약 2~5mA). 저항 없이 연결 금지.
- 위 결선은 active-high (`led_active_high: true`). LED를 3.3V 쪽에 물리고 GPIO로 싱크하면 `false`.

### 3.4 실내 수동 개방 버튼 (선택)

`exit_button_pin`에 BCM 번호를 지정하면 내부 풀업 입력으로 잡힌다. 버튼 한쪽 → GPIO, 다른 쪽 → GND. 긴 배선은 RC 필터(1kΩ + 100nF)를 권장. (앱 쪽 콜백 연결은 vision `app.py` 담당, 현재 기본 비활성)

## 4. 소프트웨어 안전 동작과 배선의 관계

| 상황 | 소프트웨어 | 배선이 보장해야 할 것 |
|---|---|---|
| 부팅 ~ 서비스 시작 전 | 아무것도 못 함 | 풀다운/풀업으로 출력 꺼짐 (3.2 주의) |
| 서비스 시작 | `ExecStartPre`(`hw_selftest.py --force-off`)가 먼저 모든 출력을 OFF, 이어서 앱이 `initial_value=False`로 claim | — |
| 개방 | 최대 `max_open_sec`(기본 5초, 코드 상한 10초) 후 워치독이 OFF | — |
| 프로세스 종료/예외/SIGTERM/SIGINT | `cleanup()`으로 OFF, systemd `ExecStopPost`(`hw_selftest.py --force-off`)가 lgpio로 다시 OFF | 라인 해제 후에도 꺼짐 유지 |
| Pi 전원 상실 | 없음 | 아래 6절 결정 |

## 5. 시험 순서 (처음 연결할 때)

1. 솔밸브 **없이**, 릴레이 입력 자리(GPIO17)에 LED+330Ω을 연결한다.
2. `python scripts/hw_selftest.py --mode pi` → 적/황 LED 순차 점멸 확인.
3. `python scripts/hw_selftest.py --mode pi --allow-solenoid --pulse-sec 0.5` → GPIO17 LED와 녹색 LED가 함께 0.5초 켜지는지 확인.
4. 릴레이 모듈을 연결(솔밸브 미연결)하고 3을 반복, 딸깍 소리와 모듈 LED로 극성(`solenoid_active_high`) 확인. 반대로 동작하면 극성 설정을 바꾼다.
5. 재부팅해서 **부팅 중 릴레이가 켜지지 않는지** 확인(3.2 주의).
6. 솔밸브 + 다이오드를 연결하고 3을 반복. 이후 `sudo systemctl stop facedoor` / `kill -9` 후에도 출력이 꺼지는지 확인.
7. `gpioinfo`로 GPIO17 라인이 사용 중(consumer)인지, 서비스 정지 후 해제되는지 확인.
8. 서비스가 꺼진 상태에서 `python scripts/hw_selftest.py --mode pi --allow-solenoid --pulse-sec 0.5`를 실행하다 중간에 `kill -9`로 끊고, `python scripts/hw_selftest.py --force-off`를 실행한다. 릴레이와 LED가 모두 꺼지는지 확인한다(`ExecStopPost`와 같은 경로). 이상한 상태가 남았을 때 수동 복구 명령으로도 쓴다.

## 6. 전원 차단 시 동작 (사용자 결정 사항)

도어 잠금 장치는 전원이 끊겼을 때 **잠김 유지(fail-secure)** 와 **열림(fail-safe)** 중 하나다. 비상 탈출·화재 관련 법규와 현장 안전 요건에 따라 **설치자가 결정**해야 하며, 이 소프트웨어는 그 선택을 대신하지 않는다. 출입 통제용이라면 실내측 기계식 비상 개방 수단을 별도로 확보한다.
솔밸브(유체 제어)라면 NC(상시 닫힘) 밸브를 쓰면 전원·Pi 고장 시 닫힘이 된다.

## 7. 사물함별 출력 (`per_locker`, 사물함 4개 `"1"`~`"4"`)

계약: `docs/INTERFACES.md` §13. 사물함마다 릴레이(또는 로직레벨 MOSFET) 1채널 + 솔밸브 1개. **인식된 사람의 배정 사물함 채널 하나만** 켜진다. 녹색 LED는 공용(어느 채널이든 ON이면 ON).

### 7.1 채널 표 (기본 제안 핀 — 실배선 후 "실물 확인" 열을 채울 것)

| 사물함 | `locker_id` | 솔밸브 BCM (물리) | 극성(`active_high`) | 부팅 기본 풀 | 리밋 입력 `limit_pin` BCM (물리) / 리밋 GND | 실물 확인(시나리오 30) |
|---|---|---|---|---|---|---|
| 1 | `"1"` | GPIO24 (18) | true (생략 시 `solenoid_active_high`) | 풀다운 | GPIO5 (29) / 30 | ☐ 열린 문 = 1번, ☐ 리밋 1 = 1번 문 |
| 2 | `"2"` | GPIO25 (22) | true | 풀다운 | GPIO6 (31) / 30 | ☐ 열린 문 = 2번, ☐ 리밋 2 = 2번 문 |
| 3 | `"3"` | GPIO16 (36) | true | 풀다운 | GPIO12 (32) / 34 | ☐ 열린 문 = 3번, ☐ 리밋 3 = 3번 문 |
| 4 | `"4"` | GPIO26 (37) | true | 풀다운 | GPIO13 (33) / 34 | ☐ 열린 문 = 4번, ☐ 리밋 4 = 4번 문 |
| (공용) 녹색 LED | — | GPIO27 (13) | `led_active_high` | — | — | |

- 이 핀들은 기존 솔밸브 17, LED 27/22/23과 겹치지 않고, SPI0(7~11)·I2C(2/3)·UART(14/15)·HAT EEPROM(0/1)을 피한다. 예약 핀 0,1,2,3,14,15는 설정 단계에서 거부된다(ConfigError).
- **BCM17은 per_locker에서 사용하지 않는다**(`solenoid_pin: null` 필수, claim·구동·force-off 대상 아님). 기존 정문 릴레이가 BCM17에 있으면 분리하거나 `{locker_id: "1", pin: 17}`처럼 사물함 채널로 명시해 재사용한다.
- 확장 후보: 19(35), 20(38), 21(40). BCM 5/6(리밋 입력)은 부팅 기본 **풀업**이라 입력으로 쓰기에 맞다. 출력(active-high)에는 쓰지 않는다(시작 시 경고).
- 사물함 본체에 번호 라벨을 붙이고 이 표와 같게 유지한다. **표의 "실물 확인"을 모두 채우기 전에는 운영 금지.**

설정 예 (`configs/local.yaml`):

```yaml
hardware:
  mode: pi
  solenoid_pin: null
  solenoid_active_high: true     # 채널 active_high 생략 시 기본값
  max_concurrent_open: 1
  close_mode: limit              # 8절. 리밋을 아직 달지 않았다면 timer (limit_pin 생략 가능, 고정 open_sec)
  limit_active_low: false        # 권장: 리밋 NC 접점 배선 (8절). NO 접점이면 true
  locker_outputs:                # 반드시 리스트(매핑 금지). 선언 순서 = 채널 순서
    - {locker_id: "1", pin: 24, limit_pin: 5}
    - {locker_id: "2", pin: 25, limit_pin: 6}
    - {locker_id: "3", pin: 16, limit_pin: 12}
    - {locker_id: "4", pin: 26, limit_pin: 13}
lockers:
  mode: per_locker
```

(`configs/default.yaml`에는 위 핀이 이미 들어 있고 `mode: mock`이다. 실기는 `configs/local.yaml`에 `hardware: {mode: pi}` 한 줄과 극성/접점 방식만 덮어쓴다.)

### 7.2 회로

각 채널은 3.1(MOSFET) 또는 3.2(릴레이)와 같은 회로를 4번 반복한다. 솔밸브마다 플라이백 다이오드(1N4007 등, 캐소드 → +V)를 단다. MOSFET 방식은 채널마다 게이트 풀다운(10k~100kΩ)을 달아 부팅 중·종료 후 꺼짐을 보장한다.

```
GPIO24 ──► CH1 IN ──► 사물함1 솔밸브(+D1)
GPIO25 ──► CH2 IN ──► 사물함2 솔밸브(+D2)
GPIO16 ──► CH3 IN ──► 사물함3 솔밸브(+D3)
GPIO26 ──► CH4 IN ──► 사물함4 솔밸브(+D4)
Pi GND ─────────── 모듈 GND ─── 솔밸브 전원 GND   (비절연 구성은 GND 공통)
```

- 4채널 릴레이 보드는 **active-low가 흔하다** → 채널별 `active_high: false`. 이 경우 3.3V HIGH에서 릴레이가 **완전히 꺼지는지** 실측한다(JD-VCC 5V 구성에서 옵토 LED 잔류 전류로 반쯤 켜지는 문제). 권장은 3.3V 로직에서 확실히 켜지고 꺼지는 active-high 드라이버(로직레벨 N-MOSFET 모듈, 또는 High-trigger 점퍼를 H로 둔 옵토 릴레이 보드).
- 릴레이 보드의 코일 전원(JD-VCC)은 **Pi 5V/3.3V 레일에서 가져오지 않는다**(코일 4개 합계 전류 + 돌입). 별도 5V 전원 사용.
- GPIO 핀은 릴레이 입력(옵토 LED, 수 mA)만 구동한다. 코일·솔밸브를 직접 구동하지 않는다.

### 7.3 전원 용량

솔밸브 전원 용량 ≥ `max_concurrent_open × 솔밸브 정격 전류 × 1.5`(돌입 여유) + 릴레이 코일 전류 합.

| 예 | 계산 | 필요 용량 |
|---|---|---|
| 12V 0.5A 솔밸브, `max_concurrent_open: 1`, 코일 4×70mA(5V 별도) | 1 × 0.5 × 1.5 | 12V **0.75A 이상**(여유 두고 1A) + 5V 0.3A |
| 같은 솔밸브, `max_concurrent_open: 2` | 2 × 0.5 × 1.5 | 12V **1.5A 이상**(여유 두고 2A) |

- **`max_concurrent_open`을 올리면 전원을 다시 산정한다**(코드 상한 4).
- 솔밸브 사양(전압/전류)과 정전 시 잠김(fail-secure)/열림(fail-safe)은 아직 미정 — 사물함별 솔밸브 종류로 사용자가 결정(6절).

### 7.4 극성 주의 — 첫 시험은 솔밸브 전원을 분리하고

**극성(`active_high`)을 잘못 쓰면 앱이 시작하는 순간 "OFF"로 구동한 레벨이 실제로는 ON이 되어 사물함 4개가 모두 열린다.** 그래서:

1. 솔밸브(12/24V) 전원을 **분리**한 상태로 Pi 전원을 넣고, 부팅~앱 시작 동안 채널마다 릴레이/모듈 LED가 **한 번도 켜지지 않는지** 육안 확인(시나리오 29). 켜지는 채널이 있으면 극성 또는 부팅 풀업/풀다운 문제다.
2. 솔밸브 전원 분리 상태에서 `python scripts/hw_selftest.py --mode pi --allow-solenoid --all-lockers --pulse-sec 0.5`
   → 사물함 1→2→3→4 순서로 **한 번에 한 채널만** 0.5초 켜진다(다음 채널 전에 이전 채널 OFF를 확인). 모듈 LED 번호와 순서가 맞는지 본다. 특정 사물함만: `--locker 2 3`.
3. 반대로 동작하면(평소 켜져 있고 펄스 때 꺼짐) 그 채널의 `active_high`를 바꾼다.
4. 솔밸브 전원을 연결하고 2를 반복하면서 **실제로 열린 문 번호 = 라벨 번호**인지 사물함마다 확인 → 7.1 표 "실물 확인" 기록(시나리오 30). 이 확인 전에는 운영 금지.
5. 서비스 정지/`kill -9` 후 `python scripts/hw_selftest.py --force-off` → solenoid(설정 시)·사물함 4채널·LED 3개가 모두 OFF 행으로 출력되는지 확인. 실패 행이 있으면 종료 코드 5.

### 7.5 소프트웨어 안전 동작 (per_locker)

| 상황 | 동작 |
|---|---|
| 시작 | 모든 채널을 OFF로 claim. 하나라도 실패하면 이미 잡은 핀을 OFF·해제하고 종료(코드 5). 일부 채널만으로 동작하지 않음 |
| 개방 전/후 점검 | OPEN이 아닌 채널이 ON으로 읽히거나 대상 채널이 ON으로 읽히지 않으면 **전 채널 OFF + FAULT**(재시작 전까지 개방 금지) |
| 거부 | 미설정 사물함·locker_id 없음·이미 열림(busy)·쿨다운·동시 개방 상한 → 출력 변화 없음, `hardware.log`에 `code=` |
| 종료/예외/시그널 | 즉시 전 채널 OFF. systemd `ExecStartPre`/`ExecStopPost`의 `--force-off`가 전 채널을 다시 OFF |

## 8. 리밋센서(문 열림/닫힘 감지)와 잠금 제어 (`hardware.close_mode: limit`)

계약: `docs/INTERFACES.md` §15. **얼굴 인식은 솔밸브 "해제"에만 쓴다.** 해제된 뒤 언제 다시 잠글지는 문에 달린 리밋센서가 결정한다.

| 단계 | 동작 |
|---|---|
| 해제 | 인식 성공 → 그 사물함 솔밸브 ON (`UNLOCKED_WAITING`). 이미 문이 열려 있거나 센서를 못 읽으면 해제하지 않는다(`door_open` / `sensor_fault` 거부) |
| 문이 안 열림 | `unlock_timeout_sec`(10초) 안에 문이 안 열리면 자동 재잠금 |
| 문 열림 | 리밋 해제가 `open_confirm_sec`(0.1초) 연속 → `OPEN`. 문이 열려 있는 동안은 **시간으로는 잠기지 않는다** |
| 닫힘 | 리밋 눌림이 `close_confirm_sec`(2초) 연속 → 솔밸브 OFF. 도중에 한 번이라도 해제되면 카운터 재시작(릴레이는 안 건드림) |
| 절대상한 | 해제 시점부터 `max_unlock_sec`(30초)에 센서와 무관하게 잠금. 연장 불가, 문이 아직 열려 있으면 `LOCKED_OPEN`(닫히기 전 재해제 거부) |
| 센서 오류 | 읽기 실패가 `sensor_fault_sec`(1초) 지속 → 즉시 잠금(`SENSOR_FAULT`), 복구 후에도 `close_confirm_sec` 동안 정상이어야 해제 가능 |

### 8.1 리밋 배선 (BCM 입력, 3.3V 전용)

```
 3.3V ──(Pi 내부 풀업, limit_pull_up: true)── GPIO5 ──┬── 리밋 COM
                                                      │      NC 접점 ──┐   (권장, 아래 참고)
 Pi GND(물리 30) ─────────────────────────────────────┴──────── 리밋 단자 ┘
```

- 리밋 한쪽은 BCM 입력 핀, 다른 쪽은 GND. **5V·12V를 입력 핀에 절대 연결하지 않는다**(3.3V 입력, 핀 최대 허용 전압 3.3V). 솔밸브 전원선과 리밋선을 한 케이블 다발로 길게 묶으면 유도 노이즈가 생기니 분리하거나 차폐선을 쓴다. 긴 배선이면 입력 핀에 1kΩ 직렬 저항 + 10nF를 GND로 단다.
- 접점 바운스(수 ms)는 소프트웨어 디바운스(`open_confirm_sec`/`close_confirm_sec`)가 흡수한다.
- 센서를 잠금과 **같은 문틀**에 달고, 문이 닫혔을 때 눌리도록(눌림 = 닫힘) 설치한다.

**권장 fail-safe 배선(설치자 확인 필요): 리밋의 NC(상시 닫힘) 접점 + `limit_active_low: false`.** 문이 닫혀 스위치가 눌리면 NC 접점이 열려 풀업으로 핀이 HIGH → "눌림=닫힘". 선이 빠지거나 끊어져도 핀이 풀업으로 HIGH가 되어 "닫힘"(잠금 쪽)으로 읽힌다.
기본 배선(NO 접점 + GND, `limit_active_low: true`)에서는 선이 빠지면 "열림"으로 읽혀 위험한 쪽이다. 이때의 방어: ① 해제 요청 시점에 열림으로 읽히면 해제를 거부(`door_open`) — **시작부터 미연결이면 절대 해제되지 않는다**, ② 해제 도중 빠지면 `max_unlock_sec`까지 유지될 수 있으나 그 시간에 반드시 잠긴다.

### 8.2 시험 순서

1. 솔밸브 전원을 분리한다. `python scripts/hw_selftest.py --mode pi --limits` 를 실행하고 사물함 1~4의 문을 열고 닫으며 해당 사물함 값만 `닫힘(눌림)` ↔ `열림(해제)` 으로 바뀌는지 확인한다(라벨과 일치 여부를 7.1 표 "리밋" 열에 기록). 반대로 보이면 `limit_active_low`를 바꾼다.
2. 센서 선을 하나 뽑아 본다: NC+`limit_active_low: false` 구성이면 값이 "닫힘"으로 유지되어야 한다(fail-safe). NO 구성이면 "열림"으로 바뀐다 → 8.1 위험 안내.
3. 7.4의 출력 시험(`--allow-solenoid`)은 문이 닫힌 상태(리밋 눌림)에서만 해제된다. 문이 열려 있으면 `hardware.log`에 `REJECT ... code=door_open`.
4. 시뮬레이션(PC): `python scripts/hw_selftest.py --sim-limits` — 1~4 + Enter = 문 열기/닫기, 5~8 = 센서 오류 주입.

### 8.3 솔밸브 연속 통전

문이 열려 있는 동안 솔밸브가 ON으로 유지되고, 최대 `max_unlock_sec`(기본 30초, 코드 상한 120초) 연속 통전된다. 솔밸브의 **연속 통전 정격(듀티)** 이 그 시간 이상인지 반드시 확인한다(일부 잠금용 솔레노이드는 수 초 펄스 전용이며 과열된다). 정격이 짧으면 `max_unlock_sec`를 낮추거나(최소 `unlock_timeout_sec + close_confirm_sec + 1`) 연속 정격 제품을 쓴다. 60초를 넘기면 시작 시 경고한다.

### 8.4 소프트웨어 안전 동작 (limit)

| 상황 | 동작 |
|---|---|
| 시작 | 모든 솔밸브 OFF. 리밋을 1회 읽어 열림 → `LOCKED_OPEN`(해제 거부), 읽기 실패 → 그 채널만 `SENSOR_FAULT`. **모든 채널이 읽기 실패면 Pi 모드는 시작 거부(종료 코드 5)** — 센서 배선 전체 오류 조기 발견. 리밋 핀 누락은 설정 오류 |
| 메인 루프(카메라·화면)가 멈춤 | 하드웨어 스레드(`limit_poll_sec`, 기본 20ms)가 계속 상태기계를 구동. 스레드까지 멈춰도 채널별 독립 타이머가 `max_unlock_sec`에 OFF |
| 폴링 스레드 예외 | 전 채널 OFF + FAULT(재시작 필요) |
| 출력 불변식 위반(잠겨 있어야 하는 채널이 ON) | 전 채널 OFF + FAULT |
| 종료/예외/시그널 | 기다리지 않고 즉시 전 채널 OFF. `--force-off`가 ExecStartPre/ExecStopPost에서 다시 OFF |
| 로그 | `hardware.log`에 `STATE`(전이마다), `SENSOR`(센서 오류/복구), `CLOSE`/`WATCHDOG` 행 끝 `cause=… unlocked_for=…s door_open_for=…s` |
