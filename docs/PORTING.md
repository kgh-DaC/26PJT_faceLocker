# PC → Raspberry Pi 5 (Ubuntu 24.04) 이식 체크리스트

> 상태: **실기 미검증.** Windows PC에서 mock 모드로만 확인했다. 각 항목의 "확인" 칸은 Pi에서 처음 실행할 때 채운다.
> 배선은 `deploy/WIRING.md`, 설치는 `deploy/install_rpi.sh`, 서비스는 `deploy/facedoor.service`.
> SD카드를 PC에서 만들어 첫 부팅에 자동 설치하는 방법은 8절(`deploy/sdcard/`)을 본다.

## 0. 원칙

- 코드 변경 없이 **설정만** 바꾼다. 장치별 값은 `configs/local.yaml`(git 제외)에 둔다.
- 전환의 핵심은 한 줄: `hardware: {mode: pi}`. Pi가 아닌 곳에서 `pi`를 지정하면 오류 종료(코드 5), `auto`는 Pi가 아니면 경고 후 mock.
- 처음엔 솔밸브 대신 LED로 시험한다.

## 1. 설치

| # | 항목 | 명령/설정 | 확인 |
|---|---|---|---|
| 1 | OS | Ubuntu 24.04 (aarch64) Server/Desktop, Python 3.12 | ☐ |
| 2 | 설치 스크립트 | `sudo deploy/install_rpi.sh` (venv `--system-site-packages`: apt `python3-lgpio`를 venv에서 쓰기 위함). 저장소 경로가 한글이면 `--target /opt/facedoor` (6절) | ☐ |
| 3 | OpenCV | apt `python3-opencv`(4.6) **금지**, pip `opencv-python>=4.9` 확인: `.venv/bin/python -c "import cv2;print(cv2.__version__)"` | ☐ |
| 4 | lgpio/gpiozero | `.venv/bin/python -c "import lgpio, gpiozero"` | ☐ |
| 5 | 그룹 | `id` 출력에 `gpio`, `video` 포함 (재로그인 필요) | ☐ |
| 6 | 모델 | `python scripts/download_models.py` 후 `models/models.lock.json` sha256 확인 | ☐ |
| 7 | 갤러리 | PC에서 등록한 `data/gallery/`를 복사하거나 Pi에서 재등록. 임베더 `model_id`가 같아야 함(다르면 `enroll.py --reembed`) | ☐ |
| 8 | 보정 | `models/calibration.json` 복사 가능(같은 model_id일 때만 적용됨). Pi 카메라로 다시 평가 권장 | ☐ |

## 2. 카메라

| 항목 | PC | Pi | 확인 |
|---|---|---|---|
| 장치 | 인덱스 `"0"` | `v4l2-ctl --list-devices` → `"/dev/video0"` (UVC는 보통 짝수 번호가 캡처 노드) | ☐ |
| 백엔드 | `auto` = DSHOW | `auto` = V4L2 | ☐ |
| 포맷 | MJPG 640x480@30 | `v4l2-ctl -d /dev/video0 --list-formats-ext`로 MJPG 지원 확인. YUYV만 되면 해상도를 낮춤 | ☐ |
| 노출 | 자동 | 역광 현장이면 `v4l2-ctl -c auto_exposure=...` 조정 후 `lowlight/backlight` 평가 | ☐ |

## 3. GPIO

