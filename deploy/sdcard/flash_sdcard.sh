#!/usr/bin/env bash
# facedoor SD card writer for Linux / macOS. Owner: hw-engineer.
# STATUS: UNVERIFIED WITH A REAL SD CARD / PI (bash -n, --dry-run and unit tests only).
#
# Writes Ubuntu 24.04 preinstalled-server (arm64+raspi) to an SD card and puts cloud-init user-data,
# network-config, meta-data, the facedoor bundle and the first-boot script on the boot partition.
# All logic that can be tested lives in sdcard_tool.py (stdlib Python >= 3.8).
#
# Usage:
#   deploy/sdcard/flash_sdcard.sh                 # DRY RUN (default): ask, stage, list devices, print the plan
#   sudo deploy/sdcard/flash_sdcard.sh --write    # really write (typed device name + 'ERASE' confirmation)
# Options:
#   --dry-run | --write      mode (default --dry-run; there is NO option to skip the confirmations)
#   --image FILE             use a local .img.xz/.img instead of downloading (verified before use)
#   --sha256 HEX | --sums-file FILE   expected checksum for --image (default: official SHA256SUMS online)
#   --fetch                  in dry-run, also download+verify the image (default: only resolve the name)
#   --image-dir DIR          download cache (default: <work>/images)
#   --work-dir DIR           staging/log directory (default: deploy/sdcard/out, excluded from the bundle)
#   --max-size-gb N          lower the 512 GiB device ceiling (cannot be raised)
#   --verify full|quick      read-back verification after writing (default full)
#   --keep-stage             keep staged user-data/network-config (contain password HASH / Wi-Fi PSK)
#
# Safety: only removable USB/SD media pass (system/boot disks, internal disks, disks holding mounted
# system paths or this tool's files, fixed-media USB disks, read-only cards, > 512 GiB or < 7 GB are refused
# by sdcard_tool.py and re-checked immediately before writing). Passwords are read with 'read -s', sent to
# Python on stdin, never placed on a command line, in the log or in a file (only the SHA-512 crypt hash
# reaches user-data).
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TOOL="$HERE/sdcard_tool.py"

DRY_RUN=1
MODE_SET=""
IMAGE=""
SHA256=""
SUMS_FILE=""
FETCH=0
IMAGE_DIR=""
WORK=""
MAX_GB=""
VERIFY="full"
KEEP_STAGE=0

die() { echo "ERROR: $*" >&2; exit 2; }
usage() { sed -n '2,29p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) [[ "$MODE_SET" == write ]] && die "--dry-run and --write are mutually exclusive"; DRY_RUN=1; MODE_SET=dry; shift ;;
    --write) [[ "$MODE_SET" == dry ]] && die "--dry-run and --write are mutually exclusive"; DRY_RUN=0; MODE_SET=write; shift ;;
    --image) IMAGE="${2:?}"; shift 2 ;;
    --sha256) SHA256="${2:?}"; shift 2 ;;
    --sums-file) SUMS_FILE="${2:?}"; shift 2 ;;
    --fetch) FETCH=1; shift ;;
    --image-dir) IMAGE_DIR="${2:?}"; shift 2 ;;
    --work-dir) WORK="${2:?}"; shift 2 ;;
    --max-size-gb) MAX_GB="${2:?}"; shift 2 ;;
    --verify) VERIFY="${2:?}"; shift 2 ;;
    --keep-stage) KEEP_STAGE=1; shift ;;
    -y|--yes|--force|--no-confirm) die "$1 is intentionally not supported: every write needs typed confirmation" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
[[ "$VERIFY" == full || "$VERIFY" == quick ]] || die "--verify must be full or quick"
if [[ -n "$MAX_GB" && ! "$MAX_GB" =~ ^[0-9]+([.][0-9]+)?$ ]]; then die "--max-size-gb must be a number"; fi
[[ -t 0 ]] || die "this script is interactive; run it from a terminal (stdin is not a TTY)"

OS="$(uname -s)"
case "$OS" in Linux|Darwin) ;; *) die "unsupported OS $OS (use flash_sdcard.ps1 on Windows)" ;; esac
if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
  die "--write needs root: sudo $0 --write ..."
fi

# ---- python ----
PY=""
for c in "$ROOT/.venv/bin/python" python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[[ -n "$PY" ]] || die "Python >= 3.8 not found"

