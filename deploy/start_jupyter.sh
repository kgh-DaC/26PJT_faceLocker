#!/bin/sh
# Pi 에서 JupyterLab 을 시작한다. 같은 네트워크에서 http://<Pi 주소>:8888/ 로 접속할 수 있다(인증 토큰 필수).
# 접속: 브라우저에서 http://172.30.1.45:8888/lab?token=<토큰>  (토큰 확인: /opt/facedoor/.venv/bin/jupyter server list)
# 주의: 같은 네트워크의 누구나 토큰만 알면 Pi 에서 코드를 실행할 수 있고, 연결은 암호화되지 않은 HTTP 이다.
#       이 장치에서만 받으려면 환경변수 JUPYTER_IP=127.0.0.1 로 시작하고 ssh -L 8888:localhost:8888 터널을 쓴다.
# 노트북 폴더: /opt/facedoor/notebooks  (커널 선택: "FaceDoor (Python 3.12)")
# 주의: 같은 카메라를 인식 프로그램과 노트북이 동시에 열 수 없다. 노트북에서 카메라를 쓰기 전에 run.py 를 종료할 것.
# 주의: 노트북에서 hardware.mode=pi 로 GPIO 를 만지지 말 것(배선·극성 확인 전). 시험은 mock 으로.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/notebooks"
cd "$ROOT/notebooks" || exit 1
exec "$ROOT/.venv/bin/jupyter" lab --no-browser --ServerApp.ip="${JUPYTER_IP:-0.0.0.0}" --ServerApp.port=8888 "$@"
