#!/usr/bin/env bash
# facedoor installer for Raspberry Pi 5 + Ubuntu 24.04 (aarch64, Python 3.12). Owner: hw-engineer.
#
# STATUS: UNVERIFIED ON REAL HARDWARE (written on a Windows PC; needs a Pi 5 run-through).
#
# Usage (from the project root, as a normal user with sudo rights):
#     sudo deploy/install_rpi.sh [--user NAME] [--no-service] [--no-models] [--start]
#                                [--target DIR] [--allow-non-ascii-root]
#
# Install location: the project root is used IN PLACE (venv, logs, service paths all point at it).
# It must be an ASCII path. A non-ASCII root (e.g. the PC folder name 'PJT5_<hangul>') makes the
# model loader copy models to ~/.cache, which the service cannot write (ProtectHome=read-only),
# so the service would crash-loop. Options:
#   --target DIR   copy the project (without .venv) to an ASCII directory such as /opt/facedoor,
#                  chown it to the run user, and continue the install from there. DIR must not exist
#                  or be empty; to update an existing install, run DIR/deploy/install_rpi.sh instead.
#   --allow-non-ascii-root   install in place anyway (manual runs only; not recommended).
# A symlink is NOT used on purpose: resolved paths would still be non-ASCII.
#
# What it does (idempotent; safe to re-run):
#   1. apt: python3-venv python3-lgpio v4l-utils gpiod libgl1 libglib2.0-0
#      (NOT python3-opencv: Ubuntu 24.04 ships 4.6, YuNet 2023mar needs >= 4.8 -> pip wheel)
#   2. venv at <root>/.venv WITH --system-site-packages, so the apt-provided lgpio
#      (C extension, not on PyPI for aarch64 in a usable form) is importable inside the venv.
#   3. pip install -r requirements-pi.txt && pip install -e .
#   4. scripts/download_models.py (YuNet + SFace)
#   5. 'gpio' group + udev rule for /dev/gpiochip*, user added to gpio + video
#   6. configs/local.yaml with "hardware: {mode: pi}" if it does not exist
#   7. systemd unit /etc/systemd/system/facedoor.service (enabled, not started unless --start)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
INSTALL_SERVICE=1
GET_MODELS=1
START=0
TARGET=""
ALLOW_NON_ASCII=0
PASS_ARGS=()   # options forwarded when re-executing from --target

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; PASS_ARGS+=("$1" "$2"); shift 2 ;;
    --no-service) INSTALL_SERVICE=0; PASS_ARGS+=("$1"); shift ;;
    --no-models) GET_MODELS=0; PASS_ARGS+=("$1"); shift ;;
    --start) START=1; PASS_ARGS+=("$1"); shift ;;
    --target) TARGET="$2"; shift 2 ;;
    --allow-non-ascii-root) ALLOW_NON_ASCII=1; PASS_ARGS+=("$1"); shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

is_ascii() { ! printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]'; }

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo: sudo $0 $*" >&2
  exit 1
fi
if [[ "$RUN_USER" == "root" ]]; then
  echo "refusing to run the service as root; pass --user <name>" >&2
  exit 1
fi
id "$RUN_USER" >/dev/null 2>&1 || { echo "user $RUN_USER does not exist" >&2; exit 1; }

