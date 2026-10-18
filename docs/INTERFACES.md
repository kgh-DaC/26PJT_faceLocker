# facedoor 인터페이스 계약 (v1.5)

> **v1.5 상태 (리밋센서 도어 제어)**: 각 도어에 리밋센서(눌림=닫힘, 안눌림=열림)를 달아 "인식으로 해제 → `unlock_timeout_sec` 안에 문이 안 열리면 재잠금 → 문이 열려 있는 동안 해제 유지 → 리밋이 `close_confirm_sec` 연속 눌림이면 잠금, 절대상한 `max_unlock_sec` 초과 시에도 **잠금**, 센서 실패/미연결은 fail-closed". **계약만 정의됨, 코드 미구현.** 영향 절: §4.1(LockerState/LockerStatus/DoorEvent/LimitInputs/CloseCause 개정), §15(v1.4 close-hold를 **대체**한 리밋 상태기계·설정·핀·시뮬레이션·QA), §16(패널 모델: 문 열림/닫힘 주 표시, `door_state.json` schema 2), §17.1(인식 창 레이아웃: 왼쪽 영상, 오른쪽 사물함 패널 4개), §13.4·§13.5·§5(거부 코드·로그·이벤트·컬럼 37), §3.13(앱 흐름). v1.4의 `close_mode: hold`, `close_hold_sec`, `DoorController.hold()`는 **폐기**(구현된 적 없음). 새 키(`hardware.close_mode: limit`, `limit_switch_pin`, `locker_outputs[].limit_pin`, `limit_pull_up`, `limit_active_low`, `limit_poll_sec`, `open_confirm_sec`, `close_confirm_sec`, `unlock_timeout_sec`, `max_unlock_sec`, `sensor_fault_sec`)는 `default.yaml`에 주석으로만 있다.

> **v1.4 상태**: §15 닫힘 유지(close-hold)는 v1.5에서 리밋센서 방식으로 대체됨. §16 사물함 상태 패널, §17 한글 표시(인식 화면 `TkDisplay`, CLI 문구 범위)를 추가했다. **계약만 정의됨, 코드 미구현.** 새 키(`display.backend/locker_panel/locker_owner/font_families/font_size`, `logging.door_state_file`)는 `default.yaml`에 주석으로만 있다.

> **v1.3 상태**: §13 사물함별 출력(`lockers.mode=per_locker`, 사물함 4개 `"1"`~`"4"`), §14 한글 관리 화면, §12.1 관리자 PIN은 **계약만 정의됨, 코드 미구현**이다. 구현이 끝나기 전까지 `per_locker`는 계속 ConfigError이고, 새 설정 키(`hardware.locker_outputs`, `hardware.max_concurrent_open`, `lockers.allow_shared`, `admin.lockout_sec`, `gui` 섹션)는 `default.yaml`에 **주석으로만** 존재한다(현재 코드는 알 수 없는 키를 ConfigError로 거부하므로). 예외: `enroll.tags`에 `hat`, `other`는 기존 코드로 동작하므로 실제 값으로 추가했다. 본문 중 "(v1.3)" 표시는 이 미구현 계약을 뜻한다.

이 문서는 병렬 구현의 **계약**이다. 시그니처·필드명·단위·CSV 컬럼·JSON 키를 바꾸려면 `face-architect`에게 요청하고, 변경 시 이 문서와 §11 변경 이력을 갱신한 뒤 `vision-engineer`, `hw-engineer`, `eval-engineer` 모두에게 알린다.
배경과 설계 이유는 `docs/ARCHITECTURE.md`를 참고한다.

- Python 3.12, 타입 힌트 필수, `from __future__ import annotations` 사용.
- 패키지 루트: `src/facedoor/` (src 레이아웃, `pip install -e .` 또는 pytest `pythonpath=src`).
- 이미지: `np.ndarray`, **BGR**, `uint8`, shape `(H, W, 3)`. 좌표는 원본 프레임 픽셀 기준.
- 시간: 간격·타이머는 `time.monotonic()` 초(float). 로그 시각은 로컬 ISO8601 밀리초(`2026-10-16T09:30:12.345+09:00`).
- **점수 스케일: 코사인 유사도** `[-1, 1]`, 높을수록 같은 사람. 수용 규칙은 항상 `score >= threshold` (등호 포함). 모든 모듈·로그·평가가 이 스케일 하나만 쓴다.
- 임베딩: `np.ndarray`, `float32`, shape `(D,)`, **L2 정규화 완료**(norm=1±1e-4). 코사인 = 내적.

---

## 0. 모듈 담당과 파일

| 파일 | 담당 | 비고 |
|---|---|---|
| `src/facedoor/__init__.py` | architect | `__version__` |
| `src/facedoor/errors.py` | architect (작성 완료) | 공통 예외 + 종료 코드 |
| `src/facedoor/__main__.py` | vision | `python -m facedoor` → `app.main()` |
| `src/facedoor/config.py` | vision | YAML 로드/병합/검증 → `AppConfig` |
| `src/facedoor/datatypes.py` | vision | 공용 데이터클래스·Enum (§1) |
| `src/facedoor/imageio.py` | vision | 유니코드 경로 안전 입출력 |
| `src/facedoor/camera.py` | vision | |
| `src/facedoor/detector.py` | vision | YuNet |
| `src/facedoor/quality.py` | vision | |
| `src/facedoor/embedder.py` | vision | SFace / ArcFace(onnxruntime) |
| `src/facedoor/liveness.py` | vision | no-op 훅 |
| `src/facedoor/database.py` | vision | 갤러리 |
| `src/facedoor/recognizer.py` | vision | 매칭 + `ThresholdPolicy` |
| `src/facedoor/pipeline.py` | vision | 프레임 1장 → `RecognitionResult` (런타임·평가 공용) |
| `src/facedoor/decision.py` | vision | N-of-M 투표 |
| `src/facedoor/display.py` | vision | 오버레이 |
| `src/facedoor/events.py` | vision | `logs/events.csv` 기록 |
| `src/facedoor/enroll.py` | vision | |
| `src/facedoor/app.py` | vision | 메인 루프, 종료 코드 |
| `src/facedoor/lockers.py` | vision | (v1.3) person→locker 매핑 해석·검증 `LockerMap` (§13.3). `facedoor.hardware` import 금지(설정 값만 사용) |
| `scripts/assign_locker.py` | vision | (v1.3) 배정 조회/변경/해제 CLI (§13.6) |
| `src/facedoor/evaluation/locker_audit.py` | eval | (v1.3) 사물함 오개방 감사 지표 (§13.8) |
| `src/facedoor/adminpin.py` | vision | (v1.3) 관리자 PIN 해시·검증 (§12.1) |
| `src/facedoor/gui/{__init__,service,preview,admin_app}.py` | vision GUI | (v1.3) 한글 관리 화면 (§14). `tkinter` import 는 `admin_app.py`·`preview.py` 안에서만 |
| `src/facedoor/texts.py` | vision 코어 | (v1.4) 사용자 노출 한글 문구 전부 + `setup_console()` (§17.3). tkinter/cv2 import 금지 |
| `src/facedoor/locker_panel.py` | vision 코어 | (v1.4) 사물함 패널 데이터 모델·`door_state.json` 읽기/쓰기 (§16). tkinter import 금지 |
| `src/facedoor/display_tk.py` | vision 코어 | (v1.4) 한글 인식 화면 `TkDisplay` (§17). tkinter 는 생성자 내부 지연 import |
| `src/facedoor/runlock.py` | vision | (v1.3) 런타임/관리 화면 상호 배제 잠금 (§14.6) |
| `scripts/admin_gui.py` | vision | (v1.3) 관리 화면 실행 |
| `src/facedoor/calibration.py` | eval | `calibration.json` 읽기/쓰기·검증 (무거운 의존성 금지: numpy/json만) |
| `src/facedoor/hardware/{base,gpio_pi,gpio_mock,factory}.py` | hw | v1.5: 채널 상태기계(LockerState, §15.3)는 `base.py`의 공통 베이스에 둔다(mock/pi 동일 코드) |
| `src/facedoor/hardware/limits.py` | hw | (v1.5) `LimitInputs`, `MockLimitInputs`, `PiLimitInputs` (§4.1·§15). gpiozero 지연 import |
| `src/facedoor/evaluation/{dataset,augment,scoring,metrics,calibrate,report}.py` | eval | matplotlib은 `report.py`에서만 import |
| `scripts/download_models.py`, `enroll.py`, `run.py` | vision | |
| `scripts/hw_selftest.py`, `deploy/*`, `docs/PORTING.md` | hw | |
| `scripts/evaluate.py`, `make_synthetic_probes.py`, `label_events.py` | eval | |
| `tests/test_<module>.py` | 각 모듈 담당 | 카메라·GPIO·네트워크 없이 통과해야 함 |

**import 규칙**
- `hardware/` 밖에서 `gpiozero`, `lgpio`를 import 금지. `hardware/gpio_pi.py` 안에서도 함수/생성자 내부 지연 import.
- `facedoor.hardware`는 `facedoor.errors`, `facedoor.hardware.*`만 import한다(비전 모듈 의존 금지).
- `facedoor.evaluation`은 런타임 모듈(`pipeline`, `recognizer`, `database`, `config`, `datatypes`)을 import해도 되지만 반대 방향은 금지(`calibration.py`만 예외적으로 런타임이 사용).
- `cv2.imread/imwrite`, `cv2.FaceDetectorYN.create(path)`, `cv2.FaceRecognizerSF.create(path)`에 경로를 직접 넘기지 말 것. 프로젝트 경로에 한글(`PJT5_얼굴인식`)이 있어 Windows에서 실패한다. §1.3 `imageio` 함수를 쓴다.

---

## 1. 공용 타입 — `facedoor/datatypes.py` (vision)

### 1.1 Enum

```python
class FrameVerdict(str, Enum):     # 프레임 1장의 판정
    MATCH = "MATCH"        # 품질 통과 + best >= threshold + margin 충족 (+ liveness 통과/미요구)
    UNKNOWN = "UNKNOWN"    # 품질 통과했지만 수용 조건 불충족
    HOLD = "HOLD"          # 품질/liveness/다중얼굴 정책으로 판정 보류 (점수 미계산 가능)
    NO_FACE = "NO_FACE"    # 검출 없음

class HoldReason(str, Enum):
    TOO_SMALL = "too_small"; LOW_DET_SCORE = "low_det_score"; BLUR = "blur"
    TOO_DARK = "too_dark"; TOO_BRIGHT = "too_bright"; POSE = "pose"; BORDER = "border"
    MULTI_FACE = "multi_face"; LIVENESS = "liveness"

class DecisionState(str, Enum):    # 투표기 상태 (화면/로그/LED 구동 근거)
    IDLE = "IDLE"; VERIFYING = "VERIFYING"; HOLD = "HOLD"
    OPEN = "OPEN"          # 이 프레임에서 개방 결정 (should_open=True 는 이 프레임 1회만)
    DENIED = "DENIED"      # 이 프레임에서 거부 이벤트 발생
    REARM = "REARM"        # OPEN 후 rearm_sec 동안 재판정 금지
```

### 1.2 데이터클래스 (모두 `@dataclass(slots=True)`; `frozen` 여부는 구현 재량)

```python
@dataclass
class Frame:
    image: np.ndarray        # BGR uint8
    index: int               # 0부터 증가
    ts: float                # time.monotonic()
    source: str              # "cam:0", "file:<path>"

@dataclass
class FaceDetection:
    bbox: tuple[int, int, int, int]  # x, y, w, h (원본 좌표, 프레임 내로 클리핑)
    landmarks: np.ndarray            # (5, 2) float32: 오른눈, 왼눈, 코, 입오른쪽, 입왼쪽 (YuNet 순서)
    score: float                     # 검출 신뢰도 0~1
    raw: np.ndarray                  # (15,) float32 YuNet 원시 행 (SFace alignCrop 용). 다른 검출기는 동일 포맷으로 채움

@dataclass
class QualityResult:
    ok: bool
    reason: HoldReason | None        # ok=False 일 때 첫 번째 실패 이유
    face_px: int                     # bbox 짧은 변
    blur_var: float
    brightness: float
    yaw_ratio: float

@dataclass
class MatchCandidate:
    person_id: str
    score: float                     # 집계(max|topk_mean)된 코사인
    tag: str                         # 그 사람 갤러리에서 최고 점수 샘플의 태그 (normal|mask|...)

@dataclass
class MatchResult:                   # Recognizer.identify() 결과 (순수 매칭, 품질 무관)
    best: MatchCandidate | None      # 갤러리가 비면 None
    second: MatchCandidate | None    # 다른 사람 중 최고. 1명 등록 시 None
    margin: float                    # best.score - second.score, second 없으면 +inf 대신 1.0
    threshold: float                 # 적용된 임계값
    threshold_group: str             # "global" | "occluded"
    threshold_source: str            # "calibration" | "default" | "max(calibration,default)" | "global:<global source>" (§7.2 규칙 4)
    accepted: bool                   # best.score >= threshold and margin >= policy.margin and 정책 통과
    scores: dict[str, float]         # 등록자 전원의 집계 점수 (평가용 전체 노출)

@dataclass
class RecognitionResult:             # FramePipeline.process() 결과 (프레임 1장)
    frame_index: int
    ts: float
    verdict: FrameVerdict
    detection: FaceDetection | None  # 판정에 쓴 얼굴 (multi_face_policy=largest 이면 가장 큰 얼굴)
    all_detections: list[FaceDetection]
    quality: QualityResult | None
    match: MatchResult | None        # HOLD/NO_FACE 면 None
    liveness: "LivenessResult | None"
    hold_reason: HoldReason | None
    timings_ms: dict[str, float]     # keys: "det", "emb", "match", "total"

@dataclass
class DecisionOutput:
    state: DecisionState
    should_open: bool                # True 는 OPEN 결정 프레임에서 정확히 1회
    should_deny: bool                # True 는 DENIED 이벤트 프레임에서 1회
    person_id: str | None            # OPEN/VERIFYING 대상
    votes: int                       # 창 안의 대상자 MATCH 수
    window: int                      # 현재 창 길이 (<= window_frames)
    best_score: float | None         # 창 안 대상자 MATCH 점수의 최대값 (OPEN reason 용)
    reason: str                      # 사람이 읽는 설명 ("3/5 votes for alice", "rearm 2.1s" ...)
```

`LivenessResult`는 §3.6, `DoorStatus`/`LedSnapshot`은 §4에 정의한다(각 모듈 소유).

### 1.3 `facedoor/imageio.py` (vision)

```python
def imread(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None   # np.fromfile + cv2.imdecode
def imwrite(path: str | Path, image: np.ndarray, params: list[int] | None = None) -> bool  # cv2.imencode + tofile, 부모 폴더 생성
def read_bytes(path: str | Path) -> bytes
def ascii_safe_path(path: str | Path, error_cls: type[FaceDoorError] = ModelError) -> Path
    """OpenCV DNN create(path)·VideoCapture(path)에 넘기기 전에 사용.
    - Windows 이외(Linux/Pi, macOS) 또는 ASCII 경로: resolve 한 경로를 그대로 반환(복사 없음 → systemd ProtectHome 아래서도 동작).
    - Windows + 비ASCII 경로: %LOCALAPPDATA%/facedoor/cache (그 경로도 비ASCII면 %SystemDrive%/facedoor_cache) 로
      sha256 이름(확장자 유지)으로 복사한 경로를 반환.
    - 복사 중 OSError(파일 없음, PermissionError, 디스크 부족 등)는 error_cls 로 변환해 던진다.
      기본 ModelError(종료 코드 4). 카메라 동영상 파일 경로(FileCamera)는 error_cls=CameraError(종료 코드 3)."""
def sha256_file(path: str | Path) -> str
```

---

## 2. 설정 — `facedoor/config.py` (vision)

```python
DEFAULT_CONFIG: Path            # <root>/configs/default.yaml
def project_root() -> Path      # FACEDOOR_HOME 환경변수 > src/facedoor 기준 상위 2단계
def load_config(path: str | Path | None = None,
                overrides: Sequence[str] = (),       # ["hardware.mode=pi", "decision.min_votes=4"] (값은 YAML 파싱)
                use_local: bool = True) -> AppConfig
    """병합 순서: default.yaml < configs/local.yaml(있으면, use_local) < path < overrides.
    알 수 없는 키·타입 오류·범위 오류는 ConfigError. 상대 경로는 project_root() 기준 절대 Path로 변환."""
```

`AppConfig`는 섹션별 데이터클래스의 묶음이며 필드명은 `configs/default.yaml` 키와 **정확히 같다**:

```python
@dataclass
class AppConfig:
    schema_version: int
    app: AppSection; runtime: RuntimeSection; camera: CameraSection; detector: DetectorSection
    quality: QualitySection; embedder: EmbedderSection; recognition: RecognitionSection
    liveness: LivenessSection; decision: DecisionSection
    hardware: HardwareConfig        # facedoor.hardware.base.HardwareConfig (hw 소유, config.py가 생성)
    display: DisplaySection; logging: LoggingSection; enroll: EnrollSection
    evaluation: EvaluationSection; models: dict[str, ModelSpec]
    admin: AdminSection = AdminSection()        # 선택 섹션(YAML에 없으면 기본값). 예약·미구현 — §12
    lockers: LockersSection = LockersSection()  # 선택 섹션. require_locker_id만 구현 — §12
    raw: dict                       # 병합된 원본 dict (리포트에 설정 스냅샷 기록용)
```

검증 규칙(최소): `1 <= decision.min_votes <= decision.window_frames`; `0 < hardware.open_sec <= hardware.max_open_sec <= 10.0`; `recognition.margin >= 0`; `quality.min_det_score >= detector.score_threshold`; `hardware.mode in {mock, pi, auto}`; `embedder.backend in {sface, arcface}`; 핀 번호 중복 금지; `admin.min_pin_len >= 4`; `hardware.close_mode ∈ {limit, timer}`(`hold`는 ConfigError), limit 모드 범위·핀 검증은 §15.2; `lockers.mode == single` (`per_locker`는 예약·미구현이라 **ConfigError**, §12.2); `lockers.locker_id_pattern`은 컴파일 가능한 정규식.
(v1.3, 구현 후) `lockers.mode ∈ {single, per_locker}`이고 섹션 간 교차 검증은 §13.2 표를 따른다(`per_locker` ⇔ `hardware.locker_outputs` 1개 이상, `solenoid_pin: null`, locker_id 패턴 일치 등).

---

## 3. 비전 파이프라인 (vision)

### 3.1 `camera.py`

```python
class Camera(Protocol):
    def open(self) -> None                    # 실패 시 CameraError
    def read(self) -> Frame                   # read_retries 연속 실패 시 CameraError. FileCamera 끝 → StopIteration
    def close(self) -> None
    def __enter__(self) -> "Camera"; def __exit__(self, *exc) -> None
    @property
    def is_live(self) -> bool                 # 실시간 카메라면 True

class OpenCVCamera:  # Camera
    def __init__(self, cfg: CameraSection, source: str | int | None = None) -> None
    # backend=auto: Windows CAP_DSHOW, Linux CAP_V4L2. MJPG/해상도/fps 설정 후 실제 적용값을 로그.
class FileCamera:    # Camera — 이미지 폴더(이름순, jpg/png) 또는 동영상 파일
    def __init__(self, path: str | Path, loop: bool = False, fps: float = 0.0) -> None

def create_camera(cfg: CameraSection, source: str | None = None) -> Camera
    """source(None이면 cfg.source)가 정수 문자열/'/dev/video*' → OpenCVCamera, 폴더/파일 → FileCamera."""
```

### 3.2 `detector.py`

```python
class FaceDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[FaceDetection]   # 점수 내림차순. 0개면 []
class YuNetDetector:  # FaceDetector
    def __init__(self, model_path: Path, score_threshold: float = 0.85, nms_threshold: float = 0.3,
                 top_k: int = 50, input_scale: float = 1.0) -> None   # 모델 없음/로드 실패 → ModelError
def create_detector(cfg: DetectorSection, models_dir: Path) -> FaceDetector
```

### 3.3 `quality.py`

```python
def assess_quality(image: np.ndarray, det: FaceDetection, cfg: QualitySection,
                   max_abs_yaw_ratio: float | None = None) -> QualityResult
    """검사 순서(첫 실패가 reason): border → too_small → low_det_score → pose → too_dark/too_bright → blur.
    max_abs_yaw_ratio 인자가 주어지면 cfg 값을 대체(등록 시 완화용). 순수 함수."""
def yaw_ratio(landmarks: np.ndarray) -> float   # (코x - 두 눈 중점x) / 눈간거리, 부호 유지
```

### 3.4 `embedder.py`

```python
class Embedder(Protocol):
    backend: str                 # "sface" | "arcface"
    dim: int                     # sface=128, arcface=512
    model_id: str                # f"{backend}:{sha256(model_file)[:12]}" — 갤러리/보정 호환성 키
    def embed(self, image: np.ndarray, det: FaceDetection) -> np.ndarray   # 정렬+전처리+추론+L2정규화, (dim,) float32
    def align(self, image: np.ndarray, det: FaceDetection) -> np.ndarray   # 112x112 BGR 정렬 crop (디버그/저장용)

class SFaceEmbedder:   # cv2.FaceRecognizerSF: alignCrop(image, det.raw) → feature → L2 정규화
    def __init__(self, model_path: Path, clahe: bool = False, clahe_clip: float = 2.0, clahe_grid: int = 8) -> None
class ArcFaceEmbedder: # onnxruntime 직접 로드, 5점 랜드마크 → ArcFace 112x112 템플릿 유사변환(cv2.estimateAffinePartial2D)
    def __init__(self, model_path: Path, providers: Sequence[str] = ("CPUExecutionProvider",),
                 clahe: bool = False, clahe_clip: float = 2.0, clahe_grid: int = 8) -> None
    # onnxruntime import는 생성자 내부 지연 import. 미설치 → ModelError(설치 안내 메시지)

def create_embedder(cfg: EmbedderSection, models_dir: Path) -> Embedder
def cosine(a: np.ndarray, b: np.ndarray) -> float           # 정규화 가정, float(a @ b)
```

### 3.5 `database.py` — 갤러리

디렉터리 규약은 §6. 클래스:

```python
@dataclass
class GallerySample:
    person_id: str
    tag: str
    index: int
    npy_path: Path
    jpg_path: Path | None
    meta: dict                     # <stem>.json 내용 (§6.2)

class FaceDatabase:
    @classmethod
    def load(cls, gallery_dir: Path, expected_model_id: str | None = None) -> "FaceDatabase"
        """_meta.json 의 model_id 가 expected_model_id 와 다르면 GalleryError (재임베딩 안내).
        폴더가 없으면 빈 DB. 차원 불일치 .npy 도 GalleryError."""
    gallery_dir: Path
    model_id: str | None
    @property
    def matrix(self) -> np.ndarray          # (K, D) float32, 행 L2 정규화
    @property
    def labels(self) -> list[str]           # 길이 K, 행별 person_id
    @property
    def tags(self) -> list[str]             # 길이 K, 행별 tag
    def persons(self) -> list[str]          # 정렬된 고유 person_id
    def samples(self, person_id: str | None = None) -> list[GallerySample]
    def __len__(self) -> int                # K
    def add(self, person_id: str, tag: str, embedding: np.ndarray,
            crop: np.ndarray | None, meta: dict) -> GallerySample   # 다음 index 로 .npy/.jpg/.json 저장, _meta.json 갱신
    def remove_sample(self, sample: GallerySample) -> None
    def remove_person(self, person_id: str) -> None
    def find_outliers(self, person_id: str, min_self_score: float) -> list[GallerySample]
        """같은 사람의 다른 샘플과 평균 코사인 < min_self_score 인 샘플 (삭제는 호출자가 결정)."""
    def all_person_ids(self) -> list[str]   # 샘플이 있거나 person.json 만 있는 사람(등록 진행 중 포함)
    def embedding(self, sample: GallerySample) -> np.ndarray
    # person.json 접근 (§12.3)
    def person_info(self, person_id: str) -> dict            # person.json 내용, 없으면 {}
    def set_person_info(self, person_id: str, **fields: object) -> dict
        """person.json 갱신(없으면 person_id/created_at 으로 생성), updated_at 기록. 갱신된 dict 반환."""
    def display_name(self, person_id: str) -> str             # display_name, 없으면 person_id
    def locker_id(self, person_id: str) -> str | None         # locker_id, 없거나 "" 또는 null 이면 None (v1.3: null 명시)
```