WORK="${WORK:-$HERE/out}"
IMAGE_DIR="${IMAGE_DIR:-$WORK/images}"
STAGE="$WORK/stage"
mkdir -p "$WORK" "$IMAGE_DIR"
LOG="$WORK/flash-$(date +%Y%m%d-%H%M%S).log"
: >"$LOG"

# log(): the ONLY thing written to the log file. Never pass a secret to it.
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; }
tool() { "$PY" "$TOOL" "$@"; }
jget() { "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); v=eval(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }

MNT=""
cleanup() {
  local rc=$?
  if [[ -n "$MNT" && -d "$MNT" ]]; then
    if mount | grep -q " $MNT "; then umount "$MNT" 2>/dev/null || true; fi
    rmdir "$MNT" 2>/dev/null || true
  fi
  if [[ $KEEP_STAGE -eq 0 && -d "$STAGE" ]]; then
    tool scrub-stage --stage "$STAGE" >/dev/null 2>&1 || true
  fi
  unset FD_PW1 FD_PW2 FD_WIFI1 FD_WIFI2 PARAMS 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT
trap 'echo; echo "interrupted" >&2; exit 130' INT TERM

if [[ $DRY_RUN -eq 1 ]]; then
  log "mode: DRY RUN (nothing is written to any device; use --write with sudo to write)"
else
  log "mode: WRITE"
fi
log "project=$ROOT python=$PY work=$WORK"

# ---------------------------------------------------------------------------------------------
# 1. questions
# ---------------------------------------------------------------------------------------------
ask() {  # ask VAR "prompt" default
  local __v; read -r -p "$2 [$3]: " __v || true
  printf -v "$1" '%s' "${__v:-$3}"
}
yesno() {  # yesno "prompt" default(y/n) -> 0 for yes
  local a; read -r -p "$1 [$( [[ $2 == y ]] && echo Y/n || echo y/N )]: " a || true
  a="${a:-$2}"; [[ "$a" == [yY]* ]]
}
has_ctrl() { local LC_ALL=C; [[ "$1" =~ [[:cntrl:]] ]]; }
json_str() {  # JSON string literal (rejects control characters)
  local s="$1"
  if has_ctrl "$s"; then die "control characters are not allowed"; fi
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

echo
echo "== facedoor SD card settings =="
ask HOSTNAME_ "Hostname" "facedoor-pi"
ask USERNAME_ "Login user (not 'ubuntu'/'root')" "facedoor"
while :; do
  read -r -s -p "Password for $USERNAME_ (min 8 chars, hidden): " FD_PW1; echo
  read -r -s -p "Repeat password: " FD_PW2; echo
  if [[ "$FD_PW1" != "$FD_PW2" ]]; then echo "  passwords differ, try again"; continue; fi
  if [[ ${#FD_PW1} -lt 8 ]]; then echo "  too short"; continue; fi
  break
done
unset FD_PW2

SSH_KEY=""
DEFAULT_PUB=""
USER_HOME="$HOME"
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" =~ ^[A-Za-z0-9._-]+$ ]]; then
  USER_HOME="$(eval echo "~$SUDO_USER")"
fi
for k in "$USER_HOME/.ssh/id_ed25519.pub" "$USER_HOME/.ssh/id_ecdsa.pub" "$USER_HOME/.ssh/id_rsa.pub"; do
  if [[ -f "$k" ]]; then DEFAULT_PUB="$k"; break; fi
done
while :; do
  ask PUB "SSH PUBLIC key file (.pub), '-' for none" "${DEFAULT_PUB:--}"
  [[ "$PUB" == "-" ]] && break
  PUB="${PUB/#\~/$USER_HOME}"
  if [[ ! -f "$PUB" ]]; then echo "  not found: $PUB"; continue; fi
  if grep -q "PRIVATE KEY" "$PUB"; then echo "  that is a PRIVATE key. Never copy it; choose the .pub file"; continue; fi
  SSH_KEY="$(grep -E '^(ssh-|ecdsa-|sk-)' "$PUB" | head -n1 || true)"
  if [[ -z "$SSH_KEY" ]]; then echo "  no 'ssh-...' public key line in $PUB"; continue; fi
  break
done
if [[ -z "$SSH_KEY" ]]; then echo "  (no key: SSH password login will be enabled)"; fi

WIFI_JSON="null"
if yesno "Configure Wi-Fi?" n; then
  while :; do read -r -p "  SSID: " SSID; [[ -n "$SSID" ]] && break; done
  while :; do
    read -r -s -p "  Wi-Fi passphrase (8-63 chars, empty = open network, hidden): " FD_WIFI1; echo
    if [[ -z "$FD_WIFI1" ]]; then
      if yesno "  open network without password?" n; then break; else continue; fi
    fi
    read -r -s -p "  repeat passphrase: " FD_WIFI2; echo
    [[ "$FD_WIFI1" == "$FD_WIFI2" ]] && break
    echo "  passphrases differ"
  done
  unset FD_WIFI2
  ask COUNTRY "  Wi-Fi country code" "KR"
  HIDDEN=false; yesno "  hidden SSID?" n && HIDDEN=true
  if [[ -z "$FD_WIFI1" ]]; then
    WIFI_JSON="{\"ssid\": $(json_str "$SSID"), \"open\": true, \"country\": $(json_str "$COUNTRY"), \"hidden\": $HIDDEN}"
  else
    WIFI_JSON="{\"ssid\": $(json_str "$SSID"), \"passphrase\": $(json_str "$FD_WIFI1"), \"country\": $(json_str "$COUNTRY"), \"hidden\": $HIDDEN}"
  fi
  unset FD_WIFI1
fi
ask TZ_ "Timezone" "Asia/Seoul"
ENABLE_SERVICE=false
yesno "Enable facedoor.service at boot? (say no until the wiring is checked)" n && ENABLE_SERVICE=true
MODELS=true
yesno "Download face models on first boot?" y || MODELS=false

KEYS_JSON="[]"
[[ -n "$SSH_KEY" ]] && KEYS_JSON="[$(json_str "$SSH_KEY")]"
PARAMS="{\"hostname\": $(json_str "$HOSTNAME_"), \"username\": $(json_str "$USERNAME_"), \"password\": $(json_str "$FD_PW1"), \"timezone\": $(json_str "$TZ_"), \"ssh_public_keys\": $KEYS_JSON, \"wifi\": $WIFI_JSON, \"enable_service\": $ENABLE_SERVICE, \"download_models\": $MODELS}"
unset FD_PW1

# ---------------------------------------------------------------------------------------------
# 2. stage (secrets travel on stdin only; printf is a shell builtin, so no process argv exposure)
# ---------------------------------------------------------------------------------------------
log "staging cloud-init files and project bundle -> $STAGE"
rm -rf "$STAGE"
if ! printf '%s' "$PARAMS" | tool --result "$WORK/stage-result.json" stage --out "$STAGE"; then
  unset PARAMS
  die "staging failed (see message above)"
fi
unset PARAMS
log "staged: $(jget "$WORK/stage-result.json" '", ".join(sorted(d["files"]))')"
log "bundle: $(jget "$WORK/stage-result.json" '"%d files, %.1f MB, sha256 %s..." % (d["bundle"]["files"], d["bundle"]["size"] / 1e6, d["bundle"]["sha256"][:16])')"
log "settings: $(jget "$WORK/stage-result.json" 'json.dumps(d["settings"])')"

# ---------------------------------------------------------------------------------------------
# 3. image
# ---------------------------------------------------------------------------------------------
IMAGE_SIZE=""
if [[ -n "$IMAGE" ]]; then
  [[ -f "$IMAGE" ]] || die "image not found: $IMAGE"
  vargs=(--image "$IMAGE")
  [[ -n "$SHA256" ]] && vargs+=(--sha256 "$SHA256")
  [[ -n "$SUMS_FILE" ]] && vargs+=(--sums-file "$SUMS_FILE")
  log "verifying $IMAGE"
  tool --result "$WORK/image.json" verify-image "${vargs[@]}" || die "image verification failed"
  IMAGE_SIZE="$(jget "$WORK/image.json" 'd.get("uncompressed_size")')"
elif [[ $DRY_RUN -eq 1 && $FETCH -eq 0 ]]; then
  log "resolving the latest Ubuntu 24.04 raspi image (no download in dry-run; add --fetch to download)"
  if tool --result "$WORK/image.json" resolve-image; then
    log "would download: $(jget "$WORK/image.json" 'd["url"]') sha256 $(jget "$WORK/image.json" 'd["sha256"]')"
  else
    log "WARNING: could not resolve the image (offline?); continuing the dry run"
  fi
else
  log "downloading + verifying the image into $IMAGE_DIR (resumable)"
  tool --result "$WORK/image.json" fetch-image --dir "$IMAGE_DIR" || die "image download/verify failed"
  IMAGE="$(jget "$WORK/image.json" 'd["path"]')"
  IMAGE_SIZE="$(jget "$WORK/image.json" 'd.get("uncompressed_size")')"
fi
[[ -n "$IMAGE" ]] && log "image: $IMAGE (uncompressed ${IMAGE_SIZE:-?} bytes)"

# ---------------------------------------------------------------------------------------------
# 4. device selection
# ---------------------------------------------------------------------------------------------
limit_args=(--protect "$WORK" --protect "$ROOT" --protect "$IMAGE_DIR")
[[ -n "$IMAGE" ]] && limit_args+=(--protect "$IMAGE")
[[ -n "$IMAGE_SIZE" ]] && limit_args+=(--image-size "$IMAGE_SIZE")
[[ -n "$MAX_GB" ]] && limit_args+=(--max-size-gb "$MAX_GB")

tool --result "$WORK/devices.json" list-devices "${limit_args[@]}" || die "device listing failed"
echo
echo "== disks (only entries marked OK can be selected) =="
"$PY" - "$WORK/devices.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for dev in d["devices"]:
    a = dev["assessment"]
    tag = "OK      " if a["ok"] else "REFUSED "
    print(f"{tag} {dev['id']:<10} {dev['path']:<16} {dev['size']/1e9:8.1f} GB  bus={dev.get('bus') or '?':<5} "
          f"removable={'yes' if dev.get('removable') else 'no ':<3}  {dev.get('model')}")
    for p in dev.get("partitions") or []:
        print(f"           - {p.get('name')}: {(p.get('size') or 0)/1e9:.2f} GB {p.get('fstype') or ''} "
              f"label={p.get('label') or '-'} mounted={p.get('mountpoint') or '-'}")
    for r in a["reasons"]:
        print(f"           x {r}")
    for w in a["warnings"]:
        print(f"           ! {w}")
PYEOF
OK_IDS=()
while IFS= read -r x; do
  if [[ -n "$x" ]]; then OK_IDS+=("$x"); fi
done < <(jget "$WORK/devices.json" '"\n".join(x["id"] for x in d["devices"] if x["assessment"]["ok"])')
if [[ ${#OK_IDS[@]} -eq 0 ]]; then
  log "no eligible SD card found (insert the card via a USB/SD reader and re-run)"
  [[ $DRY_RUN -eq 1 ]] && { log "DRY RUN finished: staging and checks OK, nothing written"; exit 0; }
  exit 6
fi

echo
DEV_ID=""
while :; do
  read -r -p "Type the device NAME exactly as listed (e.g. ${OK_IDS[0]}), or 'q' to quit: " DEV_ID
  [[ "$DEV_ID" == q ]] && { log "aborted by user"; exit 1; }
  for x in "${OK_IDS[@]}"; do [[ "$x" == "$DEV_ID" ]] && break 2; done
  echo "  '$DEV_ID' is not an eligible device"
done
DEV_PATH="$(jget "$WORK/devices.json" "next(x['path'] for x in d['devices'] if x['id']=='$DEV_ID')")"
DEV_SIZE="$(jget "$WORK/devices.json" "next(x['size'] for x in d['devices'] if x['id']=='$DEV_ID')")"
DEV_MODEL="$(jget "$WORK/devices.json" "next(x['model'] for x in d['devices'] if x['id']=='$DEV_ID')")"
log "selected $DEV_ID ($DEV_PATH, $DEV_MODEL, $DEV_SIZE bytes)"

if [[ $DRY_RUN -eq 1 ]]; then
  cat <<EOF

DRY RUN - this is what --write would do (nothing was written):
  1. re-check $DEV_PATH ($DEV_MODEL, $DEV_SIZE bytes) and ask you to type ERASE
  2. unmount every partition of $DEV_PATH
  3. write ${IMAGE:-<downloaded image>} -> $DEV_PATH, partition table last, then read back + sha256 ($VERIFY)
  4. mount the 'system-boot' partition and copy + sha256-verify:
     user-data, network-config, meta-data, facedoor-firstboot.sh, facedoor-bundle.tar.gz(.sha256)
  5. unmount / eject. On the Pi, first boot runs cloud-init, then facedoor-firstboot installs to /opt/facedoor
     (log: /var/log/facedoor-firstboot.log). GPIO outputs are not touched by the installer.
Real run:  sudo $0 --write${IMAGE:+ --image $IMAGE}
EOF
  log "DRY RUN finished"
  exit 0
fi

# ---------------------------------------------------------------------------------------------
# 5. final confirmation + write
# ---------------------------------------------------------------------------------------------
[[ -n "$IMAGE" ]] || die "no image"
echo
echo "!!! ALL DATA on $DEV_PATH ($DEV_MODEL, $(( DEV_SIZE / 1000000000 )) GB) WILL BE ERASED !!!"
read -r -p "Type ERASE (upper case) to continue, anything else aborts: " CONFIRM
[[ "$CONFIRM" == "ERASE" ]] || { log "aborted at final confirmation"; exit 1; }

log "re-checking $DEV_PATH immediately before writing"
tool --result "$WORK/check.json" check-device --target "$DEV_PATH" --expect-size "$DEV_SIZE" "${limit_args[@]}" \
  || die "device re-check failed: NOT writing"

if [[ "$OS" == Linux ]]; then
  while read -r name mp; do
    if [[ -n "${mp:-}" ]]; then
      log "unmounting $name ($mp)"
      umount "$name" || die "cannot unmount $name"
    fi
  done < <(lsblk -lnp -o NAME,MOUNTPOINT "$DEV_PATH")
  WRITE_TARGET="$DEV_PATH"
else
  diskutil unmountDisk "/dev/$DEV_ID" || die "cannot unmount /dev/$DEV_ID"
  WRITE_TARGET="/dev/r$DEV_ID"
fi

log "writing $IMAGE -> $WRITE_TARGET"
tool --result "$WORK/write.json" write --image "$IMAGE" --target "$WRITE_TARGET" \
  --expect-target-size "$DEV_SIZE" --verify "$VERIFY" || die "WRITE FAILED (card may be unusable; re-run)"
log "write + read-back verify OK: $(jget "$WORK/write.json" 'd["written_bytes"]') bytes"
sync

# ---------------------------------------------------------------------------------------------
# 6. boot partition files
# ---------------------------------------------------------------------------------------------
if [[ "$OS" == Linux ]]; then
  blockdev --rereadpt "$DEV_PATH" 2>/dev/null || partprobe "$DEV_PATH" 2>/dev/null || true
  command -v udevadm >/dev/null && udevadm settle || sleep 3
  BOOT_PART="$(lsblk -lnp -o NAME,LABEL,FSTYPE "$DEV_PATH" | awk '$2=="system-boot" && $3=="vfat"{print $1; exit}')"
  [[ -n "$BOOT_PART" ]] || BOOT_PART="$(lsblk -lnp -o NAME,TYPE,FSTYPE "$DEV_PATH" | awk '$2=="part" && $3=="vfat"{print $1; exit}')"
  [[ -n "$BOOT_PART" ]] || die "boot partition (vfat 'system-boot') not found on $DEV_PATH"
  # udisks may auto-mount it: unmount first
  if lsblk -lnp -o MOUNTPOINT "$BOOT_PART" | grep -q .; then umount "$BOOT_PART" || true; fi
  MNT="$(mktemp -d)"
  mount -t vfat "$BOOT_PART" "$MNT" || die "cannot mount $BOOT_PART"
  BOOT_DIR="$MNT"
else
  diskutil mountDisk "/dev/$DEV_ID" >/dev/null || true
  sleep 2
  BOOT_DIR="/Volumes/system-boot"
  [[ -d "$BOOT_DIR" ]] || die "boot partition not mounted at $BOOT_DIR"
fi
log "copying staged files to $BOOT_DIR"
tool --result "$WORK/copy.json" copy-to-boot --stage "$STAGE" --boot "$BOOT_DIR" || die "copy to boot partition failed"
sync
if [[ "$OS" == Linux ]]; then
  umount "$MNT" && rmdir "$MNT" && MNT=""
else
  diskutil eject "/dev/$DEV_ID" || true
fi
log "DONE. Remove the card, insert it into the Pi 5 and power on."
log "First boot: cloud-init (user, SSH, network), then facedoor-firstboot (log /var/log/facedoor-firstboot.log)."
log "Then: ssh $USERNAME_@$HOSTNAME_.local (or the IP), see docs/PORTING.md section 8."