| 항목 | 설정 키 | 기본값 | 확인 |
|---|---|---|---|
| 모드 | `hardware.mode` | PC `mock` → Pi `pi` | ☐ |
| 핀 팩토리 | `hardware.pin_factory` | `lgpio` (Pi 5는 RP1 칩이라 RPi.GPIO 불가) | ☐ |
| gpiochip | `hardware.gpio_chip` | `auto`: 라벨 `pinctrl-rp1` 칩을 찾음(커널에 따라 0 또는 4). `gpioinfo`로 확인 후 필요 시 숫자 고정 | ☐ |
| 핀 | `solenoid_pin/led_*_pin` | BCM 17/27/22/23 | ☐ |
| 솔밸브 극성 | `solenoid_active_high` | `true`. active-LOW 릴레이 모듈이면 `false` | ☐ |
| LED 극성 | `led_active_high` | `true` | ☐ |
| 시간 | `open_sec/max_open_sec/cooldown_sec` | 3.0 / 5.0 / 2.0 (max 코드 상한 10초) | ☐ |
| 자가 시험 | `python scripts/hw_selftest.py --mode pi [--allow-solenoid]` | `SELFTEST OK` | ☐ |
| 부팅 중 출력 | 재부팅하며 릴레이/LED 관찰 | 꺼져 있어야 함 (WIRING 3.2) | ☐ |
| 종료 시 출력 | 개방 중 `sudo systemctl stop facedoor`, `sudo kill -9 <pid>` | 즉시 꺼짐 (cleanup / ExecStopPost) | ☐ |
| 강제 OFF | `python scripts/hw_selftest.py --force-off [--hardware-log PATH]` | 설정된 모든 출력을 lgpio로 OFF 레벨로 만든 뒤 종료한다(서비스 `ExecStopPost`와 같은 동작, mock에서는 아무것도 하지 않음). 비하드웨어 설정이 깨져 있어도 동작한다 | ☐ |
| 워치독 | 개방 중 메인 루프 정지 상황(예: `kill -STOP <pid>`) | `max_open_sec` 내 꺼짐(타이머 스레드도 같이 멈추므로 **SIGSTOP은 막지 못함** → 하드웨어 타이머 릴레이 고려) | ☐ |

## 4. 서비스

| 항목 | 명령 | 확인 |
|---|---|---|
| 상태 | `systemctl status facedoor`, `journalctl -u facedoor -f` | ☐ |
| 정지 | `sudo systemctl stop facedoor` → 앱이 SIGTERM을 받아 출력을 OFF한 뒤 exit 130으로 끝난다. 유닛의 `SuccessExitStatus=130 143` 덕분에 `failed`가 아니라 `inactive (dead)`로 보여야 한다. 그 후 `ExecStopPost`(`--force-off`)가 실행된다 | ☐ |
| 재시작 | 카메라 분리 → 종료 코드 3 → `Restart=on-failure`로 재시작 | ☐ |
| 헤드리스 | 기본 `--no-display`. 모니터 출력이 필요하면 데스크톱 세션에서 수동 실행 | ☐ |
| 로그 | `logs/events.csv`, `logs/hardware.log`, `logs/facedoor.log` 쓰기 가능(서비스 `ReadWritePaths`) | ☐ |

## 5. 성능 (Pi 5는 CPU 추론)

1. `python scripts/run.py --no-display --max-frames 300` 로그의 FPS와 `events.csv`의 `det_ms/emb_ms/total_ms`를 기록.
2. 목표 처리 ≥ 8 FPS. 부족하면 순서대로: `detector.input_scale: 0.5` → `runtime.process_every_n: 2` → 카메라 해상도 축소.
3. `process_every_n`을 올리면 같은 시간 안의 투표 수가 줄어든다 → `decision.window_timeout_sec`가 충분한지 확인.
4. 발열: 방열판/액티브 쿨러 권장, `vcgencmd measure_temp`(Ubuntu에서는 미제공일 수 있음 → `/sys/class/thermal/thermal_zone0/temp`).

## 6. 한글 경로

**Pi에서는 ASCII 경로에 설치하는 것을 권장한다.** 한글 폴더 이름(`PJT5_얼굴인식`)은 systemd 유닛·sed 치환 등 배포 도구와 맞물려 문제가 되기 쉽다.