(v1.3) `locker_id()`는 값이 문자열이 아니면(숫자·리스트 등 수동 편집 오류) None이 아니라 `str()`로 바꾸지 말고 **None을 반환하고 WARNING**을 남긴다 — per_locker에서 잘못된 값으로 엉뚱한 채널이 선택되는 일을 막는다. `LockerMap`(§13.3)은 이를 `invalid_locker_id`로 분류한다.

### 3.6 `liveness.py` — 위조 방어 훅 (기본 no-op)

```python
@dataclass
class LivenessResult:
    is_live: bool | None     # None = 검사하지 않음/판단 불가
    score: float | None
    method: str              # "none", "blink", "ir", "depth" ...

class LivenessChecker(Protocol):
    def check(self, frame: Frame, det: FaceDetection, track_id: int | None = None) -> LivenessResult
    def reset(self) -> None
class NoopLiveness:          # 항상 LivenessResult(None, None, "none")
def create_liveness(cfg: LivenessSection) -> LivenessChecker
```

정책: `liveness.required=true` 이고 `is_live is not True` 이면 HOLD(`HoldReason.LIVENESS`). `is_live is False`는 required 여부와 무관하게 HOLD.

### 3.7 `recognizer.py` — 매칭과 임계값 정책

```python
class ThresholdPolicy:
    @classmethod
    def from_sources(cls, rec_cfg: RecognitionSection, embedder_backend: str, embedder_model_id: str,
                     calibration: "CalibrationFile | None") -> "ThresholdPolicy"
        """§7 규칙으로 global/occluded 임계값 결정. 결정 내역을 INFO 로그로 남김."""
    margin: float
    occluded_tags: frozenset[str]
    occluded_policy: str                      # "threshold" | "deny"
    def group_for(self, matched_tag: str) -> str          # "occluded" if tag in occluded_tags else "global"
    def threshold_for(self, group: str) -> float
    def source_for(self, group: str) -> str
    def describe(self) -> dict                # 화면/로그/리포트용 스냅샷

class Recognizer:
    def __init__(self, db: FaceDatabase, policy: ThresholdPolicy, aggregation: str = "max", topk: int = 3) -> None
        """aggregation 은 max|topk_mean, 아니면 ValueError. 생성 시 refresh() 호출."""
    db: FaceDatabase                  # public. app(_locker_allows, 표시 라벨)이 person_info/locker_id 조회에 사용
    policy: ThresholdPolicy           # public, 재대입 허용 (아래 계약)
    aggregation: str; topk: int
    def refresh(self) -> None
        """db.matrix/labels/tags/persons 로 내부 색인을 다시 만든다. 갤러리를 바꾼 뒤(add/remove) 반드시 호출.
        db 객체 자체를 교체한 경우에도 self.db 대입 후 refresh() 필요."""
    def score_all(self, embedding: np.ndarray) -> dict[str, tuple[float, str]]
        """person_id -> (집계 점수, 최고 샘플 tag). 평가가 직접 사용."""
    def identify(self, embedding: np.ndarray) -> MatchResult
        """best=최고 점수 사람, second=그 외 최고. threshold 는 best.tag 의 그룹으로 결정.
        accepted = best.score >= thr and margin >= policy.margin and not (group=='occluded' and policy=='deny').
        갤러리가 비면 best=None, accepted=False, threshold=global."""
```

**`policy` 재대입 계약**: `recognizer.policy = new_policy`는 허용되며 **다음 `identify()` 호출부터** 반영된다(`identify()`는 매 호출마다 `self.policy`를 읽고 캐시하지 않는다). `score_all()`은 정책과 무관하다. `scripts/evaluate.py`가 보정 분할 후 후보 정책을 이 방법으로 적용한다. 이 동작을 고정하는 테스트를 둔다(vision/eval). 런타임(`app.py`) 루프 중 재대입은 하지 않는다.

### 3.8 `pipeline.py` — 런타임·평가 공용 1프레임 처리

```python
class FramePipeline:
    def __init__(self, detector: FaceDetector, embedder: Embedder, recognizer: Recognizer,
                 quality_cfg: QualitySection, multi_face_policy: str = "largest",
                 liveness: LivenessChecker | None = None, liveness_required: bool = False) -> None
    def process(self, frame: Frame) -> RecognitionResult
        """detect → (다중얼굴 정책) → quality → liveness → embed → identify → verdict.
        HOLD 이면 embed/identify 생략 가능(match=None). 예외는 잡지 않고 올린다(앱이 fail-closed 처리)."""
    def process_image(self, image: np.ndarray, index: int = 0) -> RecognitionResult   # 평가용 편의
    @classmethod
    def from_config(cls, cfg: AppConfig, calibration: "CalibrationFile | None" = _AUTO,
                    allow_empty_gallery: bool | None = None) -> "FramePipeline"
        """모델 로드 + FaceDatabase.load(expected_model_id=embedder.model_id) + ThresholdPolicy 구성."""
    detector: FaceDetector; embedder: Embedder; recognizer: Recognizer
```

`from_config`의 `calibration` 인자 의미(혼동 주의):

| 호출 | 동작 |
|---|---|
| 인자 생략 (`_AUTO` 센티넬) | `load_calibration(cfg.app.calibration_file)`로 보정 파일을 읽어 §7.2 규칙 적용 (런타임 기본) |
| `calibration=None` 명시 | 보정 파일을 **읽지 않고** 기본 임계값(`default_thresholds`)만 사용 |
| `calibration=<CalibrationFile>` | 주어진 보정 결과로 §7.2 규칙 적용 (evaluate의 후보 보정 등) |

`allow_empty_gallery`: `None`이면 `cfg.app.allow_empty_gallery`를 따른다. 갤러리가 비어 있고 허용되지 않으면 `GalleryError`(종료 코드 6).

verdict 규칙: 검출 0 → NO_FACE; `multi_face_policy=hold` 이고 품질 통과 얼굴 ≥2 → HOLD(multi_face); 품질 실패 → HOLD(reason); liveness 실패 → HOLD(liveness); `match.accepted` → MATCH; 그 외 UNKNOWN.

### 3.9 `decision.py` — N-of-M 시간적 투표

```python
class VoteDecider:
    def __init__(self, cfg: DecisionSection, clock: Callable[[], float] = time.monotonic) -> None
    def update(self, result: RecognitionResult, now: float | None = None) -> DecisionOutput
    def reset(self) -> None
    @property
    def state(self) -> DecisionState
```

규칙(테스트로 고정할 것):
1. 창 = 최근 `window_frames`개 처리 결과(HOLD·NO_FACE 포함). `window_timeout_sec`보다 오래된 항목은 제거.
2. `UNKNOWN` → `reset_on_unknown`이면 창 비움. 연속 UNKNOWN 카운터 증가, `deny_after_unknown` 도달 시 `should_deny=True`, state=DENIED (이후 `deny_cooldown_sec` 동안 재발생 안 함).
3. `MATCH(p)` 인데 창에 다른 사람 MATCH가 있으면 → `reset_on_other_person`이면 창을 비우고 `MATCH(p)` 하나로 시작.
4. 창 안 `MATCH(p)` 수 ≥ `min_votes` → `should_open=True`, state=OPEN, 창 비움, 이후 `rearm_sec` 동안 state=REARM(모든 입력 무시, should_open=False).
5. HOLD/NO_FACE 는 창 슬롯만 차지(리셋 안 함). 최신 결과가 HOLD 이고 VERIFYING 이 아니면 state=HOLD.
6. `should_open`은 오직 MATCH 투표로만 True. UNKNOWN/HOLD/NO_FACE 입력만으로는 절대 True 가 되지 않는다.

### 3.10 `display.py`

```python
class Display(Protocol):
    def show(self, frame: Frame, result: RecognitionResult | None, decision: DecisionOutput,
             door: DoorStatus, leds: LedSnapshot, fps: float) -> int   # 눌린 키 코드, 없으면 -1
    def close(self) -> None
class OpenCVDisplay:   # cv2.imshow. 'q'/ESC 로 종료 요청
    def __init__(self, cfg: DisplaySection) -> None
class NullDisplay:     # --no-display
def render_overlay(image: np.ndarray, result: RecognitionResult | None, decision: DecisionOutput,
                   door: DoorStatus, leds: LedSnapshot, fps: float, show_scores: bool = True) -> np.ndarray
    """순수 함수(사본에 그림). 초록=MATCH(이름, 점수/임계값), 빨강=UNKNOWN, 노랑=HOLD(이유).
    우상단: 'DOOR SIGNAL: ON/OFF (남은 s)', 가상 LED 3개(G/R/A), 상태, FPS, threshold_source."""
def create_display(cfg: DisplaySection, enabled: bool) -> Display
```

(v1.4) 한글 인식 화면 `TkDisplay`, 사물함 상태 패널, `Display.set_panel()` 확장은 §16·§17. `Display.show()` 시그니처는 그대로다.

### 3.11 `events.py` — 이벤트 로그 (§5 컬럼)

```python
EVENT_COLUMNS: tuple[str, ...]       # §5 순서 그대로
class EventLogger:
    def __init__(self, path: Path, session_id: str, condition_tag: str = "live",
                 embedder_id: str = "", max_mb: float = 50.0, log_no_face: bool = False) -> None
    def log_frame(self, result: RecognitionResult, decision: DecisionOutput, door: DoorStatus,
                  event: str = "frame") -> None
    def log_event(self, event: str, **fields: object) -> None      # start/stop/error/door_close 등
    def flush(self) -> None
    def close(self) -> None
def new_session_id() -> str          # "YYYYmmdd-HHMMSS-<4hex>"
```

### 3.12 `enroll.py`

```python
@dataclass
class EnrollReport:
    person_id: str
    tag: str
    saved: int
    rejected: dict[str, int]         # 이유별 거부 수 (quality reason, "duplicate", "no_face", "multi_face")
    outliers: list[Path]
    self_score_mean: float | None    # 같은 사람 샘플 간 평균 코사인

class Enroller:
    def __init__(self, detector: FaceDetector, embedder: Embedder, db: FaceDatabase,
                 quality_cfg: QualitySection, enroll_cfg: EnrollSection) -> None
    def enroll_live(self, camera: Camera, person_id: str, tag: str, display: Display | None = None,
                    poses: Sequence[str] | None = None, samples_per_pose: int | None = None) -> EnrollReport
    def enroll_images(self, person_id: str, tag: str, paths: Sequence[Path]) -> EnrollReport
        """각 이미지에서 얼굴 1개(여러 개면 거부). meta.source_sha256 기록(누수 검사용)."""
    def reembed_all(self) -> int     # 저장된 crop(.jpg) 재검출·재임베딩, _meta.json model_id 갱신. 처리 수 반환
def validate_person_id(person_id: str, pattern: str) -> None   # 위반 시 ValueError
```

(v1.3) 관리 화면용 `enroll_live(..., *, progress, cancel, pose_countdown_sec, note)` 키워드 확장과 `EnrollProgress`, `EnrollReport.cancelled/rolled_back`은 §14.3.

### 3.13 `app.py`

```python
class FaceDoorApp:
    def __init__(self, cfg: AppConfig, camera: Camera, pipeline: FramePipeline, decider: VoteDecider,
                 hardware: HardwareBundle, display: Display, events: EventLogger,
                 clock: Callable[[], float] = time.monotonic) -> None
    def step(self) -> bool             # 1프레임 처리. 종료 요청 시 False
    def run(self, max_frames: int | None = None) -> int   # 종료 코드 (errors.ExitCode)
    def shutdown(self, reason: str) -> None              # door.close() → leds.off() → hardware.cleanup() → 로그 flush. 멱등
def main(argv: Sequence[str] | None = None) -> int
```

`step()` 순서: `camera.read()` → (process_every_n) `pipeline.process()` → `decider.update()` → `should_open`이면 (person_id 존재 + §12.2 locker 검사 통과 시) `door.open(reason)`, `should_deny`면 `leds.show_denied()`, HOLD면 `leds.set_hold(True)` 아니면 False → `door.status()` 전이 감지(door_close 로그) → `events.log_frame()` → `display.show()`.
**fail-closed**: `step()` 안의 모든 예외는 `door.close()` 후 `error` 이벤트 기록, `CameraError`/`ModelError`/`HardwareError`는 종료 코드로 종료. `door.open()`은 `decision.should_open is True`인 분기 외에서 호출 금지.

OPEN reason 문자열 형식(로그·하드웨어 로그 공통): `face:<person_id>:<best_score:.4f>:<votes>/<window>` (`app.open_reason()`).
- `votes` = OPEN 결정 시점 창 안의 해당 인물 MATCH 수 (`DecisionOutput.votes`).
- `window` = OPEN 결정 시점의 **현재 창 길이** `len(window)` (≤ `window_frames`). 설정값 M이 아니다. 예: `window_frames=5`라도 연속 3 MATCH로 바로 열리면 `3/3`.
- `best_score` = 창 안 해당 인물 MATCH 점수의 **최대값**(결정 프레임 점수가 아님). None이면 `0.0000`.

`should_open=True`이지만 문을 열지 않은 경우 `events.csv`의 `message`:
- `person_id`가 None → `open decision without person_id: ignored`
- locker 검사 실패 → `open suppressed: <pid> has no locker_id (lockers.require_locker_id)` (§12.2)
- `door.open()`이 False(OPEN/COOLDOWN/FAULT) → `<reason> (rejected by door: busy/cooldown/fault)`

(v1.3, `lockers.mode=per_locker`) 위 규칙을 다음으로 확장한다. 상세는 §13.4.
- 개방 대상은 **`decision.person_id`(투표 승자)의 배정 사물함 하나**뿐이다. 현재 프레임의 `best_id`나 화면 라벨로 사물함을 고르지 않는다.
- `app.open_reason(pid, best_score, votes, window, locker_id: str | None = None)`: `locker_id`가 주어지면 `face:<pid>:<score>:<votes>/<window>:locker=<L>`, 아니면 기존 형식(single 모드는 기존 형식 그대로).
- 호출: `door.open(reason, locker_id=L)` — 키워드 인자로만 넘긴다.
- 억제 message (`LockerResolution.message`, §13.3): `open suppressed: <pid> has no locker_id (lockers.mode=per_locker)` / `open suppressed: <pid> locker_id=<L> not configured in hardware.locker_outputs` / `open suppressed: <pid> locker_id=<L> shared with <other,...> (lockers.allow_shared=false)` / `open suppressed: <pid> locker_id invalid (<detail>)`.
- 거부 message: `<reason> (rejected by door: <door.last_reject>)` — 코드 목록은 §13.4.
- `_safe_close_door()`(예외 시)는 `door.close(reason)`(locker_id 없이) = **전 채널 OFF**.
- 문 닫힘 감지: 채널별. 직전 `status().open_lockers`에 있었고 지금 없는 사물함마다 `door_close` 행 1개(`locker_id` 컬럼 채움). **(v1.5, limit 모드) 이 감지 방식은 `door.drain_events()` 기반으로 대체**: 전이 놓침을 막기 위해 하드웨어가 쌓은 `DoorEvent`(`unlocked`/`door_opened`/`relock`/`forced_open`/`sensor_fault`/`sensor_ok`)를 매 `step()`에서 비워 events.csv 행(`door_opened`/`door_close`/`door_forced`/`door_sensor`)으로 기록한다. `hold()` 호출·"본인 유지 신호"는 없다(§15.3).

(v1.5, limit 모드) `step()` 추가 순서: `door.open()` 이후 `events = door.drain_events()` → 로그, `rows = locker_panel.build_rows(door, bundle.limits, ...)` → `display.set_panel(rows)` → (0.5 s 또는 변화 시) `write_state()` → `display.show()`가 돌려준 키 중 `1`~`8`은 `limits.is_simulated`일 때만 `_handle_sim_key()`로 가상 리밋 조작(§15.4). 억제/거부 message에 추가되는 코드: `rejected by door: door_open | sensor_fault` (§13.4). 해제 후 재잠금 알림은 별도 message 없이 `door_close` 행의 `close_cause`로 남는다.

---

## 4. 하드웨어 (hw) — `facedoor/hardware/`

### 4.1 `base.py`

```python
ABSOLUTE_MAX_OPEN_SEC: float = 10.0          # 설정으로도 넘을 수 없는 코드 상한

@dataclass
class HardwareConfig:                         # default.yaml 'hardware' 키와 동일 필드명/기본값
    mode: str = "mock"; mock_backend: str = "state"; pin_factory: str = "lgpio"; gpio_chip: str | int = "auto"
    solenoid_pin: int = 17; led_green_pin: int = 27; led_red_pin: int = 22; led_amber_pin: int = 23
    exit_button_pin: int | None = None
    solenoid_active_high: bool = True; led_active_high: bool = True
    open_sec: float = 3.0; max_open_sec: float = 5.0; cooldown_sec: float = 2.0
    deny_blink_count: int = 3; deny_blink_sec: float = 0.15
    hardware_log: Path = Path("logs/hardware.log")
    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "HardwareConfig"   # 알 수 없는 키/범위 오류 → facedoor.errors.ConfigError

class DoorState(str, Enum):
    IDLE = "IDLE"; OPEN = "OPEN"; COOLDOWN = "COOLDOWN"; FAULT = "FAULT"

@dataclass
class DoorStatus:
    state: DoorState
    is_open: bool                 # 솔밸브 출력이 활성(신호 ON)인가 — 녹색 LED와 항상 동일
    opened_at: float | None       # monotonic
    remaining_sec: float          # 개방 남은 시간 (닫힘이면 0)
    cooldown_remaining_sec: float
    last_reason: str
    open_count: int               # 프로세스 시작 이후 개방 횟수

@dataclass
class LedSnapshot:
    green: bool; red: bool; amber: bool

class DoorController(Protocol):
    def open(self, reason: str, duration: float | None = None) -> bool
        """duration None → open_sec. min(duration, max_open_sec) 로 클램프(경고 로그).
        IDLE 일 때만 수락하고 True. OPEN/COOLDOWN/FAULT 중이면 무시하고 False (연장 없음).
        녹색 LED는 솔밸브 출력과 같은 전이에서 켜고 끈다. 워치독 타이머가 만료 시 자동 close."""
    def close(self, reason: str = "") -> None     # 멱등. 출력 OFF → COOLDOWN(cooldown_sec) → IDLE
    def status(self) -> DoorStatus
    def cleanup(self) -> None                      # 출력 OFF, 타이머 취소, 핀 해제. 멱등, 예외 삼킴(로그)

class StatusLeds(Protocol):                         # 적색/황색 (녹색은 DoorController 소유)
    def show_denied(self) -> None                   # 적색 deny_blink_count 회 점멸, 비차단
    def set_hold(self, on: bool) -> None            # 황색
    def snapshot(self) -> LedSnapshot               # 녹색 포함 (DoorController 상태 반영)
    def off(self) -> None
    def cleanup(self) -> None

@dataclass
class HardwareBundle:
    mode: str                                       # 실제 선택된 모드 "pi" | "mock"
    door: DoorController
    leds: StatusLeds
    exit_button: Any | None                         # 콜백 등록용, 미사용 시 None
    def cleanup(self) -> None                       # door.cleanup() → leds.cleanup(), 멱등
```

스레드 안전: `open/close/status`는 내부 `threading.Lock`으로 보호. 타이머는 `threading.Timer`(daemon). `clock` 주입: 구현 생성자에 `clock: Callable[[], float] = time.monotonic` 인자를 둔다(테스트용).

**(v1.3) 사물함 채널 확장 — 하위호환 방식.** 기존 호출(`open(reason)`, `open(reason, 2.0)`, `close()`, `status()`)은 single 모드에서 의미가 바뀌지 않는다.

```python
ABSOLUTE_MAX_LOCKERS: int = 16              # 채널 수 코드 상한 (설정으로 넘을 수 없음)
ABSOLUTE_MAX_CONCURRENT_OPEN: int = 4       # 동시 개방 코드 상한
RESERVED_OUTPUT_PINS: frozenset[int] = frozenset({0, 1, 2, 3, 14, 15})
    # 0/1 = HAT ID EEPROM(I2C0), 2/3 = I2C1(보드에 1.8k 풀업 → 부팅 중 HIGH), 14/15 = UART(콘솔 TX 부팅 중 토글)

@dataclass(slots=True, frozen=True)
class LockerOutput:                           # hardware.locker_outputs 의 한 항목
    locker_id: str                            # 비어 있지 않은 ASCII. 패턴 검사는 config.py(§13.2)
    pin: int                                  # BCM
    active_high: bool | None = None           # None → HardwareConfig.solenoid_active_high 상속
    def effective_active_high(self, cfg: "HardwareConfig") -> bool

@dataclass
class HardwareConfig:   # 추가/변경 필드만
    solenoid_pin: int | None = 17             # 변경: None 허용. single=int 필수, per_locker=None 필수
    locker_outputs: tuple[LockerOutput, ...] = ()   # YAML: list of {locker_id, pin[, active_high]}
    max_concurrent_open: int = 1              # 1 <= v <= min(len(locker_outputs) or 1, ABSOLUTE_MAX_CONCURRENT_OPEN)
    @property
    def per_locker(self) -> bool              # len(locker_outputs) > 0
    def pins(self) -> dict[str, int]          # 기존 이름 + "locker:<id>" 항목 (solenoid_pin None 이면 "solenoid" 없음)
    def channel_map(self) -> dict[str, tuple[int, bool]]   # locker_id -> (pin, effective_active_high), 선언 순서

@dataclass(slots=True, frozen=True)
class DoorStatus:       # 기존 7필드 뒤에 기본값 있는 필드 추가 (위치 인자 생성 코드 호환)
    ...
    locker_id: str | None = None              # 마지막 OPEN/CLOSE 전이의 사물함 (single 모드 None)
    open_lockers: tuple[str, ...] = ()        # 지금 출력 ON 인 사물함, 정렬. single 모드 ()
    # is_open = 어떤 채널이든 ON (녹색 LED 와 동일). state = 집계: FAULT > OPEN > COOLDOWN > IDLE

class DoorController(Protocol):
    def open(self, reason: str, duration: float | None = None, *, locker_id: str | None = None) -> bool
        """single: locker_id 는 라우팅에 쓰지 않고(DEBUG 로그) 기존 동작.
        per_locker: 그 locker_id 채널 하나만 ON. 거부 조건·코드는 §13.4. 거부 시 False + last_reject 설정."""
    def close(self, reason: str = "", *, locker_id: str | None = None) -> None
        """locker_id None → 전 채널 OFF(멱등). 지정 → 그 채널만 OFF. 미설정 locker_id → 전 채널 OFF + WARNING (fail-closed)."""
    def status(self) -> DoorStatus                         # 집계 상태
    def locker_status(self, locker_id: str) -> DoorStatus  # 채널 단위. 미설정 id → KeyError. single 모드 → KeyError
    def locker_ids(self) -> tuple[str, ...]                # 선언 순서. single 모드 ()
    @property
    def last_reject(self) -> str                           # 마지막 open() 거부 코드, 성공 시 "" (§13.4)
    def cleanup(self) -> None                              # 전 채널 OFF → 타이머 취소 → 핀 해제
```

`HardwareConfig.validate()` 추가 규칙(ConfigError): `solenoid_pin is None` XOR `locker_outputs` 비어 있음 위반; `len(locker_outputs) > ABSOLUTE_MAX_LOCKERS`; locker_id 중복·빈 문자열·공백 포함; 모든 핀(솔밸브·사물함·LED·버튼) 중복; 솔밸브/사물함 핀이 `RESERVED_OUTPUT_PINS`; `max_concurrent_open` 범위. WARNING(오류 아님): LED 핀이 `RESERVED_OUTPUT_PINS`; `effective_active_high=True`인 사물함 핀이 BCM 0~8(부팅 기본 풀업 → 부팅 중 릴레이 오동작 가능, Pi5 RP1에서 미검증).
YAML 파싱: `locker_outputs`는 **리스트**만 허용(매핑 형태는 PyYAML이 중복 키를 조용히 덮어쓰므로 금지). 항목의 알 수 없는 키는 ConfigError. `locker_id`가 0 이상 정수면 `str()`로 변환(`locker_id: 1` → `"1"`), bool·실수·그 외 타입은 ConfigError.

**(v1.5) 리밋센서·잠금 상태기계 확장 — 하위호환.** 규칙·상태기계는 §15. (v1.4의 close-hold `hold()`·`CloseCause.CLOSE_HOLD_ELAPSED`·`close_hold_sec`는 **폐기**: 구현된 적 없고 §15가 대체한다.)

