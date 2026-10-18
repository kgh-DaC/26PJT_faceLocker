"""facedoor SD-card helper (stdlib only, Python >= 3.8).

Used by deploy/sdcard/flash_sdcard.ps1 (Windows) and flash_sdcard.sh (Linux/macOS).
All heavy or security-sensitive logic lives here so it can be unit-tested without a real SD card.

Subcommands
    hash-password      read password from stdin -> SHA-512 crypt ($6$) hash (cloud-init `passwd`)
    stage              read params JSON from stdin (or --params-from-env) -> render cloud-init files,
                       copy first-boot script, build project bundle + sha256 into --out DIR
    bundle             build only the project bundle (for inspection)
    resolve-image      fetch SHA256SUMS and pick the latest Ubuntu 24.04 preinstalled-server arm64+raspi image
    fetch-image        download (resumable .part) and verify the image against SHA256SUMS
    verify-image       sha256-verify a local image against SHA256SUMS (url/file) or an explicit hash
    image-info         report uncompressed size of .img / .img.xz (parses the xz index, no decompression)
    write              stream-decompress the image to a block device (or a file in tests) and verify by read-back
    device-size        print the byte length of a block device
    list-devices       list whole disks with a safety assessment (only removable USB/SD cards can pass)
    check-device       re-collect and assess ONE device right before writing (exit 6 if refused)
    assess-devices     assess normalised device JSON from stdin (pure logic; used by tests/wrappers)
    copy-to-boot       copy staged files to the mounted boot partition and sha256-verify the copies
    scrub-stage        delete the secret-bearing staged files (user-data, network-config, meta-data)

Secrets never appear on the command line: passwords/passphrases come from stdin JSON or env.
Exit codes: 0 ok, 2 usage/validation error, 3 checksum mismatch, 4 write/verify failure, 5 network error,
6 device refused by the safety checks.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import gzip
import hashlib
import io
import json
import ntpath
import os
import plistlib
import posixpath
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
PROJECT_ROOT_DEFAULT = HERE.parents[1]
TEMPLATES_DIR = HERE / "templates"
FIRSTBOOT_SCRIPT = HERE / "firstboot" / "facedoor-firstboot.sh"

DEFAULT_IMAGE_BASE = "https://cdimage.ubuntu.com/releases/24.04/release/"
IMAGE_NAME_RE = re.compile(r"^ubuntu-24\.04(?:\.(\d+))?-preinstalled-server-arm64\+raspi\.img\.xz$")

BUNDLE_NAME = "facedoor-bundle.tar.gz"
FIRSTBOOT_NAME = "facedoor-firstboot.sh"
INSTALL_DIR_DEFAULT = "/opt/facedoor"
BUNDLE_MAX_BYTES = 150 * 1024 * 1024  # boot partition (~512 MB) also holds firmware/kernel

EXIT_USAGE = 2
EXIT_CHECKSUM = 3
EXIT_WRITE = 4
EXIT_NETWORK = 5
EXIT_DEVICE = 6

SECTOR = 512
WRITE_BLOCK = 4 * 1024 * 1024
HEAD_BYTES = 1024 * 1024  # partition table region written LAST (Windows must not see partitions mid-write)


class ToolError(Exception):
    def __init__(self, msg: str, code: int = EXIT_USAGE):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------------------------
# SHA-512 crypt ($6$), Ulrich Drepper spec. Python's `crypt` module is unavailable on Windows
# (and removed in 3.13), so it is implemented here and checked against the spec test vectors.
# --------------------------------------------------------------------------------------------
_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SHA512_ORDER = [
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26), (6, 27, 48),
    (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13),
    (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41),
]


def _b64_from_24bit(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_ITOA64[w & 0x3F])
        w >>= 6
    return "".join(out)


def sha512_crypt(password: str, salt: Optional[str] = None, rounds: Optional[int] = 100_000) -> str:
    """Return a glibc-compatible `$6$` hash. rounds=None -> default 5000 without `rounds=` prefix."""
    key = password.encode("utf-8")
    if salt is None:
        salt = "".join(secrets.choice(_ITOA64) for _ in range(16))
    salt_b = salt.encode("ascii")[:16]
    custom = rounds is not None
    r = 5000 if rounds is None else max(1000, min(999_999_999, int(rounds)))

    b = hashlib.sha512(key + salt_b + key).digest()
    a = hashlib.sha512(key + salt_b)
    n = len(key)
    while n > 64:
        a.update(b)
        n -= 64
    a.update(b[:n])
    n = len(key)
    while n > 0:
        a.update(b if n & 1 else key)
        n >>= 1
    a_d = a.digest()

    dp = hashlib.sha512(key * len(key)).digest()
    p = (dp * (len(key) // 64 + 1))[: len(key)]
    ds = hashlib.sha512(salt_b * (16 + a_d[0])).digest()
    s = (ds * (len(salt_b) // 64 + 1))[: len(salt_b)]

    c = a_d
    for i in range(r):
        h = hashlib.sha512()
        h.update(p if i & 1 else c)
        if i % 3:
            h.update(s)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()

    enc = "".join(_b64_from_24bit(c[x], c[y], c[z], 4) for x, y, z in _SHA512_ORDER)
    enc += _b64_from_24bit(0, 0, c[63], 2)
    prefix = f"$6$rounds={r}$" if custom else "$6$"
    return f"{prefix}{salt_b.decode('ascii')}${enc}"


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------
HOSTNAME_RE = re.compile(r"^(?=.{1,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
TIMEZONE_RE = re.compile(r"^(?:UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){1,2})$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}\.UTF-8$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
SSH_KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+(?: [^\r\n]{0,200})?$"
)
INSTALL_DIR_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
RESERVED_USERS = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy",
    "www-data", "backup", "list", "irc", "nobody", "systemd-network", "systemd-resolve", "messagebus",
    "syslog", "sshd", "ubuntu", "admin", "sudo", "gpio", "video", "dialout", "adm", "users", "plugdev",
}


def ssh_key_ok(k: str) -> bool:
    """One-line OpenSSH public key whose base64 blob really encodes the declared key type."""
    if not SSH_KEY_RE.match(k):
        return False
    typ, blob = k.split(" ")[:2]
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError):
        return False
    if len(raw) < 4:
        return False
    n = struct.unpack(">I", raw[:4])[0]
    return raw[4:4 + n] == typ.encode("ascii") and len(raw) >= 4 + n + 20


def validate_params(p: Dict) -> Dict:
    """Normalise and validate stage params. Raises ToolError with a user-facing message."""
    out = dict(p)
    out["hostname"] = str(p.get("hostname", "facedoor-pi")).strip().lower()
    if not HOSTNAME_RE.match(out["hostname"]):
        raise ToolError(f"invalid hostname {out['hostname']!r}: a-z 0-9 -, 1-63 chars, no leading/trailing '-'")
    out["username"] = str(p.get("username", "facedoor")).strip()
    if not USERNAME_RE.match(out["username"]) or out["username"] in RESERVED_USERS:
        raise ToolError(f"invalid or reserved username {out['username']!r}: ^[a-z_][a-z0-9_-]{{0,31}}$")
    out["timezone"] = str(p.get("timezone", "Asia/Seoul")).strip()
    if not TIMEZONE_RE.match(out["timezone"]):
        raise ToolError(f"invalid timezone {out['timezone']!r} (e.g. Asia/Seoul)")
    out["locale"] = str(p.get("locale", "en_US.UTF-8")).strip()
    if not LOCALE_RE.match(out["locale"]):
        raise ToolError(f"invalid locale {out['locale']!r} (e.g. en_US.UTF-8, ko_KR.UTF-8)")
    out["install_dir"] = str(p.get("install_dir", INSTALL_DIR_DEFAULT)).strip()
    if not INSTALL_DIR_RE.match(out["install_dir"]) or out["install_dir"] in ("/", "/usr", "/etc", "/boot", "/home"):
        raise ToolError(f"invalid install_dir {out['install_dir']!r}")

    keys = p.get("ssh_public_keys") or []
    if isinstance(keys, str):
        keys = [keys]
    clean_keys: List[str] = []
    for k in keys:
        k = " ".join(str(k).strip().split())
        if not k:
            continue
        if not ssh_key_ok(k):
            raise ToolError("invalid SSH public key (expected one line like 'ssh-ed25519 AAAA... comment'); "
                            "did you pass the PRIVATE key? never copy the private key")
        clean_keys.append(k)
    out["ssh_public_keys"] = clean_keys

    # password: plaintext (hashed here, then dropped) or a ready-made hash
    pw_hash = p.get("password_hash")
    pw = p.get("password")
    if pw:
        if len(pw) < 8:
            raise ToolError("password must be at least 8 characters")
        if any(ch in pw for ch in "\r\n\x00"):
            raise ToolError("password must not contain newline/NUL")
        pw_hash = sha512_crypt(pw)
    if not pw_hash or not re.match(r"^\$(6|5|y|2[aby])\$[^\s:]+$", str(pw_hash)):
        raise ToolError("password or password_hash ($6$...) is required")
    out["password_hash"] = str(pw_hash)
    out.pop("password", None)

    wifi = p.get("wifi")
    if wifi:
        ssid = str(wifi.get("ssid", ""))
        if not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 0x20 for c in ssid):
            raise ToolError("invalid Wi-Fi SSID (1-32 bytes, no control characters)")
        passphrase = wifi.get("passphrase")
        psk_hex = wifi.get("psk_hex")
        country = str(wifi.get("country", "KR")).upper()
        if not COUNTRY_RE.match(country):
            raise ToolError(f"invalid Wi-Fi country code {country!r} (ISO 3166 alpha-2, e.g. KR)")
        plaintext = bool(wifi.get("plaintext", False))
        open_net = bool(wifi.get("open", False))
        secret = None
        if not open_net:
            if passphrase is not None and passphrase != "":
                if not (8 <= len(passphrase) <= 63) or any(not (0x20 <= ord(c) <= 0x7E) for c in passphrase):
                    raise ToolError("WPA2 passphrase must be 8-63 printable ASCII characters")
                secret = passphrase if plaintext else wpa_psk_hex(ssid, passphrase)
            elif psk_hex:
                if not re.fullmatch(r"[0-9a-fA-F]{64}", str(psk_hex)):
                    raise ToolError("psk_hex must be 64 hex characters")
                secret = str(psk_hex).lower()
            else:
                raise ToolError("Wi-Fi passphrase is required (or set open=true for an open network)")
        out["wifi"] = {"ssid": ssid, "secret": secret, "country": country,
                       "hidden": bool(wifi.get("hidden", False)), "plaintext": plaintext}
    else:
        out["wifi"] = None

    out["enable_service"] = bool(p.get("enable_service", False))
    out["download_models"] = bool(p.get("download_models", True))
    out["set_pi_mode"] = bool(p.get("set_pi_mode", True))
    out["include_models"] = bool(p.get("include_models", False))
    out["include_tests"] = bool(p.get("include_tests", True))
    return out


def wpa_psk_hex(ssid: str, passphrase: str) -> str:
    """WPA2-PSK 256-bit key = PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 32). netplan accepts 64-hex PSKs."""
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode("ascii"), ssid.encode("utf-8"), 4096, 32).hex()


# --------------------------------------------------------------------------------------------
# Template rendering
# --------------------------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def yq(s: str) -> str:
    """Quote a string as a YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(s, ensure_ascii=True)


