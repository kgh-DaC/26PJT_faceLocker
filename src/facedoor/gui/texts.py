"""All user-facing Korean strings of the admin GUI (INTERFACES section 14.8). Keep strings here only.

Every value must contain Hangul (tests/test_admin_gui.py G9). Placeholders use str.format names.
Console / file logs may stay in English.
"""

from __future__ import annotations

# ------------------------------------------------------------------------------------------- window / menu
APP_TITLE = "얼굴인식 출입 관리"
MENU_REGISTER = "사용자 등록"
MENU_LIST = "사용자 목록·사물함 배정"
MENU_AUGMENT = "인식 보강(추가 촬영)"
MENU_REMOVE = "사용자 제거"
MENU_EXIT = "종료"
MENU_HEADER = "관리 메뉴"
HOME_GUIDE = "왼쪽 메뉴에서 작업을 선택하세요. 카메라는 촬영 시작 버튼을 누를 때만 켜집니다."

STATUS_MODE_SINGLE = "모드: 단일 출력"
STATUS_MODE_PER_LOCKER = "모드: 사물함 {n}개"
STATUS_USERS = "등록 인원: {n}명"
STATUS_CAMERA_OK = "카메라: 사용 가능"
STATUS_CAMERA_NO = "카메라: 사용 불가"
CAMERA_READY = "카메라를 사용할 수 있습니다."

# ------------------------------------------------------------------------------------------- PIN
PIN_TITLE = "관리자 확인"
PIN_SETUP = "처음 실행입니다. 관리자 PIN(숫자 {min}자리 이상)을 설정하세요."
PIN_SETUP_CONFIRM = "확인을 위해 같은 PIN을 한 번 더 입력하세요."
PIN_SETUP_MISMATCH = "두 번 입력한 PIN이 서로 다릅니다. 다시 설정하세요."
PIN_SETUP_DONE = "관리자 PIN이 설정되었습니다. 잊지 않도록 안전한 곳에 기록해 두세요."
PIN_PROMPT = "관리자 PIN을 입력하세요."
PIN_WRONG = "PIN이 올바르지 않습니다. (남은 횟수 {n}회)"
PIN_LOCKED = "입력 횟수를 초과했습니다. {min}분 후 다시 시도하세요."
PIN_LABEL = "PIN 번호"
PIN_LABEL_AGAIN = "PIN 확인"
PIN_DISABLED_WARNING = "개발용 설정(gui.require_pin: false)으로 PIN 확인 없이 실행합니다."

# ------------------------------------------------------------------------------------------- common buttons
BTN_OK = "확인"
BTN_CANCEL = "취소"
BTN_CLOSE = "닫기"
BTN_REFRESH = "새로고침"
BTN_START_REGISTER = "등록 시작"
BTN_START_AUGMENT = "추가 촬영 시작"
BTN_STOP_CAPTURE = "촬영 취소"
BTN_REMOVE_SELECTED = "선택한 사용자 제거"
BTN_ASSIGN = "배정·변경"
BTN_UNASSIGN = "배정 해제"
BTN_TAKE_OVER = "{owner}님 배정을 해제하고 옮기기"
BTN_SHARE = "함께 사용(공유)"
BTN_ADVANCED = "고급(사용자 ID 수정)"
BTN_BACK_HOME = "처음 화면"

TITLE_INFO = "안내"
TITLE_WARNING = "주의"
TITLE_ERROR = "오류"
TITLE_CONFIRM = "확인"

# ------------------------------------------------------------------------------------------- errors
CAMERA_BUSY = ("카메라를 사용할 수 없습니다. 얼굴인식 프로그램이 실행 중입니다({holder}). 먼저 종료하세요. "
               "(라즈베리파이: sudo systemctl stop facedoor)")
CAMERA_FAIL = "카메라를 열 수 없습니다. 연결 상태와 설정(camera.source)을 확인하세요."
MODEL_MISSING = "얼굴 인식 모델 파일이 없습니다. 먼저 'python scripts/download_models.py'를 실행하세요."
GALLERY_MISMATCH = ("저장된 얼굴 데이터가 현재 인식 모델과 맞지 않습니다. "
                    "'python scripts/enroll.py --reembed'로 변환하세요.")
GALLERY_BUSY = "다른 관리 작업이 진행 중입니다. 잠시 후 다시 시도하세요."
CAPTURE_RUNNING = "촬영이 진행 중입니다. 촬영을 마치거나 취소한 뒤 다시 시도하세요."
ERROR_GENERIC = "오류가 발생했습니다: {detail}\n자세한 내용은 logs/facedoor.log 를 확인하세요."
TK_FONT_MISSING = ("한글 글꼴을 찾지 못했습니다. 글자가 깨질 수 있습니다. "
                   "(우분투/라즈베리파이: sudo apt install fonts-nanum)")

