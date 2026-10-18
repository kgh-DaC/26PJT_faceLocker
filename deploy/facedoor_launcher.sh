#!/bin/bash
# 바탕화면 실행기: 등록 프로그램(admin_gui) / 인식 프로그램(run.py, mock 또는 pi)을 고른다.
# 설치:  bash /opt/facedoor/deploy/install_desktop_launcher.sh
# 직접 실행:  /opt/facedoor/deploy/facedoor_launcher.sh [admin | run-mock | run-pi]
# 등록 프로그램(admin_gui)은 GPIO를 쓰지 않으므로 mock/pi 구분이 없다.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
export GPIOZERO_PIN_FACTORY=lgpio
export FACEDOOR_HOME="$ROOT"
cd "$ROOT" || exit 1

[ -x "$PY" ] || { zenity --error --text="가상환경이 없습니다: $PY\ninstall_rpi.sh 를 먼저 실행하세요." 2>/dev/null || echo "venv 없음: $PY" >&2; exit 1; }

CHOICE="$1"
if [ -z "$CHOICE" ]; then
    if command -v zenity >/dev/null 2>&1; then
        CHOICE=$(zenity --list --title="얼굴인식 도어" --text="실행할 프로그램을 선택하세요" \
            --column="" --column="프로그램" --hide-column=1 --print-column=1 \
            --width=560 --height=420 \
            admin    "등록 프로그램 (얼굴 등록/관리)" \
            run-mock "인식 프로그램 - mock (가상 출력, 실제 신호 없음)" \
            run-pi   "인식 프로그램 - pi (실제 GPIO/솔밸브 출력)") || exit 0
    else
        echo "1) 등록 프로그램   2) 인식 - mock   3) 인식 - pi"
        read -r -p "선택: " n
        case "$n" in 1) CHOICE=admin ;; 2) CHOICE=run-mock ;; 3) CHOICE=run-pi ;; *) exit 0 ;; esac
    fi
fi

# 실기 출력은 실수로 켜지지 않도록 한 번 더 확인한다.
if [ "$CHOICE" = "run-pi" ] && command -v zenity >/dev/null 2>&1; then
    zenity --question --title="실기 모드" --width=380 \
        --text="실제 GPIO로 솔밸브/LED 신호가 출력됩니다.\n배선을 확인하셨나요?" || exit 0
fi

case "$CHOICE" in
    admin)    exec "$PY" scripts/admin_gui.py ;;
    run-mock) exec "$PY" scripts/run.py --set hardware.mode=mock ;;
    run-pi)   exec "$PY" scripts/run.py --set hardware.mode=pi ;;
    *) echo "알 수 없는 선택: $CHOICE (admin | run-mock | run-pi)" >&2; exit 2 ;;
esac
