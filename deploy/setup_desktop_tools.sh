#!/bin/bash
# Pi(Ubuntu 24.04 arm64 데스크톱)에 터미네이터, VS Code, 한글 환경을 설치한다. 멱등(여러 번 실행해도 됨).
# 사용법:  sudo bash /opt/facedoor/deploy/setup_desktop_tools.sh [terminator] [vscode] [korean]   (인자 없으면 전부)
# 되돌리기: sudo apt remove terminator code ibus-hangul / sudo rm /etc/apt/sources.list.d/vscode.sources
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "sudo 로 실행하세요: sudo bash $0" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
WANT="${*:-terminator vscode korean}"
has() { case " $WANT " in *" $1 "*) return 0 ;; esac; return 1; }

# 자동 업데이트(unattended-upgrade)가 apt 잠금을 쥐고 있으면 끝날 때까지 기다린다.
APT_OPT="-o DPkg::Lock::Timeout=1800"
apt-get $APT_OPT update

if has terminator; then
    echo "=== Terminator"
    apt-get $APT_OPT install -y terminator
fi

if has vscode; then
    echo "=== VS Code (Microsoft 공식 apt 저장소, arm64)"
    apt-get $APT_OPT install -y wget gpg ca-certificates
    install -d -m 0755 /usr/share/keyrings
    wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft.gpg
    chmod 0644 /usr/share/keyrings/microsoft.gpg
    cat > /etc/apt/sources.list.d/vscode.sources <<'EOF'
Types: deb
URIs: https://packages.microsoft.com/repos/code
Suites: stable
Components: main
Architectures: arm64
Signed-By: /usr/share/keyrings/microsoft.gpg
EOF
    apt-get $APT_OPT update
    apt-get $APT_OPT install -y code
fi

if has korean; then
    echo "=== 한글 환경 (로케일, 글꼴, 한글 입력기)"
    apt-get $APT_OPT install -y language-pack-ko language-pack-gnome-ko fonts-nanum fonts-noto-cjk ibus ibus-hangul
    locale-gen ko_KR.UTF-8 en_US.UTF-8
    # 시스템 언어는 영어를 유지하고 한글 로케일만 추가한다(로그/오류 메시지는 영어 그대로 두는 편이 문제 분석에 유리).
    # 화면 언어까지 한국어로 바꾸려면:  sudo localectl set-locale LANG=ko_KR.UTF-8 LANGUAGE=ko:en
    update-locale LANG=en_US.UTF-8
    fc-cache -f >/dev/null 2>&1 || true
fi

echo
echo "=== 완료. 마무리(데스크톱에서 직접):"
has korean && cat <<'EOF'
1) 로그아웃 후 다시 로그인(또는 sudo reboot).
2) 설정 > 키보드 > 입력 소스 > + > 한국어 > "Korean (Hangul)" 추가.
3) 한/영 전환: Shift+Space (또는 설정에서 Hangul 키 지정).
4) 화면 언어도 한국어로: 설정 > 시스템 > 지역 및 언어 > 언어 > 한국어 선택 후 재로그인.
EOF
has vscode && echo "VS Code: 메뉴에서 'Visual Studio Code' 또는 터미널에서 code"
has terminator && echo "Terminator: 메뉴에서 'Terminator' 또는 터미널에서 terminator"