def render_template(text: str, values: Dict[str, str]) -> str:
    missing = sorted({m for m in PLACEHOLDER_RE.findall(text) if m not in values})
    if missing:
        raise ToolError(f"template placeholders without value: {missing}")
    out = PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)
    return out.replace("\r\n", "\n")


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join((pad + ln) if ln else ln for ln in text.splitlines())


def build_env_file(p: Dict) -> str:
    def b(v: bool) -> str:
        return "1" if v else "0"

    lines = [
        "# facedoor first-boot settings (generated by deploy/sdcard). Parsed (not sourced) by facedoor-firstboot.sh",
        f"FACEDOOR_USER={p['username']}",
        f"FACEDOOR_DIR={p['install_dir']}",
        f"FACEDOOR_BUNDLE=/boot/firmware/{BUNDLE_NAME}",
        f"FACEDOOR_ENABLE_SERVICE={b(p['enable_service'])}",
        f"FACEDOOR_DOWNLOAD_MODELS={b(p['download_models'])}",
        f"FACEDOOR_SET_PI_MODE={b(p['set_pi_mode'])}",
        "FACEDOOR_NET_TIMEOUT=900",
    ]
    return "\n".join(lines) + "\n"


def render_user_data(p: Dict, generated_at: Optional[str] = None) -> str:
    tpl = (TEMPLATES_DIR / "user-data.tpl").read_text(encoding="utf-8")
    keys = p["ssh_public_keys"]
    if keys:
        ssh_block = "    ssh_authorized_keys:\n" + "\n".join(f"      - {yq(k)}" for k in keys)
    else:
        ssh_block = "    # ssh_authorized_keys: (none given; password login over SSH is enabled)"
    values = {
        "GENERATED_AT": generated_at or _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "HOSTNAME": yq(p["hostname"]),
        "USERNAME": yq(p["username"]),
        "PASSWORD_HASH": yq(p["password_hash"]),
        "TIMEZONE": yq(p["timezone"]),
        "LOCALE": yq(p["locale"]),
        # without a key, password SSH login is the only way in -> keep it on
        "SSH_PWAUTH": "false" if keys else "true",
        "SSH_KEYS_BLOCK": ssh_block,
        "ENV_CONTENT": _indent(build_env_file(p).rstrip("\n"), 6),
        "FIRSTBOOT_NAME": FIRSTBOOT_NAME,
    }
    return render_template(tpl, values)


def render_network_config(p: Dict) -> str:
    tpl = (TEMPLATES_DIR / "network-config.tpl").read_text(encoding="utf-8")
    w = p.get("wifi")
    if w:
        ap = [f"        {yq(w['ssid'])}:"]
        if w["secret"] is not None:
            ap.append(f"          password: {yq(w['secret'])}")
        if w["hidden"]:
            ap.append("          hidden: true")
        if len(ap) == 1:
            ap[0] = ap[0] + " {}"
        wifi_block = "\n".join([
            "  wifis:",
            "    wlan0:",
            "      dhcp4: true",
            "      optional: true",
            f"      regulatory-domain: {yq(w['country'])}",
            "      access-points:",
            *ap,
        ])
    else:
        wifi_block = "  # wifis: (none configured; wired Ethernet DHCP only)"
    return render_template(tpl, {"WIFI_BLOCK": wifi_block})


def render_meta_data(p: Dict, generated_at: str) -> str:
    """NoCloud meta-data with an instance-id unique per staging run (so cloud-init re-applies user-data)."""
    tpl = (TEMPLATES_DIR / "meta-data.tpl").read_text(encoding="utf-8")
    tag = hashlib.sha256(f"{p['hostname']}|{generated_at}".encode("utf-8")).hexdigest()[:12]
    return render_template(tpl, {"INSTANCE_ID": f"facedoor-{p['hostname']}-{tag}", "HOSTNAME": p["hostname"]})


# --------------------------------------------------------------------------------------------
# Project bundle
# --------------------------------------------------------------------------------------------
BUNDLE_TOP_DIRS = ("src", "configs", "scripts", "deploy", "docs", "tests")
BUNDLE_TOP_FILES = ("requirements-pi.txt", "requirements.txt", "pyproject.toml", "README.md", "LICENSE")
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".venv", "venv", "data", "logs",
    "reports", "node_modules", ".idea", ".vscode", "build", "dist", "out", "_staging",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".npy", ".npz", ".pkl", ".part", ".img", ".xz", ".iso", ".zip",
                    ".tar", ".gz", ".bak", ".tmp"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".mp4", ".avi", ".mov",
                  ".mkv"}