```python
ABSOLUTE_MAX_UNLOCK_SEC: float = 120.0       # limit 모드 절대상한(코드). 설정 max_unlock_sec 이 넘을 수 없음 (timer 모드는 ABSOLUTE_MAX_OPEN_SEC=10.0 유지)

class LockerState(str, Enum):                # 사물함(채널) 1개의 잠금+문 상태 (§15.3)
    LOCKED_CLOSED = "LOCKED_CLOSED"          # 솔밸브 OFF, 리밋 눌림(문 닫힘)
    UNLOCKED_WAITING = "UNLOCKED_WAITING"    # 솔밸브 ON, 리밋 눌림 — 문이 열리길 기다림 (unlock_timeout_sec)
    OPEN = "OPEN"                            # 솔밸브 ON, 리밋 해제(문 열림)
    CLOSING_CONFIRM = "CLOSING_CONFIRM"      # 솔밸브 ON, 리밋이 다시 눌림, close_confirm_sec 연속 확인 중
    COOLDOWN = "COOLDOWN"                    # 방금 재잠금, cooldown_sec 동안 재해제 거부
    LOCKED_OPEN = "LOCKED_OPEN"              # 솔밸브 OFF인데 리밋 해제(강제로 열림/문 반쯤 열림/센서 선 빠짐) — 해제 요청 거부
    SENSOR_FAULT = "SENSOR_FAULT"            # 리밋 읽기 실패/미연결 — 솔밸브 OFF 강제, 해제 요청 거부
    FAULT = "FAULT"                          # 출력 불변식 위반 등(기존). 컨트롤러 전체 FAULT, 재시작 필요

class CloseCause(str, Enum):                 # 솔밸브 OFF(재잠금) 원인
    UNLOCK_TIMEOUT = "unlock_timeout"        # 해제 후 unlock_timeout_sec 안에 문이 안 열림
    CLOSE_CONFIRMED = "close_confirmed"      # 문이 열렸다 닫히고 close_confirm_sec 연속 눌림
    WATCHDOG = "watchdog"                    # 해제 시점 + max_unlock_sec 도달 (센서가 무엇을 말하든) — fail-safe 방향 강제 잠금
    SENSOR_FAULT = "sensor_fault"            # 센서 읽기 실패가 sensor_fault_sec 지속
    SHUTDOWN = "shutdown"                    # cleanup/시그널/atexit/앱 예외/카메라 실패
    FAULT = "fault"
    TIMER = "timer"                          # close_mode=timer(기존 open_sec 고정) 만료
    MANUAL = "manual"                        # close() 직접 호출(기본값)

@dataclass
class HardwareConfig:   # 추가 필드 (default.yaml hardware 키와 동일 이름). 의미·범위는 §15.2
    close_mode: str = "timer"                 # "timer"(구 설정 호환 기본) | "limit". default.yaml 은 limit. "hold" 는 ConfigError
    limit_switch_pin: int | None = None       # single 모드 리밋 입력 BCM. per_locker 에서는 사용 금지(None), 채널별 limit_pin 사용
    limit_pull_up: bool = True
    limit_active_low: bool = True             # True: 눌림 = 핀 LOW
    limit_poll_sec: float = 0.02
    open_confirm_sec: float = 0.1
    close_confirm_sec: float = 2.0
    unlock_timeout_sec: float = 10.0
    max_unlock_sec: float = 30.0
    sensor_fault_sec: float = 1.0

@dataclass(slots=True, frozen=True)
class LockerOutput:     # 추가 필드(끝, 기본값 None)
    limit_pin: int | None = None              # 그 사물함 리밋 입력 BCM. limit 모드 + pi 에서는 필수

@dataclass(slots=True, frozen=True)
class LockerStatus:                          # 사물함 1개의 읽기 전용 스냅샷 (패널·로그·테스트용)
    locker_id: str | None                    # single 모드 None
    state: LockerState
    solenoid_on: bool                        # 출력 ON 인가 (state 가 UNLOCKED_WAITING/OPEN/CLOSING_CONFIRM 일 때만 True — 불변식)
    door_closed: bool | None                 # 최신 유효 샘플의 디바운스 전 값 아님: 현재 확정 판단. True=닫힘(눌림) False=열림(해제) None=센서 불명
    sensor_ok: bool
    unlock_wait_remaining_sec: float         # UNLOCKED_WAITING 일 때 재잠금까지, 그 외 0.0
    close_confirm_remaining_sec: float       # CLOSING_CONFIRM 일 때 잠금까지, 그 외 0.0
    hard_remaining_sec: float                # solenoid_on 일 때 max_unlock_sec 까지, 그 외 0.0
    cooldown_remaining_sec: float
    unlocked_at: float | None                # monotonic, 마지막 해제 시각
    last_close_cause: str                    # CloseCause 값, 없으면 ""
    last_reject: str

@dataclass(slots=True, frozen=True)
class DoorEvent:                             # 하드웨어 스레드가 만든 전이 기록. 앱이 매 step() drain 해 events.csv 로 옮김 (놓침 방지)
    ts: float                                # monotonic
    locker_id: str | None
    kind: str                                # "unlocked" | "door_opened" | "door_closing" | "relock" | "forced_open" | "sensor_fault" | "sensor_ok"
    cause: str                               # relock 일 때 CloseCause 값, 그 외 ""
    detail: str                              # 예: "unlocked_for=3.2s door_open_for=2.1s"

class LimitInputs(Protocol):                 # 리밋센서 입력 묶음 (hardware/limits.py)
    is_simulated: bool                       # mock state 백엔드 / MockFactory 이면 True
    def read(self, locker_id: str | None) -> bool | None
        """True=눌림(문 닫힘), False=해제(문 열림), None=읽기 실패/미설정. 극성(limit_active_low)은 여기서 해석. 예외를 밖으로 던지지 않는다."""
    def close(self) -> None
class MockLimitInputs(LimitInputs):           # 시뮬레이션. 초기값 전 채널 눌림(닫힘)
    def set_closed(self, locker_id: str | None, closed: bool) -> None
    def toggle(self, locker_id: str | None) -> bool      # 토글 후의 closed 값
    def set_fault(self, locker_id: str | None, fault: bool) -> None   # True → read() 가 None
    def snapshot(self) -> dict[str, bool | None]
class PiLimitInputs(LimitInputs):             # gpiozero 입력(pull_up=limit_pull_up). 핀 claim 실패 → HardwareError(5)

class DoorController(Protocol):              # 추가/변경
    def open(self, reason: str, duration: float | None = None, *, locker_id: str | None = None) -> bool
        """limit 모드에서 duration 은 무시(DEBUG 로그). 수락 조건·거부 코드는 §15.3 표. 수락 시 UNLOCKED_WAITING."""
    def close(self, reason: str = "", *, locker_id: str | None = None, cause: str = "manual") -> None
    def locker_state(self, locker_id: str | None = None) -> LockerStatus      # single 모드는 None. 미설정 id → KeyError
    def locker_states(self) -> tuple[LockerStatus, ...]                       # 선언 순서(single: 1개). 패널 구성의 유일한 원천
    def drain_events(self) -> list[DoorEvent]                                 # 호출 시 비움, 스레드 안전
    def poll_once(self) -> None
        """센서 1회 샘플링 + 상태기계 1틱. 감시 스레드가 limit_poll_sec 마다 호출. 테스트는 가짜 clock + 이 메서드로 결정적으로 구동."""
```

`HardwareBundle`에 `limits: LimitInputs` 추가(mock 이면 `MockLimitInputs`, pi 이면 `PiLimitInputs`, `close_mode=timer`이면 None 허용). `create_hardware`는 `mock_backend: gpiozero`일 때 MockFactory 핀으로 `PiLimitInputs`와 같은 경로를 쓰고 `is_simulated=True`, 테스트가 `pin.drive_low()/drive_high()`로 리밋을 조작한다. `hardware/` 밖에서 `LimitInputs.read`를 직접 호출하는 코드는 만들지 않는다(앱은 `locker_states()`만 본다).

### 4.2 `gpio_mock.py`, `gpio_pi.py`, `factory.py`

```python
# gpio_mock.py
class MockDoorController:  # DoorController — 순수 파이썬 상태 + logs/hardware.log + history
    def __init__(self, cfg: HardwareConfig, clock: Callable[[], float] = time.monotonic) -> None
    history: list[tuple[float, str, str]]         # (ts, "OPEN"/"CLOSE"/"REJECT"/"WATCHDOG", reason)
class MockStatusLeds:      # StatusLeds
    def __init__(self, cfg: HardwareConfig, door: MockDoorController) -> None

# gpio_pi.py  — gpiozero DigitalOutputDevice/LED, initial_value=False. gpiozero import는 생성자 내부.
class PiDoorController:    # DoorController
    def __init__(self, cfg: HardwareConfig, pin_factory: Any | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None
class PiStatusLeds:        # StatusLeds
    def __init__(self, cfg: HardwareConfig, door: PiDoorController, pin_factory: Any | None = None) -> None
# pin_factory 를 주입하면(예: gpiozero.pins.mock.MockFactory()) PC에서 Pi 코드 경로를 테스트할 수 있어야 한다.

# factory.py
def is_raspberry_pi() -> bool                       # /proc/device-tree/model 에 "Raspberry Pi"
def create_hardware(cfg: HardwareConfig) -> HardwareBundle
    """mode=pi: Pi 아님/ lgpio·gpiozero import 실패 → HardwareUnavailableError (mock 폴백 금지).
    mode=auto: Pi 이면 pi, 아니면 WARNING 로그 후 mock. mode=mock: mock_backend 에 따라 state|gpiozero(MockFactory).
    생성 즉시 atexit + SIGINT/SIGTERM 핸들러에 bundle.cleanup 등록(기존 핸들러 체이닝)."""
```

(v1.3) 구현 영향:
- **권장 구현**: `BaseDoorController`를 "채널 표"로 일반화한다. single 모드 = 내부 채널 1개(id 없음), per_locker = `locker_outputs` 선언 순서의 N채널. 상태 머신(IDLE→OPEN→COOLDOWN→IDLE, FAULT)·워치독·쿨다운은 **채널별**, 락(`RLock`) 하나와 녹색 LED는 **공유**. 하위 클래스 훅은 `_drive_channel(idx, on)`, `_read_channel(idx)`, `_drive_green(on)`, `_read_green()`로 바꾼다(이름은 hw 재량, 공개 API만 계약).
- `MockDoorController`: 채널별 상태 + `history`의 reason에 `locker=<id>` 포함. 테스트용 `channel_levels() -> dict[str, bool]`(per_locker) 제공.
- `PiDoorController`: 채널마다 `gpiozero.DigitalOutputDevice(pin, active_high=..., initial_value=False)`. **하나라도 claim 실패하면 이미 잡은 핀을 모두 OFF·해제하고 HardwareError(종료 코드 5)** — 일부 채널만으로 동작 금지. `mock_backend: gpiozero`(MockFactory)로 같은 경로를 PC에서 테스트.
- `force_outputs_off(cfg)`: `solenoid_pin`(있으면) + **모든 `locker_outputs`** + LED를 OFF 레벨로. 항목별 성공/실패를 반환(하나 실패해도 나머지 계속).
- `load_hardware_config()`: `hardware` 섹션만 읽는 원칙 유지. 사물함 채널이 `hardware` 섹션에 있으므로 systemd `ExecStopPost`(`--force-off`)가 다른 설정 오류와 무관하게 전 채널을 끌 수 있다(이것이 채널 목록을 `lockers`가 아니라 `hardware`에 둔 이유).
- `factory.create_hardware`: 분기 없음(`cfg.per_locker`에 따라 컨트롤러가 채널 수를 결정).

---

## 5. 이벤트 로그 — `logs/events.csv`

UTF-8(BOM 없음), 헤더 1행, `csv` 모듈 기본 방언, 행 단위 즉시 flush. 실수는 소수 4자리, 해당 없음은 빈 문자열. 열 순서 고정:

| # | 컬럼 | 타입/값 | 설명 |
|---|---|---|---|
| 1 | `ts` | ISO8601 ms + TZ | 기록 시각 |
| 2 | `session_id` | str | 프로세스 실행 단위 |
| 3 | `frame_idx` | int | |
| 4 | `event` | `start`\|`frame`\|`open`\|`deny`\|`door_close`\|`error`\|`stop` + (v1.5) `door_opened`\|`door_forced`\|`door_sensor` | `open`/`deny`는 해당 결정 프레임 행. v1.5: `door_close` = **솔밸브 OFF(재잠금)** 행(`close_cause`), `door_opened` = 리밋 해제 확정(문 열림), `door_forced` = 해제 없이 문이 열림, `door_sensor` = 센서 오류/복구. 이 4종은 `DoorEvent` 1건당 1행, 얼굴 컬럼(5~25)은 빈칸 |
| 5 | `verdict` | FrameVerdict | |
| 6 | `decision_state` | DecisionState | |
| 7 | `best_id` | str | 최고 점수 등록자(수용 여부 무관) |
| 8 | `best_score` | float | 코사인 |
| 9 | `second_id` | str | |
| 10 | `second_score` | float | |
| 11 | `margin` | float | |
| 12 | `threshold` | float | 적용 임계값 |
| 13 | `threshold_group` | `global`\|`occluded` | |
| 14 | `threshold_source` | `calibration`\|`default`\|`max(calibration,default)`\|`global:<source>` | `global:<source>` = occluded 그룹 임계값이 global 값으로 **올려진** 경우(§7.2 규칙 4). occluded 보정값은 쓰이지 않았고 `<source>`는 global 임계값의 출처. 예 `global:max(calibration,default)`, `global:calibration`, `global:default` |
| 15 | `matched_tag` | str | 최고 갤러리 샘플 태그 |
| 16 | `condition_tag` | str | 실행 시 `--tag` (기본 `live`) |
| 17 | `hold_reason` | HoldReason 값 | |
| 18 | `n_faces` | int | |
| 19 | `face_w` | int | |
| 20 | `face_h` | int | |
| 21 | `det_score` | float | |
| 22 | `blur_var` | float | |
| 23 | `brightness` | float | |
| 24 | `yaw_ratio` | float | |
| 25 | `liveness` | `none`\|`live`\|`spoof`\|`unknown` | |
| 26 | `votes` | `"n/m"` | n=창 안 대상자 MATCH 수, m=**현재 창 길이**(≤ `window_frames`). 예 `2/5`, 첫 프레임들은 `1/1`, `2/2` |
| 27 | `door_signal` | 0\|1 | 기록 시점 솔밸브 출력 (`door.open()` 호출 **후** 읽은 `DoorStatus.is_open`) |
| 28 | `det_ms` | float | |
| 29 | `emb_ms` | float | |
| 30 | `total_ms` | float | |
| 31 | `embedder_id` | str | `Embedder.model_id` |
| 32 | `message` | str | error 메시지, open reason 등 |
| 33 | `label` | 빈칸\|`<person_id>`\|`_impostor`\|`_ignore` | 사후 라벨(사용자/`scripts/label_events.py`). 프로그램은 빈칸으로 씀 |
| 34 | `locker_id` | str | (v1.3) `event=open`: `decision.person_id`의 person.json locker_id **원값**(억제된 경우도 기록, 없으면 빈칸). `event=door_close`: 닫힌 사물함. 그 외 행·single 모드에서 open 외 행은 빈칸. single 모드 open 행도 배정값이 있으면 기록(참고용) |
| 35 | `open_lockers` | str | (v1.3) per_locker 모드에서 기록 시점 출력 ON 사물함 목록(`;` 구분, 정렬, 없으면 빈칸). single 모드는 항상 빈칸 |
| 36 | `close_cause` | `CloseCause` 값 | (v1.4) `event=door_close` 행에만. 그 외 빈칸 (§15). v1.5 값: `unlock_timeout`, `close_confirmed`, `watchdog`, `sensor_fault`, `shutdown`, `fault`, `timer`, `manual` |
| 37 | `locker_states` | str | (v1.5) 기록 시점 사물함 상태 `"<id>=<LockerState>"`를 `;`로 연결(선언 순서, single은 `-=<state>`). 예 `1=LOCKED_CLOSED;2=OPEN;3=LOCKED_CLOSED;4=SENSOR_FAULT`. timer 모드는 빈칸 |

**(v1.3) 컬럼 추가의 하위호환 규칙**
- 새 컬럼은 **끝에만** 추가한다(1~33 순서·의미 불변). `EVENT_COLUMNS` 길이 37(v1.5). 읽는 쪽은 33/35/36/37 모두 허용.
- `EventLogger` 시작 시 기존 `events.csv`의 헤더가 현재 `EVENT_COLUMNS`와 다르면 이어쓰지 않고 기존 파일을 `events_<YYYYmmdd_HHMMSS>.csv`로 회전한 뒤 새 헤더로 시작한다(한 파일 안에 열 수가 다른 행 금지).
- 읽는 쪽(`records_from_events`, `label_events.py`, `locker_audit`)은 `csv.DictReader`로 이름 기반 접근, 33컬럼(구버전) 파일도 받아들이며 없는 컬럼은 빈칸으로 본다. `label_events.py`는 다시 쓸 때 **원래 헤더의 모든 컬럼을 보존**한다.
- per_locker 모드에서 `door_signal`(27)은 "**대상 사물함** 채널이 ON인가"(`locker_status(L).is_open`)이다. 다른 사물함이 켜져 있는지는 `open_lockers`로 본다.

**`event=open`은 개방 *결정* 이벤트다.** `DecisionOutput.should_open=True`인 프레임 행에 기록되며, 실제로 솔밸브가 켜졌는지와는 별개다. locker 검사로 억제되었거나(§12.2) 문이 OPEN/COOLDOWN/FAULT라 `door.open()`이 거부된 경우에도 `event=open`이 남는다(이유는 `message`, §3.13). 따라서:
- 실제 개방 여부는 같은 행의 `door_signal`(1이면 이 결정으로 출력 ON 상태)과 `logs/hardware.log`의 `OPEN`/`REJECT` 기록으로 판단한다.
- 실제 개방 횟수를 셀 때는 `event=open` 행 수가 아니라 `event=open and door_signal=1` 또는 `hardware.log`의 `OPEN` 수를 쓴다.
- (검토 중, 미채택) 억제/거부를 별도 이벤트 `open_suppressed`로 분리하는 안은 계약 변경이므로 채택 시 이 절과 `EVENT_NAMES`를 함께 갱신한다.

평가(`scripts/evaluate.py --events`)는 `label`이 채워진 `event=frame` 행만 사용한다. 라벨 `<person_id>` 행: genuine(그 사람으로 MATCH면 정수용). `_impostor` 행: verdict=MATCH 면 오수용(FA).

---

## 6. 데이터 디렉터리 규약

### 6.1 트리

```
data/
  gallery/
    _meta.json                        # {"schema_version":1, "model_id":"sface:ab12cd34ef56", "dim":128, "updated_at":"..."}
    <person_id>/
      person.json                     # {"person_id":..., "display_name":"홍길동"(선택), "locker_id":"A12"(선택), "created_at":..., "updated_at":...} — §12.3
      <tag>_<n>.npy                   # (D,) float32 L2 정규화 임베딩. n은 태그별 0부터 3자리 (normal_000.npy)
      <tag>_<n>.jpg                   # 비정렬 얼굴 crop (bbox를 crop_margin 만큼 확장). 재임베딩용
      <tag>_<n>.json                  # 샘플 메타 (§6.2)
  probe/
    <condition>/<person_id>/*.jpg|png # 시험용 (gallery 원본과 겹치면 안 됨)
    _impostor/<impostor_id>/*.jpg     # 미등록자 (FAR 계산용). condition 하위가 아니라 최상위 특수 폴더
    _impostor_<condition>/<id>/*.jpg  # (선택) 조건별 미등록자
    syn_<cond>/<person_id>/*.jpg      # make_synthetic_probes.py 출력 (합성)
```

- `person_id`: `^[A-Za-z0-9_-]{1,32}$` (ASCII; 한글 이름은 `person.json.display_name`).
- `tag`: `normal, glasses, noglasses, mask, scarf, lowlight, backlight` + (v1.3) `hat`(모자), `other`(기타) + 합성 증강 샘플은 `synthetic_<원태그>` (예 `synthetic_mask`). `hat`/`other`는 `global` 임계값 그룹(§14.5).
- `condition`: `normal, lowlight, backlight, mask, glasses, noglasses, scarf, angle` (+ `syn_*`). 새 조건 이름은 소문자·숫자·`_`만.

### 6.2 샘플 메타 `<tag>_<n>.json`

```json
{"person_id": "alice", "tag": "mask", "index": 3, "pose": "left",
 "source": "live:cam:0" | "file:<원본 경로>", "source_sha256": "<원본 이미지 sha256 또는 null(라이브)>",
 "crop_sha256": "<jpg sha256>", "model_id": "sface:ab12cd34ef56",
 "det_score": 0.97, "quality": {"face_px": 142, "blur_var": 210.5, "brightness": 118.2, "yaw_ratio": -0.31},
 "created_at": "2026-10-16T09:30:12.345+09:00"}
```

누수 검사는 probe 이미지 sha256 ↔ gallery `source_sha256`·`crop_sha256` 비교로 한다.

---

## 7. 임계값 결정 규칙과 `models/calibration.json`

### 7.1 스키마 (I/O: `facedoor/calibration.py`, eval 소유)

```json
{
  "schema_version": 1,
  "created_at": "2026-10-16T10:00:00+09:00",
  "score": "cosine",
  "embedder": {"backend": "sface", "model_id": "sface:ab12cd34ef56"},
  "aggregation": "max",
  "target_far": 0.001,
  "margin": 0.05,
  "thresholds": {
    "global":   {"threshold": 0.4721, "far": 0.0, "frr": 0.031, "far_upper95": 0.0021,
                 "n_genuine": 640, "n_impostor": 1420, "impostor_source": "real+cross",
                 "synthetic_only": false, "reliable": false},
    "occluded": null
  },
  "data": {"gallery_dir": "data/gallery", "probe_dir": "data/probe", "split_by": "person",
           "calibration_fraction": 0.5, "seed": 42, "synthetic_fraction": 0.25},
  "report_dir": "reports/20261016-100000",
  "notes": ["impostor 표본 1420 < 3/target_far=3000 → reliable=false"]
}
```

```python
# facedoor/calibration.py
@dataclass
class GroupThreshold:
    threshold: float; far: float | None; frr: float | None; far_upper95: float | None
    n_genuine: int; n_impostor: int; impostor_source: str; synthetic_only: bool; reliable: bool
@dataclass
class CalibrationFile:
    schema_version: int; created_at: str; score: str; embedder_backend: str; embedder_model_id: str
    aggregation: str; target_far: float; margin: float | None
    thresholds: dict[str, GroupThreshold | None]   # keys "global", "occluded"
    data: dict; report_dir: str | None; notes: list[str]
def load_calibration(path: Path) -> CalibrationFile | None   # 없음 → None. 손상/스키마 오류 → WARNING 로그 후 None
def save_calibration(path: Path, cal: CalibrationFile) -> None  # 임시 파일 쓰고 원자적 교체, 기존 파일은 .bak
```

`reliable` = `n_impostor >= ceil(3 / target_far)` and `not synthetic_only` and `impostor_source != "none"`.
`far_upper95`: 관측 FA가 0이면 3/N(Rule of three), 아니면 Clopper-Pearson 상한.

### 7.2 런타임 결정 규칙 (`ThresholdPolicy.from_sources`)

그룹 g ∈ {global, occluded}, 백엔드 b, 기본값 D = `default_thresholds[b][g]`, 바닥 F = `threshold_floor[b]`:

1. calibration 없음 / `score != "cosine"` / `embedder_model_id` ≠ 현재 `Embedder.model_id` / `thresholds[g]` 가 null → `thr = D`, source=`default`.
2. `reliable=true` → `thr = max(C, F)`, source=`calibration`.
3. `reliable=false` 이고 `unreliable_calibration=max_with_default` → `thr = max(C, D, F)`, source=`max(calibration,default)`. `ignore` → `thr = D`.
4. occluded 그룹: 위에서 구한 값이 global 값보다 낮아지는 것은 `thresholds.occluded.reliable=true` 이고 `synthetic_only=false`일 때만 허용. 아니면 occluded = global 값으로 올리고 source=`global:<global의 source>` (`recognizer.SRC_RAISED_PREFIX`), notes에 사유 기록.
5. `margin`: 설정값 사용 (calibration.margin은 참고 기록용).