# --- install location (must be ASCII, see header) ---
if [[ -n "$TARGET" ]]; then
  case "$TARGET" in /*) ;; *) echo "--target must be an absolute path" >&2; exit 2 ;; esac
  TARGET="${TARGET%/}"
  if ! is_ascii "$TARGET" || [[ "$TARGET" == *[[:space:]#]* ]]; then
    echo "--target must be ASCII without spaces or '#': $TARGET" >&2; exit 2
  fi
  if [[ "$(realpath -m "$TARGET")" != "$ROOT" ]]; then
    if [[ -e "$TARGET" && -n "$(ls -A "$TARGET" 2>/dev/null)" ]]; then
      echo "[facedoor] $TARGET exists and is not empty." >&2
      echo "           To update that install run: sudo $TARGET/deploy/install_rpi.sh" >&2
      exit 1
    fi
    echo "[facedoor] copying $ROOT -> $TARGET (without .venv)"
    mkdir -p "$TARGET"
    tar -C "$ROOT" --exclude=./.venv --exclude='__pycache__' --exclude='*.egg-info' -cf - . \
      | tar -C "$TARGET" -xf -
    chown -R "$RUN_USER":"$RUN_USER" "$TARGET"
    exec bash "$TARGET/deploy/install_rpi.sh" "${PASS_ARGS[@]}"
  fi
fi
if ! is_ascii "$ROOT" || [[ "$ROOT" == *[[:space:]#]* ]]; then
  if [[ $ALLOW_NON_ASCII -eq 1 ]]; then
    echo "[facedoor] WARNING: non-ASCII/space path '$ROOT'. The systemd service will fail to load models" \
         "(ProtectHome=read-only). Use --target /opt/facedoor for service installs." >&2
  else
    echo "[facedoor] ERROR: project root '$ROOT' is not a plain ASCII path." >&2
    echo "           Re-run with:  sudo $0 --target /opt/facedoor ${PASS_ARGS[*]}" >&2
    echo "           (or --allow-non-ascii-root for a manual, non-service setup)" >&2
    exit 1
  fi
fi

MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
echo "[facedoor] root=$ROOT user=$RUN_USER board=$MODEL"
if [[ "$MODEL" != *"Raspberry Pi"* ]]; then
  echo "[facedoor] WARNING: not a Raspberry Pi; hardware.mode=pi will refuse to start here." >&2
fi

as_user() { sudo -u "$RUN_USER" -H "$@"; }

echo "[facedoor] 1/7 apt packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-venv python3-pip python3-lgpio python3-yaml \
  v4l-utils gpiod libgl1 libglib2.0-0
if dpkg -s python3-opencv >/dev/null 2>&1; then
  echo "[facedoor] WARNING: apt python3-opencv is installed (4.6). The venv pip wheel will shadow it," \
       "but consider 'sudo apt remove python3-opencv'." >&2
fi

echo "[facedoor] 2/7 venv (--system-site-packages for lgpio)"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  as_user python3 -m venv --system-site-packages "$ROOT/.venv"
fi
PY="$ROOT/.venv/bin/python"

echo "[facedoor] 3/7 pip install"
as_user "$PY" -m pip install --upgrade pip
as_user "$PY" -m pip install -r "$ROOT/requirements-pi.txt"
as_user "$PY" -m pip install -e "$ROOT"
as_user "$PY" -c "import lgpio, gpiozero, cv2; print('lgpio OK, gpiozero OK, opencv', cv2.__version__)"

echo "[facedoor] 4/7 models"
if [[ $GET_MODELS -eq 1 && -f "$ROOT/scripts/download_models.py" ]]; then
  as_user "$PY" "$ROOT/scripts/download_models.py" || echo "[facedoor] WARNING: model download failed; rerun later" >&2
fi

echo "[facedoor] 5/7 gpio/video permissions"
getent group gpio >/dev/null || groupadd --system gpio
UDEV_RULE=/etc/udev/rules.d/60-facedoor-gpio.rules
cat >"$UDEV_RULE" <<'EOF'
# facedoor: allow members of 'gpio' to use the GPIO character devices (lgpio)
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=gpio || true
usermod -aG gpio,video "$RUN_USER"
mkdir -p "$ROOT/logs" "$ROOT/data" "$ROOT/models"
chown -R "$RUN_USER":"$RUN_USER" "$ROOT/logs" "$ROOT/data" "$ROOT/models"

echo "[facedoor] 6/7 configs/local.yaml"
if [[ ! -f "$ROOT/configs/local.yaml" ]]; then
  as_user tee "$ROOT/configs/local.yaml" >/dev/null <<'EOF'
# Device-local overrides (not in git). See docs/PORTING.md.
hardware:
  mode: pi
  # solenoid_active_high: false   # most opto-isolated relay modules are active-LOW: check deploy/WIRING.md
camera:
  source: "/dev/video0"
EOF
  echo "[facedoor] wrote configs/local.yaml (hardware.mode=pi)"
else
  echo "[facedoor] configs/local.yaml exists; left unchanged"
fi

echo "[facedoor] 7/7 systemd unit"
if [[ $INSTALL_SERVICE -eq 1 ]]; then
  sed -e "s#@ROOT@#$ROOT#g" -e "s#@USER@#$RUN_USER#g" "$ROOT/deploy/facedoor.service" \
    >/etc/systemd/system/facedoor.service
  systemctl daemon-reload
  systemctl enable facedoor.service
  if [[ $START -eq 1 ]]; then
    systemctl restart facedoor.service
  fi
fi

cat <<EOF

[facedoor] done (root=$ROOT). Next steps (log out/in or reboot so group membership applies):
  cd $ROOT
  v4l2-ctl --list-devices                       # find the webcam
  gpioinfo | grep -i -A3 rp1                    # RP1 gpiochip (0 or 4 depending on kernel)
  $PY scripts/hw_selftest.py --mode pi          # LEDs only
  $PY scripts/hw_selftest.py --mode pi --limits # limit switch wiring check (live readings, Ctrl-C)
  $PY scripts/hw_selftest.py --mode pi --allow-solenoid --pulse-sec 0.5   # with an LED in place of the relay first!
  $PY scripts/hw_selftest.py --force-off        # drive all outputs OFF (same as service ExecStopPost)
  sudo systemctl start facedoor && journalctl -u facedoor -f
EOF
