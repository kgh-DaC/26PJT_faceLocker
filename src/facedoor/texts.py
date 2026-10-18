"""Korean user-facing strings of the recognition screen and the locker panel (INTERFACES 16.2, 17.1, 17.3).

Every value must contain Hangul (static test). Machine-readable log text stays English elsewhere.
The admin GUI keeps its own strings in facedoor.gui.texts; this module holds the shared/core ones.
"""

from __future__ import annotations

# ------------------------------------------------------------------------------------------- locker panel
PANEL_LOCKER = "사물함 {locker}"
PANEL_DOOR = "도어"
PANEL_OWNER = "사용자: {owner}"

BADGE_CLOSED = "닫힘"
BADGE_OPEN = "열림"
BADGE_UNKNOWN = "불명"
BADGE_LOCKED = "잠김"            # sensorless (close_mode=timer): lock state only
BADGE_UNLOCKED = "해제"

SUB_LOCKED = "잠김"
SUB_UNLOCKED_WAITING = "잠금 해제됨 · 문을 여세요 ({sec:.1f}초 후 재잠금)"
SUB_OPEN = "해제 유지 중"
SUB_CLOSING = "닫힘 확인 중 {sec:.1f}초 후 잠김"
SUB_COOLDOWN = "잠김 · 재해제 대기 {sec:.1f}초"
SUB_LOCKED_OPEN = "잠긴 상태에서 문이 열려 있음 — 확인 필요"
SUB_SENSOR_FAULT = "센서 오류 — 잠금 유지, 해제 불가"
SUB_FAULT = "고장 — 관리자 확인"
SUB_UNKNOWN = "상태 알 수 없음"
SUB_HARD = "최대 {sec:.0f}초"
SUB_TIMER_UNLOCKED = "해제 중 ({sec:.1f}초 후 잠김)"
SUB_SENSOR_INJECTED = "센서 오류 주입됨(시뮬레이션)"

SIM_HINT = "[시뮬레이션] 클릭 또는 1~4 키: 문 열기/닫기 · 5~8 키: 센서 오류 주입"
SIM_HINT_ASCII_FALLBACK = "[시뮬레이션] 1~4 키: 문 열기/닫기 · 5~8 키: 센서 오류"

# ------------------------------------------------------------------------------------------- recognition line
REC_IDLE = "얼굴을 기다리는 중"
REC_MATCH = "인식됨: {name}"
REC_MATCH_SCORE = "인식됨: {name} ({score:.2f} / 기준 {thr:.2f})"
REC_UNKNOWN = "미등록 얼굴"
REC_UNKNOWN_SCORE = "미등록 얼굴 ({score:.2f} / 기준 {thr:.2f})"
REC_HOLD = "판정 보류: {reason}"
REC_VERIFYING = "확인 중 {votes}/{window}"
REC_DENIED = "출입 거부"
REC_REARM = "처리 완료 — 잠시 후 다시 인식합니다"
REC_THR_SOURCE = "임계값: {source}"
REC_FPS = "속도 {fps:.1f} FPS"

HOLD_REASONS = {
    "too_small": "얼굴이 너무 작습니다",
    "low_det_score": "얼굴이 잘 보이지 않습니다",
    "blur": "영상이 흐립니다",
    "too_dark": "너무 어둡습니다",
    "too_bright": "너무 밝거나 역광입니다",
    "pose": "고개를 정면으로 맞춰 주세요",
    "border": "얼굴을 화면 가운데로 옮겨 주세요",
    "multi_face": "한 사람만 화면에 나오게 해 주세요",
    "liveness": "실물 확인에 실패했습니다",
}
HOLD_UNKNOWN = "알 수 없는 이유"

# ------------------------------------------------------------------------------------------- notices (17.1)
NOTICE_UNLOCKED = "사물함 {locker} 잠금 해제 — {sec:.0f}초 안에 문을 여세요"
NOTICE_UNLOCKED_SINGLE = "잠금 해제 — {sec:.0f}초 안에 문을 여세요"
NOTICE_UNLOCK_TIMEOUT = "문이 열리지 않아 사물함 {locker}를 다시 잠갔습니다"
NOTICE_CLOSED_CONFIRMED = "사물함 {locker}가 닫혀 잠갔습니다"
NOTICE_WATCHDOG = "최대 해제 시간이 지나 사물함 {locker}를 잠갔습니다"
NOTICE_SENSOR_FAULT_RELOCK = "사물함 {locker} 센서 오류로 잠갔습니다"
NOTICE_DOOR_OPEN_REJECT = "사물함 {locker}는 문이 열려 있어 해제할 수 없습니다"
NOTICE_SENSOR_REJECT = "사물함 {locker} 센서 오류 — 해제할 수 없습니다"
NOTICE_BUSY = "사물함 {locker}는 이미 해제 중입니다"
NOTICE_COOLDOWN = "사물함 {locker}는 잠시 후 다시 해제할 수 있습니다"
NOTICE_REJECTED = "사물함 {locker} 해제가 거부되었습니다 ({code})"
NOTICE_FORCED = "사물함 {locker} 문이 해제 없이 열렸습니다 — 확인 필요"
NOTICE_SENSOR_OK = "사물함 {locker} 센서가 복구되었습니다"
NOTICE_NO_LOCKER = "사물함이 배정되지 않았습니다"
NOTICE_LOCKER_UNUSABLE = "사물함을 사용할 수 없습니다 (배정 확인 필요)"

# ------------------------------------------------------------------------------------------- misc
TK_FONT_MISSING = ("한글 글꼴을 찾지 못했습니다. 글자가 깨질 수 있습니다. "
                   "(우분투/라즈베리파이: sudo apt install fonts-nanum)")
TK_BACKEND_FAILED = "tk 인식 화면을 열 수 없습니다: {detail}"
TK_FALLBACK = "tk 화면을 쓸 수 없어 영문(OpenCV) 화면으로 전환합니다: {detail}"
WINDOW_TITLE = "얼굴인식 도어"


def locker_name(locker_id: str | None) -> str:
    return PANEL_DOOR if locker_id is None else PANEL_LOCKER.format(locker=locker_id)


def hold_reason_text(reason: str | None) -> str:
    return HOLD_REASONS.get(reason or "", HOLD_UNKNOWN)


def locker_label(locker_id: str | None) -> str:
    """'2' -> '2' ; None -> '도어' (for notice templates that already say 사물함)."""
    return "도어" if locker_id is None else str(locker_id)


def all_texts() -> dict[str, object]:
    """Every user-facing constant (str values and dict values) for the static Hangul test."""
    out: dict[str, object] = {}
    for k, v in globals().items():
        if k.startswith("_") or not k.isupper():
            continue
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, dict):
            for kk, vv in v.items():
                out[f"{k}[{kk}]"] = vv
    return out