즉, 보정 결과는 **임계값을 올리는 방향으로는 언제나**, 내리는 방향으로는 충분한 실측 impostor 표본이 있을 때만 반영된다.

---

## 8. 평가 (eval) — `facedoor/evaluation/`

```python
# dataset.py
@dataclass
class ProbeSample:
    path: Path; condition: str; true_id: str | None   # None = impostor
    impostor_id: str | None; is_synthetic: bool; sha256: str
def scan_gallery(gallery_dir: Path) -> list[GallerySample]
def scan_probes(probe_dir: Path, impostor_dir_name: str = "_impostor", synthetic_prefix: str = "syn_") -> list[ProbeSample]
def check_leakage(gallery: Sequence[GallerySample], probes: Sequence[ProbeSample]) -> list[tuple[Path, Path]]
def split_calibration(probes: Sequence[ProbeSample], fraction: float, by: str, seed: int) -> tuple[list[ProbeSample], list[ProbeSample]]

# augment.py
def apply_condition(image: np.ndarray, condition: str, rng: np.random.Generator,
                    landmarks: np.ndarray | None = None) -> np.ndarray   # condition ∈ evaluation.synthetic_conditions

# scoring.py
@dataclass
class ScoreRecord:   # scores.csv 1행 (§8.1)
    probe_path: str; condition: str; is_synthetic: bool; true_id: str; split: str   # true_id "_impostor" 가능; split "cal"|"test"
    verdict: str; hold_reason: str; best_id: str; best_score: float | None; second_id: str; second_score: float | None
    own_score: float | None          # 본인 점수 (impostor 는 None)
    max_other_score: float | None    # 본인 제외 최고 점수 (impostor 는 전체 최고)
    matched_tag: str; threshold: float | None; threshold_group: str; accepted_id: str; latency_ms: float
    scores: dict[str, float]         # CSV에는 scores_json 컬럼(JSON 문자열)
def score_probes(pipeline: FramePipeline, probes: Sequence[ProbeSample], split: Mapping[Path, str],
                 enrolled: Iterable[str] | None = None, score_hold: bool = True) -> list[ScoreRecord]
def verification_scores(records: Sequence[ScoreRecord], include_hold: bool = False) -> tuple[np.ndarray, np.ndarray]
    """1:1 genuine = own_score, impostor = 모든 (probe, 다른 등록자) 쌍 점수. ROC/DET/EER 용.
    HOLD/NO_FACE 레코드는 include_hold=True 일 때만 포함."""
def openset_scores(records: Sequence[ScoreRecord], include_hold: bool = False) -> tuple[np.ndarray, np.ndarray]
    """genuine = own_score, impostor = max_other_score. 문 개방 위험에 직접 대응 → 임계값 보정에 사용."""
def records_from_events(events_csv: Path, ignore_label: str = "_ignore") -> tuple[list[ScoreRecord], list[str]]
    """라벨된 event=frame 행 → ScoreRecord(split="test", is_synthetic=False). 두 번째 값은 경고 목록."""

# summary.py — metrics.json 구성 (순수 계산)
def compute_metrics(records: Sequence[ScoreRecord], *, target_far: float = 0.001, count_hold_as_reject: bool = True,
                    bootstrap_iters: int = 1000, ci: float = 0.95, seed: int = 42, data_origin: str = "pipeline",
                    embedder: dict | None = None, thresholds: dict | None = None, ...) -> dict
def compare_metrics(current: dict, previous: dict) -> list[dict]

# metrics.py — 모두 순수 함수, 수용 규칙 score >= thr
def far_frr_at(genuine: np.ndarray, impostor: np.ndarray, thr: float) -> tuple[float, float]
def roc_curve(genuine, impostor) -> tuple[np.ndarray, np.ndarray, np.ndarray]   # thresholds, far, tar
def det_curve(genuine, impostor) -> tuple[np.ndarray, np.ndarray, np.ndarray]   # thresholds, far, frr
def eer(genuine, impostor) -> tuple[float, float]                               # eer, threshold
def threshold_at_far(impostor: np.ndarray, target_far: float) -> float
    """FAR(thr) <= target_far 를 만족하는 최소 thr. 표본 부족 시 max(impostor)+1e-6."""
def d_prime(genuine, impostor) -> float
def rule_of_three(n: int) -> float
def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]
def bootstrap_ci(stat: Callable[..., float], groups: Sequence[Any], iters: int, ci: float, seed: int) -> tuple[float, float]
def confusion_matrix(true_ids: Sequence[str], pred_ids: Sequence[str], labels: Sequence[str]) -> np.ndarray  # labels 에 "Unknown","HOLD" 포함

# calibrate.py
def calibrate(cal_records: Sequence[ScoreRecord], target_far: float, embedder: Embedder, cfg: AppConfig, *,
              report_dir: str | None = None, data: dict | None = None) -> CalibrationFile

# report.py (matplotlib 사용 유일 모듈)
def build_report(out_dir: Path, records: Sequence[ScoreRecord], metrics: dict, cfg: AppConfig,
                 previous_metrics: dict | None = None) -> Path   # report.html 경로
```

### 8.1 산출물 `reports/<YYYYmmdd-HHMMSS>/`

`scores.csv`(ScoreRecord 컬럼 순서 그대로, `scores` → `scores_json`), `metrics.json`, `per_condition.csv`, `roc.png`, `det.png`, `score_dist.png`, `confusion.png`, `condition_bar.png`, `report.html`, `config_snapshot.yaml`.

`metrics.json` 최상위 키: `schema_version, created_at, embedder, thresholds(실제 사용값과 source), sample_counts{genuine, impostor, real_impostor, synthetic_fraction, per_condition}, overall{far, frr, frr_matcher, tar, hold_rate, misid_rate, eer, eer_threshold, d_prime, far_upper95, ci{...}}, per_condition{<cond>: 같은 구조}, latency_ms{mean, p50, p95}, warnings[]`. 계산 불가 지표는 `null`과 `warnings`에 이유.

시스템 지표 정의(테스트 데이터, 실제 임계값):
- FAR = impostor probe 중 MATCH(누구로든) 비율.
- misid_rate = genuine probe 중 **다른 사람**으로 MATCH 비율 (보안 사고, FAR과 함께 보고).
- FRR(system) = genuine probe 중 본인으로 MATCH 되지 않은 비율 (HOLD 포함, `count_hold_as_reject`). FRR(matcher) = HOLD 제외.
- 모두 프레임 단위(투표 전)이며 투표 적용 시 실제 오개방률은 이보다 낮다는 점을 리포트에 명시.

---

## 9. 스크립트 CLI 계약

| 스크립트 | 담당 | 주요 인자 |
|---|---|---|
| `scripts/download_models.py` | vision | `[--config] [--force] [--only yunet,sface]` 체크섬 검증, `models/models.lock.json` 기록 |
| `scripts/enroll.py` | vision | `--name <person_id> --tag <tag> [--images DIR\|GLOB] [--source 0] [--count N] [--display-name 이름] [--locker-id ID] [--no-display] [--reembed] [--prune-outliers] [--config] [--set k=v ...]` |
| `scripts/run.py` | vision | `[--config] [--set k=v ...] [--source 0\|path] [--no-display] [--tag live] [--max-frames N]` 종료 코드 = `ExitCode` |
| `scripts/hw_selftest.py` | hw | `[--config] [--set k=v ...] [--mode mock\|pi\|auto] [--allow-solenoid] [--pulse-sec 0.5] [--step-sec 0.5] [--hardware-log PATH] [--force-off]` LED 순차 점멸, 솔밸브는 `--allow-solenoid`가 있을 때만. (v1.3) `[--locker ID ...]` 지정 사물함만, `[--all-lockers]` 선언 순서로 **한 번에 하나씩**(다음 채널 전에 이전 채널 OFF 확인) 펄스 — 둘 다 `--allow-solenoid` 필요 |
| `scripts/assign_locker.py` | vision | (v1.3) `list\|check\|set\|clear` — §13.6 |
| `scripts/admin_gui.py` | vision | (v1.3) `[--config] [--set k=v ...] [--source 0]` 한글 관리 화면 — §14. 실행 시 카메라를 열지 않는다 |
| `scripts/evaluate.py` | eval | `[--gallery data/gallery] [--probe data/probe] [--target-far 0.001] [--out reports/] [--write-calibration] [--events logs/events.csv] [--compare reports/<old>] [--no-calibrate] [--config] [--set k=v ...]` |
| `scripts/make_synthetic_probes.py` | eval | `--src data/probe/normal --out data/probe [--conditions syn_lowlight,...] [--seed 42] [--per-image 1]` |
| `scripts/label_events.py` | eval | `--events logs/events.csv --session <id> --label <person_id\|_impostor\|_ignore> [--from-frame a --to-frame b]` |

모든 스크립트는 `--config`/`--set`을 `load_config`로 처리하고, 실행 시작 시 사용 설정·모델 id·임계값 source를 로그로 출력한다.
`--write-calibration`이 없으면 `evaluate.py`는 `models/calibration.json`을 덮어쓰지 않는다.

옵션 의미 보충:
- `enroll.py --count N`
  - 라이브 모드: **최소 N장**이다. 포즈별 목표 `spp = ceil(N / len(enroll.poses))`이므로 실제 목표는 `len(poses) * spp`(포즈 수의 배수로 올림). 예: `--count 3`, 포즈 5개 → 5장. 생략하면 `enroll.samples_per_pose`.
  - `--images` 모드: 이름순으로 정렬한 이미지 중 사용할 **최대 파일 수**(앞에서 N개).
- `enroll.py --display-name 이름`: `person.json.display_name` 기록(한글 허용).
- `enroll.py --locker-id ID`: `lockers.locker_id_pattern`과 **전체 일치**(`re.fullmatch`)해야 한다. 위반 시 아무것도 쓰지 않고 종료 코드 2(CONFIG). 통과하면 `person.json.locker_id` 기록. (v1.3) 기록 전에 `assign_locker.py set`과 **같은 검증 함수**(`lockers.check_assignment`, §13.6: 설정된 사물함인지, 중복 배정)를 거친다. 캡처 모드에서는 검증을 **캡처 전에** 수행해 실패 시 촬영하지 않고 종료 코드 2.
- 동작 모드(`--reembed` 제외):
  - `--tag` 또는 `--images`가 있거나, `--display-name`/`--locker-id`/`--prune-outliers`가 모두 없으면 → 등록(캡처). `--tag`가 없으면 종료 코드 2. 캡처 뒤 `--display-name`/`--locker-id`는 그 사람의 샘플이 하나 이상 저장됐을 때만 `set_person_info`로 기록(0장이면 경고만, person.json 미기록).
  - `--name X`에 `--display-name`/`--locker-id`/`--prune-outliers`만 주면(`--tag`·`--images` 없음) → **정보 갱신 모드**: 이미 등록된(샘플 ≥1) 사람의 `person.json` 갱신·이상치 정리만 한다. 미등록이면 종료 코드 6(GALLERY).
- `evaluate.py --no-calibrate`: 보정/평가 분할을 하지 않고 모든 probe를 **현재 런타임 임계값**(calibration 파일 + §7.2)으로 평가한다. 보정 후보(`calibration_candidate.json`)를 만들지 않으므로 `--write-calibration`과 함께 써도 보정 파일을 쓰지 않는다.
- `hw_selftest.py --force-off`: 설정된 모든 출력(솔밸브·LED)을 OFF 레벨로 구동하고 즉시 종료한다. 하드웨어 섹션만 읽으므로 다른 설정 오류가 있어도 동작한다. pi 모드에서는 앱 종료 후 lgpio로 직접 구동, mock 모드에서는 로그만 남긴다. systemd `ExecStopPost`가 사용한다. `--hardware-log`는 `hardware.hardware_log` 대체.

---

## 10. 예외와 종료 코드 — `facedoor/errors.py` (작성 완료)

`FaceDoorError` ← `ConfigError`, `CameraError`, `ModelError`, `GalleryError`, `CalibrationError`, `HardwareError` ← `HardwareUnavailableError`.
`ExitCode`: `OK=0, ERROR=1, CONFIG=2, CAMERA=3, MODEL=4, HARDWARE=5, GALLERY=6, INTERRUPTED=130`.
어떤 종료 경로든 그 전에 `HardwareBundle.cleanup()`이 실행되어야 한다.

**시그널 종료**: SIGINT(`KeyboardInterrupt`)와 SIGTERM 모두 `ExitCode.INTERRUPTED(130)`으로 끝난다. SIGTERM은 `hardware.factory` 핸들러가 cleanup(출력 OFF) 후 `SystemExit(143)`을 던지고, `FaceDoorApp.run()`이 이를 잡아 문을 닫은 뒤 130으로 바꾼다. 원래 시그널은 `stop` 이벤트 `message`(`interrupted (SIGINT)` / `terminated (143)`)로 구분한다. 143을 위한 별도 ExitCode는 두지 않는다.
- systemd 유닛(`deploy/facedoor.service`, hw 담당)은 `SuccessExitStatus=130 143`을 가져야 한다. 없으면 `systemctl stop`마다 서비스가 `failed`로 표시된다(현재 유닛에 반영됨).
- `ExecStopPost`의 `hw_selftest.py --force-off`는 종료 코드와 무관하게 실행되어 출력을 다시 OFF로 만든다.

---

## 11. 변경 이력

| 날짜 | 버전 | 변경 | 작성 |
|---|---|---|---|
| 2026-10-16 | v1 | 최초 계약 | face-architect |
| 2026-10-16 | v1.1 | QA 통합 리포트(D-1~D-3, B-1~B-3) 반영, 코드 기준 문서화(코드 변경 없음): §12 관리자 PIN·사물함 신설; §2 AppConfig `admin`/`lockers` 선택 섹션과 검증 규칙; §3.5 `person_info/set_person_info/display_name/locker_id/all_person_ids/embedding`; §3.7 `Recognizer.db/policy/refresh()`와 policy 재대입 계약; §3.8 `from_config` 인자 의미(생략=보정 파일 로드, None=기본값만, `allow_empty_gallery`); §3.13 `clock` 인자·open 억제 message·OPEN reason의 votes/window/best_score 정의; §5 `event=open`은 결정 이벤트(실제 출력은 `door_signal`/hardware.log), `votes` 정의; §6.1 person.json `locker_id`; §8 `calibrate(*, report_dir, data)`, `include_hold`, `records_from_events`, `summary.compute_metrics`; §9 누락 CLI 옵션과 `--count` 의미; §10 SIGINT/SIGTERM→130, `SuccessExitStatus=130 143`. 예정 코드 변경: `lockers.mode=per_locker` ConfigError 거부, `enroll.py --locker-id`, `SuccessExitStatus` | face-architect |
| 2026-10-16 | v1.2 | vision 코드 수정 반영: §1.3 `ascii_safe_path(path, error_cls=ModelError)` Windows에서만 복사, OSError→ModelError(FileCamera는 CameraError); §1.2/§5/§7.2 `threshold_source`에 `global:<source>` 추가(occluded가 global로 올려진 경우); §2 `lockers.mode=per_locker` ConfigError(구현됨), pattern 컴파일 검증; §9/§12 `enroll.py --locker-id` 구현됨(fullmatch, 실패 exit 2), 정보 갱신 모드(미등록 exit 6, `--prune-outliers` 포함), require_locker_id 운영 경고 갱신; §10 `SuccessExitStatus=130 143` 유닛 반영 확인; `default.yaml` admin 주석 "예약됨, 미구현" | face-architect |
| 2026-10-16 | v1.3 | **사물함별 출력(per_locker) 설계 계약 추가, 코드 미구현**: §13 신설(설정 스키마·교차 검증, LockerMap, 런타임 fail-closed 규칙, 거부 코드, hardware.log 형식, assign_locker CLI, 평가 지표, QA 시나리오, 미결 사항); §0 `lockers.py`/`assign_locker.py`/`evaluation/locker_audit.py`; §2 교차 검증 참조; §3.5 `locker_id()` null/비문자열 처리; §3.13 per_locker open 경로·message; §4.1 `LockerOutput`, `HardwareConfig.solenoid_pin: int\|None`·`locker_outputs`·`max_concurrent_open`, `DoorStatus.locker_id/open_lockers`, `open(..., *, locker_id)`·`close(..., *, locker_id)`·`locker_status`·`locker_ids`·`last_reject`, 예약 핀; §4.2 구현 영향; §5 컬럼 34 `locker_id`, 35 `open_lockers`(끝에 추가, 헤더 불일치 시 회전); §9 `hw_selftest --locker/--all-lockers`, `enroll --locker-id` 검증 공유. single 모드 동작 불변. **사용자 결정 반영**: 사물함 4개 확정(`"1"`~`"4"`, 제안 핀 BCM24/25/16/26, active-high 기본), YAML 정수 locker_id → 문자열 변환, per_locker에서 BCM17 미사용(하위호환 설명). **§14 한글 관리 화면 신설**(tkinter, PIL 없이 PPM 미리보기, 등록/제거/목록·사물함 배정/인식 보강, `AdminService`, `Enroller.enroll_live(progress, cancel)` 확장, `runlock` 카메라·갤러리 상호 배제, `gui` 섹션), **§12.1 관리자 PIN 계약**(`adminpin.py`, `lockout_sec`), §6.1 태그 `hat`(global 그룹)·`other` | face-architect |
| 2026-10-16 | v1.4 | 사용자 요구 3건(계약만, 미구현): §15 닫힘 유지(`close_mode: hold`, `close_hold_sec` 5.0, 최소 열림=close_hold_sec, 절대상한 `max_open_sec` 10.0 유지·hold로 연장 불가, watchdog 후 부재 재무장, 다른 사람 MATCH 갱신 금지, fail-closed 즉시 OFF), §4.1 `CloseCause`·`hold()`·`close(cause=)`·`DoorStatus.hold_remaining_sec/hard_remaining_sec/close_cause`, §5 컬럼 36 `close_cause`; §16 사물함 상태 패널(`locker_panel.py`, `door_state.json`, 관리 화면은 상태 불명 시 `unknown`); §17 인식 화면 `TkDisplay`(tkinter, Pillow 기각), 한글화 범위(사용자 노출 한글/로그·식별자 영문), `facedoor/texts.py` 공용화, `gui.font_families` → `display.font_families` | face-architect |
| 2026-10-17 | v1.5 | 사용자 요구(리밋센서 도어, 계약만·미구현): §15를 close-hold에서 **리밋센서 상태기계**로 교체(눌림=닫힘; 해제 후 `unlock_timeout_sec` 내 미개방 시 재잠금; 열려 있는 동안 해제 유지; 눌림이 `close_confirm_sec` 연속이면 잠금; `max_unlock_sec` 워치독은 초과 시 잠금; 센서 실패/미연결 fail-closed: 시작·요청 시 동기 읽기, `door_open`/`sensor_fault` 거부, NC 배선 권장), 설정 키·범위·핀(리밋 BCM 5/6/12/13), mock 시뮬레이션(키 1~4·패널 클릭=문 토글, 5~8=센서 실패 주입, `MockLimitInputs`), QA L1~L17; §4.1 `LockerState/LockerStatus/DoorEvent/LimitInputs/ABSOLUTE_MAX_UNLOCK_SEC`, `CloseCause` 개정, `hold()`/`close_hold_*` 폐기, `locker_outputs[].limit_pin`; §13.4 거부 코드 `door_open`/`sensor_fault`; §13.5 `STATE`/`SENSOR`; §5 이벤트 `door_opened/door_forced/door_sensor`, 컬럼 37 `locker_states`; §16 패널을 문 열림/닫힘 주 표시로 개정(`door_state.json` schema 2); §17.1 창 레이아웃(왼쪽 영상, 오른쪽 사물함 패널 4개) | face-architect |

---

## 12. 관리자 PIN과 사물함(locker)

두 설정 섹션 모두 **선택**이다(YAML에 없으면 기본값, `config._OPTIONAL_SECTIONS`). 둘 다 "제한을 추가하는 방향"으로만 동작해야 하며, 어떤 설정도 개방 조건(§3.9 투표 + §7 임계값)을 완화하지 않는다.

### 12.1 `admin` — **예약됨, 미구현**

```python
@dataclass(slots=True)
class AdminSection:
    pin_file: Path = Path("data/admin_pin.json")   # PBKDF2-SHA256 해시+salt (예정)
    min_pin_len: int = 4                           # 검증: >= 4
    max_attempts: int = 3
    pbkdf2_iterations: int = 200_000
```

- 현재 상태: 설정 로드·검증만 있고 이 값을 사용하는 코드가 없다(PIN 메뉴, `admin_pin.json` 읽기/쓰기 모두 없음). 등록/삭제는 `scripts/enroll.py` CLI로만 하며, 접근 통제는 OS 계정 권한에 의존한다.
- 구현할 때 이 절에 PIN 파일 스키마와 API를 먼저 정의한다. 구현 계획이 확정되기 전까지 `default.yaml`의 주석은 "예약됨, 미구현"으로 표기한다(architect).

**(v1.3 계약, 미구현) 관리 화면(§14) 접근 보호에 사용한다.** 모듈 `facedoor/adminpin.py`(vision, 표준 라이브러리만: `hashlib.pbkdf2_hmac`, `secrets`, `hmac.compare_digest`).

```python
@dataclass(slots=True)
class AdminSection:                                # 추가 필드 (기존 4개 유지)
    lockout_sec: float = 300.0                     # max_attempts 연속 실패 후 잠금 시간. 검증: 0 < v <= 86400

def pin_is_set(cfg: AdminSection) -> bool                     # pin_file 존재 + 스키마 유효
def set_pin(cfg: AdminSection, pin: str) -> None
    """숫자만, len >= min_pin_len 아니면 ValueError(한글 메시지). salt 16바이트, 임시 파일+os.replace, POSIX 권한 0600."""
def verify_pin(cfg: AdminSection, pin: str, now: float | None = None) -> PinResult
    """잠금 중이면 즉시 LOCKED. 실패 횟수·잠금 해제 시각은 pin_file 옆 `<pin_file>.state`(JSON)에 저장 → 프로그램 재시작으로 우회 불가."""
class PinResult(str, Enum): OK = "ok"; WRONG = "wrong"; LOCKED = "locked"; NOT_SET = "not_set"
```

`data/admin_pin.json`: `{"schema_version": 1, "algo": "pbkdf2_sha256", "iterations": 200000, "salt": "<hex>", "hash": "<hex>", "created_at": "..."}`. 손상/스키마 오류 → `NOT_SET`으로 보지 **않고** 관리 화면 진입 거부 + 한글 안내("PIN 파일이 손상되었습니다. 관리자가 data/admin_pin.json 을 삭제한 뒤 다시 설정하세요"). PIN 재설정은 OS 관리자가 파일을 지우는 방식(파일 접근 권한 = 최종 신뢰 경계라는 점을 문서화). PIN은 GUI만 보호하며 CLI(`enroll.py`, `assign_locker.py`)는 계속 OS 권한에 의존한다.

### 12.2 `lockers` — `mode=single` + `require_locker_id` 구현, `per_locker`는 예약

```python
@dataclass(slots=True)
class LockersSection:
    mode: str = "single"                           # single 만 허용. per_locker 는 예약(ConfigError)
    require_locker_id: bool = False
    locker_id_pattern: str = "^[A-Za-z0-9_-]{1,16}$"
```

| 키 | 상태 | 동작 |
|---|---|---|
| `mode: single` | 구현(기본) | locker_id와 무관하게 단일 솔밸브 출력 하나를 사용 |
| `mode: per_locker` | 예약·미구현 (v1.3 계약은 §13) | `load_config`가 **ConfigError**로 거부(종료 코드 2). single로 조용히 대체되지 않는다. 그 밖의 값도 ConfigError. §13 구현 완료 시 이 행을 갱신 |
| `require_locker_id` | 구현 | true면 `should_open=True`라도 `recognizer.db.locker_id(pid)`가 None인 사람은 `door.open()`을 호출하지 않는다(`FaceDoorApp._locker_allows`). 행은 `event=open`, `door_signal`은 변화 없음, `message=open suppressed: <pid> has no locker_id (lockers.require_locker_id)`, WARNING 로그 |
| `locker_id_pattern` | 구현 | 설정 로드 시 정규식 컴파일 검증(실패 → ConfigError). `enroll.py --locker-id`가 `enroll.validate_locker_id()`(`re.fullmatch`)로 전체 일치 검증, 위반 → 종료 코드 2. 런타임(`_locker_allows`)은 패턴을 다시 검사하지 않고 존재 여부만 본다 |

