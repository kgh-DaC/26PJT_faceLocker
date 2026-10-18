#!/bin/sh
# Pi 모니터(HDMI)에 한글 인식 화면(TkDisplay)을 띄운다. 데스크톱 없이 최소 X 서버만 사용한다.
# 사전 설치(한 번, sudo 필요):
#   sudo apt install -y xorg openbox python3-tk fonts-nanum
#   sudo mkdir -p /etc/X11/xorg.conf.d && sudo cp /opt/facedoor/deploy/99-pi5-modesetting.conf /etc/X11/xorg.conf.d/
# 사용법(Pi 키보드로 tty1 에 로그인한 상태에서; SSH 에서는 X 서버가 거부된다):
#   /opt/facedoor/deploy/run_on_monitor.sh [run.py 옵션...]
#   예) 배선 전 시험:  /opt/facedoor/deploy/run_on_monitor.sh --set hardware.mode=mock
# 종료: 인식 창에서 q/ESC. 창이 닫히면 X 도 같이 끝난다.
# startx 는 인자를 다시 단어 분리해 "sh -c openbox" 만 실행해 버리므로, 실행 내용은 임시 xinitrc 파일에 쓰고
# startx 에는 그 파일 경로 하나만 넘긴다.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RC="$(mktemp /tmp/facedoor-xinitrc.XXXXXX)" || exit 1
trap 'rm -f "$RC"' EXIT INT TERM

ARGS=""
for a in "$@"; do
    case "$a" in
        *"'"*) echo "인자에 작은따옴표(')는 쓸 수 없습니다: $a" >&2; exit 2 ;;
    esac
    ARGS="$ARGS '$a'"
done

LOG="$ROOT/logs/monitor.out"      # X 안에서 일어난 오류를 모니터 대신 이 파일에서 확인한다
mkdir -p "$ROOT/logs"
{
    echo "#!/bin/sh"
    echo "exec >> '$LOG' 2>&1"
    echo "echo \"=== \$(date '+%F %T') xinitrc start DISPLAY=\$DISPLAY\""
    echo "openbox &"
    echo "'$ROOT/.venv/bin/python' '$ROOT/scripts/run.py' --set display.backend=tk$ARGS"
    echo "echo \"=== run.py exit code \$?\""
} > "$RC"
chmod +x "$RC"
startx "$RC" -- :0 vt1