# ------------------------------------------------------------------------------------------- validation
NAME_EMPTY = "이름을 입력하세요."
NAME_INVALID = "이름에 사용할 수 없는 문자가 있습니다. (한글·영문·숫자·공백·.-_ 만 가능)"
NAME_TOO_LONG = "이름은 {max}자 이하로 입력하세요."
NAME_DUPLICATE = "이미 '{name}'({pid})이(가) 있습니다. 계속할까요?"
ID_EMPTY = "사용자 ID를 입력하세요."
ID_INVALID = "사용자 ID는 영문·숫자·_- 만 사용할 수 있고 '_'로 시작할 수 없습니다: {pid}"
ID_EXISTS = "이미 사용 중인 사용자 ID입니다: {pid}"
USER_NOT_FOUND = "등록되지 않은 사용자입니다: {pid}"
USER_NOT_SELECTED = "목록에서 사용자를 먼저 선택하세요."
TAG_INVALID = "알 수 없는 촬영 조건입니다: {tag}"
NOTE_TOO_LONG = "메모는 {max}자 이하로 입력하세요."

# ------------------------------------------------------------------------------------------- poses / rejections
POSE_front = "정면을 보세요"
POSE_left = "고개를 왼쪽으로 살짝 돌리세요"
POSE_right = "고개를 오른쪽으로 살짝 돌리세요"
POSE_up = "턱을 살짝 드세요"
POSE_down = "턱을 살짝 내리세요"

POSE_SHORT = {"front": "정면", "left": "왼쪽", "right": "오른쪽", "up": "위", "down": "아래"}

REJ_too_small = "카메라에 조금 더 가까이 오세요"
REJ_too_dark = "너무 어둡습니다 — 조명을 조금 밝혀 주세요"
REJ_too_bright = "너무 밝거나 역광입니다"
REJ_blur = "움직이지 말고 잠시 멈춰 주세요"
REJ_pose = "안내된 방향으로 고개를 맞춰 주세요"
REJ_border = "얼굴을 화면 가운데로 옮겨 주세요"
REJ_low_det_score = "얼굴이 잘 보이지 않습니다"
REJ_multi_face = "한 사람만 화면에 나오게 해 주세요"
REJ_no_face = "얼굴이 보이지 않습니다"
REJ_duplicate = "같은 자세가 반복됩니다. 조금씩 움직여 주세요"
REJ_other = "촬영 조건이 맞지 않습니다. 잠시 후 다시 시도됩니다"

# ------------------------------------------------------------------------------------------- capture
CAPTURE_PROGRESS = "{pose} ({idx}/{n}) · 저장 {saved}/{target}장"
CAPTURE_COUNTDOWN = "{sec}초 후 촬영을 시작합니다"
CAPTURE_PREPARING = "카메라를 준비하고 있습니다… (모델을 처음 불러올 때는 몇 초 걸릴 수 있습니다)"
CAPTURE_SAVED = "저장했습니다"
CAPTURE_CANCELLING = "촬영을 취소하는 중입니다…"
CAPTURE_IDLE = "이름을 입력하고 [등록 시작]을 누르면 카메라가 켜집니다."
CAPTURE_IDLE_AUGMENT = "사용자와 조건을 고른 뒤 [추가 촬영 시작]을 누르면 카메라가 켜집니다."
PREVIEW_PLACEHOLDER = "미리보기 (촬영 중에만 표시)"
MASK_HAT_HINT = "마스크와 모자를 함께 쓴 경우에는 '마스크'로 촬영하세요."
LOWLIGHT_HINT = "저조도·역광 촬영도 품질 기준은 낮추지 않습니다. 너무 어두우면 조명을 조금 밝혀 주세요."

REGISTER_OK = "'{name}'님 등록이 완료되었습니다 ({saved}장)."
REGISTER_FAIL = ("얼굴이 충분히 촬영되지 않아 등록을 취소했습니다 ({saved}/{need}장). "
                 "밝은 곳에서 정면을 보고 다시 시도하세요.")
REGISTER_CANCELLED = "등록을 취소했습니다. 저장된 사진은 모두 삭제했습니다."
AUGMENT_OK = "'{name}'님 추가 촬영을 마쳤습니다. {tag} {saved}장 저장."
AUGMENT_CANCELLED = "추가 촬영을 취소했습니다. 이미 저장된 {saved}장은 유지됩니다."
AUGMENT_NONE = "저장된 사진이 없습니다. 밝은 곳에서 다시 시도하세요."
LOCKER_ASSIGN_WARN = "등록은 완료되었지만 사물함 배정은 하지 못했습니다: {detail}"