- `imageio.ascii_safe_path()`(OpenCV가 Windows에서 비ASCII 경로를 못 여는 문제의 우회)는 **Windows에서만** 모델/동영상을 캐시로 복사한다. Linux/Pi에서는 경로를 그대로 쓰므로 `~/.cache` 복사와 `ProtectHome=read-only` 충돌은 일어나지 않는다. Windows에서 복사가 실패하면 `ModelError`(카메라 동영상은 `CameraError`)로 정상 종료 코드가 나온다.
- 그래도 Pi 한글 경로에서 OpenCV/ONNX 로딩이 실제로 되는지는 **미검증**이다(PC에서 확인 불가). 확인 전까지는 아래 ASCII 설치를 쓴다.
- `install_rpi.sh`는 저장소 위치를 그대로 설치 루트(venv, 서비스 경로, `FACEDOOR_HOME`)로 쓴다. 루트가 ASCII가 아니거나 공백/`#`이 들어 있으면 **오류로 중단**한다. 이때는 아래처럼 ASCII 경로로 복사해서 설치한다.
  ```bash
  sudo deploy/install_rpi.sh --target /opt/facedoor [--start]   # .venv 빼고 복사, 소유자 변경 후 거기서 설치 계속
  # 이후 갱신: sudo /opt/facedoor/deploy/install_rpi.sh
  ```
  심볼릭 링크는 쓰지 않는다. 경로가 resolve되면 다시 비ASCII가 되기 때문이다. `--allow-non-ascii-root`는 서비스 없이 수동으로 실행할 때만 쓴다(경고 출력).
- 코드 쪽 근본 수정(Linux에서는 우회하지 않고 OSError를 `ModelError`로 변환)은 vision 담당이다(QA C-1). 이 수정이 들어가더라도 ASCII 경로 설치를 계속 권장한다.

## 7. 미검증 항목 (실기 필요)

- `gpio_pi.py`의 실제 lgpio 동작, `gpio_chip: auto` 라벨 탐지(`pinctrl-rp1`), `force_outputs_off()`(라인 해제 후 레벨 유지 여부).
- Ubuntu 24.04의 `/dev/gpiochip*` 권한 구성(설치 스크립트가 `gpio` 그룹 + udev 규칙을 추가함).
- systemd 유닛의 `ExecStopPost`, 하드닝 옵션과 카메라/GPIO 접근 호환성.
- 릴레이 모듈의 3.3V 입력 호환성, 부팅 중 출력 상태.
- Pi 5 FPS.
- SD카드 방식(8절) 전체: 실제 SD카드 쓰기, Ubuntu raspi 이미지의 cloud-init 적용, `facedoor-firstboot` 서비스.

## 8. SD카드 방식 (PC에서 카드 작성 → 첫 부팅 자동 설치)

> **상태: 실제 SD카드·Pi로 검증하지 못했다.** Windows PC에서 단위 테스트(`tests/test_sdcard_tool.py`: SHA-512 crypt 표준 벡터, cloud-init 렌더링/YAML 파싱, 번들·sha256, 파일 대상 write+읽기 재검증, 장치 안전 판정, 네트워크 모킹), `flash_sdcard.ps1 -DryRun`(가짜 이미지, 이동식 장치 없음 → 시스템 NVMe 거부 확인), `bash -n`, PowerShell 5.1 파서 검사만 했다. 실제 장치 쓰기, 이미지 다운로드, macOS/Linux 래퍼 실행, Pi 첫 부팅은 **미검증**이다. 처음에는 버려도 되는 카드로 해 보고, 결과를 이 절에 기록한다.

### 8.1 구성

| 파일 | 역할 |
|---|---|
| `deploy/sdcard/flash_sdcard.ps1` | Windows (PowerShell 5.1+, 쓰기는 관리자 권한) 대화형 래퍼 |
| `deploy/sdcard/flash_sdcard.sh` | Linux/macOS 대화형 래퍼 (쓰기는 `sudo`) |
| `deploy/sdcard/sdcard_tool.py` | 표준 라이브러리 전용 로직: 비밀번호 해시, 렌더링(stage), 번들, 이미지 resolve/fetch/verify, write+읽기 검증, 장치 목록·안전 판정, 부트 파티션 복사·검증, 스테이징 비밀 삭제 |
| `deploy/sdcard/templates/` | `user-data.tpl`, `network-config.tpl`, `meta-data.tpl` (cloud-init NoCloud) |
| `deploy/sdcard/firstboot/facedoor-firstboot.sh` | Pi 첫 부팅 설치 스크립트 (root, oneshot 서비스) |
| `deploy/sdcard/out/` | 작업 폴더(스테이징, 로그, 이미지 캐시). 번들·git에서 제외 |

### 8.2 절차