PUBLIC_ASSET_DIRS = ("tests/assets",)  # public OpenCV sample images used by tests
EXCLUDE_REL_PATHS = {"configs/local.yaml", "deploy/sdcard/out"}
MAX_FILE_BYTES = 20 * 1024 * 1024


def _bundle_decision(rel: str, size: int, include_models: bool, include_tests: bool) -> Tuple[bool, str]:
    parts = rel.split("/")
    if any(x in EXCLUDE_DIR_NAMES for x in parts[:-1]):
        return False, "excluded directory"
    if rel in EXCLUDE_REL_PATHS or any(rel.startswith(x + "/") for x in EXCLUDE_REL_PATHS):
        return False, "local/private path"
    if parts[0] == "tests" and not include_tests:
        return False, "tests disabled"
    name = parts[-1]
    suffix = os.path.splitext(name)[1].lower()
    if name.startswith(".env") or name.endswith((".pem", ".key")) or name in ("id_rsa", "id_ed25519"):
        return False, "secret-like file"
    if suffix in EXCLUDE_SUFFIXES:
        return False, f"excluded type {suffix}"
    if suffix in IMAGE_SUFFIXES and not any(rel.startswith(d + "/") for d in PUBLIC_ASSET_DIRS):
        return False, "image/video (possible personal data)"
    if parts[0] == "models":
        if name == "models.lock.json":
            return True, ""
        if suffix == ".onnx" and include_models:
            return True, ""
        return False, "models (downloaded on first boot)" if suffix == ".onnx" else "model artefact"
    if size > MAX_FILE_BYTES:
        return False, f"too large ({size} bytes)"
    return True, ""


def collect_bundle_files(root: Path, include_models: bool = False, include_tests: bool = True
                         ) -> Tuple[List[Tuple[str, Path]], List[Tuple[str, str]]]:
    """Return (included [(relpath, path)], excluded [(relpath, reason)]) sorted by relpath."""
    root = Path(root)
    candidates: List[Tuple[str, Path]] = []
    for top in BUNDLE_TOP_DIRS:
        d = root / top
        if d.is_dir():
            for dirpath, dirnames, filenames in os.walk(d):
                dirnames[:] = sorted(x for x in dirnames if x not in EXCLUDE_DIR_NAMES)
                for fn in filenames:
                    fp = Path(dirpath) / fn
                    candidates.append((fp.relative_to(root).as_posix(), fp))
    for f in BUNDLE_TOP_FILES:
        if (root / f).is_file():
            candidates.append((f, root / f))
    md = root / "models"
    if md.is_dir():
        for fp in md.iterdir():
            if fp.is_file():
                candidates.append((f"models/{fp.name}", fp))

    inc: List[Tuple[str, Path]] = []
    exc: List[Tuple[str, str]] = []
    for rel, fp in sorted(candidates):
        if fp.is_symlink():
            exc.append((rel, "symlink"))
            continue
        ok, why = _bundle_decision(rel, fp.stat().st_size, include_models, include_tests)
        (inc.append((rel, fp)) if ok else exc.append((rel, why)))
    return inc, exc


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def build_bundle(root: Path, out_path: Path, include_models: bool = False, include_tests: bool = True,
                 mtime: int = 0) -> Dict:
    """Deterministic tar.gz with prefix `facedoor/` and an embedded MANIFEST.sha256."""
    inc, exc = collect_bundle_files(root, include_models, include_tests)
    if not any(rel.startswith("src/") for rel, _ in inc):
        raise ToolError(f"project root {root} has no src/ - wrong directory?")
    manifest_lines = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    total = 0
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=mtime, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                def add_bytes(name: str, data: bytes, mode: int = 0o644) -> None:
                    ti = tarfile.TarInfo(name)
                    ti.size, ti.mode, ti.mtime = len(data), mode, mtime
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    tar.addfile(ti, io.BytesIO(data))

                for rel, fp in inc:
                    data = fp.read_bytes()
                    if rel.endswith(".sh"):
                        data = data.replace(b"\r\n", b"\n")
                    total += len(data)
                    mode = 0o755 if rel.endswith(".sh") or (rel.startswith("scripts/") and data[:2] == b"#!") else 0o644
                    add_bytes(f"facedoor/{rel}", data, mode)
                    manifest_lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
                add_bytes("facedoor/MANIFEST.sha256", ("\n".join(manifest_lines) + "\n").encode("utf-8"))
    os.replace(tmp, out_path)
    size = out_path.stat().st_size
    if size > BUNDLE_MAX_BYTES:
        raise ToolError(f"bundle too large for the boot partition: {size} bytes (> {BUNDLE_MAX_BYTES})")
    digest = sha256_file(out_path)
    (out_path.parent / (out_path.name + ".sha256")).write_text(f"{digest}  {out_path.name}\n", encoding="ascii",
                                                               newline="\n")
    return {"path": str(out_path), "sha256": digest, "size": size, "files": len(inc), "raw_bytes": total,
            "excluded": [{"path": r, "reason": w} for r, w in exc]}