# ------------------------------------------------------------------------------------------- register screen
REG_TITLE = "사용자 등록"
REG_NAME = "이름"
REG_ID = "사용자 ID"
REG_ID_HINT = "자동으로 정해집니다. 바꾸려면 고급을 누르세요."
REG_LOCKER = "사물함"
LOCKER_NONE = "배정 안 함"
LOCKER_IN_USE = "{locker} (사용 중: {owner})"

# ------------------------------------------------------------------------------------------- augment screen
AUG_TITLE = "인식 보강(추가 촬영)"
AUG_USER = "사용자"
AUG_CONDITION = "촬영 조건"
AUG_NOTE = "메모(기타)"
AUG_INFO = "샘플 {n}장 · {tags} · 자체점수 {score}"
AUG_WEAK = "보강 권장: {reasons}"
AUG_NOT_WEAK = "보강이 꼭 필요하지는 않습니다."

TAG_LABELS = {
    "normal": "일반", "mask": "마스크", "hat": "모자", "glasses": "안경", "noglasses": "안경 없음",
    "scarf": "목도리", "lowlight": "저조도", "backlight": "역광", "other": "기타",
}
AUGMENT_TAG_ORDER = ["normal", "mask", "hat", "glasses", "noglasses", "scarf", "lowlight", "backlight", "other"]

# ------------------------------------------------------------------------------------------- list / locker screen
LIST_TITLE = "사용자 목록·사물함 배정"
COL_NAME = "이름"
COL_ID = "사용자 ID"
COL_LOCKER = "사물함"
COL_SAMPLES = "총 샘플"
COL_TAGS = "태그별 수"
COL_SELF = "자체점수"
COL_WEAK = "보강"
COL_LOCKER_ID = "사물함 번호"
COL_PIN = "출력 핀"
COL_OWNER = "배정자"
COL_STATUS = "상태"
WEAK_MARK = "보강 필요"
WEAK_OK = "양호"
NO_VALUE = "없음"
LOCKER_STATUS = {
    "free": "비어 있음", "assigned": "배정됨", "shared": "공유(허용)", "shared_denied": "공유 경고",
    "unknown": "설정에 없음",
}
LOCKER_SELECT = "배정할 사물함"
LOCKER_TAKEN = "사물함 {locker}번은 이미 '{owner}'님에게 배정되어 있습니다."
LOCKER_NOT_CONFIGURED = "설정에 없는 사물함입니다: {locker}"
LOCKER_REJECTED = "사물함을 배정할 수 없습니다: {detail}"
LOCKER_SHARED_NOT_ALLOWED = "사물함 공유가 허용되지 않은 설정입니다 (lockers.allow_shared: false)."
LOCKER_SINGLE_MODE = "현재 단일 출력 모드입니다. 사물함 배정은 저장되지만 문 열기에는 사용되지 않습니다."
LOCKER_ASSIGNED = "'{name}'님에게 사물함 {locker}번을 배정했습니다."
LOCKER_CLEARED = "'{name}'님의 사물함 배정을 해제했습니다."
LOCKER_NOTHING = "'{name}'님은 배정된 사물함이 없습니다."
LOCKER_PROBLEM = "문제: {detail}"

# ------------------------------------------------------------------------------------------- remove screen
REMOVE_TITLE = "사용자 제거"
REMOVE_CONFIRM = ("'{name}'({pid}) 사용자를 삭제할까요?\n"
                  "얼굴 데이터 {n}장과 사물함 {locker} 배정이 함께 삭제되며 되돌릴 수 없습니다.")
REMOVE_DONE = "'{name}'({pid}) 사용자를 삭제했습니다."
REMOVE_RESTART_WARN = ("[중요] 얼굴인식 프로그램을 다시 시작하기 전까지는 삭제한 사람이 계속 인식될 수 있습니다. "
                       "지금 다시 시작하세요.")
RESTART_NOTICE = ("변경 내용은 얼굴인식 프로그램을 다시 시작해야 반영됩니다. "
                  "(라즈베리파이: sudo systemctl restart facedoor)")

# ------------------------------------------------------------------------------------------- weak reasons
WEAK_FEW_SAMPLES = "샘플이 적습니다 ({n}/{need}장)"
WEAK_MISSING_TAG = "{tag} 촬영이 없습니다"
WEAK_LOW_SELF = "촬영 사진끼리 일관성이 낮습니다 ({score:.2f})"
WEAK_OUTLIERS = "이상치 샘플 {n}장"
WEAK_FRR = "최근 평가에서 인식 실패율이 높습니다 ({pct:.0f}%)"