- 운영 절차: `require_locker_id=true`를 켜기 **전에** 모든 등록자에게 locker_id를 넣는다. 이미 등록된 사람은 `python scripts/enroll.py --name <id> --locker-id <L>`(정보 갱신 모드, 미등록이면 종료 코드 6), 새 사람은 `--tag`와 함께 `--locker-id`를 준다. locker_id가 없는 사람은 인식돼도 열리지 않는다(fail-closed). 켜기 전에 `person.json`을 점검할 것.
- `person.json`을 손으로 편집해 넣은 locker_id는 패턴 검증을 거치지 않는다. 가능하면 CLI를 쓴다.
- 표시: `OpenCVDisplay` 라벨은 locker_id가 있으면 `<pid> [L:<locker_id>]`(ASCII만, 한글 display_name은 화면에 그리지 않음).

### 12.3 `person.json`과 `FaceDatabase` 접근자

```json
{"person_id": "alice", "display_name": "홍길동", "locker_id": "A12",
 "created_at": "2026-10-16T09:30:12.345+09:00", "updated_at": "2026-10-16T09:31:00.000+09:00"}
```

- `person_id`, `created_at`: `FaceDatabase.add()`가 첫 샘플 저장 시(또는 `set_person_info`가 파일이 없을 때) 생성.
- `display_name`(선택, 한글 허용): `enroll.py --display-name`.
- `locker_id`(선택, 문자열): `lockers.locker_id_pattern` 형식. `enroll.py --locker-id`로 기록(샘플이 있는 사람만). 없거나 `""`이면 "locker 없음".
- `updated_at`: `set_person_info` 호출마다 갱신.
- 알 수 없는 키는 보존한다(`set_person_info`는 기존 dict에 update).
- 접근 API는 §3.5: `person_info`, `set_person_info(**fields)`, `display_name`, `locker_id`. 앱은 `pipeline.recognizer.db`를 통해 조회한다.

---

## 13. 사물함별 출력 (`lockers.mode=per_locker`) — v1.3 계약, **미구현**

목표: 등록자마다 사물함 1개를 배정하고, 인식된 사람의 **배정 사물함 출력 핀 하나만** 일정 시간 ON 한다. 다른 사물함 핀은 어떤 경우에도 ON 되지 않는다. 설계 이유·배선 주의는 ARCHITECTURE §6.1.

### 13.1 결정 요약

| 항목 | 결정 | 이유 |
|---|---|---|
| 채널(사물함→핀) 정의 위치 | `hardware.locker_outputs` | `hardware/`는 `hardware` 섹션만 읽는다. systemd `--force-off`가 다른 설정 오류와 무관하게 전 채널을 끌 수 있어야 한다 |
| 모드 스위치 | `lockers.mode: per_locker` **그리고** `hardware.locker_outputs` 비어 있지 않음 (불일치하면 ConfigError) | 한쪽만 바꿔 생기는 오동작을 시작 단계에서 차단 |
| 기존 `solenoid_pin` | single 전용. per_locker에서는 `null` 필수 (BCM17을 쓰려면 사물함 채널로 다시 선언) | "어느 핀이 무엇을 여는가"가 한 곳에서만 정의되게 |
| 녹색 LED | 공용 1개. 어느 채널이든 ON이면 ON | 사물함별 LED는 범위 밖(§13.11) |
| 사람→사물함 매핑 | `person.json.locker_id` (문자열 1개) | 이미 있는 필드, 갤러리와 함께 백업·삭제됨 |
| 1인 다사물함 | 미지원. `locker_ids`(리스트) 키는 **예약** — v1.3 코드는 무시하고 WARNING | 여러 개면 무엇을 열지 고를 UI가 필요 |
| 사물함 공유 | 기본 금지(`lockers.allow_shared: false`). 허용해도 CLI에서 `--shared` 명시 필요 | 오배정을 공유로 착각하는 일 방지 |
| 동시 개방 | `hardware.max_concurrent_open` 기본 1, 코드 상한 4 | 전원 용량·돌입전류, 오동작 시 피해 범위 축소 |
| 매핑 반영 시점 | 앱 시작 시 스냅샷. 변경은 재시작 후 반영 | 갤러리 임베딩도 시작 시 로드되므로 일관됨. CLI가 재시작 안내 |
| 이벤트 로그 | 컬럼 34 `locker_id`, 35 `open_lockers` 끝에 추가 (§5) | 하위호환 |

### 13.2 설정 스키마와 검증

확정 구성(사용자 결정 2026-10-16): **사물함 4개, locker_id `"1"`~`"4"`**.

```yaml
hardware:
  solenoid_pin: null                 # per_locker 에서는 null 필수 (BCM17 은 구동·claim 하지 않음, 아래 참고)
  solenoid_active_high: true         # locker_outputs 항목 active_high 생략 시 기본값
  max_concurrent_open: 1             # 1..min(채널 수, 4)
  locker_outputs:                    # 리스트만 허용 (선언 순서 = 채널 순서)
    - {locker_id: "1", pin: 24, limit_pin: 5}    # 솔밸브 물리 18 / 리밋 입력 물리 29  (v1.5 limit_pin, §15.2)
    - {locker_id: "2", pin: 25, limit_pin: 6}    # 물리 22 / 31
    - {locker_id: "3", pin: 16, limit_pin: 12}   # 물리 36 / 32
    - {locker_id: "4", pin: 26, limit_pin: 13}   # 물리 37 / 33
lockers:
  mode: per_locker
  require_locker_id: false           # per_locker 에서는 값과 무관하게 true 로 취급 (INFO 로그)
  locker_id_pattern: "^[A-Za-z0-9_-]{1,16}$"
  allow_shared: false                # 한 사물함을 여러 사람에게 배정 허용 여부
```

**기본 핀 근거**: BCM 24/25/16/26은 기존 솔밸브 17, LED 27/22/23과 겹치지 않고, 모두 BCM ≥ 9(부팅 기본 풀다운 → active-high 드라이버가 부팅 중 켜지지 않음), SPI0(7~11)·I2C(2/3)·UART(14/15)·HAT EEPROM(0/1)을 피한다. 확장 시 후보: BCM 5(29)·6(31)는 부팅 풀업이라 active-low 채널에만, 그 외 12(32)·13(33)·19(35)·20(38)·21(40).

**릴레이 극성 기본값**: `active_high: true`(생략 시 `solenoid_active_high` 상속 = true, 기존과 동일). 권장 드라이버는 3.3V 로직에서 확실히 켜지고 꺼지는 **active-high** 모듈(로직레벨 N-MOSFET 모듈, 또는 High-trigger 점퍼가 있는 옵토 릴레이 보드를 H로 설정). 흔한 5V active-low 릴레이 보드를 쓰면 채널별 `active_high: false`로 바꾸고, 3.3V HIGH에서 릴레이가 완전히 꺼지는지(JD-VCC 5V 구성에서 옵토 LED 잔류 전류로 반쯤 켜지는 문제) 실측해야 한다. **극성을 잘못 쓰면 시작 즉시 4개가 모두 열린다** → QA 시나리오 29는 솔밸브 전원을 분리하고 수행.

**`locker_id` 형식**: 문자열. YAML에서 `locker_id: 1`(정수)로 써도 `HardwareConfig.from_dict`가 `"1"`로 변환한다(0 이상 정수만, `01` 같은 앞자리 0 표기는 문자열로 쓸 것). `person.json`의 값은 반드시 문자열 `"1"`(CLI/GUI가 그렇게 씀) — 숫자 `1`은 `invalid_locker_id`로 억제된다(fail-closed, §3.5).

**기존 단일 출력(BCM17)과의 관계(하위호환)**:
- `lockers.mode: single`(기본): v1.2와 완전히 같다. BCM17 하나가 모든 인식에 대해 열린다. 새 키가 없어도, 기존 `local.yaml`도 그대로 동작.
- `per_locker`: `solenoid_pin: null`이 필수이며 BCM17은 **claim·구동·force-off 대상에서 빠진다**(입력 상태로 남고 Pi 기본 풀다운). 기존 정문 릴레이가 BCM17에 배선돼 있으면 분리하거나, 그 배선을 사물함 채널로 재사용하려면 `locker_outputs`에 `{locker_id: "1", pin: 17}`처럼 명시한다(허용됨, 기본 제안은 17을 비워 둠).
- single로 되돌리기: `lockers.mode: single`, `solenoid_pin: 17`, `locker_outputs: []`. `person.json.locker_id`는 남아 있어도 무해(single에서는 라우팅에 쓰지 않음).

| 규칙 | 검사 위치 | 위반 시 |
|---|---|---|
| `lockers.mode ∈ {single, per_locker}` | config.py | ConfigError |
| `per_locker` ⇒ `locker_outputs` ≥ 1, `single` ⇒ `locker_outputs` = [] | config.py | ConfigError |
| `solenoid_pin is None` ⇔ `locker_outputs` ≥ 1 | `HardwareConfig.validate` | ConfigError |
| 각 `locker_id`가 `locker_id_pattern`과 fullmatch | config.py | ConfigError |
| `locker_id` 중복, **대소문자만 다른 중복**(`A01`/`a01`), 빈 문자열·공백 | `HardwareConfig.validate` | ConfigError |
| 채널 수 ≤ `ABSOLUTE_MAX_LOCKERS`(16) | `HardwareConfig.validate` | ConfigError |
| 솔밸브·사물함·LED·버튼 핀 전체 중복 없음, BCM 0~27 | `HardwareConfig.validate` | ConfigError |
| 솔밸브/사물함 핀 ∉ `RESERVED_OUTPUT_PINS` {0,1,2,3,14,15} | `HardwareConfig.validate` | ConfigError |
| `1 <= max_concurrent_open <= min(max(N,1), 4)` | `HardwareConfig.validate` | ConfigError |
| `locker_outputs`는 리스트만, 항목 키는 `locker_id, pin, active_high`(v1.5: `, limit_pin`)만 | `HardwareConfig.from_dict` | ConfigError |
| active_high 사물함 핀이 BCM 0~8 | `HardwareConfig.validate` | WARNING (부팅 기본 풀업, Pi5 RP1 미검증) |
| LED 핀이 예약 핀 | `HardwareConfig.validate` | WARNING |
| `decision.rearm_sec >= hardware.open_sec + hardware.cooldown_sec` | config.py | WARNING (기존 권장 유지) |

### 13.3 매핑 저장과 `facedoor/lockers.py` (vision)

저장: `data/gallery/<pid>/person.json`의 `"locker_id": "A03"`(문자열). 해제는 `"locker_id": null`. 변경 이력은 §13.6 `logs/locker_assign.log`.

```python
class LockerCheck(str, Enum):
    OK = "ok"                                 # 개방 허용
    NOT_REQUIRED = "not_required"             # single + require_locker_id=false: locker 무관 허용
    NO_LOCKER_ID = "no_locker_id"
    INVALID_LOCKER_ID = "invalid_locker_id"   # 비문자열·패턴 불일치(수동 편집)
    UNKNOWN_LOCKER = "unknown_locker"         # hardware.locker_outputs 에 없음
    SHARED_DENIED = "shared_denied"           # 다른 사람과 공유 + allow_shared=false

@dataclass(frozen=True, slots=True)
class LockerResolution:
    person_id: str
    locker_id: str | None      # person.json 원값 (events.csv locker_id 컬럼에 기록)
    check: LockerCheck
    allowed: bool              # check ∈ {OK, NOT_REQUIRED}
    target: str | None         # per_locker: door.open(locker_id=target). single: None
    message: str               # 허용이면 "", 아니면 §3.13 억제 message

class LockerMap:
    @classmethod
    def build(cls, db: FaceDatabase, cfg: LockersSection, configured: Sequence[str]) -> "LockerMap"
        """configured = [o.locker_id for o in hw.locker_outputs] (single 모드 ()). person.json 을 1회 읽어 스냅샷."""
    mode: str
    def resolve(self, person_id: str) -> LockerResolution
    def assignments(self) -> dict[str, str | None]       # pid -> locker_id (all_person_ids 전원)
    def owners(self, locker_id: str) -> list[str]
    def problems(self) -> list[LockerResolution]          # allowed=False 인 등록자 전원
    def free_lockers(self) -> list[str]                   # 아무도 배정 안 된 설정 사물함
    def summary(self) -> str                              # "per_locker n=4 assigned=3 free=1 problems=1"

def check_assignment(db: FaceDatabase, cfg: LockersSection, configured: Sequence[str], person_id: str,
                     locker_id: str, *, shared_ok: bool = False) -> list[str]
    """enroll --locker-id 와 assign_locker set 이 공유. 빈 리스트 = 통과. 오류 문자열 목록:
    패턴 불일치 / per_locker 인데 configured 에 없음 / 다른 사람이 이미 배정(allow_shared=false, 또는 true 인데 shared_ok=False).
    single 모드: 패턴만 검사(단일 출력이므로 중복은 의미 없음)."""
```

`resolve()` 판정 순서 (per_locker): locker 없음 → `NO_LOCKER_ID` → 비문자열/패턴 불일치 → `INVALID_LOCKER_ID` → 설정에 없음 → `UNKNOWN_LOCKER` → 배정자 2명 이상이고 `allow_shared=false` → `SHARED_DENIED`(**관련자 전원** 억제) → `OK`.
single 모드: `require_locker_id=false` → `NOT_REQUIRED`, true 이고 locker 없음 → `NO_LOCKER_ID`(v1.2와 같은 message), 그 외 `OK`(target=None). **single 모드 런타임 동작은 v1.2와 동일해야 한다.**

공유 정책: 수동 편집 등으로 금지된 공유가 생기면 **시작은 하되** 해당 사물함 배정자 전원을 억제한다(다른 사물함 사용자까지 막지 않기 위함). 시작 시 `LockerMap.problems()` 각각을 WARNING, `start` 이벤트 message에 `summary()` 포함.

### 13.4 런타임 규칙 (fail-closed)

앱(`app.py`):
1. `door.open()`은 `decision.should_open is True` 분기에서만 호출(기존). 대상 = `LockerMap.resolve(decision.person_id)`.
2. `allowed=False` → `door.open()` 호출 없음. `event=open` 행, `locker_id`=원값, `door_signal`=0, message=`resolution.message`, WARNING 로그. **어떤 출력도 바뀌지 않는다.**
3. `allowed=True` → `door.open(open_reason(..., locker_id=target), locker_id=target)`. 반환 False면 message=`<reason> (rejected by door: <door.last_reject>)`.
4. 예외 → `door.close(reason)` = 전 채널 OFF(기존 fail-closed 경로 그대로).

도어 컨트롤러(`hardware/`, 앱 검사와 별개로 **반드시 다시 검사** — 방어선 2중화):

| 순서 | 조건 | 결과 / `last_reject` |
|---|---|---|
| 1 | cleanup 이후 | 거부 `shutdown` |
| 2 | 지연 전이 적용(만료 채널 OFF, 쿨다운 경과) 후 컨트롤러 FAULT | 거부 `fault` |
| 3 | per_locker인데 `locker_id is None` | 거부 `no_locker_id` |
| 4 | 설정에 없는 `locker_id` | 거부 `unknown_locker` |
| 5 | 그 채널 OPEN(timer 모드) / UNLOCKED_WAITING·OPEN·CLOSING_CONFIRM(limit 모드) | 거부 `busy` (연장 없음) |
| 5a | (v1.5 limit) 채널 SENSOR_FAULT, 또는 요청 시점 리밋 재읽기 실패 | 거부 `sensor_fault` |
| 5b | (v1.5 limit) 채널 LOCKED_OPEN, 또는 요청 시점 리밋이 해제(문 열림) | 거부 `door_open` |
| 6 | 그 채널 COOLDOWN | 거부 `cooldown` |
| 7 | 다른 OPEN 채널 수 ≥ `max_concurrent_open` | 거부 `concurrency_limit` |
| 8 | duration 비정상(NaN, ≤0) | 거부 `invalid_duration` |
| 9 | **사전 불변식**: OPEN 상태가 아닌 채널의 출력이 ON으로 읽힘 | 전 채널 OFF, FAULT, 거부 `invariant`, ERROR 로그 |
| 10 | 대상 채널 ON → 녹색 ON. 출력 구동 예외 | 전 채널 OFF, FAULT, `HardwareError` (기존과 동일) |
| 11 | **사후 불변식**: 대상 채널이 ON으로 읽히지 않거나, OPEN이 아닌 채널이 ON | 전 채널 OFF, FAULT, False `invariant` |
| 12 | 성공 | 채널 OPEN, 타이머 = `min(duration or open_sec, max_open_sec, 10.0)`, True, `last_reject=""` |

single 모드 `last_reject` 코드: `shutdown`, `fault`, `busy`, `cooldown`, `invalid_duration` (기존 동작에 코드만 노출).

- 워치독·쿨다운은 **채널별**: 채널마다 `threading.Timer` + `status()`/`open()`의 지연 시각 검사. 만료 → 그 채널 OFF → COOLDOWN(`cooldown_sec`) → IDLE. 다른 채널의 쿨다운과 무관.
- (v1.5) `close_mode: limit`에서는 위 "OPEN 후 타이머 만료 → OFF"가 **§15.3 상태기계**로 대체된다(`unlock_timeout_sec`/`close_confirm_sec`/`max_unlock_sec` 기준, 워치독 = `max_unlock_sec`). 위 거부 코드 표의 순서와 `concurrency_limit`·`invariant` 규칙은 그대로이고, 코드 `busy`/`cooldown`의 대상 상태만 §15.3 표를 따른다. `HardwareConfig.max_open_sec`·`open_sec`는 timer 모드 전용.
- 녹색 LED = 어느 채널이든 ON. 켤 때 출력→녹색, 끌 때 마지막 ON 채널이면 녹색→출력(기존 순서 유지).
- 한 채널이라도 FAULT → 컨트롤러 전체 FAULT(전 채널 OFF 시도, 이후 모든 open 거부, 재시작 필요).
- 시작: 모든 채널을 `initial_value=False`로 claim → INIT 로그. 하나라도 claim 실패 → 잡은 핀 OFF·해제 후 `HardwareError`(종료 코드 5).
- 종료: `cleanup()`/atexit/SIGINT/SIGTERM → 전 채널 OFF(기존 경로). systemd `ExecStopPost --force-off`가 `locker_outputs` 전체를 다시 OFF. (권장) `ExecStartPre`에도 `--force-off`.
- 모든 공개 메서드는 같은 `RLock` 아래에서 채널 표를 읽고 쓴다(타이머 스레드 포함).

### 13.5 `logs/hardware.log` 형식

기존 형식 유지: `<ISO8601 ms+TZ>\t<source>\t<EVENT>\t<detail>` (UTF-8, 행 단위 기록). `source` 예: `pi-door`, `pi-leds`, mock은 구현 이름. EVENT ∈ `INIT, OPEN, CLOSE, WATCHDOG, REJECT, FAULT, CLEANUP, DENY_BLINK, HOLD_LED`, (v1.5) `STATE, SENSOR` (§15.5). v1.5 `INIT` detail에 `limit=<ch>:BCM<n>:<L|H>` (눌림 레벨)을 추가하고, `CLOSE`/`WATCHDOG` detail 끝에 `cause=… unlocked_for=…s door_open_for=…s`.
single 모드 detail은 v1.2와 동일. **per_locker 모드에서 채널 관련 행(OPEN/CLOSE/WATCHDOG/REJECT/FAULT)의 detail은 `locker=<id|-> pin=BCM<n|-> ` 토큰으로 시작**하고, REJECT는 끝에 `code=<last_reject>`를 붙인다. 감사 도구는 정규식 `^locker=(\S+) pin=BCM(\S+) `로 파싱한다.

```
2026-10-16T09:00:00.001+09:00	pi-door	INIT	per_locker channels=1:BCM24:H,2:BCM25:H,3:BCM16:H,4:BCM26:H green=BCM27 max_concurrent_open=1; outputs OFF
2026-10-16T09:30:12.345+09:00	pi-door	OPEN	locker=2 pin=BCM25 face:u0001:0.6123:3/3:locker=2 signal=ON duration=3.000s
2026-10-16T09:30:15.346+09:00	pi-door	WATCHDOG	locker=2 pin=BCM25 timeout signal=OFF
2026-10-16T09:30:16.010+09:00	pi-door	REJECT	locker=2 pin=BCM25 face:u0001:0.6050:3/3:locker=2 code=cooldown
2026-10-16T09:31:00.000+09:00	pi-door	REJECT	locker=- pin=BCM- manual-test code=no_locker_id
2026-10-16T09:40:00.000+09:00	pi-door	CLEANUP	outputs OFF (all channels)
```

### 13.6 배정 CLI — `scripts/assign_locker.py` (vision)

공통 인자 `[--config] [--set k=v ...]`. 모든 하위 명령은 `load_config` → `FaceDatabase.load(gallery_dir, expected_model_id=None)`(임베더·모델 로드 불필요).

| 명령 | 동작 | 종료 코드 |
|---|---|---|
| `list [--json]` | 등록자별 `person_id, display_name, locker_id, check`(§13.3)와 `free_lockers`. 읽기 전용 | 0 |
| `check` | `list`와 같은 검사. 문제(allowed=False 등록자, 금지된 공유, 미설정 locker)가 있으면 목록 출력 | 문제 없음 0, 있음 6 |
| `set <pid> <locker_id> [--shared] [--yes]` | `check_assignment` 통과 시 `set_person_info(locker_id=...)`. 이미 다른 사물함이면 `old -> new` 표시(1인 1사물함: 교체). 다른 사람이 쓰는 사물함: `allow_shared=false`면 거부, true면 `--shared` 필요 | 성공 0, 패턴/미설정/중복 2, 미등록 pid 6 |
| `clear <pid> [--yes]` | `locker_id: null` 기록 | 성공 0, 미등록 6 |

- 쓰기 명령은 person.json을 임시 파일 + `os.replace`로 원자적 교체(`set_person_info`가 아직 그렇지 않다면 보장할 것).
- 쓰기 성공 시 `logs/locker_assign.log`에 `<ts>\t<os user>\t<set|clear>\t<pid>\t<old>\t<new>` 1행 추가, 표준출력에 "실행 중인 서비스는 재시작해야 반영됨 (sudo systemctl restart facedoor)" 안내.
- 기존 배정을 바꾸는 경우(교체·공유)는 확인 프롬프트, `--yes`로 생략. stdin이 TTY가 아니고 `--yes`도 없으면 거부(종료 코드 2).
- `enroll.py --locker-id`도 `check_assignment`를 사용(§9).
- 관리자 인증은 OS 계정 권한에 의존(§12.1 admin PIN 미구현).

### 13.7 화면 표시 (vision, `display.py`)

시그니처 변경 없음. per_locker 모드에서 우상단을 `LOCKER A02: ON (2.4s)` / `LOCKER: OFF`로 표시(`DoorStatus.open_lockers`, `remaining_sec`). 인식 라벨은 기존 `<pid> [L:<locker_id>]`. 억제 시 1초간 `NO LOCKER` / `LOCKER NOT CONFIGURED` 표시(선택).

### 13.8 평가 영향 (eval, `evaluation/locker_audit.py`)

입력: `logs/events.csv`(33 또는 35컬럼), `logs/hardware.log`, 선택적으로 사후 라벨. 출력: `metrics.json` 최상위 키 `locker`(per_locker 데이터가 있을 때만), `report.html` "사물함 감사" 표. CLI: `scripts/evaluate.py --events ... [--hardware-log logs/hardware.log]`.