# --------------------------------------------------------------------------------------------
# Stage: everything that goes onto the boot partition
# --------------------------------------------------------------------------------------------
def stage(params: Dict, out_dir: Path, project_root: Path, generated_at: Optional[str] = None) -> Dict:
    p = validate_params(params)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}

    def write(name: str, text: str) -> None:
        path = out_dir / name
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        files[name] = sha256_file(path)

    ts = generated_at or _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    write("user-data", render_user_data(p, ts))
    write("network-config", render_network_config(p))
    write("meta-data", render_meta_data(p, ts))
    write(FIRSTBOOT_NAME, FIRSTBOOT_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n"))
    b = build_bundle(project_root, out_dir / BUNDLE_NAME, p["include_models"], p["include_tests"])
    files[BUNDLE_NAME] = b["sha256"]
    files[BUNDLE_NAME + ".sha256"] = sha256_file(out_dir / (BUNDLE_NAME + ".sha256"))
    summary = {
        "out_dir": str(out_dir),
        "files": files,  # name -> sha256 (used to verify the copies on the SD card)
        "bundle": {k: b[k] for k in ("sha256", "size", "files", "raw_bytes")},
        "bundle_excluded": b["excluded"],
        "settings": {  # no secrets here
            "hostname": p["hostname"], "username": p["username"], "timezone": p["timezone"],
            "locale": p["locale"], "install_dir": p["install_dir"], "ssh_keys": len(p["ssh_public_keys"]),
            "ssh_password_auth": not p["ssh_public_keys"],
            "wifi_ssid": p["wifi"]["ssid"] if p["wifi"] else None,
            "wifi_secret_form": (None if not p["wifi"] else "open" if p["wifi"]["secret"] is None
                                 else "plaintext" if p["wifi"]["plaintext"] else "psk-hex"),
            "wifi_country": p["wifi"]["country"] if p["wifi"] else None,
            "enable_service": p["enable_service"], "download_models": p["download_models"],
            "set_pi_mode": p["set_pi_mode"], "include_models": p["include_models"],
        },
    }
    with open(out_dir / "stage-summary.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def params_from_env() -> Dict:
    e = os.environ.get
    p: Dict = {
        "hostname": e("FD_HOSTNAME", "facedoor-pi"), "username": e("FD_USERNAME", "facedoor"),
        "password": e("FD_PASSWORD") or None, "password_hash": e("FD_PASSWORD_HASH") or None,
        "timezone": e("FD_TIMEZONE", "Asia/Seoul"), "locale": e("FD_LOCALE", "en_US.UTF-8"),
        "ssh_public_keys": [k for k in (e("FD_SSH_KEY") or "").split("\n") if k.strip()],
        "enable_service": e("FD_ENABLE_SERVICE") == "1", "include_models": e("FD_INCLUDE_MODELS") == "1",
        "set_pi_mode": e("FD_SET_PI_MODE", "1") == "1", "download_models": e("FD_DOWNLOAD_MODELS", "1") == "1",
    }
    if e("FD_WIFI_SSID"):
        p["wifi"] = {"ssid": e("FD_WIFI_SSID"), "passphrase": e("FD_WIFI_PASS") or None,
                     "country": e("FD_WIFI_COUNTRY", "KR"), "hidden": e("FD_WIFI_HIDDEN") == "1",
                     "plaintext": e("FD_WIFI_PLAINTEXT") == "1", "open": e("FD_WIFI_OPEN") == "1"}
    return p


# --------------------------------------------------------------------------------------------
# Image discovery / download / checksum
# --------------------------------------------------------------------------------------------
def parse_sha256sums(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def pick_raspi_image(sums: Dict[str, str]) -> Tuple[str, str]:
    best = None
    for name, digest in sums.items():
        m = IMAGE_NAME_RE.match(name)
        if m:
            point = int(m.group(1) or 0)
            if best is None or point > best[0]:
                best = (point, name, digest)
    if not best:
        raise ToolError("no ubuntu-24.04*-preinstalled-server-arm64+raspi.img.xz entry in SHA256SUMS")
    return best[1], best[2]


def _http_get(url: str, timeout: int = 60) -> bytes:
    if not url.lower().startswith("https://"):
        raise ToolError(f"refusing non-HTTPS URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "facedoor-sdcard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as ex:  # noqa: BLE001
        raise ToolError(f"download failed: {url}: {ex}", EXIT_NETWORK)


def resolve_image(base: str) -> Dict:
    base = base if base.endswith("/") else base + "/"
    sums_text = _http_get(base + "SHA256SUMS").decode("utf-8", "replace")
    name, digest = pick_raspi_image(parse_sha256sums(sums_text))
    return {"name": name, "sha256": digest, "url": base + name, "sums_url": base + "SHA256SUMS"}


class Progress:
    def __init__(self, label: str, total: Optional[int], stream=None):
        self.label, self.total, self.done = label, total, 0
        self.t0 = self.last = time.monotonic()
        self.stream = stream or sys.stderr

    def update(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self.last >= 1.0:
            self.last = now
            self._print()

    def _print(self, end: str = "\r") -> None:
        el = max(1e-6, time.monotonic() - self.t0)
        mb = self.done / 1e6
        rate = mb / el
        if self.total:
            pct = 100.0 * self.done / self.total
            eta = (self.total - self.done) / 1e6 / rate if rate > 0 else 0
            msg = f"{self.label}: {mb:8.1f} / {self.total / 1e6:.1f} MB ({pct:5.1f}%) {rate:6.1f} MB/s ETA {eta:5.0f}s"
        else:
            msg = f"{self.label}: {mb:8.1f} MB {rate:6.1f} MB/s"
        self.stream.write(msg + "   " + end)
        self.stream.flush()

    def finish(self) -> None:
        self._print(end="\n")


def download(url: str, dst: Path, expected_sha256: str) -> Dict:
    """Resumable download to dst (via dst.part) with streaming sha256; raises on mismatch."""
    if not url.lower().startswith("https://"):
        raise ToolError(f"refusing non-HTTPS URL: {url}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_file():
        got = sha256_file(dst)
        if got == expected_sha256:
            return {"path": str(dst), "sha256": got, "reused": True}
        dst.unlink()
    part = dst.with_name(dst.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "facedoor-sdcard/1.0"}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if start and r.status != 206:
                start = 0
            length = r.headers.get("Content-Length")
            total = (int(length) + start) if length else None
            prog = Progress("download", total)
            prog.done = start
            with open(part, "ab" if start else "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    prog.update(len(b))
            prog.finish()
    except ToolError:
        raise
    except Exception as ex:  # noqa: BLE001
        raise ToolError(f"download interrupted ({ex}); re-run to resume from {part}", EXIT_NETWORK)
    got = sha256_file(part)
    if got != expected_sha256:
        bad = part.with_name(part.name + ".bad")
        os.replace(part, bad)
        raise ToolError(f"SHA256 mismatch for {dst.name}: got {got}, expected {expected_sha256} "
                        f"(moved to {bad})", EXIT_CHECKSUM)
    os.replace(part, dst)
    return {"path": str(dst), "sha256": got, "reused": False}


def verify_image(image: Path, expected: Optional[str], sums_text: Optional[str]) -> Dict:
    if not image.is_file():
        raise ToolError(f"image not found: {image}")
    if not expected:
        if not sums_text:
            raise ToolError("no expected SHA256: pass --sha256, --sums-file or --sums-url")
        sums = parse_sha256sums(sums_text)
        expected = sums.get(image.name)
        if not expected:
            raise ToolError(f"{image.name} is not listed in SHA256SUMS (renamed file? pass --sha256 explicitly)")
    expected = expected.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ToolError("expected SHA256 must be 64 hex characters")
    prog = Progress("sha256", image.stat().st_size)
    h = hashlib.sha256()
    with open(image, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
            prog.update(len(b))
    prog.finish()
    got = h.hexdigest()
    if got != expected:
        raise ToolError(f"SHA256 MISMATCH for {image.name}: got {got}, expected {expected}", EXIT_CHECKSUM)
    return {"path": str(image), "sha256": got, "ok": True}


# --------------------------------------------------------------------------------------------
# xz index parsing (uncompressed size without decompressing)
# --------------------------------------------------------------------------------------------
def _read_vli(buf: bytes, pos: int) -> Tuple[int, int]:
    val = shift = 0
    for i in range(9):
        b = buf[pos + i]
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, pos + i + 1
    raise ValueError("bad xz VLI")


def xz_uncompressed_size(path: Path) -> int:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        total = 0
        while end > 0:
            # skip stream padding (multiples of 4 null bytes)
            while end >= 4:
                f.seek(end - 4)
                if f.read(4) == b"\0\0\0\0":
                    end -= 4
                else:
                    break
            if end < 24:
                raise ValueError("truncated xz file")
            f.seek(end - 12)
            footer = f.read(12)
            if footer[10:12] != b"YZ":
                raise ValueError("not an xz stream footer")
            backward = (struct.unpack("<I", footer[4:8])[0] + 1) * 4
            f.seek(end - 12 - backward)
            index = f.read(backward)
            if index[0] != 0:
                raise ValueError("bad xz index indicator")
            nrec, pos = _read_vli(index, 1)
            blocks = 0
            for _ in range(nrec):
                unpadded, pos = _read_vli(index, pos)
                usize, pos = _read_vli(index, pos)
                total += usize
                blocks += (unpadded + 3) // 4 * 4
            end = end - 12 - backward - blocks - 12  # minus index, blocks, stream header
            if end < 0:
                raise ValueError("inconsistent xz index")
        return total


def image_size(path: Path) -> Optional[int]:
    n = path.name.lower()
    if n.endswith(".xz"):
        try:
            return xz_uncompressed_size(path)
        except (ValueError, IndexError, OSError):
            return None
    return path.stat().st_size


# --------------------------------------------------------------------------------------------
# Raw device helpers
# --------------------------------------------------------------------------------------------
def device_size(dev: str) -> int:
    if os.name == "nt" and dev.startswith("\\\\.\\"):
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        wintypes.LPVOID]
        GENERIC_READ, SHARE_RW, OPEN_EXISTING = 0x80000000, 0x3, 3
        h = k32.CreateFileW(dev, GENERIC_READ, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h in (None, wintypes.HANDLE(-1).value):
            raise ToolError(f"cannot open {dev}: winerror {ctypes.get_last_error()} (run as Administrator)", EXIT_WRITE)
        try:
            out = ctypes.c_longlong(0)
            ret = wintypes.DWORD(0)
            ok = k32.DeviceIoControl(h, 0x0007405C, None, 0, ctypes.byref(out), 8, ctypes.byref(ret), None)
            if not ok:
                raise ToolError(f"IOCTL_DISK_GET_LENGTH_INFO failed: {ctypes.get_last_error()}", EXIT_WRITE)
            return int(out.value)
        finally:
            k32.CloseHandle(h)
    if sys.platform == "darwin" and dev.startswith("/dev/"):
        # seeking to the end of a macOS disk node returns 0; ask diskutil instead
        info = _diskutil_info(dev.replace("/dev/rdisk", "/dev/disk"))
        size = int(info.get("TotalSize") or info.get("Size") or 0)
        if size <= 0:
            raise ToolError(f"cannot determine size of {dev}", EXIT_WRITE)
        return size
    with open(dev, "rb") as f:
        f.seek(0, os.SEEK_END)
        return f.tell()


def find_7zip() -> Optional[str]:
    for c in (shutil.which("7z"), shutil.which("7za"), shutil.which("7zz"),
              os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "7-Zip", "7z.exe"),
              os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "7-Zip", "7z.exe")):
        if c and os.path.isfile(c):
            return c
    return None


def open_image_stream(path: Path, decompressor: str = "auto"):
    """Return (readable binary stream, closer, description)."""
    n = path.name.lower()
    if not n.endswith(".xz"):
        f = open(path, "rb")
        return f, f.close, "raw"
    use = decompressor
    if use == "auto":
        try:
            import lzma  # noqa: F401
            use = "lzma"
        except ImportError:
            use = "7z"
    if use == "lzma":
        import lzma

        f = lzma.open(path, "rb")
        return f, f.close, "python-lzma"
    exe = find_7zip()
    if not exe:
        raise ToolError("python lzma unavailable and 7-Zip not found")
    proc = subprocess.Popen([exe, "x", "-so", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def close() -> None:
        proc.stdout.close()
        if proc.wait() != 0:
            raise ToolError(f"7-Zip exited with {proc.returncode}", EXIT_WRITE)

    return proc.stdout, close, f"7zip ({exe})"


def _read_full(stream, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        b = stream.read(n - got)
        if not b:
            break
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def _pad(b: bytes) -> bytes:
    r = len(b) % SECTOR
    return b if r == 0 else b + b"\0" * (SECTOR - r)


def write_image(image: Path, target: str, expected_target_size: Optional[int], decompressor: str = "auto",
                verify: str = "full", allow_file_target: bool = False,
                progress_stream=None) -> Dict:
    """Stream image -> target. The first HEAD_BYTES (partition table) are written last.

    verify: 'full' re-reads and hashes every written byte; 'quick' re-reads the first 64 MiB and the last
    written block; 'none' skips (not recommended).
    """
    is_dev = target.startswith("\\\\.\\") or target.startswith("/dev/")
    if is_dev and allow_file_target:
        raise ToolError("--allow-file-target is for regular files (tests) only, not device paths")
    if not is_dev and not allow_file_target:
        raise ToolError(f"target {target!r} is not a block device path")
    if is_dev:
        tsize = device_size(target)
        if expected_target_size is not None and tsize != expected_target_size:
            raise ToolError(f"device size changed: expected {expected_target_size}, actual {tsize}. "
                            "Card swapped? aborting", EXIT_WRITE)
    else:
        tsize = expected_target_size if expected_target_size is not None else 1 << 62
    isize = image_size(image)
    if isize is not None and isize > tsize:
        raise ToolError(f"image ({isize} bytes) larger than target ({tsize} bytes)", EXIT_WRITE)

    stream, close, how = open_image_stream(image, decompressor)
    h = hashlib.sha256()
    written = 0
    prog = Progress(f"write[{how}]", isize, progress_stream)
    mode = "r+b" if (is_dev or os.path.exists(target)) else "w+b"
    try:
        with open(target, mode, buffering=0) as out:
            head = _read_full(stream, HEAD_BYTES)
            if not head:
                raise ToolError("image is empty", EXIT_WRITE)
            h.update(head)
            written = len(head)
            prog.update(len(head))
            if len(head) == HEAD_BYTES:
                out.seek(HEAD_BYTES)
                while True:
                    blk = _read_full(stream, WRITE_BLOCK)
                    if not blk:
                        break
                    if written + len(blk) > tsize:
                        raise ToolError("image exceeds target size", EXIT_WRITE)
                    h.update(blk)
                    out.write(_pad(blk))
                    written += len(blk)
                    prog.update(len(blk))
            out.seek(0)
            out.write(_pad(head))
            out.flush()
            try:
                os.fsync(out.fileno())
            except OSError:
                pass
    finally:
        close()
    prog.finish()
    digest = h.hexdigest()
    result = {"written_bytes": written, "sha256": digest, "decompressor": how, "verify": verify}
    if verify != "none":
        result["verify_ok"] = verify_target(target, written, digest, verify, progress_stream)
        if not result["verify_ok"]:
            raise ToolError("READ-BACK VERIFY FAILED: card contents differ from the image (bad card/reader?)",
                            EXIT_WRITE)
    return result


def _read_exact_aligned(f, offset: int, n: int) -> bytes:
    start = offset - offset % SECTOR
    f.seek(start)
    need = (offset - start) + n
    b = f.read(need + ((-need) % SECTOR))
    return b[offset - start: offset - start + n]


def verify_target(target: str, length: int, digest: str, mode: str = "full", progress_stream=None) -> bool:
    if mode == "quick":
        # compare against a re-hash of just the regions; caller must re-decompress for exactness, so quick
        # mode only checks that the head region is readable and non-zero, plus full hash is skipped.
        with open(target, "rb", buffering=0) as f:
            head = _read_exact_aligned(f, 0, min(length, 64 * 1024 * 1024))
        return len(head) == min(length, 64 * 1024 * 1024) and head[510:512] == b"\x55\xaa"
    h = hashlib.sha256()
    prog = Progress("verify", length, progress_stream)
    with open(target, "rb", buffering=0) as f:
        pos = 0
        while pos < length:
            n = min(WRITE_BLOCK, length - pos)
            b = _read_exact_aligned(f, pos, n)
            if len(b) != n:
                return False
            h.update(b)
            pos += n
            prog.update(n)
    prog.finish()
    return h.hexdigest() == digest


# --------------------------------------------------------------------------------------------
# Device safety: only removable USB/SD media between MIN and MAX size, never a system/boot disk
# --------------------------------------------------------------------------------------------
MAX_DEVICE_BYTES = 512 * 1024 ** 3      # 512 GiB hard ceiling (can be lowered, never raised)
MIN_DEVICE_BYTES = 7_000_000_000        # "8 GB" cards report ~7.4-7.9e9 bytes (floor; can be raised)
RECOMMENDED_MIN_BYTES = 15_000_000_000  # 16 GB+ recommended (venv + models + logs)
ALLOWED_BUS = {"usb", "sd", "mmc"}
SYSTEM_MOUNTS_POSIX = {"/", "/boot", "/boot/efi", "/boot/firmware", "/efi", "/usr", "/var", "/home", "/opt",
                       "/srv", "/root", "/nix", "/snap", "[SWAP]"}
WIN_BUS_CODES = {1: "scsi", 2: "atapi", 3: "ata", 4: "1394", 5: "ssa", 6: "fibre channel", 7: "usb", 8: "raid",
                 9: "iscsi", 10: "sas", 11: "sata", 12: "sd", 13: "mmc", 14: "virtual",
                 15: "file backed virtual", 16: "spaces", 17: "nvme", 18: "scm", 19: "ufs"}
_BUS_ALIASES = {"secure digital": "sd", "sdio": "sd", "sdcard": "sd", "pci-express": "pcie", "pci": "pcie"}


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"))


def fmt_bytes(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def norm_bus(v) -> str:
    if v is None:
        return ""
    if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
        return WIN_BUS_CODES.get(int(v), "unknown")
    s = str(v).strip().lower()
    return _BUS_ALIASES.get(s, s)


def is_system_mount(mp: Optional[str]) -> bool:
    if not mp:
        return False
    mp = mp.rstrip("/") or "/"
    return mp in SYSTEM_MOUNTS_POSIX or mp.startswith("/System/Volumes") or mp.startswith("/boot/")


def assess_device(dev: Dict, max_bytes: int = MAX_DEVICE_BYTES, min_bytes: int = MIN_DEVICE_BYTES,
                  image_bytes: Optional[int] = None) -> Dict:
    """Pure decision on a normalised device dict. ok=True only if NO refusal reason applies."""
    max_bytes = min(int(max_bytes), MAX_DEVICE_BYTES)
    min_bytes = max(int(min_bytes), MIN_DEVICE_BYTES)
    reasons: List[str] = []
    warnings: List[str] = []
    size = int(dev.get("size") or 0)
    bus = norm_bus(dev.get("bus"))
    if dev.get("is_system"):
        reasons.append("system disk")
    if dev.get("is_boot"):
        reasons.append("boot disk")
    if dev.get("system_mounts"):
        reasons.append(f"holds system mount points/drives {list(dev['system_mounts'])}")
    if dev.get("protected_hits"):
        reasons.append(f"holds files this tool is using {list(dev['protected_hits'])}")
    if dev.get("holders"):
        reasons.append(f"has LVM/RAID/crypt/device-mapper members {list(dev['holders'])}")
    if dev.get("virtual"):
        reasons.append("virtual disk")
    if bus not in ALLOWED_BUS:
        reasons.append(f"bus '{bus or 'unknown'}' is not USB/SD/MMC")
    elif bus == "usb" and not dev.get("removable"):
        reasons.append("USB disk with FIXED media (external HDD/SSD?): only card readers / removable media")
    if bus == "mmc" and dev.get("emmc"):
        reasons.append("internal eMMC storage")
    if dev.get("internal") and bus not in ("sd", "mmc"):
        reasons.append("internal disk")
    if dev.get("readonly"):
        reasons.append("read-only (SD card lock switch?)")
    if size <= 0:
        reasons.append("no media (size 0)")
    else:
        if size > max_bytes:
            reasons.append(f"too large: {fmt_bytes(size)} > limit {fmt_bytes(max_bytes)}")
        if size < min_bytes:
            reasons.append(f"too small: {fmt_bytes(size)} < {fmt_bytes(min_bytes)}")
        if image_bytes and size < int(image_bytes):
            reasons.append(f"smaller than the image ({fmt_bytes(size)} < {fmt_bytes(int(image_bytes))})")
        if size < RECOMMENDED_MIN_BYTES and not reasons:
            warnings.append("under 16 GB: works, but 16 GB or more is recommended")
    mounted = [p.get("mountpoint") for p in dev.get("partitions") or [] if p.get("mountpoint")]
    if mounted and not reasons:
        warnings.append(f"mounted partitions {mounted} will be unmounted and ERASED")
    return {"ok": not reasons, "reasons": reasons, "warnings": warnings}


# ---- Linux (lsblk) ----
LSBLK_COLS = "NAME,KNAME,PATH,MAJ:MIN,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,HOTPLUG,RO,TYPE,MOUNTPOINT,LABEL,FSTYPE"
_SKIP_DISK_PREFIXES = ("loop", "ram", "zram", "sr", "fd", "nbd", "md", "dm-")


def _dev_numbers(paths: Iterable[str]) -> Dict[Tuple[int, int], str]:
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        out[(os.major(st.st_dev), os.minor(st.st_dev))] = p
    return out


def normalize_lsblk(data: Dict, protect: Optional[Dict[Tuple[int, int], str]] = None) -> List[Dict]:
    protect = protect or {}
    disks = [d for d in data.get("blockdevices", []) if d.get("type") == "disk"]
    names = {str(d.get("name") or d.get("kname") or "") for d in disks}
    out = []
    for d in disks:
        name = str(d.get("name") or d.get("kname") or "")
        if not name or name.startswith(_SKIP_DISK_PREFIXES) or re.search(r"boot\d+$|rpmb$", name):
            continue
        parts: List[Dict] = []
        system_mounts: List[str] = []
        holders: List[str] = []
        hits: List[str] = []

        def visit(node: Dict, top: bool) -> None:
            mps = [node.get("mountpoint")] + list(node.get("mountpoints") or [])
            mps = sorted({m for m in mps if m})
            for m in mps:
                if is_system_mount(m):
                    system_mounts.append(m)
            mm = str(node.get("maj:min") or "")
            if ":" in mm:
                try:
                    key = (int(mm.split(":")[0]), int(mm.split(":")[1]))
                    if key in protect:
                        hits.append(protect[key])
                except ValueError:
                    pass
            if not top:
                t = str(node.get("type") or "")
                if t == "part":
                    parts.append({"name": node.get("name"), "size": int(node.get("size") or 0),
                                  "fstype": node.get("fstype"), "label": node.get("label"),
                                  "mountpoint": mps[0] if mps else None})
                else:
                    holders.append(f"{t}:{node.get('name')}")
            for c in node.get("children") or []:
                visit(c, False)

        visit(d, True)
        bus = str(d.get("tran") or "").lower() or ("mmc" if name.startswith("mmcblk") else "")
        model = " ".join(str(x).strip() for x in (d.get("vendor"), d.get("model")) if x and str(x).strip())
        out.append({
            "id": name, "path": d.get("path") or f"/dev/{name}", "size": int(d.get("size") or 0),
            "model": model or "(unknown)", "serial": d.get("serial") or "", "bus": bus,
            "removable": _truthy(d.get("rm")), "hotplug": _truthy(d.get("hotplug")),
            "readonly": _truthy(d.get("ro")), "is_system": False, "is_boot": False,
            "emmc": name.startswith("mmcblk") and f"{name}boot0" in names,
            "partitions": parts, "system_mounts": sorted(set(system_mounts)), "holders": holders,
            "protected_hits": sorted(set(hits)),
        })
    return out


def collect_linux(protect_paths: Iterable[str] = ()) -> List[Dict]:
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-o", LSBLK_COLS], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as ex:
        raise ToolError(f"lsblk failed: {ex}", EXIT_DEVICE)
    return normalize_lsblk(json.loads(r.stdout.decode("utf-8", "replace")), _dev_numbers(protect_paths))


# ---- macOS (diskutil) ----
def _diskutil_info(dev: str) -> Dict:
    try:
        r = subprocess.run(["diskutil", "info", "-plist", dev], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as ex:
        raise ToolError(f"diskutil info {dev} failed: {ex}", EXIT_DEVICE)
    return plistlib.loads(r.stdout)


def normalize_macos(listing: Dict, infos: Dict[str, Dict], protect_paths: Iterable[str] = ()) -> List[Dict]:
    protect_paths = [posixpath.normpath(str(p)) for p in protect_paths]  # absolute macOS paths
    out = []
    for d in listing.get("AllDisksAndPartitions", []):
        ident = d.get("DeviceIdentifier")
        if not ident:
            continue
        info = infos.get(ident, {})
        parts, sysm, hits = [], [], []
        nodes = [d] + list(d.get("Partitions") or []) + list(d.get("APFSVolumes") or [])
        for n in nodes:
            mp = n.get("MountPoint")
            if is_system_mount(mp):
                sysm.append(mp)
            if mp and mp != "/":
                hits += [p for p in protect_paths if p == mp or p.startswith(mp.rstrip("/") + "/")]
            if n is not d:
                parts.append({"name": n.get("DeviceIdentifier"), "size": int(n.get("Size") or 0),
                              "fstype": n.get("Content"), "label": n.get("VolumeName"), "mountpoint": mp})
        removable = bool(info.get("RemovableMedia") or info.get("Removable"))
        out.append({
            "id": ident, "path": f"/dev/r{ident}", "size": int(info.get("TotalSize") or d.get("Size") or 0),
            "model": str(info.get("MediaName") or info.get("IORegistryEntryName") or "(unknown)"),
            "serial": "", "bus": str(info.get("BusProtocol") or ""), "removable": removable,
            "internal": bool(info.get("Internal")), "readonly": info.get("WritableMedia") is False,
            "virtual": str(info.get("VirtualOrPhysical", "")).lower() == "virtual",
            "is_system": False, "is_boot": False, "emmc": False, "partitions": parts,
            "system_mounts": sorted(set(sysm)), "holders": [], "protected_hits": sorted(set(hits)),
        })
    return out


def collect_macos(protect_paths: Iterable[str] = ()) -> List[Dict]:
    try:
        r = subprocess.run(["diskutil", "list", "-plist", "physical"], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as ex:
        raise ToolError(f"diskutil list failed: {ex}", EXIT_DEVICE)
    listing = plistlib.loads(r.stdout)
    infos = {d["DeviceIdentifier"]: _diskutil_info(d["DeviceIdentifier"])
             for d in listing.get("AllDisksAndPartitions", []) if d.get("DeviceIdentifier")}
    return normalize_macos(listing, infos, protect_paths)


# ---- Windows (Get-Disk via PowerShell 5.1) ----
WIN_COLLECT_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dd = @{}
Get-CimInstance Win32_DiskDrive | ForEach-Object { $dd[[int]$_.Index] = $_ }
$out = foreach ($d in Get-Disk) {
  $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
    $l = ''
    if ($_.DriveLetter -and [int][char]$_.DriveLetter -ne 0) { $l = [string]$_.DriveLetter }
    [pscustomobject]@{ number = [int]$_.PartitionNumber; size = [int64]$_.Size; letter = $l;
                       is_system = [bool]$_.IsSystem; is_boot = [bool]$_.IsBoot; type = [string]$_.Type }
  })
  $w = $dd[[int]$d.Number]
  $mt = ''
  if ($w) { $mt = [string]$w.MediaType }
  [pscustomobject]@{ number = [int]$d.Number; friendly = [string]$d.FriendlyName; serial = [string]$d.SerialNumber;
                     size = [int64]$d.Size; bus = [string]$d.BusType; is_system = [bool]$d.IsSystem;
                     is_boot = [bool]$d.IsBoot; readonly = [bool]$d.IsReadOnly; offline = [bool]$d.IsOffline;
                     media_type = $mt; partitions = $parts }
}
ConvertTo-Json -InputObject @($out) -Depth 5 -Compress
"""


def normalize_windows(disks: List[Dict], system_drive: str = "C", protect_paths: Iterable[str] = ()) -> List[Dict]:
    sysl = (system_drive or "C").strip().rstrip(":\\").upper()[:1]
    prot: Dict[str, List[str]] = {}
    for p in protect_paths:
        drv = ntpath.splitdrive(str(p))[0].rstrip(":").upper()  # wrapper passes absolute Windows paths
        if len(drv) == 1:
            prot.setdefault(drv, []).append(p)
    out = []
    for d in disks or []:
        num = int(d.get("number"))
        parts, sysm, hits = [], [], []
        for p in d.get("partitions") or []:
            letter = str(p.get("letter") or "").upper()[:1]
            mp = f"{letter}:\\" if letter else None
            if letter and letter == sysl:
                sysm.append(mp)
            if p.get("is_system") or p.get("is_boot"):
                sysm.append(f"partition {p.get('number')} ({p.get('type') or 'system'})")
            if letter in prot:
                hits += prot[letter]
            parts.append({"name": f"partition {p.get('number')}", "size": int(p.get("size") or 0),
                          "fstype": p.get("type"), "label": None, "mountpoint": mp})
        bus = norm_bus(d.get("bus"))
        media = str(d.get("media_type") or "")
        out.append({
            "id": str(num), "path": f"\\\\.\\PhysicalDrive{num}", "size": int(d.get("size") or 0),
            "model": str(d.get("friendly") or "(unknown)"), "serial": str(d.get("serial") or "").strip(),
            "bus": bus, "media_type": media,
            "removable": media.lower().startswith("removable") or bus in ("sd", "mmc"),
            "readonly": bool(d.get("readonly")), "is_system": bool(d.get("is_system")),
            "is_boot": bool(d.get("is_boot")), "virtual": bus in ("virtual", "file backed virtual", "spaces"),
            "emmc": False, "partitions": parts, "system_mounts": sysm, "holders": [],
            "protected_hits": sorted(set(hits)),
        })
    return out


def collect_windows(protect_paths: Iterable[str] = ()) -> List[Dict]:
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", WIN_COLLECT_PS]
    try:
        r = subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as ex:
        raise ToolError(f"Get-Disk failed: {ex}", EXIT_DEVICE)
    text = r.stdout.decode("utf-8-sig", "replace").strip() or "[]"
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    return normalize_windows(data, os.environ.get("SystemDrive", "C:"), protect_paths)


def collect_devices(protect_paths: Iterable[str] = ()) -> List[Dict]:
    protect_paths = list(protect_paths) + [str(HERE)]
    if os.name == "nt":
        return collect_windows(protect_paths)
    if sys.platform == "darwin":
        return collect_macos(protect_paths)
    return collect_linux(protect_paths)


def assess_all(devs: List[Dict], **limits) -> List[Dict]:
    return [dict(d, assessment=assess_device(d, **limits)) for d in devs]


def _same_device(dev: Dict, target: str) -> bool:
    """Whole-disk path match only (Linux /dev/sdX, macOS /dev/diskN or /dev/rdiskN, Windows \\\\.\\PhysicalDriveN)."""
    ident = str(dev.get("id"))
    return target.strip() in {dev.get("path"), f"/dev/{ident}", f"/dev/r{ident}"}


def guard_device(target: str, expected_size: Optional[int] = None, protect_paths: Iterable[str] = (),
                 collector: Optional[Callable[[Iterable[str]], List[Dict]]] = None, **limits) -> Dict:
    """Re-collect devices and refuse (EXIT_DEVICE) unless `target` passes every check."""
    devs = (collector or collect_devices)(protect_paths)
    match = [d for d in devs if _same_device(d, target)]
    if len(match) != 1:
        raise ToolError(f"target {target!r} is not a known whole disk (partitions are not accepted)", EXIT_DEVICE)
    dev = match[0]
    a = assess_device(dev, **limits)
    if not a["ok"]:
        raise ToolError(f"REFUSED {target} ({dev.get('model')}, {fmt_bytes(int(dev.get('size') or 0))}): "
                        + "; ".join(a["reasons"]), EXIT_DEVICE)
    if expected_size is not None and int(dev.get("size") or 0) != int(expected_size):
        raise ToolError(f"device size changed ({dev.get('size')} != {expected_size}); card swapped? aborting",
                        EXIT_DEVICE)
    return dict(dev, assessment=a)


# --------------------------------------------------------------------------------------------
# Boot partition copy + staged secret cleanup
# --------------------------------------------------------------------------------------------
BOOT_COPY_ORDER = (BUNDLE_NAME, BUNDLE_NAME + ".sha256", FIRSTBOOT_NAME, "network-config", "meta-data", "user-data")
SECRET_STAGE_FILES = ("user-data", "network-config", "meta-data")


def copy_to_boot(stage_dir: Path, boot_dir: Path) -> Dict:
    stage_dir, boot_dir = Path(stage_dir), Path(boot_dir)
    summary_path = stage_dir / "stage-summary.json"
    if not summary_path.is_file():
        raise ToolError(f"{summary_path} missing: run 'stage' first")
    files = json.loads(summary_path.read_text(encoding="utf-8"))["files"]
    missing = [n for n in BOOT_COPY_ORDER if n not in files or not (stage_dir / n).is_file()]
    if missing:
        raise ToolError(f"staged files missing: {missing}")
    if not boot_dir.is_dir():
        raise ToolError(f"boot partition {boot_dir} is not a directory / not mounted", EXIT_DEVICE)
    if not ((boot_dir / "config.txt").is_file() and (boot_dir / "cmdline.txt").is_file()):
        raise ToolError(f"{boot_dir} does not look like a Raspberry Pi boot partition (no config.txt/cmdline.txt)",
                        EXIT_DEVICE)
    for n in BOOT_COPY_ORDER:
        if sha256_file(stage_dir / n) != files[n]:
            raise ToolError(f"staged file {n} changed since staging", EXIT_CHECKSUM)
    need = sum((stage_dir / n).stat().st_size for n in BOOT_COPY_ORDER)
    need -= sum((boot_dir / n).stat().st_size for n in BOOT_COPY_ORDER if (boot_dir / n).is_file())
    free = shutil.disk_usage(str(boot_dir)).free
    if need + (1 << 20) > free:
        raise ToolError(f"not enough space on the boot partition: need {need} bytes, free {free}", EXIT_WRITE)
    copied = {}
    for n in BOOT_COPY_ORDER:
        dst = boot_dir / n
        tmp = boot_dir / (n + ".tmp")
        with open(stage_dir / n, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            try:
                os.fsync(out.fileno())
            except OSError:
                pass
        os.replace(tmp, dst)
        got = sha256_file(dst)
        if got != files[n]:
            raise ToolError(f"copy of {n} on the boot partition does not match (got {got})", EXIT_WRITE)
        copied[n] = got
    return {"boot_dir": str(boot_dir), "copied": copied, "ok": True}


def scrub_stage(stage_dir: Path) -> List[str]:
    """Best-effort overwrite + delete of staged files that contain credential material (hash / PSK)."""
    removed = []
    for n in SECRET_STAGE_FILES:
        p = Path(stage_dir) / n
        if p.is_file():
            try:
                size = p.stat().st_size
                with open(p, "r+b") as f:
                    f.write(b"\0" * size)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass
            p.unlink()
            removed.append(n)
    return removed


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _read_stdin_json() -> Dict:
    raw = sys.stdin.buffer.read().decode("utf-8-sig").strip()
    if not raw:
        raise ToolError("expected params JSON on stdin")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as ex:
        raise ToolError(f"bad params JSON: {ex}")


def _emit(obj: Dict, result_path: Optional[str]) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if result_path:
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        with open(result_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        print(text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", help="write the JSON result to this file instead of stdout")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("hash-password")
    s = sub.add_parser("stage")
    s.add_argument("--out", required=True)
    s.add_argument("--project-root", default=str(PROJECT_ROOT_DEFAULT))
    s.add_argument("--params-from-env", action="store_true")
    s = sub.add_parser("bundle")
    s.add_argument("--out", required=True)
    s.add_argument("--project-root", default=str(PROJECT_ROOT_DEFAULT))
    s.add_argument("--include-models", action="store_true")
    s = sub.add_parser("resolve-image")
    s.add_argument("--base", default=DEFAULT_IMAGE_BASE)
    s = sub.add_parser("fetch-image")
    s.add_argument("--base", default=DEFAULT_IMAGE_BASE)
    s.add_argument("--dir", required=True)
    s = sub.add_parser("verify-image")
    s.add_argument("--image", required=True)
    s.add_argument("--sha256")
    s.add_argument("--sums-file")
    s.add_argument("--sums-url")
    s = sub.add_parser("image-info")
    s.add_argument("--image", required=True)
    s = sub.add_parser("write")
    s.add_argument("--image", required=True)
    s.add_argument("--target", required=True)
    s.add_argument("--expect-target-size", type=int)
    s.add_argument("--decompressor", choices=["auto", "lzma", "7z"], default="auto")
    s.add_argument("--verify", choices=["full", "quick", "none"], default="full")
    s.add_argument("--allow-file-target", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("device-size")
    s.add_argument("--target", required=True)

    def add_limits(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--max-size-gb", type=float, default=MAX_DEVICE_BYTES / 1e9,
                        help="upper size limit in GB (can only be LOWERED below 512 GiB)")
        sp.add_argument("--min-size-gb", type=float, default=MIN_DEVICE_BYTES / 1e9)
        sp.add_argument("--image-size", type=int, help="uncompressed image size in bytes")
        sp.add_argument("--protect", action="append", default=[],
                        help="path whose disk must never be chosen (image, work dir); repeatable")

    s = sub.add_parser("list-devices")
    add_limits(s)
    s = sub.add_parser("check-device")
    s.add_argument("--target", required=True)
    s.add_argument("--expect-size", type=int)
    add_limits(s)
    s = sub.add_parser("assess-devices")
    add_limits(s)
    s = sub.add_parser("copy-to-boot")
    s.add_argument("--stage", required=True)
    s.add_argument("--boot", required=True)
    s = sub.add_parser("scrub-stage")
    s.add_argument("--stage", required=True)
    a = ap.parse_args(argv)

    def limits() -> Dict:
        return {"max_bytes": int(a.max_size_gb * 1e9), "min_bytes": int(a.min_size_gb * 1e9),
                "image_bytes": a.image_size}

    try:
        if a.cmd == "hash-password":
            pw = sys.stdin.buffer.read().decode("utf-8-sig").rstrip("\r\n")
            if len(pw) < 8:
                raise ToolError("password must be at least 8 characters")
            print(sha512_crypt(pw))
            return 0
        if a.cmd == "stage":
            params = params_from_env() if a.params_from_env else _read_stdin_json()
            _emit(stage(params, Path(a.out), Path(a.project_root)), a.result)
            return 0
        if a.cmd == "bundle":
            r = build_bundle(Path(a.project_root), Path(a.out) / BUNDLE_NAME, a.include_models)
            _emit(r, a.result)
            return 0
        if a.cmd == "resolve-image":
            _emit(resolve_image(a.base), a.result)
            return 0
        if a.cmd == "fetch-image":
            info = resolve_image(a.base)
            r = download(info["url"], Path(a.dir) / info["name"], info["sha256"])
            r.update(info)
            r["uncompressed_size"] = image_size(Path(r["path"]))
            _emit(r, a.result)
            return 0
        if a.cmd == "verify-image":
            sums = None
            if a.sums_file:
                sums = Path(a.sums_file).read_text(encoding="utf-8", errors="replace")
            elif a.sums_url:
                sums = _http_get(a.sums_url).decode("utf-8", "replace")
            elif not a.sha256:
                sums = _http_get(DEFAULT_IMAGE_BASE + "SHA256SUMS").decode("utf-8", "replace")
            r = verify_image(Path(a.image), a.sha256, sums)
            r["uncompressed_size"] = image_size(Path(a.image))
            _emit(r, a.result)
            return 0
        if a.cmd == "image-info":
            p = Path(a.image)
            _emit({"path": str(p), "size": p.stat().st_size, "uncompressed_size": image_size(p)}, a.result)
            return 0
        if a.cmd == "write":
            if not a.allow_file_target:
                # defence in depth: the wrappers already checked, but never trust a bare device path
                g = guard_device(a.target, a.expect_target_size, [a.image])
                sys.stderr.write(f"device check OK: {g['path']} {g['model']} {fmt_bytes(g['size'])}\n")
            r = write_image(Path(a.image), a.target, a.expect_target_size, a.decompressor, a.verify,
                            a.allow_file_target)
            _emit(r, a.result)
            return 0
        if a.cmd == "device-size":
            _emit({"target": a.target, "size": device_size(a.target)}, a.result)
            return 0
        if a.cmd == "list-devices":
            devs = assess_all(collect_devices(a.protect), **limits())
            _emit({"platform": sys.platform, "devices": devs}, a.result)
            return 0
        if a.cmd == "check-device":
            _emit(guard_device(a.target, a.expect_size, a.protect, **limits()), a.result)
            return 0
        if a.cmd == "assess-devices":
            devs = _read_stdin_json()
            if isinstance(devs, dict):
                devs = devs.get("devices", [devs])
            _emit({"devices": assess_all(devs, **limits())}, a.result)
            return 0
        if a.cmd == "copy-to-boot":
            _emit(copy_to_boot(Path(a.stage), Path(a.boot)), a.result)
            return 0
        if a.cmd == "scrub-stage":
            _emit({"removed": scrub_stage(Path(a.stage))}, a.result)
            return 0
    except ToolError as ex:
        sys.stderr.write(f"\nERROR: {ex}\n")
        if a.result:
            _emit({"error": str(ex), "code": ex.code}, a.result)
        return ex.code
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