1. **먼저 dry-run** (기본값, 아무 장치에도 쓰지 않음). 질문에 답하면 스테이징·이미지 확인·장치 목록·판정·실행 계획을 보여 준다.
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\sdcard\flash_sdcard.ps1            # Windows
   powershell -ExecutionPolicy Bypass -File deploy\sdcard\flash_sdcard.ps1 -Image D:\img\ubuntu-24.04.3-preinstalled-server-arm64+raspi.img.xz
   ```
   ```bash
   deploy/sdcard/flash_sdcard.sh                                                      # Linux/macOS
   ```
   질문: 호스트명, 사용자(`ubuntu`/`root` 등 예약어 금지), 비밀번호(숨김 입력 2회, 8자 이상), SSH **공개키** 파일(`.pub`; 개인키를 고르면 거부), Wi-Fi(SSID, 암호 숨김 입력, 국가 코드 기본 KR), 시간대, 서비스 자동 시작 여부(배선 확인 전에는 "아니오" 권장), 모델 다운로드 여부.
2. 이미지: `-Image/--image`로 로컬 파일을 주면 공식 `SHA256SUMS`(또는 `-Sha256`/`-SumsFile`)로 검증한다. 주지 않으면 쓰기 모드에서 최신 `ubuntu-24.04.x-preinstalled-server-arm64+raspi.img.xz`를 HTTPS로 받고(이어받기 지원) 검증한다. dry-run에서는 이름만 확인한다(`-Fetch/--fetch`로 다운로드).
3. **실제 쓰기**: 관리자 PowerShell에서 `-Write`, Linux/macOS는 `sudo deploy/sdcard/flash_sdcard.sh --write`. 장치 목록에서 OK 장치의 번호(Windows) / 이름(`sdb`, `disk4`)을 직접 타이핑 → 장치 재확인 → `ERASE` 타이핑 → 쓰기(파티션 테이블은 마지막에) → 전체 읽기 재검증(sha256) → 부트 파티션(`system-boot`, FAT32)에 `user-data`, `network-config`, `meta-data`, `facedoor-firstboot.sh`, `facedoor-bundle.tar.gz(.sha256)` 복사 후 sha256 재검증.
   Windows는 쓰기 직후 "디스크를 포맷해야 합니다" 창이 뜰 수 있다(ext4 파티션). **반드시 취소**.
4. 카드를 Pi 5에 꽂고 전원. cloud-init이 사용자·SSH·네트워크를 설정하고 `facedoor-firstboot.service`를 시작한다. 첫 부팅 설치는 apt/pip 때문에 수십 분 걸릴 수 있다.
5. 확인 (SSH: `ssh <user>@<hostname>.local` 또는 공유기에서 IP 확인):
   ```bash
   sudo tail -f /var/log/facedoor-firstboot.log      # 진행 로그
   cat /var/lib/facedoor/firstboot.done              # 성공 마커 (bundle sha256, 설치 경로)
   cat /var/lib/facedoor/firstboot.failed            # 실패 시: 단계, 종료 코드, 사유
   sudo /usr/local/sbin/facedoor-firstboot           # 수동 재시도 (멱등). --force 는 완료 마커 무시
   ```
   그 다음 1~4절 체크리스트를 그대로 진행한다(설치 경로는 `/opt/facedoor`, ASCII).

### 8.3 첫 부팅 스크립트 동작 (`facedoor-firstboot.sh`)

| 단계 | 내용 | 실패 시 |
|---|---|---|
| 설정 | `/etc/facedoor/firstboot.env`를 `KEY=VALUE`로 파싱(셸 `source` 안 함)하고 사용자/경로/플래그를 검증 | 종료 2 |
| 네트워크 | `ports.ubuntu.com`, `pypi.org` 이름 해석을 최대 900초 대기 | 종료 5 |
| 번들 검증 | `/boot/firmware/facedoor-bundle.tar.gz`의 sha256을 `.sha256` 파일과 비교 | 종료 3 |
| 압축 해제 | 모든 항목이 `facedoor/` 아래인지·`..` 없는지 확인 → 임시 폴더에 풀고 `MANIFEST.sha256` 검증 → `/opt/facedoor`로 이동. 같은 번들이 이미 풀려 있으면 건너뜀. 다른 번들이 있으면 `.bak-<시각>`으로 옮기고 `configs/local.yaml`, `data/`, `models/`, `logs/`를 이어받음 | 종료 3/4 |
| 설정 파일 | `configs/local.yaml`이 없을 때만 `hardware.mode: pi`(또는 `set_pi_mode=false`면 `mock`) 작성 | - |
| 설치 | `deploy/install_rpi.sh --user <user> [--no-service] [--no-models]` (**`--start` 안 씀**) | 종료 6 |
| 완료 | `/var/lib/facedoor/firstboot.done` 작성, 서비스 disable | - |

- 실패하면 로그와 `firstboot.failed`를 남기고 0이 아닌 코드로 끝난다. 서비스는 enable 상태로 남아 **다음 부팅에 재시도**한다(모든 단계 멱등).
- **솔밸브/LED 출력은 건드리지 않는다.** GPIO 접근, `hw_selftest.py`, `facedoor.service` 시작을 하지 않는다. 서비스 자동 시작을 선택했으면 enable만 되고 **다음 재부팅**부터 동작한다. 그 전에 3절 자가 시험(LED로 대체)을 먼저 한다.

### 8.4 안전장치

| 구분 | 내용 |
|---|---|
| 모드 | 기본 dry-run. 쓰기는 `-Write`/`--write` 명시 + Windows 관리자 / `sudo` 필요. 확인을 건너뛰는 옵션(`--yes` 등)은 없다(sh는 `-y/--yes/--force`를 오류 처리) |
| 장치 판정 (`sdcard_tool.assess_device`) | 허용: 버스 USB/SD/MMC이면서 이동식 미디어(USB는 removable 필수; Windows `MediaType=Removable Media`). 거부: 시스템/부팅 디스크, 시스템 드라이브(`C:` 등)·`/`·`/boot*`·`/home`·`/usr`·`/var`·swap 등이 있는 디스크, 이미지/작업 폴더/프로젝트가 있는 디스크, LVM/RAID/crypt 멤버, NVMe/SATA/SAS/RAID/가상 디스크, 고정 미디어 USB(외장 HDD/SSD), 내장 eMMC, 쓰기 금지 카드, 미디어 없음, **512 GiB 초과**, **7 GB 미만**, 이미지보다 작은 장치. 상한은 `--max-size-gb`로 낮출 수만 있다. 16 GB 미만은 경고 |
| 확인 | 장치 모델/크기/버스/현재 파티션·마운트를 표시 → 장치 번호/이름 직접 타이핑 → `ERASE`(대문자) 타이핑 |
| 재확인 | 쓰기 직전 `check-device`로 다시 수집·판정 + 크기 동일성(카드 교체 감지). Windows는 Get-Disk로 독립 재확인(IsSystem/IsBoot/BusType/크기/시스템 드라이브). `sdcard_tool.py write` 자체도 장치 경로면 판정을 다시 돌리고 거부 시 종료 코드 6 |
| 쓰기 | 파티션 테이블 영역(첫 1 MiB)을 마지막에 기록, 전체 읽기 재검증(sha256) 기본, 부트 파티션 복사본도 sha256 검증. 부트 파티션은 `config.txt`/`cmdline.txt`가 있어야 복사 |
| 비밀 | 비밀번호·Wi-Fi 암호는 숨김 입력 → Python **stdin**(JSON)으로만 전달, 명령줄·로그·파일에 평문 없음. `user-data`에는 SHA-512 crypt 해시(100000 rounds), `network-config`에는 PBKDF2로 유도한 64-hex PSK만 들어간다. 작업 폴더의 `user-data`/`network-config`/`meta-data`는 종료 시 덮어쓰고 삭제(`-KeepStage/--keep-stage`로 유지) |
| 무결성 | 이미지: HTTPS + 공식 SHA256SUMS. 번들: 결정적 tar.gz + `.sha256` + 내부 `MANIFEST.sha256`, Pi에서 둘 다 검증 |

주의: SD카드의 부트 파티션은 누구나 읽을 수 있다. 카드를 분실하면 비밀번호 해시와 Wi-Fi PSK가 노출된 것으로 보고 둘 다 바꾼다. 강한 비밀번호와 SSH 키 로그인을 쓴다. 설치 후 부트 파티션의 `user-data`/`network-config`를 지웠을 때 cloud-init 동작은 확인하지 않았다(미검증).