| 지표 | 정의 | 합격 기준 |
|---|---|---|
| `wrong_locker_opens` | hardware.log `OPEN`의 `locker=` 토큰 ≠ 그 reason의 `:locker=` 값, 또는 ≠ 대응 `event=open` 행의 `locker_id` (reason 문자열 일치 + 시각 차 ≤ 1 s로 대응). **올바른 사람인데 다른 사물함이 열린 경우** | **0 (중대 오류)** |
| `extra_channel_on` | `open_lockers` 개수 > `max_concurrent_open`인 행 수 + hardware.log OPEN 구간 겹침이 상한을 넘은 횟수 | **0** |
| `unassigned_opens` | 출력이 실제 ON 된 open 중 `locker_id`가 빈칸이거나 설정에 없던 경우 | **0** |
| `watchdog_overruns` | 솔밸브 ON 구간 > `max_unlock_sec + 0.5 s` (limit 모드) / `max_open_sec + 0.5 s` (timer 모드) | **0** |
| `on_while_closed_overtime` (v1.5, limit 모드만) | 솔밸브 ON 이고 문이 닫힘으로 읽힌 구간 > `unlock_timeout_sec + 0.5 s` | **0** |
| `suppressed{no_locker_id, invalid_locker_id, unknown_locker, shared_denied}` | 억제된 open 결정 수(message로 분류) | 보고 |
| `rejected{busy, cooldown, concurrency_limit, fault, invariant, ...}` | 도어 거부 수(hardware.log `code=`) | `invariant`·`fault`는 0이어야 함 |
| `misid_locker_opens` (라벨 필요) | `event=open`, `door_signal=1`, 라벨 `<person_id>` ≠ 결정 person → 다른 사람 사물함이 열림 | 보안 사고. FAR/misID와 함께 보고, 목표 0 |
| `impostor_locker_opens` (라벨 필요) | 라벨 `_impostor`인데 `door_signal=1` | 목표 0 |
| `locker_misroute_rate` (probe 평가) | genuine probe가 MATCH된 사람의 배정 사물함 ≠ 본인 배정 사물함인 비율(갤러리 person.json 기준). 공유 금지면 `misid_rate`와 같음 | 보고 |

- 위 무결성 지표(`wrong_locker_opens`, `extra_channel_on`, `unassigned_opens`, `watchdog_overruns`, limit 모드는 `on_while_closed_overtime` 포함 5개; v1.4 `chatter_reopens`는 close-hold 폐기로 삭제)는 라벨 없이 계산되는 **소프트웨어·배선 무결성 지표**다. 하나라도 0이 아니면 리포트 최상단 빨간 경고, `evaluate.py` 종료 코드 1.
- 이 지표들은 "로그상 라우팅이 맞았는가"만 증명한다. 배선이 바뀌어 BCM24가 실제로는 A03 문에 연결된 경우는 로그로 알 수 없으므로 §13.9 시나리오 30(실물 매칭 확인)이 필수다.
- `label_events.py`는 지정 프레임 범위의 `event=open` 행에도 라벨을 써야 한다(eval이 확인·보장).

### 13.9 QA 테스트 시나리오 (mock 필수, Pi 표시 항목은 실기)

시나리오의 `A01`~`A03`, `Z99`는 형식 검증을 겸한 임의 ID다. 확정 구성 테스트는 `"1"`~`"4"`로 같은 시나리오를 반복하고, 추가로 (a) YAML `locker_id: 1`(정수) → `"1"` 변환, (b) person.json `"locker_id": 1`(숫자) → `invalid_locker_id` 억제, (c) per_locker에서 BCM17이 claim되지 않음(MockFactory 핀 17 미사용)을 확인한다.

설정
1. 기본 `default.yaml`(single) 그대로 → v1.2와 동일 동작. events.csv 35컬럼(35는 빈칸).
2. `mode: per_locker` + `locker_outputs` 비어 있음 → ConfigError(2). 반대(single + locker_outputs 있음) → ConfigError.
3. per_locker인데 `solenoid_pin: 17` 유지 → ConfigError.
4. 사물함 핀이 LED 핀(27)과 같음 / 두 사물함 같은 핀 / 예약 핀 2 / 범위 밖 30 → 각각 ConfigError.
5. locker_id 중복, `A01`/`a01`, 패턴 위반(`A 01`), 17개 채널, `locker_outputs`를 매핑으로 작성, 항목에 모르는 키 → ConfigError.
6. `max_concurrent_open: 0`, `5`, 채널 수 초과 → ConfigError.
7. active_high 사물함 핀 BCM5 → WARNING만, 시작됨.

런타임 (mock_backend `state`와 `gpiozero` 둘 다)
8. alice(A02) 인식 → A02만 ON, 나머지 채널 레벨 전부 OFF(`channel_levels()` / MockFactory 핀 상태), 녹색 ON, `open_sec` 후 자동 OFF.
9. locker 없는 bob 인식 → 어떤 핀도 변화 없음, `event=open`, message `has no locker_id`, door_signal 0.
10. person.json에 설정에 없는 `Z99` → 출력 없음, `not configured` message.
11. person.json에 숫자 `12` / 패턴 위반 값 → `invalid_locker_id`, 출력 없음.
12. 두 사람이 A01 공유 + `allow_shared=false` → 둘 다 억제, 시작 WARNING. `true` → 둘 다 A01 개방.
13. A02 OPEN 중 carol(A03) 인식(`max_concurrent_open=1`) → A03 거부 `concurrency_limit`, A02 영향 없음. `=2`이면 둘 다 ON, 각자 타이머로 OFF.
14. A02 닫힘 직후 alice 재인식 → `cooldown` 거부. 다른 사물함 A03은 즉시 개방 가능.
15. `door.open(reason, duration=99, locker_id="A02")` → `max_open_sec`로 클램프, 10초 절대상한 확인.
16. 메인 루프가 멈춘 상태에서도 워치독 타이머가 채널을 OFF.
17. per_locker에서 `door.open(reason)`을 locker_id 없이 직접 호출 → False, `no_locker_id`. 미설정 id → `unknown_locker`.
18. 불변식: mock에서 A01 출력을 강제로 ON(상태는 IDLE)으로 만든 뒤 A02 open → 전 채널 OFF, FAULT, `invariant`, 이후 모든 open 거부.
19. 채널 3개 중 2번째 claim 실패 주입 → HardwareError(5), 1번째 핀 OFF·해제 확인.
20. `step()`에서 예외 주입 → `door.close()`로 전 채널 OFF.
21. A02 ON 상태에서 SIGINT/SIGTERM/atexit → 전 채널 OFF, 종료 코드 130.
22. `hw_selftest.py --force-off` → (있으면)solenoid·전 사물함·LED OFF 결과 행 출력. 다른 섹션 설정 오류가 있어도 동작.
23. 33컬럼 구버전 events.csv가 있는 상태에서 시작 → 회전 후 35컬럼 헤더. `label_events.py`가 컬럼 보존. `records_from_events`가 두 형식 모두 읽음.
24. hardware.log OPEN/REJECT 행이 `locker=… pin=BCM…` 형식, `locker_audit`가 파싱해 `wrong_locker_opens=0`.
25. `locker_audit` 역검증: 조작한 hardware.log(OPEN locker=A03인데 reason은 locker=A02) → `wrong_locker_opens=1`, 종료 코드 1.

CLI
26. `assign_locker.py set alice A02` 성공·감사 로그 행; 패턴 위반·미설정 locker → 2; 미등록 → 6; bob이 이미 A02 → 2(공유 금지); `allow_shared=true` + `--shared` → 성공.
27. `set alice A03`(기존 A02) → 교체, `old -> new` 출력. `clear alice` → null, 재시작 후 인식 시 억제.
28. `check` 문제 있음 → 6, 없음 → 0. `enroll.py --tag normal --locker-id A02`가 중복이면 촬영 전에 종료 코드 2.

Pi 실기 (사용자 확인 필요)
29. 솔밸브 전원을 분리한 상태로 Pi 전원 투입 → 부팅~앱 시작 동안 채널마다 릴레이 LED가 한 번도 켜지지 않는지 육안 확인.
30. `hw_selftest.py --all-lockers --allow-solenoid` → 한 번에 한 채널만 동작, 사물함 번호 라벨과 실제 열린 문이 일치하는지 사물함마다 확인 → 결과를 `deploy/WIRING.md` 표에 기록. **이 확인 전에는 운영 금지.**

### 13.10 배선 안전 (상세는 `deploy/WIRING.md`, hw)

- 사물함 N개 = 릴레이(또는 로직레벨 MOSFET) N채널. GPIO로 솔밸브·릴레이 코일을 직접 구동하지 않는다. 솔밸브마다 플라이백 다이오드.
- 솔밸브 전원은 외부 전원. 용량 ≥ `max_concurrent_open × 솔밸브 정격 전류 × 1.5`(돌입 여유) + 릴레이 코일 전류 합. 기본 동시 1개 기준이며 **동시 개방 상한을 올리면 전원을 다시 산정**한다.
- 다채널 릴레이 보드는 active-low인 경우가 많다 → 채널별 `active_high: false`. 보드의 JD-VCC(코일 전원)를 Pi 5V/3.3V 레일에서 가져오지 않는다. 비절연 구성은 GND 공통.
- 사물함 번호 ↔ 채널 ↔ BCM ↔ 물리 핀 표를 `WIRING.md`와 사물함 본체 라벨로 유지. 시나리오 30 확인 전에는 운영 금지.
- 정전 시 잠김(fail-secure)/열림(fail-safe)은 사물함별 솔밸브 종류로 결정 — 사용자 결정.

### 13.11 확정 사항과 미결 사항

| 항목 | 상태 | 값 / 제안 기본값 | 비고 |
|---|---|---|---|
| 사물함 개수 | **확정** | 4 (코드 상한 16) | 상한 근거: 헤더 GPIO 28개 − 예약 6 − LED 3 − 여유 |
| locker_id 표기 | **확정** | `"1"`~`"4"` | 기존 패턴 `^[A-Za-z0-9_-]{1,16}$` 그대로 통과 |
| 핀 배정 | 제안(실배선 시 확인) | 솔밸브: 1=BCM24(물리18), 2=BCM25(물리22), 3=BCM16(물리36), 4=BCM26(물리37). (v1.5) 리밋 입력: 1=BCM5(29), 2=BCM6(31), 3=BCM12(32), 4=BCM13(33) | §13.2·§15.2 근거. 17/27/22/23과 충돌 없음 |
| 릴레이 극성 | 제안(보드 확인 필요) | `active_high: true`(active-high 드라이버 권장) | active-low 보드면 채널별 false + 3.3V 완전 OFF 실측 |
| 기존 BCM17 단일 출력 | **확정(설계)** | single=그대로, per_locker=미사용(`solenoid_pin: null`) | §13.2 하위호환 |
| 동시 개방 | 미결 | 1 | 2 이상 필요 시 전원 재산정 |
| 공유 허용 | 미결 | false | 가족·팀 공유 필요 시 true + `--shared` |
| 사물함별 표시 LED | 미결 | 없음(공용 녹색 1개) | 필요하면 `locker_outputs[].led_pin`을 v1.4에서 추가(핀 예산 2배) |
| 정문(단일 솔밸브) + 사물함 혼합 | 미결 | 미지원 | 필요하면 별도 계약(v1.4) |
| 매핑 즉시 반영 | 미결 | 재시작 필요 | 즉시 해제가 필요하면 person.json mtime 감시를 v1.4에서 검토 |
| 솔밸브 사양(전압/전류), 정전 시 잠김 방식 | 미결 | 미정 | 전원 용량·다이오드 선정에 필요 |

---

## 14. 한글 관리 화면 (`scripts/admin_gui.py`) — v1.3 계약, **미구현** (vision)

### 14.1 원칙

1. **실행 즉시 등록이 시작되지 않는다.** 시작 화면은 PIN 입력 → 메인 메뉴. 카메라는 사용자가 [등록 시작]/[추가 촬영 시작] 버튼을 누른 순간에만 열고, 촬영이 끝나거나 취소되면 즉시 닫는다.
2. 모든 메뉴·버튼·안내·오류 문구는 **한글**이며 `facedoor/texts.py`(v1.4: GUI·인식 화면·CLI 공용, tkinter import 없음) 한 곳에 모은다(코드에 문자열 산재 금지). 콘솔/파일 로그는 기존처럼 영어 가능.
3. 화면 로직(`admin_app.py`, tkinter)과 업무 로직(`gui/service.py`, tkinter import 금지)을 분리한다. `AdminService`는 디스플레이 없이 pytest로 검증한다.
4. 갤러리·사물함 변경 규칙은 CLI와 **같은 함수**를 쓴다(`Enroller`, `FaceDatabase`, `lockers.check_assignment`). GUI 전용 우회 경로 금지.
5. 관리 화면은 문을 열지 않는다. `facedoor.hardware`를 import하지 않는다.
6. 기술: **tkinter**(표준 라이브러리; Windows 기본 포함, Pi/Ubuntu는 `sudo apt install python3-tk fonts-nanum`) + OpenCV 카메라. PIL/Pillow 미사용.

### 14.2 화면과 기능

메인 메뉴 버튼: **[사용자 등록] [사용자 목록·사물함 배정] [인식 보강(추가 촬영)] [사용자 제거] [종료]**. 상단 상태줄: 현재 모드(`단일 출력` / `사물함 4개`), 등록 인원, 카메라 사용 가능 여부(§14.6).

| # | 화면 | 구성 | 동작 |
|---|---|---|---|
| ① | 사용자 등록 | 이름 입력칸(한글 허용), 사용자 ID 칸(자동 제안 `u0001`, "고급"에서만 수정), 사물함 선택(`배정 안 함`/1~4, 이미 배정된 번호는 "(사용 중: 홍길동)" 표시), [등록 시작], [취소], 미리보기, 포즈 안내·진행 막대 | [등록 시작]을 눌러야 검증(§14.7) → 카메라 잠금(§14.6) → 열기 → 포즈별 안내("정면을 보세요" → "고개를 왼쪽으로 살짝 돌리세요" …) 및 `pose_countdown_sec` 카운트다운 → `normal` 태그 캡처. 진행 표시 "왼쪽 (2/5) · 저장 4/10장". 거부 이유는 한글 안내로 실시간 표시. 완료 시 저장 수 < `min_register_samples`면 **롤백**(`remove_person`) 후 실패 안내. 성공 시 display_name 기록, 사물함 선택이 있으면 `check_assignment` 통과 시 배정 |
| ② | 사용자 제거 | 사용자 목록(이름·ID·사물함·샘플 수), [선택한 사용자 제거] | 확인 대화상자(§14.8 문구, 이름·샘플 수·사물함 번호 명시, 기본 버튼=취소) → `FaceDatabase.remove_person`(샘플·crop·person.json 폴더 삭제 → 사물함 배정도 함께 사라짐) → `locker_assign.log`에 `clear`(사유 remove) 행, `logs/admin.log`에 행 → 재시작 안내 |
| ③ | 사용자 목록·사물함 배정 | 표 2개: (가) 사용자 표: 이름, ID, 사물함, 총 샘플, 태그별 수, 자체점수, 보강 필요 표시 / (나) 사물함 표: 사물함 1~4, 배정자, 상태(비어 있음/배정됨/공유 경고/설정에 없음). [배정·변경] [배정 해제] [새로고침] | 배정은 콤보박스로 1~4 선택 → `AdminService.assign_locker`. 중복 배정이면 경고: "사물함 2번은 이미 '김철수'님에게 배정되어 있습니다." + 선택지 [김철수님 배정을 해제하고 옮기기] [취소] (`allow_shared=true`면 [함께 사용(공유)] 추가). `check`에서 문제가 있는 행은 빨간색 |
| ④ | 인식 보강(추가 촬영) | 사용자 선택, 조건 라디오: 일반 / 마스크 / 모자 / 안경 / 안경 없음 / 목도리 / 저조도 / 역광 / 기타(메모 입력칸), 선택한 사용자의 현재 샘플 수·태그별 수·자체점수·보강 권장 사유, [추가 촬영 시작] | ①과 같은 캡처 흐름, `new_user=False`(취소·부족해도 이미 저장된 샘플 유지, 롤백 없음). 태그 매핑: 일반 `normal`, 마스크 `mask`, 모자 `hat`, 안경 `glasses`, 안경 없음 `noglasses`, 목도리 `scarf`, 저조도 `lowlight`, 역광 `backlight`, 기타 `other`(메모는 샘플 메타 `note`) |

보강 권장(표에 "보강 필요" 글자로 표시) 판정 — `UserSummary.weak_reasons`(한글):
- 총 샘플 < `gui.weak_min_samples` → "샘플이 적습니다 (6/10장)"
- `gui.weak_required_tags` 중 없는 태그 → "일반 촬영이 없습니다"
- `self_score_mean` < `gui.weak_self_score` → "촬영 사진끼리 일관성이 낮습니다 (0.38)"
- 이상치 샘플 ≥ 1 → "이상치 샘플 N장"
- (선택, eval 제공 시) 최근 리포트 `metrics.json.per_person[pid].frr` > `gui.weak_frr` → "최근 평가에서 인식 실패율이 높습니다 (25%)"

저조도/역광 촬영은 **기존 품질 기준을 낮추지 않는다**(보안). 너무 어두운 프레임은 거부되며 화면에 "너무 어둡습니다 — 조명을 조금 밝혀 주세요"라고 안내한다. 그 결과 "어둡지만 품질 하한(밝기 ≥ 50) 이상"인 샘플만 쌓인다.

### 14.3 업무 로직 API — `facedoor/gui/service.py`

```python
@dataclass(slots=True)
class UserSummary:
    person_id: str; display_name: str; locker_id: str | None
    n_samples: int; tag_counts: dict[str, int]
    self_score_mean: float | None; n_outliers: int
    weak_reasons: list[str]                    # 한글. 비어 있으면 보강 불필요

@dataclass(slots=True)
class LockerRow:
    locker_id: str; pin: int | None            # single 모드는 None
    owners: list[str]                          # person_id
    status: str                                # "free" | "assigned" | "shared" | "shared_denied"

class AdminService:
    def __init__(self, cfg: AppConfig) -> None
        """FaceDatabase 로드. 검출기·임베더는 첫 캡처 때 지연 로드(모델 없음 → ModelError → 한글 안내).
        임베더 model_id 와 갤러리 _meta.json 불일치 → GalleryError → 한글 안내(재임베딩 CLI 안내), 캡처 불가·목록은 가능."""
    def users(self) -> list[UserSummary]
    def locker_choices(self) -> list[str]       # per_locker: locker_outputs id / single: gui.locker_choices
    def locker_rows(self) -> list[LockerRow]
    def suggest_person_id(self) -> str          # "u%04d", 기존 u#### 최대값+1
    def validate_new_user(self, display_name: str, person_id: str) -> tuple[list[str], list[str]]
        """(오류, 경고) 한글 메시지. 오류가 있으면 캡처 금지. 경고(같은 이름 존재 등)는 확인 후 진행."""
    def camera_status(self) -> tuple[bool, str]  # (사용 가능, 한글 사유) — §14.6
    def capture(self, person_id: str, tag: str, *, new_user: bool, display_name: str | None = None,
                locker_id: str | None = None, note: str | None = None,
                progress: Callable[[EnrollProgress], None] | None = None,
                cancel: threading.Event | None = None) -> EnrollReport
        """워커 스레드에서 호출. camera.lock 획득 → create_camera → Enroller.enroll_live → 카메라 닫기 → 잠금 해제(finally).
        new_user=True 이고 saved < gui.min_register_samples 또는 취소 → remove_person 롤백, report.rolled_back=True.
        성공 후 display_name·locker_id 기록(locker 검증 실패 시 등록은 유지, 결과에 한글 경고)."""
    def remove_user(self, person_id: str) -> None
    def assign_locker(self, person_id: str, locker_id: str, *, take_over: bool = False,
                      shared: bool = False) -> list[str]
        """빈 리스트=성공. take_over=True 면 기존 배정자의 locker_id 를 null 로 바꾼 뒤 배정(두 기록 모두 locker_assign.log)."""
    def clear_locker(self, person_id: str) -> None
```

`Enroller` 확장 (§3.12, 하위호환 — 키워드 인자 추가만):

```python
@dataclass(slots=True)
class EnrollProgress:
    phase: str                     # "capturing" | "pose_change" | "done" | "cancelled"
    pose: str | None; pose_index: int; n_poses: int
    saved: int; target: int
    last_reject: str | None        # HoldReason 값 | "duplicate" | "no_face" | "multi_face"
    frame: np.ndarray | None       # 미리보기용 BGR (복사본). 얼굴 박스는 GUI 가 cv2.rectangle 로 그림(글자 없음)
    bbox: tuple[int, int, int, int] | None

def enroll_live(self, camera, person_id, tag, display=None, poses=None, samples_per_pose=None, *,
                progress: Callable[[EnrollProgress], None] | None = None,
                cancel: threading.Event | None = None,
                pose_countdown_sec: float = 0.0,
                note: str | None = None) -> EnrollReport
# EnrollReport 추가 필드: cancelled: bool = False, rolled_back: bool = False
# progress 는 워커 스레드에서 호출된다. 콜백 안에서 tkinter 를 만지지 말 것(큐에 넣기만).
```

### 14.4 미리보기와 한글 표시 (PIL 없이)

- `cv2.putText`는 한글을 그리지 못하므로 **영상 위에 글자를 그리지 않는다.** 안내·이름·진행 상황은 미리보기 옆/아래의 tkinter `Label`(한글 폰트)로 표시하고, 영상에는 얼굴 사각형(색만: 초록=저장됨, 노랑=대기, 빨강=거부)만 그린다.
- 프레임 → tkinter: 워커 스레드가 `gui.preview_width`로 축소 후 `cv2.imencode(".ppm", bgr)`(OpenCV가 RGB 순서로 기록) → 바이트를 `queue.Queue(maxsize=1)`에 넣는다(가득 차면 오래된 것 버림). 메인 스레드가 `root.after(1000 // preview_fps)`로 꺼내 `tk.PhotoImage(data=buf, format="PPM")`로 만들어 `Label`에 설정하고 참조를 유지한다. Tk 버전 문제로 PPM 바이너리 `data`가 실패하면 `cv2.imencode(".png")` + base64 + `format="png"`(Tk ≥ 8.6)로 대체(구현 시 Windows·Pi 양쪽 확인).
- tkinter 위젯은 **메인 스레드에서만** 만진다. 카메라 읽기·검출·임베딩은 워커 스레드 1개.
- 한글 폰트: `display.font_families`(v1.4: GUI·인식 화면 공용) 중 `tkinter.font.families()`에 있는 첫 번째(Windows `Malgun Gothic`, Ubuntu `NanumGothic` 또는 `Noto Sans CJK KR`). 하나도 없으면 시작 시 경고 대화상자 + 콘솔에 `sudo apt install fonts-nanum` 안내.
- 창을 닫으면: cancel 설정 → 워커 join(최대 3초) → 카메라 닫기 → 잠금 해제 → 종료.

### 14.5 '모자(hat)' 태그와 임계값 그룹 — 결정: **global 그룹**

| 선택지 | 효과 | 판단 |
|---|---|---|
| hat ∈ occluded (임계값 0.55, 더 엄격) | 모자 쓴 본인의 FRR 상승. occluded 보정 데이터(마스크·목도리 = 얼굴 **아래쪽** 가림)에 성격이 다른 모자 샘플이 섞여 보정이 왜곡됨 | 채택 안 함 |
| **hat ∈ global (0.50)** | 모자는 이마·눈썹 일부만 가리고 눈·코·입이 남아 임베딩 특징 손실이 마스크보다 작다. 챙이 눈을 가릴 정도면 랜드마크·품질 게이트에서 HOLD | **채택** |

- 근거는 경험적 가정이므로 `evaluation.conditions`에 `hat`을 추가해 조건별 FAR/FRR을 측정하고, 모자 조건 FAR이 global 평균보다 유의하게 높으면 `recognition.occluded_tags`에 `hat`을 추가한다(설정만으로 전환 가능, 코드 변경 불필요).
- `other`(기타)도 global. 내용이 불명확하므로 평가 조건으로는 쓰지 않는다.
- `enroll.tags`에 `hat`, `other` 추가(이 버전 `default.yaml`에 반영 — 기존 코드로도 동작). 합성 조건 `syn_hat`은 만들지 않는다(실촬영만).
- 마스크+모자처럼 겹치는 경우는 **더 엄격한 쪽 태그**(mask)로 촬영하도록 GUI에 안내한다.

### 14.6 카메라 충돌과 상호 배제 — `facedoor/runlock.py`

웹캠은 한 프로세스만 안정적으로 쓸 수 있다(Windows DirectShow는 배타적, V4L2도 동시 사용 보장 없음). 그래서 인식 프로그램과 관리 화면 캡처는 동시에 실행되지 않게 한다.

