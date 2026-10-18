#!/bin/bash
# 바탕화면에 실행 아이콘 3개를 만든다(선택 메뉴 / 등록 / 인식). sudo 없이 일반 사용자로 실행. 멱등.
# 사용법:  bash /opt/facedoor/deploy/install_desktop_launcher.sh
# 제거:    rm "$(xdg-user-dir DESKTOP)"/facedoor-*.desktop ~/.local/share/applications/facedoor-*.desktop
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH="$ROOT/deploy/facedoor_launcher.sh"
chmod +x "$LAUNCH"
command -v zenity >/dev/null 2>&1 || echo "참고: zenity 가 없어 선택창이 안 뜹니다 -> sudo apt install -y zenity"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"   # 한글 환경이면 ~/바탕화면
mkdir -p "$DESKTOP_DIR" "$HOME/.local/share/applications"

# 아이콘: deploy/icons/logo.png (또는 .svg/.jpg)가 있으면 그것을, 없으면 기본 카메라 아이콘을 쓴다.
ICON="camera-photo"
for ext in png svg jpg jpeg; do
    [ -f "$ROOT/deploy/icons/logo.$ext" ] && { ICON="$ROOT/deploy/icons/logo.$ext"; break; }
done
echo "아이콘: $ICON"

make() {  # make <파일명> <이름> <인자> <터미널 여부>
    local f="$DESKTOP_DIR/$1.desktop"
    cat > "$f" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=얼굴인식 도어 시스템
Exec=$LAUNCH $3
Path=$ROOT
Icon=$ICON
Terminal=$4
Categories=Utility;
EOF
    chmod +x "$f"
    gio set "$f" metadata::trusted true 2>/dev/null || true     # GNOME: "실행 허용" 처리
    cp "$f" "$HOME/.local/share/applications/"
}

make facedoor-menu  "얼굴인식 도어 (선택)"     ""         false
make facedoor-admin "얼굴 등록 프로그램"       "admin"    false
make facedoor-run   "얼굴 인식 (실기 pi)"      "run-pi"   true

echo "바탕화면($DESKTOP_DIR)에 아이콘을 만들었습니다."
echo "아이콘이 회색이면: 우클릭 > '실행 허용(Allow Launching)'."