# ------------------------------------------------------------------------------------------- door panel (16.2)
PANEL_TITLE = "사물함 상태"
PANEL_DOOR = "도어"
PANEL_LOCKER = "사물함 {locker}"
PANEL_OPEN = "{name}  열림 · 잠김까지 {sec:.1f}초"
PANEL_CLOSED = "{name}  잠김"
PANEL_COOLDOWN = "{name}  잠김 · 재개방 대기 {sec:.1f}초"
PANEL_FAULT = "{name}  고장 — 관리자 확인"
PANEL_UNKNOWN = "{name}  상태 알 수 없음"
PANEL_OWNER = " · 사용자 {owner}"
PANEL_PROGRAM_OFF = "인식 프로그램이 꺼져 있어 도어 상태를 알 수 없습니다"

# ------------------------------------------------------------------------------------------- CLI (admin_gui.py)
CLI_DESCRIPTION = "얼굴인식 출입 관리 화면(한글). 실행해도 카메라는 켜지지 않으며 버튼을 눌러야 촬영합니다."
CLI_HELP_CONFIG = "설정 파일 경로 (기본: configs/default.yaml + configs/local.yaml)"
CLI_HELP_SET = "설정 덮어쓰기 (예: camera.source=1), 여러 번 지정 가능"
CLI_HELP_SOURCE = "촬영에 쓸 카메라 번호 또는 영상/이미지 폴더 경로 (기본: camera.source)"
CLI_ERROR = "오류: {msg}"
CLI_ERROR_ORIGINAL = "({cls}: {detail})"
CLI_TK_MISSING = ("tkinter를 불러올 수 없습니다. 우분투/라즈베리파이에서는 "
                  "'sudo apt install python3-tk fonts-nanum'을 실행하세요.")
CLI_NO_DISPLAY = "화면(디스플레이)을 열 수 없습니다. 모니터가 연결된 데스크톱에서 실행하세요."
CLI_CONFIG_ERROR = "설정 오류입니다. 설정 파일과 --set 값을 확인하세요."
CLI_STARTED = "관리 화면을 시작합니다. (카메라는 촬영 버튼을 누를 때만 켜집니다)"
CLI_EXITED = "관리 화면을 종료했습니다."
CLI_PIN_CORRUPT = "PIN 파일 문제로 관리 화면에 들어갈 수 없습니다."
CLI_FONT_HINT = "한글 글꼴이 없습니다: sudo apt install fonts-nanum"

# ------------------------------------------------------------------------------------------- close
CLOSE_WHILE_CAPTURE = "촬영 중입니다. 촬영을 취소하고 종료할까요?"


def tag_label(tag: str) -> str:
    return TAG_LABELS.get(tag, tag)


def pose_text(pose: str | None) -> str:
    if not pose:
        return ""
    return globals().get(f"POSE_{pose}", pose)


def reject_text(reason: str | None) -> str:
    if not reason:
        return ""
    return globals().get(f"REJ_{reason}", REJ_other)


def panel_text(locker_id: str | None, state: str, hold_sec: float = 0.0, cooldown_sec: float = 0.0,
               owner: str | None = None) -> str:
    name = PANEL_DOOR if locker_id is None else PANEL_LOCKER.format(locker=locker_id)
    if state == "open":
        s = PANEL_OPEN.format(name=name, sec=max(0.0, hold_sec))
    elif state == "closed":
        s = PANEL_CLOSED.format(name=name)
    elif state == "cooldown":
        s = PANEL_COOLDOWN.format(name=name, sec=max(0.0, cooldown_sec))
    elif state == "fault":
        s = PANEL_FAULT.format(name=name)
    else:
        s = PANEL_UNKNOWN.format(name=name)
    if owner and state == "open":
        s += PANEL_OWNER.format(owner=owner)
    return s


_NOT_USER_TEXT = {"AUGMENT_TAG_ORDER", "CLI_ERROR_ORIGINAL"}   # format of the English original (17.3)


def all_texts() -> dict[str, object]:
    """Every user-facing constant: str values and the values of dict constants (static Hangul test G9)."""
    out: dict[str, object] = {}
    for k, v in globals().items():
        if k.startswith("_") or k in _NOT_USER_TEXT or not (k.isupper() or k.startswith(("POSE_", "REJ_"))):
            continue
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, dict):
            for kk, vv in v.items():
                out[f"{k}[{kk}]"] = vv
    return out