```python
class RunLock:
    def __init__(self, path: Path) -> None
    def acquire(self) -> bool          # 비차단. OS 잠금(Windows msvcrt.locking, POSIX fcntl.flock) + pid/프로그램명 기록
    def release(self) -> None          # 멱등
    def __enter__/__exit__
def lock_holder(path: Path) -> str | None   # 잠금 보유 중이면 "run.py pid=1234" 같은 설명, 아니면 None
CAMERA_LOCK = "<logs_dir>/locks/camera.lock"
GALLERY_LOCK = "<logs_dir>/locks/gallery.lock"
```

- `run.py`(앱)는 시작 시 `camera.lock`을 획득하고 종료 시 해제(프로세스가 죽으면 OS가 자동 해제 → 잔존 잠금 없음). 획득 실패 → `CameraError`(종료 코드 3) "camera is in use by admin GUI".
- 관리 화면은 캡처 직전 `camera.lock` 획득 시도. 실패 → 한글: "카메라를 사용할 수 없습니다. 얼굴인식 프로그램이 실행 중입니다({holder}). 먼저 종료하세요. (라즈베리파이: sudo systemctl stop facedoor)". 메인 메뉴 상태줄에도 표시하고 캡처 버튼 비활성화(목록·배정·제거는 가능).
- 갤러리를 쓰는 모든 주체(GUI 캡처·제거·배정, `enroll.py`, `assign_locker.py`)는 쓰는 동안 `gallery.lock`을 잡는다. 실패 → 한글/CLI 오류 "다른 관리 작업이 진행 중입니다".
- 인식 프로그램이 실행 중일 때의 제거·배정 변경은 **재시작 전까지 반영되지 않는다**(§13.1). 특히 제거한 사람이 재시작 전까지 계속 인식될 수 있으므로 제거 완료 대화상자에 굵게 안내한다.

### 14.7 이름·ID 검증

- **권장안 채택: `person_id`(ASCII 슬러그) + `display_name`(한글) 분리.** 폴더명·`cv2.putText` 화면 라벨·events.csv·Windows 한글 경로 문제(ARCHITECTURE §7) 때문에 `person_id`는 계속 ASCII(`enroll.person_id_pattern`)로 유지한다. 한글 이름을 로마자로 자동 변환하지 않고 일련번호 `u0001`을 자동 제안한다(변환 규칙 혼란·동명이인 회피).
- `display_name`: 앞뒤 공백 제거 후 1~20자, 한글·영문·숫자·공백·`.`·`-`·`_` 허용, 제어문자와 `/ \ : * ? " < > |` 금지. 같은 display_name이 이미 있으면 **경고**(동명이인 허용, "이미 '홍길동'(u0003)이 있습니다. 계속할까요?").
- `person_id`: 패턴 불일치·이미 존재 → 오류.
- (v1.4) 인식 화면은 `TkDisplay`(§17)에서 한글 `display_name`을 표시한다. ASCII 폴백(`OpenCVDisplay`)에서만 `person_id [L:2]`.

### 14.8 한글 문구 (`facedoor/texts.py` 필수 키와 예)

| 키 | 문구 |
|---|---|
| `PIN_SETUP` | 처음 실행입니다. 관리자 PIN(숫자 {min}자리 이상)을 설정하세요. |
| `PIN_PROMPT` / `PIN_WRONG` / `PIN_LOCKED` | 관리자 PIN을 입력하세요. / PIN이 올바르지 않습니다. (남은 횟수 {n}회) / 입력 횟수를 초과했습니다. {min}분 후 다시 시도하세요. |
| `CAMERA_BUSY` | §14.6 문구 |
| `CAMERA_FAIL` | 카메라를 열 수 없습니다. 연결 상태와 설정(camera.source)을 확인하세요. |
| `MODEL_MISSING` | 얼굴 인식 모델 파일이 없습니다. 먼저 'python scripts/download_models.py'를 실행하세요. |
| `GALLERY_MISMATCH` | 저장된 얼굴 데이터가 현재 인식 모델과 맞지 않습니다. 'python scripts/enroll.py --reembed'로 변환하세요. |
| `NAME_EMPTY` / `NAME_INVALID` / `ID_EXISTS` | 이름을 입력하세요. / 이름에 사용할 수 없는 문자가 있습니다. / 이미 사용 중인 사용자 ID입니다: {pid} |
| `POSE_front/left/right/up/down` | 정면을 보세요 / 고개를 왼쪽으로 살짝 돌리세요 / 고개를 오른쪽으로 살짝 돌리세요 / 턱을 살짝 드세요 / 턱을 살짝 내리세요 |
| `REJ_too_small/too_dark/too_bright/blur/pose/border/low_det_score/multi_face/no_face/duplicate` | 카메라에 조금 더 가까이 오세요 / 너무 어둡습니다 — 조명을 조금 밝혀 주세요 / 너무 밝거나 역광입니다 / 움직이지 말고 잠시 멈춰 주세요 / 안내된 방향으로 고개를 맞춰 주세요 / 얼굴을 화면 가운데로 옮겨 주세요 / 얼굴이 잘 보이지 않습니다 / 한 사람만 화면에 나오게 해 주세요 / 얼굴이 보이지 않습니다 / 같은 자세가 반복됩니다. 조금씩 움직여 주세요 |
| `REGISTER_OK` / `REGISTER_FAIL` | '{name}'님 등록이 완료되었습니다 ({saved}장). / 얼굴이 충분히 촬영되지 않아 등록을 취소했습니다 ({saved}/{need}장). 밝은 곳에서 정면을 보고 다시 시도하세요. |
| `REMOVE_CONFIRM` | '{name}'({pid}) 사용자를 삭제할까요?\n얼굴 데이터 {n}장과 사물함 {locker} 배정이 함께 삭제되며 되돌릴 수 없습니다. |
| `LOCKER_TAKEN` | 사물함 {locker}번은 이미 '{owner}'님에게 배정되어 있습니다. |
| `LOCKER_SINGLE_MODE` | 현재 단일 출력 모드입니다. 사물함 배정은 저장되지만 문 열기에는 사용되지 않습니다. |
| `RESTART_NOTICE` | 변경 내용은 얼굴인식 프로그램을 다시 시작해야 반영됩니다. (라즈베리파이: sudo systemctl restart facedoor) |
| `ERROR_GENERIC` | 오류가 발생했습니다: {detail}\n자세한 내용은 logs/facedoor.log 를 확인하세요. |

### 14.9 설정 — 선택 섹션 `gui` (v1.3, 구현 후 `default.yaml` 주석 해제)

```python
@dataclass(slots=True)
class GuiSection:
    require_pin: bool = True              # false 는 개발용. 경고 로그
    preview_width: int = 480
    preview_fps: int = 15
    pose_countdown_sec: float = 2.0
    min_register_samples: int = 5         # 신규 등록 시 이보다 적으면 롤백
    weak_min_samples: int = 10
    weak_self_score: float = 0.45         # SFace 기준 제안값. ArcFace 는 평가 후 조정
    weak_required_tags: list[str] = ["normal"]
    weak_frr: float = 0.2                 # metrics.json per_person 이 있을 때만 사용
    locker_choices: list[str] = ["1", "2", "3", "4"]   # single 모드에서만 사용
    # (v1.4) font_families/font_size 는 display 섹션으로 이동 (§17.2)
```

`config._OPTIONAL_SECTIONS`에 `gui` 추가. 검증: `1 <= min_register_samples <= len(enroll.poses) * enroll.samples_per_pose`, `weak_required_tags ⊆ enroll.tags`, `locker_choices` 각 항목 `lockers.locker_id_pattern` fullmatch, 중복 금지.

### 14.10 QA 시나리오 (관리 화면)

서비스 계층(자동, 디스플레이·카메라 없이 `FileCamera`/가짜 검출기로)
G1. `AdminService.capture(new_user=True)`에서 저장 < `min_register_samples` → 폴더 없음(롤백), `rolled_back=True`.
G2. 캡처 중 `cancel.set()` → `cancelled=True`, 신규면 롤백, 보강이면 저장된 샘플 유지.
G3. `remove_user` → 폴더 삭제, `LockerMap`에서 사라짐, `locker_assign.log`·`admin.log` 행.
G4. `assign_locker`: 빈 사물함 성공 / 타인 사물함 → 한글 오류 / `take_over=True` → 이전 배정자 null + 로그 2행 / 미등록 → 오류.
G5. `validate_new_user`: 빈 이름, `홍/길동`, 21자 → 오류; 동명이인 → 경고; ID 중복 → 오류. `suggest_person_id`가 `u0007` 다음 `u0008`.
G6. `users()` 보강 사유: 샘플 6장 → "샘플이 적습니다", normal 없음, 자체점수 낮음.
G7. `camera.lock`을 다른 프로세스가 보유 → `camera_status()` False + 한글 사유, `capture()` 호출 시 카메라를 열지 않음. 반대로 GUI 캡처 중 `run.py` 시작 → 종료 코드 3.
G8. 모델 파일 없음 → 한글 `MODEL_MISSING`, 목록 화면은 동작.
G9. `texts.py`의 모든 값이 한글 포함(영문 전용 사용자 문구 없음) — 정적 테스트.
G10. `adminpin`: 설정·검증·3회 실패 잠금·재시작 후에도 잠금 유지·손상 파일 진입 거부·숫자 외/짧은 PIN 거부.

수동(Windows + Pi 각각)
G11. 실행 직후 카메라 LED가 켜지지 않음(등록 버튼 전까지).
G12. 미리보기 15fps 내외, 한글 글자 깨짐 없음(Pi는 fonts-nanum 설치 전/후 비교).
G13. 등록 → 목록에 표시 → 사물함 2번 배정 → 인식 프로그램 재시작 → 그 사람 인식 시 2번만 열림(mock은 hardware.log, Pi는 실물).
G14. 사용자 제거 후 재시작 → 인식되지 않음, 사물함 표에서 2번이 "비어 있음".
G15. 창 닫기(촬영 중) → 카메라 해제, 잠금 해제(곧바로 run.py 실행 가능).

### 14.11 미결 (관리 화면)

| 항목 | 제안 기본값 |
|---|---|
| 관리 화면 PIN 사용 | 사용(`gui.require_pin: true`), 6자리 권장(최소 4) |
| 인식 화면 한글 이름 표시 | (v1.4 결정) `TkDisplay`로 표시 (§17) |
| 관리 화면에서 서비스 중지/재시작 버튼 | 안 함(sudo 필요, 권한 상승 위험). 안내 문구만 |
| 원격(SSH) 사용 | 범위 밖. Pi에 모니터·키보드 연결 전제 |

---

## 15. 리밋센서 기반 잠금 제어 — v1.5 계약, **미구현** (hw + vision 코어)

> v1.4의 close-hold(얼굴 유지 신호로 잠금 시점 결정)는 **폐기·대체**되었다(구현된 적 없음). 이유: 문이 실제로 닫혔는지는 얼굴이 아니라 **리밋센서**가 알려 준다. 얼굴은 "해제"에만 쓰고, "유지/재잠금"은 문 상태로만 결정한다. 설계 배경은 ARCHITECTURE §4·§6.4.

사용자 요구: 각 도어에 리밋센서(눌림 = 문 닫힘, 안 눌림 = 문 열림). 인식으로 해제 → `unlock_timeout_sec` 안에 문이 안 열리면 자동 재잠금 → 문이 열리면 닫힐 때까지 해제 유지 → 리밋이 `close_confirm_sec` 연속 눌려야 잠금 → 절대상한 워치독은 유지하되 초과 시 **잠금** → 센서 읽기 실패/미연결은 fail-closed.

### 15.1 센서 의미와 배선

- 논리 값: `door_closed = (리밋 눌림)`. 눌림=True=닫힘, 해제=False=열림, 읽기 실패=None(불명). 불명은 "열림"도 "닫힘"도 아니며 **어느 쪽 판단에도 쓰지 않는다**.
- 극성은 설정(`limit_pull_up`, `limit_active_low`). 기본 = 내부 풀업 + 눌림 시 GND로 단락(`active_low`, 눌림=LOW). 배선은 BCM 입력 ↔ 리밋 COM, 리밋 단자 ↔ GND (3.3V 입력만, 5V 금지).
- **권장 배선(fail-safe, 사용자 확인 필요)**: 리밋의 **NC 접점**을 쓰고 `limit_active_low: false`(눌림=HIGH)로 설정한다. 그러면 선이 빠지거나 끊어져도 핀이 풀업으로 HIGH = "눌림 = 닫힘"으로 읽혀 **잠금 쪽**으로 오판한다. 반대로 기본(NO 접점 + 눌림=LOW)에서는 선이 빠지면 "해제 = 열림"으로 읽혀 위험한 쪽이므로, 아래 §15.3의 `door_open` 거부 + 워치독으로만 막는다(해제 요청 시점에 열림으로 읽히면 해제를 거부하므로 **시작부터 미연결이면 절대 해제되지 않는다**. 해제 후 도중에 빠지면 `max_unlock_sec`까지 유지될 수 있음).
- 하드웨어 디바운스: `limit_poll_sec`(기본 20 ms) 주기로 샘플링하고 소프트웨어 디바운스는 §15.3의 `open_confirm_sec`/`close_confirm_sec`로 한다. 접점 바운스(수 ms)는 이 안에서 흡수.

### 15.2 설정 스키마와 검증

| 키 | 기본(default.yaml, 구현 후) | 코드 기본 | 범위·검증 |
|---|---|---|---|
| `hardware.close_mode` | `limit` | `timer`(키 없는 구 설정 호환) | `{limit, timer}`. `hold` → ConfigError("§15로 대체됨"). `timer` = v1.3 동작(고정 `open_sec`) |
| `hardware.limit_switch_pin` | `5` (single 모드) | `None` | BCM. per_locker 에서는 `null` 필수(채널별 `limit_pin`) |
| `hardware.locker_outputs[].limit_pin` | 아래 핀 표 | `None` | per_locker + limit 모드 + 실제 pi 이면 **필수**. mock `state` 백엔드는 생략 가능(시뮬레이션) |
| `hardware.limit_pull_up` | `true` | `True` | bool |
| `hardware.limit_active_low` | `true` | `True` | bool. true=눌림이 LOW. §15.1 권장 배선(NC)은 `false` |
| `hardware.limit_poll_sec` | `0.02` | 0.02 | `0.005 <= v <= 0.1` |
| `hardware.open_confirm_sec` | `0.1` | 0.1 | `0.0 <= v <= 1.0`. 리밋 **해제**가 이만큼 연속이면 "문 열림" 확정 |
| `hardware.close_confirm_sec` | `2.0` | 2.0 | `0.5 <= v <= 10.0`. 리밋 **눌림**이 이만큼 연속이면 "문 닫힘" 확정 → 재잠금 (사용자 문구 "close_confirm_sec") |
| `hardware.unlock_timeout_sec` | `10.0` | 10.0 | `1.0 <= v <= 60.0`. 해제 후 문이 안 열리면 재잠금 (사용자 문구 "grace") |
| `hardware.max_unlock_sec` | `30.0` | 30.0 | **해제 시점부터** 솔밸브 ON 절대상한. `unlock_timeout_sec + close_confirm_sec + 1.0 <= v <= ABSOLUTE_MAX_UNLOCK_SEC(120)` |
| `hardware.sensor_fault_sec` | `1.0` | 1.0 | `0.2 <= v <= 5.0`. 읽기 실패(None) 연속 시간이 이만큼이면 센서 오류 |
| `hardware.cooldown_sec` | `2.0` | 2.0 | 재잠금 후 재해제 최소 간격(기존) |
| `hardware.open_sec`, `max_open_sec` | 유지 | 유지 | **timer 모드 전용**. limit 모드에서 `open_sec`는 무시(설정돼 있으면 INFO), `max_open_sec`는 무시. 기존 `open_sec <= max_open_sec <= 10.0` 검증도 timer 모드에서만 적용 |

추가 ConfigError: limit 모드인데 pi(또는 auto→pi)에서 리밋 핀 누락 / 리밋 핀이 다른 모든 핀(솔밸브·사물함·LED·버튼·다른 리밋)과 중복 / 리밋 핀이 `RESERVED_OUTPUT_PINS` / 범위 밖. WARNING: `decision.rearm_sec < cooldown_sec`; `max_unlock_sec > 60`(솔밸브 연속 통전 발열 — 솔밸브 정격 듀티 확인); `limit_pull_up=false`(외부 풀업/풀다운 저항 필요); `limit_active_low=true`(NO 접점 배선이면 §15.1의 선 빠짐 위험 안내).

**핀 매핑 제안** (기존 출력/LED와 겹치지 않음, 입력 전용 사용, 모두 부팅 시 풀업 기본값이라 `pull_up=true`와 일치. 실배선에서 확인):

| 사물함 | 솔밸브(출력) BCM(물리) | 리밋(입력) BCM(물리) | 리밋 GND 근처 물리 핀 |
|---|---|---|---|
| 1 | 24 (18) | **5 (29)** | 30 |
| 2 | 25 (22) | **6 (31)** | 30 |
| 3 | 16 (36) | **12 (32)** | 34 |
| 4 | 26 (37) | **13 (33)** | 34 |
| single(정문 1개) | 17 (11) | **5 (29)** | 30 |

LED 27/22/23, 수동 버튼(`exit_button_pin`, 미사용)과 충돌 없음. 여유 입력 후보: BCM 19(35), 20(38), 21(40).

### 15.3 상태기계 (사물함(채널)별 독립, 전부 하드웨어 스레드에서 구동)

감시 스레드 1개가 `limit_poll_sec`마다 `poll_once()`를 호출한다(채널 전부 샘플링). 메인 루프(`app.step()`)·카메라·디스플레이가 멈춰도 전이는 계속된다. 상태 읽기/쓰기는 컨트롤러 `RLock` 아래.

```
                         open() 수락 (조건 §아래)                          리밋 해제가 open_confirm_sec 연속
LOCKED_CLOSED ───────────────────────────────▶ UNLOCKED_WAITING ───────────────────────────────▶ OPEN
   ▲  ▲   (솔밸브 OFF, 눌림)                  (솔밸브 ON, 눌림)                                    │ ▲ (솔밸브 ON, 해제)
   │  │                                          │ unlock_timeout_sec 경과                          │ │ 리밋 다시 해제
   │  │                                          ▼ [cause=unlock_timeout]                          ▼ │
   │  │ cooldown_sec 경과                     COOLDOWN ◀──── 눌림이 close_confirm_sec 연속 ── CLOSING_CONFIRM
   │  └────────────────────────────────────────  ▲          [cause=close_confirmed]              (솔밸브 ON, 눌림 확인 중)
   │                                             │
   │   max_unlock_sec 경과(ON 상태 어디서든) ─────┘ [cause=watchdog]  (그때 리밋이 해제 상태면 COOLDOWN 후 LOCKED_OPEN)
   │   센서 읽기 실패 sensor_fault_sec 지속 ───▶ SENSOR_FAULT [cause=sensor_fault, 즉시 OFF]
   │
LOCKED_CLOSED ──(솔밸브 OFF인데 해제가 open_confirm_sec 연속)──▶ LOCKED_OPEN   (event forced_open, 솔밸브는 계속 OFF)
LOCKED_OPEN ──(눌림이 close_confirm_sec 연속)──▶ LOCKED_CLOSED
SENSOR_FAULT ──(유효 샘플이 close_confirm_sec 연속 정상으로 읽힘)──▶ LOCKED_CLOSED(눌림) 또는 LOCKED_OPEN(해제)
어떤 상태든 cleanup()/예외/SIGINT/SIGTERM/atexit/컨트롤러 FAULT ──▶ 솔밸브 즉시 OFF [cause=shutdown|fault]
```

솔밸브 ON 상태는 정확히 `UNLOCKED_WAITING`, `OPEN`, `CLOSING_CONFIRM` 셋뿐이다(**불변식**: 그 외 상태에서 출력이 ON으로 읽히면 §13.4 규칙 9·11과 같이 전 채널 OFF + FAULT).

**해제 요청 `open(reason, locker_id=L)` 수락 조건** (기존 §13.4 순서에 아래 검사를 끼운다. 모두 만족해야 True, 어느 하나라도 아니면 거부 + `last_reject`):

| 순서 | 조건 | `last_reject` |
|---|---|---|
| a | cleanup 이후 / 컨트롤러 FAULT | `shutdown` / `fault` |
| b | (구현 순서) 채널 `UNLOCKED_WAITING`/`OPEN`/`CLOSING_CONFIRM` (이미 해제 중, **연장 없음**). 센서 상태보다 먼저 검사하므로 ON 상태에서 센서가 None/열림이어도 거부 코드는 `busy` | `busy` |
| c | 채널 `SENSOR_FAULT` | `sensor_fault` |
| d | **요청 시점에 리밋을 동기 재읽기**해서 읽기 실패(None) | `sensor_fault` (이때 상태는 바꾸지 않고 로그만; 연속 실패 판정은 감시 스레드) |
| e | 채널 `LOCKED_OPEN`, 또는 재읽기 값이 해제(열림) — **문이 이미 열려 있으면 해제 안 함** (센서 미연결을 NO 배선에서 열림으로 읽는 경우 포함) | `door_open` |
| f | 채널 `COOLDOWN` | `cooldown` |
| g | 다른 ON 채널 수 ≥ `max_concurrent_open` | `concurrency_limit` |
| h | (기존) 불변식 사전/사후 위반, 구동 예외 | `invariant` / HardwareError |
| i | 성공: 채널 `UNLOCKED_WAITING`, 솔밸브 ON, 녹색 ON, `unlocked_at=now`, 데드라인 두 개 설정 (`unlock_timeout_sec`, `max_unlock_sec`) | `""` |

**전이 규칙**

| # | 규칙 |
|---|---|
| R1 | **해제는 얼굴 인식(`should_open`) 한 가지 경로뿐**이다. 리밋 해제(문 열림)가 솔밸브를 켜지 않는다. 유지/재잠금은 리밋만으로 결정한다 |
| R2 | `UNLOCKED_WAITING`: 샘플이 **해제(False)로 연속 `open_confirm_sec`** → `OPEN`(`door_opened` 이벤트). `unlocked_at + unlock_timeout_sec` 경과 → 솔밸브 OFF, cause `unlock_timeout`, `COOLDOWN`. 이 사이 짧은 해제(< `open_confirm_sec`, 접점 바운스)는 무시 |
| R3 | `OPEN`: **시간으로는 잠기지 않는다**(문이 열린 동안 해제 유지). 샘플이 눌림(True)이면 `CLOSING_CONFIRM`(`close_confirm` 시작) |
| R4 | `CLOSING_CONFIRM`: 눌림이 **연속 `close_confirm_sec`** 이면 솔밸브 OFF, cause `close_confirmed`, `COOLDOWN`. 도중에 해제(False)가 한 샘플이라도 오면 `OPEN`으로 복귀하고 카운터 리셋(출력 변화 없음 → 채터링 없음). 도중에 불명(None)이 오면 카운터를 리셋하고 상태 유지(불명을 눌림으로 세지 않음) |
| R5 | **절대상한 워치독(fail-safe)**: 솔밸브가 ON 인 채널은 `unlocked_at + max_unlock_sec`에 센서 값과 무관하게 OFF, cause `watchdog`. 독립 `threading.Timer`(채널별, 해제 시 설정)와 감시 스레드·`status()` 지연 검사가 **모두** 이 데드라인을 확인한다(셋 중 하나만 살아 있어도 동작). 워치독은 연장·리셋되지 않는다(문을 열고 닫기를 반복해도 같은 `unlocked_at` 기준). 문이 아직 열려 있으면 OFF 후 `LOCKED_OPEN`이 되어 문이 닫히기 전까지 재해제 거부 |
| R5a | **쿨다운 만료 시 닫힘 확정(H-1/H-3)**: 문이 열렸던 해제(`door_opened` 이후)가 `close_confirmed` 외의 원인(`watchdog`/`sensor_fault`/`manual`/`shutdown` 등)으로 끝났거나 원인이 `watchdog`/`sensor_fault`이면, 쿨다운이 끝나도 눌림이 **연속 `close_confirm_sec`** 일 때만 `LOCKED_CLOSED`(그 전엔 `LOCKED_OPEN`, 솔밸브 OFF 유지). `close_confirmed`·`unlock_timeout`(문이 안 열림) 경유는 기존대로 쿨다운 만료 즉시. 마지막 샘플이 None이면 어떤 경우에도 `LOCKED_CLOSED`로 보지 않고 `COOLDOWN`을 유지해 다음 유효 샘플/`sensor_fault_sec`가 결정 |
| R6 | **센서 불명**: 샘플이 None이면 "마지막 유효 샘플"을 쓰지 않고 `door_closed=None`으로 둔다. None이 연속 `sensor_fault_sec` 지속되면 — 솔밸브 ON 이든 OFF 든 — **즉시 솔밸브 OFF**, cause `sensor_fault`, `SENSOR_FAULT`(`sensor_fault` 이벤트). 잠금 방향으로만 작동하므로 "열림으로 오인해 유지" 경로가 없다. 복구: 유효 샘플이 `close_confirm_sec` 연속 → `sensor_ok` 이벤트, 눌림이면 `LOCKED_CLOSED`, 해제면 `LOCKED_OPEN` |
| R7 | **시작 시 검사**: 컨트롤러 생성 직후 모든 채널을 1회 동기 읽기. 읽기 실패 → 그 채널 `SENSOR_FAULT`(시작은 계속, 해당 채널만 해제 불가, WARNING + `start` 이벤트 message). 해제(열림) → `LOCKED_OPEN`. 눌림 → `LOCKED_CLOSED`. **모든 채널이 SENSOR_FAULT이면 pi 모드에서 HardwareError(5)로 시작 거부**(센서 배선 전체 오류 조기 발견). 시작 시 솔밸브는 항상 OFF |
| R8 | **감시 스레드 자체가 죽거나 예외**: 예외 → 전 채널 OFF, FAULT, ERROR 로그. 스레드 정지(heartbeat 지연)는 R5의 독립 타이머가 `max_unlock_sec`에 OFF 시킨다. 메인 루프가 멈춰도 R2~R6은 하드웨어 스레드에서 진행 |
| R9 | `COOLDOWN`은 모든 재잠금 뒤 `cooldown_sec`. 만료 시 리밋이 눌림이면 `LOCKED_CLOSED`, 해제면 `LOCKED_OPEN` |
| R10 | 한 채널의 실패는 다른 채널에 영향 없음(채널별 상태·타이머). 단 컨트롤러 `FAULT`(출력 불변식·구동 예외)는 전체 OFF·전체 거부(기존) |
| R11 | `timer` 모드(`close_mode: timer`)는 v1.3과 동일(리밋 미사용, `LockerState`는 `LOCKED_CLOSED`/`OPEN`/`COOLDOWN`/`FAULT`만 사용, `door_closed=None`, 패널은 "센서 없음"으로 열림/닫힘 대신 잠금/해제만 표시) |

**앱(`app.py`) 영향**: `door.open()`은 `should_open` 분기에서만(불변). `hold()`·"본인 유지 신호"·(v1.4의) 부재 재무장 규칙·`close_hold` 메시지는 **전부 삭제**. 이미 해제 중인 사물함에 `should_open`이 다시 나오면 도어가 `busy`로 거부(연장 없음)하고 message는 `<reason> (rejected by door: busy)`. 앱은 매 `step()`에서 `door.drain_events()`를 events.csv 행으로 기록하고 `door.locker_states()`로 패널을 갱신한다. 어떤 앱 예외도 `door.close(reason)`(전 채널 OFF, cause `shutdown`).

**재잠금 후 재해제 억제**: `rearm_sec`(투표기)와 `cooldown_sec`(도어)가 담당한다. `unlock_timeout`으로 잠긴 뒤 같은 사람이 계속 서 있으면 `max(rearm_sec, cooldown_sec)` 뒤에 다시 해제된다(허용: 새 인식에 의한 정당한 새 해제). 워치독 재해제는 `LOCKED_OPEN`(문이 안 닫힘)이면 `door_open`으로 거부.

### 15.4 시뮬레이션(mock)에서 리밋 조작

PC에는 리밋이 없으므로 `MockLimitInputs`(§4.1)가 가상 리밋이다. 초기값은 전 채널 **눌림(닫힘)**.

- **인식 화면(TkDisplay)**: 오른쪽 사물함 패널 클릭 또는 키 `1`~`4` = 해당 사물함 문 열기/닫기 토글(`limits.toggle`). 키 `5`~`8` = 사물함 1~4 센서 읽기 실패 주입 토글(`limits.set_fault`). 패널 하단에 `[시뮬레이션] 클릭 또는 1~4 키: 문 열기/닫기` 안내 한 줄(`limits.is_simulated`일 때만).
- **OpenCV 폴백(`OpenCVDisplay`)**: `show()`가 키 코드를 반환하므로 `ord('1')`~`ord('4')`, `ord('5')`~`ord('8')`을 **앱이** 처리한다(앱 `_handle_sim_key(key)`). `limits.is_simulated`가 아니면(실기) 이 키들은 완전히 무시 — 실기에서 키보드로 센서를 속이는 경로 없음.
- **`Display` 확장(선택, Protocol 기본 no-op)**: `set_sim_handler(cb: Callable[[str | None, str], None] | None) -> None` — TkDisplay가 패널 클릭 시 `cb(locker_id, "toggle")`을 호출. 시뮬레이션이 아니면 앱이 `None`을 넘기고 클릭은 무시된다.
- **`--no-display`/테스트**: 테스트는 `bundle.limits.set_closed("2", False)`와 가짜 clock + `door.poll_once()`로 결정적 구동. 수동 점검용으로 `scripts/hw_selftest.py --limits`(리밋 상태 실시간 출력, 실기 배선 점검)과 `--sim-limits`(mock에서 1~4 키 토글 대화형)을 hw가 제공.
- 시뮬레이션 입력도 실제와 같은 코드 경로(`LimitInputs.read` → 상태기계)를 통과한다. 상태기계를 우회해 `LockerState`를 직접 바꾸는 시뮬레이션 API는 두지 않는다.

### 15.5 로그

- `hardware.log`: 기존 형식(§13.5) 유지, 새 EVENT `STATE`(전이마다 1행: `locker=2 pin=BCM25 OPEN->CLOSING_CONFIRM limit=pressed`), `SENSOR`(`sensor_fault`/`sensor_ok`, `locker=… limit_pin=BCM6 none_for=1.02s`). 솔밸브 OFF 행(`CLOSE`/`WATCHDOG`)은 detail 끝에 `cause=<CloseCause> unlocked_for=<s>s door_open_for=<s>s`(문이 안 열렸으면 `door_open_for=0.000s`). `WATCHDOG`는 cause `watchdog`에만 쓴다.
- `events.csv`: 새 이벤트 이름(§5) — `door_opened`(R2 확정), `door_close`(**솔밸브 OFF = 재잠금** 행, `close_cause` 채움, message `cause=<c> unlocked_for=…`), `door_sensor`(`sensor_fault`/`sensor_ok`, message에 locker·사유), `door_forced`(LOCKED_CLOSED→LOCKED_OPEN). 컬럼 37 `locker_states`.

### 15.6 QA 시나리오 (가짜 clock + `MockLimitInputs` + `poll_once()`, mock `state`·`gpiozero` 두 백엔드 모두)

L1. 해제 후 문이 안 열림(리밋 계속 눌림) → `unlock_timeout_sec`(10.0 s) 정각에 OFF, cause `unlock_timeout`. 9.9 s에는 ON. 이후 `COOLDOWN` → `LOCKED_CLOSED`.
L2. 해제 후 5 s에 문 열림(리밋 해제 ≥ 0.1 s) → `OPEN`. 이후 `unlock_timeout_sec`가 지나도 **잠기지 않음**(R3).
L3. OPEN 중 리밋 눌림 1.9 s 후 다시 해제 → 잠기지 않음, 출력 전이 0회(채터링 없음). 눌림 연속 2.0 s → OFF, cause `close_confirmed`. 1.99 s에는 ON.
L4. 해제 후 문 열림 → 계속 열어 둠 → `max_unlock_sec`(30 s)에 OFF cause `watchdog`, 이후 `LOCKED_OPEN`, 문이 닫혀 2 s 눌림이면 `LOCKED_CLOSED`. 워치독은 문을 열고 닫기를 반복해도 `unlocked_at` 기준 30 s에 발동.
L5. 접점 바운스: UNLOCKED_WAITING 중 해제 50 ms 후 눌림 복귀 → `OPEN` 안 됨(< `open_confirm_sec`). CLOSING_CONFIRM 중 20 ms짜리 해제 샘플 → `OPEN`으로 복귀하고 닫힘 카운터 리셋(바운스가 닫힘 확정을 지연시키는 쪽은 허용: 오잠금이 아니라 늦은 잠금이며 워치독이 상한). 반대로 눌림 쪽 바운스 때문에 `close_confirm_sec`가 일찍 채워지지는 않아야 한다.
L6. 센서 읽기 실패 주입(`set_fault`) — OPEN 중: `sensor_fault_sec`(1.0 s) 정각에 즉시 OFF, cause `sensor_fault`, `SENSOR_FAULT`. 0.9 s 후 복구하면 유지.
L7. 센서 미연결 모사(NO 배선, 항상 해제로 읽힘): 시작부터 → `LOCKED_OPEN`, `open()` 거부 `door_open`, 출력 ON 0회. 해제 직후 선이 빠지는 경우(UNLOCKED_WAITING 중 해제로 바뀜) → `OPEN`으로 유지되지만 `max_unlock_sec`에 반드시 OFF(워치독 확인).
L8. 시작 시 읽기 실패 채널 → `SENSOR_FAULT`, 다른 채널은 정상 해제. 전 채널 실패 + pi(gpiozero MockFactory를 pi로 취급) → HardwareError(5).
L9. 해제 요청 시 동기 재읽기가 None → 거부 `sensor_fault`, 출력 변화 0.
L10. 메인 루프(`app.step`) 정지 상태에서 `poll_once()`만 → R2~R6 모두 정상 진행. 감시 스레드를 죽이고(예외 주입) → 전 채널 OFF + FAULT. 스레드를 정지시키고 독립 타이머만 → `max_unlock_sec`에 OFF.
L11. UNLOCKED_WAITING/OPEN/CLOSING_CONFIRM 중 `open()` 재호출 → `busy`, 데드라인 불변(연장 없음). `COOLDOWN` 중 → `cooldown`.
L12. per_locker 1·2 동시(`max_concurrent_open=2`) → 사물함 2의 문이 열려 있어도 사물함 1의 `unlock_timeout`·워치독 독립. 사물함 1의 센서 오류가 사물함 2를 끄지 않음.
L13. `SHUTDOWN`/예외/SIGTERM/카메라 실패 → OPEN 중에도 즉시 전 채널 OFF(대기 없음), cause `shutdown`.
L14. 불변식: LOCKED_CLOSED인데 출력 ON을 강제 → 다음 `open()` 시 전 채널 OFF + FAULT(`invariant`). `poll_once()`도 같은 검사를 수행해 FAULT.
L15. 시뮬레이션: 키 `1` 토글 → 사물함 1 패널이 닫힘↔열림, 실기(`is_simulated=False`)에서는 키·클릭 무시. 키 `5` → 사물함 1 센서 오류 표시 후 1.0 s에 SENSOR_FAULT.
L16. 설정 검증: `close_mode: hold` → ConfigError; `max_unlock_sec`가 `unlock_timeout_sec + close_confirm_sec + 1` 미만 → ConfigError; `> 120` → ConfigError; pi + limit 모드 + 리밋 핀 누락/중복/예약 핀 → ConfigError; `timer` 모드 구 설정(리밋 키 없음) → v1.3과 동일 동작.
L17. 로그: hardware.log `STATE`/`SENSOR` 행·`cause=` 파싱, events.csv 37컬럼(`locker_states`), `door_opened`/`door_close`/`door_sensor`/`door_forced` 이벤트가 `drain_events()`와 1:1.

평가(eval, `locker_audit`): `watchdog_overruns` 기준을 `max_unlock_sec + 0.5 s`로 변경(limit 모드). 추가 지표 — `on_while_closed_overtime`(솔밸브 ON 이면서 문이 닫힘으로 계속 읽힌 시간이 `unlock_timeout_sec + 0.5 s` 초과한 횟수, **0이어야 함**), `unlock_timeouts`(해제 후 안 열려 재잠금된 횟수 — 오인식/문 안 열림 지표), `watchdog_relocks`(보고, 비율이 높으면 `max_unlock_sec` 재검토), `sensor_faults`(보고), `forced_opens`(해제 없이 문이 열린 횟수, 보고), `open_duration`(해제→재잠금 시간 p50/p95/max).

---

## 16. 사물함(도어) 상태 패널 — v1.5 (v1.4 개정), **미구현** (vision 코어 + GUI)

### 16.1 데이터 모델 — `facedoor/locker_panel.py`

```python
@dataclass(frozen=True, slots=True)
class LockerPanelRow:
    locker_id: str | None        # single 모드 None ("도어")
    state: str                   # LockerState 값 (§4.1/§15.3). 센서 상태를 모르는 관리 화면은 "UNKNOWN"
    door: str                    # "open" | "closed" | "unknown"  ← 패널의 주 표시(열림/닫힘). open 이면 door_closed=False, closed 이면 True, 센서 불명·상태 모름 → unknown
    unlocked: bool               # 솔밸브 ON(잠금 해제 중)인가 — 보조 표시
    countdown_kind: str          # "unlock_wait"(문 열기 대기·재잠금까지) | "close_confirm"(닫힘 확인 중) | "hard"(max_unlock 까지, 작게) | ""
    countdown_sec: float         # 위 종류에 해당하는 남은 시간, 없으면 0.0
    cooldown_remaining_sec: float
    owner_label: str | None      # 표시 정책(§16.2)에 따른 한글 이름 또는 None
    sim_toggle: bool             # True 면 시뮬레이션이라 클릭/키로 문 토글 가능(안내 표시용)
def build_rows(door: DoorController, limits: LimitInputs | None, locker_map: LockerMap | None, opener: Mapping[str, str],
               owner_policy: str, display_names: Mapping[str, str]) -> list[LockerPanelRow]
    """원천 = door.locker_states() 만. limits 는 sim_toggle(= limits.is_simulated) 용. 센서를 직접 읽지 않는다."""
def write_state(path: Path, rows: Sequence[LockerPanelRow], session_id: str) -> None   # 원자적 교체
def read_state(path: Path, max_age_sec: float = 3.0) -> list[LockerPanelRow] | None   # 오래됐거나 없음 → None
```

- 행 순서 = `locker_outputs` 선언 순서(사물함 1~4). single 모드는 행 1개.
- 앱은 상태가 바뀌거나 0.5 s마다 `logging.door_state_file`(기본 `logs/door_state.json`, `{"schema_version":2,"ts":..., "session_id":..., "rows":[...]}`)에 쓴다. 종료 시 마지막으로 행 전부를 `door="unknown"`, `state="UNKNOWN"`로 한 번 쓰고 끝낸다(종료 후 "닫힘"으로 오인 금지). schema_version 1(v1.4 초안) 파일은 읽지 않고 None.

### 16.2 표시 규칙 (공통 문구는 `texts.py`)

패널의 주 표시는 **문 열림/닫힘(리밋센서)**, 보조 표시는 잠금 해제 여부와 카운트다운이다.

| state | 주 표시 (door) | 보조 문구 예 | 색 |
|---|---|---|---|
| LOCKED_CLOSED | 닫힘 | `잠김` | 회색 |
| UNLOCKED_WAITING | 닫힘 | `잠금 해제됨 · 문을 여세요 (7.2초 후 재잠금)` | 노랑 |
| OPEN | 열림 | `해제 유지 중` (+ 소유자, 작게 `최대 25초`) | 초록 |
| CLOSING_CONFIRM | 닫힘 | `닫힘 확인 중 1.2초 후 잠김` | 연초록 |
| COOLDOWN | 닫힘 | `잠김 · 재해제 대기 1.2초` | 회색 |
| LOCKED_OPEN | 열림 | `잠긴 상태에서 문이 열려 있음 — 확인 필요` | 주황 |
| SENSOR_FAULT | 불명 | `센서 오류 — 잠금 유지, 해제 불가` | 노랑(굵게) |
| FAULT | 불명 | `고장 — 관리자 확인` | 빨강 |
| (관리 화면에서 상태 모름) | 불명 | `상태 알 수 없음` | 노랑 |

- 소유자 표시 `display.locker_owner`: `none` | `when_open`(기본: 해제된 동안(UNLOCKED_WAITING~CLOSING_CONFIRM) 연 사람의 display_name) | `always`(배정자 이름 항상 — 공용 화면이면 개인정보 노출, 비권장).
- 인식 화면(`TkDisplay`) 레이아웃은 §17.1. ASCII 폴백(`OpenCVDisplay`)은 영상 위 오른쪽에 `L2 OPEN unlocked` / `L2 CLOSED locked` / `L3 SENSOR?` 형태의 짧은 줄.
- 관리 화면은 하드웨어를 모르므로 `read_state()`로 읽는다. 파일이 없거나 3초 이상 오래됐거나 `camera.lock`을 인식 프로그램이 잡고 있지 않으면 모든 행 `unknown` + "인식 프로그램이 꺼져 있어 도어 상태를 알 수 없습니다". **"닫힘/잠김"으로 추정 표시 금지**(거짓 안심 방지).

---

## 17. 한글 표시 — 인식 화면과 CLI, v1.4 **미구현** (vision 코어) — v1.5: 창 레이아웃·시뮬레이션 입력 확정

### 17.1 결정: 인식 화면은 **TkDisplay**(tkinter), Pillow 미도입

| 선택지 | 장점 | 단점 | 판단 |
|---|---|---|---|
| **TkDisplay**: 영상은 `cv2.imencode(".ppm")` → `tk.PhotoImage`, 글자는 tk 위젯 | 관리 화면과 같은 기술·폰트·문구 모듈 재사용, 추가 pip 의존성 없음, Pi는 GUI 때문에 이미 `python3-tk`·`fonts-nanum` 필요 | tk 이벤트 처리를 메인 루프에서 직접 돌려야 함 | **채택** |
| Pillow + 한글 TTF로 프레임에 그리기 | 화면 1개(cv2.imshow) 유지 | 의존성 추가, 폰트 경로 OS별 관리, 프레임마다 그리기 비용 | 기각 |

`TkDisplay(Display)` 계약:
- `__init__(cfg: DisplaySection, texts=facedoor.texts)`: tkinter는 생성자 내부 지연 import. **창 레이아웃(v1.5, 고정)**:

```
┌───────────────────────────────┬──────────────────────┐
│                               │ 사물함 1             │  <- 오른쪽 열: 위에서부터 사물함 패널
│   왼쪽: 웹캠 영상             │  [ 닫힘 ] 잠김       │     (locker_outputs 선언 순서, 보통 4개, 높이 균등)
│   (얼굴 사각형만 색으로)      ├──────────────────────┤
│                               │ 사물함 2             │
│                               │  [ 열림 ] 해제 유지  │  패널 1개의 구성:
│                               ├──────────────────────┤   - 제목 "사물함 N" (+ 소유자, locker_owner 정책)
│                               │ 사물함 3             │   - 큰 배지 [닫힘]/[열림]/[불명] = row.door (리밋센서)
│                               │  [ 닫힘 ] 문을 여세요│   - 보조 문구 + 카운트다운 막대 (§16.2)
│                               ├──────────────────────┤   - 배경색 = state 색 (§16.2)
│                               │ 사물함 4             │   - 시뮬레이션이면 클릭 가능(손모양 커서)
│                               │  [ 불명 ] 센서 오류  │
├───────────────────────────────┴──────────────────────┤
│ 하단 상태줄: 인식 상태 문구 · 임계값 출처 · FPS       │  <- 인식 상태/점수/FPS는 영상 아래 한 줄(오른쪽 열은 사물함 전용)
│ [시뮬레이션] 클릭 또는 1~4 키: 문 열기/닫기 (mock일 때만) │
└──────────────────────────────────────────────────────┘
```

  패널 수 = `len(rows)`(single 모드는 1개 "도어", 4개 초과 시 균등 축소). 오른쪽 열 너비 약 320~360 px(영상 폭의 ~55%), 영상 폭은 `display.scale`을 따른다. 패널은 `state` 변화 또는 카운트다운 변화가 있을 때만 위젯을 갱신(매 프레임 전체 재그리기 금지).
- 시뮬레이션 입력: `set_sim_handler(cb)`(§15.4)가 `None`이 아닐 때만 패널 클릭이 `cb(locker_id, "toggle")`을 호출하고, 키 `1`~`4`(문 토글)·`5`~`8`(센서 실패 토글)은 `show()` 반환 키 코드로 앱에 전달된다. `None`(실기)이면 클릭·안내 줄 모두 비활성.
- `show(...)`: 메인 스레드에서 호출(기존 루프). 영상에는 얼굴 사각형만 색으로 그리고(초록 MATCH/빨강 UNKNOWN/노랑 HOLD, 글자 없음), 위젯 갱신 후 `root.update_idletasks(); root.update()`(mainloop 사용 안 함). `q`/`Esc`/창 닫기 → 종료 요청 키 코드 반환.
- 인식 상태 문구: `인식됨: 홍길동 (0.62 / 기준 0.50)`, `미등록 얼굴`, `판정 보류: 너무 어둡습니다`, `확인 중 2/3`, `출입 거부`, `사물함 2 잠금 해제 — 10초 안에 문을 여세요`, `문이 열리지 않아 사물함 2를 다시 잠갔습니다`, `사물함 2는 문이 열려 있어 해제할 수 없습니다`, `사물함 2 센서 오류 — 해제할 수 없습니다`, `최대 해제 시간이 지나 사물함 2를 잠갔습니다`, `사물함이 배정되지 않았습니다`. 점수 표시는 `display.show_scores`.
- 추가 메서드(선택, Protocol 기본 no-op): `set_panel(rows: Sequence[LockerPanelRow]) -> None` — 앱이 `show()` 직전에 호출. `set_sim_handler(cb)` — §15.4. `OpenCVDisplay`/`NullDisplay`도 구현(무시 또는 ASCII 요약).
- `create_display`: `display.backend` = `auto`(tk import 가능 + 디스플레이 있음(Windows 또는 `DISPLAY`/`WAYLAND_DISPLAY`) → tk, 아니면 WARNING 후 opencv) | `tk`(실패 시 오류 종료 1, 조용한 폴백 없음) | `opencv`(ASCII). `--no-display`는 기존대로 `NullDisplay`.
- 성능: Pi5 640x480에서 PPM 변환+PhotoImage 갱신 시간을 측정해 리포트 `latency_ms`와 함께 기록. 부족하면 `display.scale` 축소.
- Pi 실행: systemd 서비스는 `--no-display` 유지. 모니터 표시가 필요하면 자동 로그인 데스크톱 세션의 자동 시작(autostart)으로 `run.py`를 실행한다(Ubuntu 24.04 기본 Wayland → tkinter는 XWayland로 동작, 실기 확인 필요).

### 17.2 설정 (`display` 섹션 추가 키, 구현 후 주석 해제)

```yaml
display:
  backend: auto                 # auto | tk | opencv
  locker_panel: true
  locker_owner: when_open       # none | when_open | always
  font_families: ["Malgun Gothic", "NanumGothic", "Noto Sans CJK KR"]   # GUI 와 공용 (gui.font_families 폐기)
  font_size: 14
logging:
  door_state_file: logs/door_state.json
```

### 17.3 한글화 범위 (모든 담당 공통)

| 대상 | 언어 |
|---|---|
| 화면에 보이는 모든 문구(인식 화면·관리 화면·대화상자·사물함 패널·경고) | **한글** (`facedoor/texts.py`) |
| CLI 스크립트의 사용자 출력(결과 요약, 오류·경고·확인 질문, 다음 할 일 안내, `argparse` description/help) | **한글** (`texts.py`) |
| `report.html` 제목·설명·표 머리글 | 한글 (그래프 축 글자는 한글 폰트가 있으면 한글, 없으면 영문 허용) |
| `logging` 로그(`facedoor.log`, `hardware.log`), CSV 컬럼명, JSON 키, Enum 값, 이벤트 `message` 기계 판독 문구, CLI 옵션 이름, 종료 코드, 코드 식별자 | **영문 유지**(파싱·검색·호환성) |

- `texts.setup_console()`: 모든 스크립트 시작 시 호출. `sys.stdout/stderr.reconfigure(errors="replace")`(Windows 콘솔·리다이렉트·systemd journal에서 한글로 죽지 않게).
- 오류 출력 형식: `오류: <한글 설명>` 다음 줄에 `(<영문 예외 클래스>: <원문>)` — 검색·지원용 원문 보존.
- `texts.py` 정적 테스트: 모든 사용자 문구 값에 한글이 포함되어야 함(G9 확장).
