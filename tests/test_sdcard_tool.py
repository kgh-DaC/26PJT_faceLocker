"""Unit tests for deploy/sdcard/sdcard_tool.py and the SD-card wrapper scripts.

No SD card, no block device, no network, no real write: images are written to regular files,
network calls are monkeypatched, device listings are fixtures.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SDCARD = ROOT / "deploy" / "sdcard"


def _load_tool():
    spec = importlib.util.spec_from_file_location("sdcard_tool", SDCARD / "sdcard_tool.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sdcard_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


T = _load_tool()

PASSWORD = "Corr3ct-horse-battery"
WIFI_PASS = "wifi-pass-1234"
KEY = "ssh-ed25519 " + base64.b64encode(struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
                                        + bytes(range(32))).decode() + " user@pc"
GiB = 1024 ** 3


def base_params(**kw):
    p = {"hostname": "facedoor-pi", "username": "door", "password": PASSWORD, "ssh_public_keys": [KEY],
         "wifi": {"ssid": "Lab WiFi", "passphrase": WIFI_PASS, "country": "kr"}}
    p.update(kw)
    return p


def make_project(root: Path) -> Path:
    (root / "src" / "facedoor").mkdir(parents=True)
    (root / "src" / "facedoor" / "__init__.py").write_text("x = 1\n")
    (root / "configs").mkdir()
    (root / "configs" / "default.yaml").write_text("hardware: {mode: mock}\n")
    (root / "configs" / "local.yaml").write_text("secret: local\n")
    (root / "deploy").mkdir()
    (root / "deploy" / "install_rpi.sh").write_bytes(b"#!/usr/bin/env bash\r\necho hi\r\n")
    (root / "deploy" / "sdcard" / "out").mkdir(parents=True)
    (root / "deploy" / "sdcard" / "out" / "user-data").write_text("old staged secret")
    (root / "data" / "gallery").mkdir(parents=True)
    (root / "data" / "gallery" / "alice.npy").write_bytes(b"\0" * 10)
    (root / ".venv").mkdir()
    (root / ".venv" / "x.py").write_text("venv")
    (root / ".env").write_text("TOKEN=abc")
    (root / "docs").mkdir()
    (root / "docs" / "photo.jpg").write_bytes(b"\xff\xd8personal")
    (root / "tests" / "assets").mkdir(parents=True)
    (root / "tests" / "assets" / "sample.jpg").write_bytes(b"\xff\xd8public")
    (root / "models").mkdir()
    (root / "models" / "models.lock.json").write_text("{}")
    (root / "models" / "face.onnx").write_bytes(b"onnx")
    (root / "requirements-pi.txt").write_text("numpy\n")
    return root


# ------------------------------------------------------------------------------------------
# SHA-512 crypt: test vectors from Ulrich Drepper's "Unix crypt using SHA-256 and SHA-512"
# ------------------------------------------------------------------------------------------
SHA512_VECTORS = [
    ("Hello world!", "saltstring", None,
     "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1"),
    ("Hello world!", "saltstringsaltstring", 10000,
     "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8vOuZeHBy/YTBmSK6H9qs/"
     "y3RnOaw5v."),
    ("This is just a test", "toolongsaltstring", 5000,
     "$6$rounds=5000$toolongsaltstrin$lQ8jolhgVRVhY4b5pZKaysCLi0QBxGoNeKQzQ3glMhwllF7oGDZxUhx1yxdYcz/e1JSbq3y6JMxxl8"
     "audkUEm0"),
    ("a very much longer text to encrypt.  This one even stretches over morethan one line.", "anotherlongsaltstring",
     1400,
     "$6$rounds=1400$anotherlongsalts$POfYwTEok97VWcjxIiSOjiykti.o/pQs.wPvMxQ6Fm7I6IoYN3CmLs66x9t0oSwbtEW7o7UmJEiDwGq"
     "d8p4ur1"),
    ("we have a short salt string but not a short password", "short", 77777,
     "$6$rounds=77777$short$WuQyW2YR.hBNpjjRhpYD/ifIw05xdfeEyQoMxIXbkvr0gge1a1x3yRULJ5CCaUeOxFmtlcGZelFl5CxtgfiAc0"),
    ("a short string", "asaltof16chars..", 123456,
     "$6$rounds=123456$asaltof16chars..$BtCwjqMJGx5hrJhZywWvt0RLE8uZ4oPwcelCjmw2kSYu.Ec6ycULevoBK25fs2xXgMNrCzIMVcgEJ"
     "AstJeonj1"),
    ("the minimum number is still observed", "roundstoolow", 10,
     "$6$rounds=1000$roundstoolow$kUMsbe306n21p9R.FRkW3IGn.S9NPN0x50YhH1xhLsPuWGsUSklZt58jaTfF4ZEQpyUNGc0dqbpBYYBaH"
     "HrsX."),
]


@pytest.mark.parametrize("pw,salt,rounds,expected", SHA512_VECTORS)
def test_sha512_crypt_spec_vectors(pw, salt, rounds, expected):
    assert T.sha512_crypt(pw, salt, rounds) == expected


def test_sha512_crypt_random_salt_and_cli(monkeypatch, capsys):
    h1, h2 = T.sha512_crypt("same password"), T.sha512_crypt("same password")
    assert h1 != h2 and h1.startswith("$6$rounds=100000$")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"longenough\n")))
    assert T.main(["hash-password"]) == 0
    out = capsys.readouterr().out.strip()
    salt = out.split("$")[3]
    assert out == T.sha512_crypt("longenough", salt, 100000)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"short\n")))
    assert T.main(["hash-password"]) == T.EXIT_USAGE


# ------------------------------------------------------------------------------------------
# params validation
# ------------------------------------------------------------------------------------------
def test_wpa_psk_ieee_vector():
    # IEEE 802.11i-2004 Annex H.4 test vector
    assert T.wpa_psk_hex("IEEE", "password") == \
        "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"


def test_validate_params_ok_drops_plaintext():
    p = T.validate_params(base_params())
    assert "password" not in p
    assert p["password_hash"].startswith("$6$")
    assert p["wifi"]["secret"] == T.wpa_psk_hex("Lab WiFi", WIFI_PASS)
    assert p["wifi"]["country"] == "KR"
    assert p["ssh_public_keys"] == [KEY]
    assert p["install_dir"] == "/opt/facedoor"


@pytest.mark.parametrize("override,msg", [
    ({"hostname": "-bad"}, "hostname"),
    ({"hostname": "a" * 64}, "hostname"),
    ({"hostname": "has space"}, "hostname"),
    ({"username": "ubuntu"}, "username"),
    ({"username": "root"}, "username"),
    ({"username": "Bad User"}, "username"),
    ({"password": "short"}, "8 characters"),
    ({"password": "line\nbreak123"}, "newline"),
    ({"password": None}, "password"),
    ({"timezone": "Not A Zone"}, "timezone"),
    ({"locale": "korean"}, "locale"),
    ({"install_dir": "/"}, "install_dir"),
    ({"install_dir": "/opt/얼굴"}, "install_dir"),
    ({"install_dir": "relative/path"}, "install_dir"),
    ({"ssh_public_keys": ["-----BEGIN OPENSSH PRIVATE KEY-----"]}, "PRIVATE"),
    ({"ssh_public_keys": ["ssh-ed25519 not base64 !!"]}, "SSH"),
    ({"ssh_public_keys": ["ssh-rsa " + KEY.split()[1]]}, "SSH"),  # blob says ed25519
    ({"wifi": {"ssid": "x" * 33, "passphrase": WIFI_PASS}}, "SSID"),
    ({"wifi": {"ssid": "", "passphrase": WIFI_PASS}}, "SSID"),
    ({"wifi": {"ssid": "a\tb", "passphrase": WIFI_PASS}}, "SSID"),
    ({"wifi": {"ssid": "lab", "passphrase": "short"}}, "8-63"),
    ({"wifi": {"ssid": "lab", "passphrase": "p" * 64}}, "8-63"),
    ({"wifi": {"ssid": "lab", "passphrase": "ünicode-pass"}}, "8-63"),
    ({"wifi": {"ssid": "lab"}}, "passphrase"),
    ({"wifi": {"ssid": "lab", "psk_hex": "zz"}}, "psk_hex"),
    ({"wifi": {"ssid": "lab", "passphrase": WIFI_PASS, "country": "KOR"}}, "country"),
])
def test_validate_params_rejects(override, msg):
    with pytest.raises(T.ToolError) as ei:
        T.validate_params(base_params(**override))
    assert msg.lower() in str(ei.value).lower()
    assert ei.value.code == T.EXIT_USAGE


def test_validate_params_open_network_and_hash_only():
    p = T.validate_params(base_params(password=None, password_hash=T.sha512_crypt("x" * 10, "abc"),
                                      wifi={"ssid": "guest", "open": True}, ssh_public_keys=[]))
    assert p["wifi"]["secret"] is None
    assert p["ssh_public_keys"] == []


# ------------------------------------------------------------------------------------------
# rendering
# ------------------------------------------------------------------------------------------
def test_render_template_missing_placeholder():
    with pytest.raises(T.ToolError):
        T.render_template("a {{X}} {{Y}}", {"X": "1"})
    assert T.render_template("a {{X}}\r\n", {"X": "1"}) == "a 1\n"


def test_templates_have_known_placeholders_only():
    for name in ("user-data.tpl", "network-config.tpl", "meta-data.tpl"):
        text = (T.TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert "\r" not in text
        found = set(T.PLACEHOLDER_RE.findall(text))
        assert found, name
    assert set(T.PLACEHOLDER_RE.findall((T.TEMPLATES_DIR / "user-data.tpl").read_text(encoding="utf-8"))) == {
        "GENERATED_AT", "HOSTNAME", "USERNAME", "PASSWORD_HASH", "TIMEZONE", "LOCALE", "SSH_PWAUTH",
        "SSH_KEYS_BLOCK", "ENV_CONTENT", "FIRSTBOOT_NAME"}


def test_stage_renders_valid_cloud_init(tmp_path):
    proj = make_project(tmp_path / "proj")
    out = tmp_path / "stage"
    tricky_ssid = 'Lab "5G": #1'
    params = base_params(wifi={"ssid": tricky_ssid, "passphrase": WIFI_PASS, "country": "KR", "hidden": True},
                         enable_service=True)
    s = T.stage(params, out, proj, generated_at="2026-10-16T10:00:00+09:00")

    ud_text = (out / "user-data").read_text(encoding="utf-8")
    assert ud_text.startswith("#cloud-config\n")
    ud = yaml.safe_load(ud_text)
    assert ud["hostname"] == "facedoor-pi"
    assert ud["ssh_pwauth"] is False  # key given -> password SSH off
    user = ud["users"][0]
    assert user["name"] == "door" and user["lock_passwd"] is False
    assert user["passwd"].startswith("$6$rounds=100000$")
    assert user["ssh_authorized_keys"] == [KEY]
    assert "default" not in ud["users"]
    env = next(f for f in ud["write_files"] if f["path"] == "/etc/facedoor/firstboot.env")["content"]
    env_kv = dict(ln.split("=", 1) for ln in env.splitlines() if ln and not ln.startswith("#"))
    assert env_kv["FACEDOOR_USER"] == "door"
    assert env_kv["FACEDOOR_DIR"] == "/opt/facedoor"
    assert env_kv["FACEDOOR_ENABLE_SERVICE"] == "1"
    assert env_kv["FACEDOOR_BUNDLE"] == "/boot/firmware/facedoor-bundle.tar.gz"
    unit = next(f for f in ud["write_files"] if f["path"].endswith("facedoor-firstboot.service"))["content"]
    assert "ConditionPathExists=!/var/lib/facedoor/firstboot.done" in unit
    assert any("/boot/firmware/facedoor-firstboot.sh" in str(c) for c in ud["runcmd"])

    nc = yaml.safe_load((out / "network-config").read_text(encoding="utf-8"))["network"]
    assert nc["version"] == 2 and nc["ethernets"]["eth0"]["dhcp4"] is True
    ap = nc["wifis"]["wlan0"]["access-points"]
    assert list(ap) == [tricky_ssid]
    assert ap[tricky_ssid]["password"] == T.wpa_psk_hex(tricky_ssid, WIFI_PASS)
    assert ap[tricky_ssid]["hidden"] is True
    assert nc["wifis"]["wlan0"]["regulatory-domain"] == "KR"

    md = yaml.safe_load((out / "meta-data").read_text(encoding="utf-8"))
    assert md["instance-id"].startswith("facedoor-facedoor-pi-") and md["local-hostname"] == "facedoor-pi"

    # no plaintext secret anywhere in the staging output
    for f in out.iterdir():
        data = f.read_bytes()
        if f.suffix == ".gz":
            data = gzip.decompress(data)
        assert PASSWORD.encode() not in data, f.name
        assert WIFI_PASS.encode() not in data, f.name
    assert s["settings"]["wifi_secret_form"] == "psk-hex"
    assert set(s["files"]) == {"user-data", "network-config", "meta-data", "facedoor-firstboot.sh",
                               "facedoor-bundle.tar.gz", "facedoor-bundle.tar.gz.sha256"}
    for name, digest in s["files"].items():
        assert T.sha256_file(out / name) == digest
    assert b"\r" not in (out / "facedoor-firstboot.sh").read_bytes()


def test_stage_without_key_or_wifi(tmp_path):
    proj = make_project(tmp_path / "proj")
    T.stage(base_params(ssh_public_keys=[], wifi=None), tmp_path / "st", proj, generated_at="t")
    ud = yaml.safe_load((tmp_path / "st" / "user-data").read_text(encoding="utf-8"))
    assert ud["ssh_pwauth"] is True and "ssh_authorized_keys" not in ud["users"][0]
    nc = yaml.safe_load((tmp_path / "st" / "network-config").read_text(encoding="utf-8"))["network"]
    assert "wifis" not in nc


def test_stage_cli_reads_stdin_json(tmp_path, monkeypatch):
    proj = make_project(tmp_path / "proj")
    raw = json.dumps(base_params()).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    res = tmp_path / "r.json"
    assert T.main(["--result", str(res), "stage", "--out", str(tmp_path / "o"), "--project-root", str(proj)]) == 0
    assert PASSWORD not in res.read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{not json")))
    assert T.main(["stage", "--out", str(tmp_path / "o2"), "--project-root", str(proj)]) == T.EXIT_USAGE


# ------------------------------------------------------------------------------------------
# bundle
# ------------------------------------------------------------------------------------------
def test_bundle_contents_and_sha256(tmp_path):
    proj = make_project(tmp_path / "proj")
    r = T.build_bundle(proj, tmp_path / "b" / T.BUNDLE_NAME)
    path = Path(r["path"])
    assert r["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    side = (path.parent / (path.name + ".sha256")).read_text(encoding="ascii")
    assert side == f"{r['sha256']}  {T.BUNDLE_NAME}\n"
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
        assert all(n.startswith("facedoor/") and ".." not in n for n in names)
        rel = {n[len("facedoor/"):] for n in names}
        assert {"src/facedoor/__init__.py", "configs/default.yaml", "deploy/install_rpi.sh",
                "tests/assets/sample.jpg", "models/models.lock.json", "requirements-pi.txt",
                "MANIFEST.sha256"} <= rel
        for bad in ("configs/local.yaml", "data/gallery/alice.npy", ".venv/x.py", ".env", "docs/photo.jpg",
                    "models/face.onnx", "deploy/sdcard/out/user-data"):
            assert bad not in rel, bad
        sh = tar.getmember("facedoor/deploy/install_rpi.sh")
        assert sh.mode == 0o755
        assert b"\r" not in tar.extractfile(sh).read()
        manifest = tar.extractfile("facedoor/MANIFEST.sha256").read().decode()
        for line in manifest.splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256(tar.extractfile("facedoor/" + name).read()).hexdigest() == digest
    # deterministic
    r2 = T.build_bundle(proj, tmp_path / "b2" / T.BUNDLE_NAME)
    assert r2["sha256"] == r["sha256"]
    # models opt-in
    r3 = T.build_bundle(proj, tmp_path / "b3" / T.BUNDLE_NAME, include_models=True)
    with tarfile.open(r3["path"], "r:gz") as tar:
        assert "facedoor/models/face.onnx" in tar.getnames()


def test_bundle_requires_src(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "a.yaml").write_text("a: 1")
    with pytest.raises(T.ToolError):
        T.build_bundle(tmp_path, tmp_path / "out" / T.BUNDLE_NAME)


def test_real_project_bundle_has_no_private_data(tmp_path):
    r = T.build_bundle(ROOT, tmp_path / T.BUNDLE_NAME)
    assert r["size"] < T.BUNDLE_MAX_BYTES
    with tarfile.open(r["path"], "r:gz") as tar:
        names = tar.getnames()
    assert "facedoor/deploy/sdcard/firstboot/facedoor-firstboot.sh" in names
    assert "facedoor/deploy/install_rpi.sh" in names
    assert not any(n.startswith(("facedoor/data/", "facedoor/logs/", "facedoor/.venv/", "facedoor/deploy/sdcard/out/"))
                   for n in names)
    assert not any(n.endswith((".onnx", ".npy", "local.yaml")) for n in names)


# ------------------------------------------------------------------------------------------
# write to a regular file + read-back verification
# ------------------------------------------------------------------------------------------
def _raw_image(n: int, seed: int = 1) -> bytes:
    rnd = hashlib.sha256(str(seed).encode()).digest()
    data = bytearray((rnd * (n // len(rnd) + 1))[:n])
    for i in range(0, n, 4096):  # make it less trivially periodic
        data[i:i + 8] = i.to_bytes(8, "little")
    data[510:512] = b"\x55\xaa"
    return bytes(data)


@pytest.mark.parametrize("n", [T.HEAD_BYTES * 2 + 12345, 200_000])
def test_write_raw_image_to_file_and_verify(tmp_path, n):
    img = tmp_path / "test.img"
    raw = _raw_image(n)
    img.write_bytes(raw)
    target = tmp_path / "card.bin"
    r = T.write_image(img, str(target), None, allow_file_target=True, progress_stream=io.StringIO())
    assert r["written_bytes"] == n and r["verify_ok"] is True
    assert r["sha256"] == hashlib.sha256(raw).hexdigest()
    got = target.read_bytes()
    assert got[:n] == raw and len(got) % T.SECTOR == 0
    # read-back detects corruption
    with open(target, "r+b") as f:
        f.seek(n // 2)
        f.write(b"CORRUPT")
    assert T.verify_target(str(target), n, r["sha256"], "full", io.StringIO()) is False
    assert T.verify_target(str(target), n, r["sha256"], "quick", io.StringIO()) is True  # MBR signature only


def test_write_xz_image_and_size(tmp_path):
    raw = _raw_image(T.HEAD_BYTES + 777_777, seed=2)
    img = tmp_path / "test.img.xz"
    img.write_bytes(lzma.compress(raw, format=lzma.FORMAT_XZ))
    assert T.xz_uncompressed_size(img) == len(raw)
    info = T.main(["image-info", "--image", str(img)])
    assert info == 0
    target = tmp_path / "card.bin"
    target.write_bytes(b"\xee" * (len(raw) + 4 * T.SECTOR))  # existing "card" with old data
    r = T.write_image(img, str(target), len(raw) + 4 * T.SECTOR, decompressor="lzma", allow_file_target=True,
                      progress_stream=io.StringIO())
    assert r["verify_ok"] and target.read_bytes()[:len(raw)] == raw


def test_write_refuses_bad_targets(tmp_path):
    img = tmp_path / "a.img"
    img.write_bytes(_raw_image(T.HEAD_BYTES * 2))
    with pytest.raises(T.ToolError):  # plain file without the test-only flag
        T.write_image(img, str(tmp_path / "x.bin"), None)
    with pytest.raises(T.ToolError):  # test-only flag never applies to device paths
        T.write_image(img, "/dev/sdz", None, allow_file_target=True)
    with pytest.raises(T.ToolError):
        T.write_image(img, "\\\\.\\PhysicalDrive9", None, allow_file_target=True)
    with pytest.raises(T.ToolError) as ei:  # image larger than the target
        T.write_image(img, str(tmp_path / "y.bin"), T.HEAD_BYTES, allow_file_target=True)
    assert ei.value.code == T.EXIT_WRITE


def test_write_cli_refuses_unsafe_device_before_opening(tmp_path, monkeypatch):
    img = tmp_path / "a.img"
    img.write_bytes(_raw_image(T.HEAD_BYTES * 2))
    system_disk = {"id": "sda", "path": "/dev/sda", "size": 256 * 10 ** 9, "model": "SSD", "bus": "sata",
                   "removable": False, "system_mounts": ["/"]}
    monkeypatch.setattr(T, "collect_devices", lambda protect=(): [system_disk])
    monkeypatch.setattr(T, "write_image", lambda *a, **k: pytest.fail("write_image must not be called"))
    assert T.main(["write", "--image", str(img), "--target", "/dev/sda"]) == T.EXIT_DEVICE
    assert T.main(["write", "--image", str(img), "--target", "/dev/sda1"]) == T.EXIT_DEVICE


# ------------------------------------------------------------------------------------------
# device safety
# ------------------------------------------------------------------------------------------
def card(**kw):
    d = {"id": "sdb", "path": "/dev/sdb", "size": 32 * 10 ** 9, "model": "Generic SD reader", "bus": "usb",
         "removable": True, "readonly": False, "is_system": False, "is_boot": False, "partitions": [],
         "system_mounts": [], "holders": [], "protected_hits": []}
    d.update(kw)
    return d


def test_assess_good_card():
    a = T.assess_device(card())
    assert a["ok"] and a["reasons"] == []
    assert T.assess_device(card(bus="sd", removable=False))["ok"]
    assert T.assess_device(card(bus="mmc", removable=False))["ok"]
    assert T.assess_device(card(bus="Secure Digital"))["ok"]
    assert T.assess_device(card(bus=7))["ok"]  # Windows numeric BusType 7 = USB
    w = T.assess_device(card(size=8 * 10 ** 9 - 10 ** 8, partitions=[{"mountpoint": "/media/u/BOOT"}]))
    assert w["ok"] and len(w["warnings"]) == 2


@pytest.mark.parametrize("kw,reason", [
    ({"is_system": True}, "system disk"),
    ({"is_boot": True}, "boot disk"),
    ({"system_mounts": ["/"]}, "system mount"),
    ({"system_mounts": ["C:\\"]}, "system mount"),
    ({"protected_hits": ["/home/u/img.xz"]}, "files this tool"),
    ({"holders": ["lvm:vg-root"]}, "LVM"),
    ({"bus": "nvme"}, "not USB/SD/MMC"),
    ({"bus": "sata"}, "not USB/SD/MMC"),
    ({"bus": ""}, "not USB/SD/MMC"),
    ({"bus": "virtual"}, "not USB/SD/MMC"),
    ({"removable": False}, "FIXED media"),
    ({"bus": "mmc", "emmc": True}, "eMMC"),
    ({"internal": True}, "internal disk"),
    ({"readonly": True}, "read-only"),
    ({"size": 0}, "no media"),
    ({"size": 513 * GiB}, "too large"),
    ({"size": 1000 * 10 ** 9}, "too large"),
    ({"size": 4 * 10 ** 9}, "too small"),
    ({"virtual": True}, "virtual"),
])
def test_assess_refusals(kw, reason):
    a = T.assess_device(card(**kw))
    assert not a["ok"]
    assert any(reason.lower() in r.lower() for r in a["reasons"]), a["reasons"]


def test_assess_limits_can_only_tighten():
    big = card(size=600 * GiB)
    assert not T.assess_device(big, max_bytes=10 ** 15)["ok"]  # ceiling cannot be raised
    assert not T.assess_device(card(size=64 * 10 ** 9), max_bytes=32 * 10 ** 9)["ok"]  # but can be lowered
    assert not T.assess_device(card(size=5 * 10 ** 9), min_bytes=1)["ok"]  # floor cannot be lowered
    assert not T.assess_device(card(size=8 * 10 ** 9), image_bytes=9 * 10 ** 9)["ok"]
    assert T.assess_device(card(size=512 * GiB))["ok"]  # exactly the ceiling is allowed


LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "maj:min": "259:0", "size": 512110190592, "model": "Samsung SSD",
     "tran": "nvme", "rm": False, "ro": False, "type": "disk", "mountpoint": None, "children": [
         {"name": "nvme0n1p1", "maj:min": "259:1", "size": 536870912, "type": "part", "fstype": "vfat",
          "mountpoint": "/boot/efi"},
         {"name": "nvme0n1p2", "maj:min": "259:2", "size": 511000000000, "type": "part", "fstype": "LVM2_member",
          "mountpoint": None, "children": [
              {"name": "vg-root", "maj:min": "253:0", "size": 511000000000, "type": "lvm", "fstype": "ext4",
               "mountpoint": "/"}]}]},
    {"name": "sda", "path": "/dev/sda", "maj:min": "8:0", "size": "2000398934016", "model": "Backup HDD",
     "tran": "usb", "rm": "0", "hotplug": "1", "ro": "0", "type": "disk", "children": [
         {"name": "sda1", "maj:min": "8:1", "size": 2000397885440, "type": "part", "fstype": "ntfs",
          "mountpoint": "/media/u/Backup"}]},
    {"name": "sdb", "path": "/dev/sdb", "maj:min": "8:16", "size": 31914983424, "vendor": "Generic ",
     "model": "STORAGE DEVICE", "tran": "usb", "rm": True, "hotplug": True, "ro": False, "type": "disk",
     "children": [
         {"name": "sdb1", "maj:min": "8:17", "size": 268435456, "type": "part", "fstype": "vfat",
          "label": "system-boot", "mountpoint": "/media/u/system-boot"},
         {"name": "sdb2", "maj:min": "8:18", "size": 3500000000, "type": "part", "fstype": "ext4",
          "label": "writable", "mountpoint": None}]},
    {"name": "sdc", "path": "/dev/sdc", "maj:min": "8:32", "size": 63864569856, "model": "Card Reader",
     "tran": "usb", "rm": True, "ro": False, "type": "disk", "children": [
         {"name": "sdc1", "maj:min": "8:33", "size": 63864569856, "type": "part", "fstype": "exfat",
          "mountpoint": "/media/u/STICK"}]},
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "maj:min": "179:0", "size": 31268536320, "tran": None,
     "rm": False, "ro": False, "type": "disk", "children": []},
    {"name": "mmcblk0boot0", "path": "/dev/mmcblk0boot0", "maj:min": "179:8", "size": 4194304, "rm": False,
     "ro": True, "type": "disk"},
    {"name": "mmcblk1", "path": "/dev/mmcblk1", "maj:min": "179:16", "size": 15931539456, "tran": None,
     "rm": False, "ro": False, "type": "disk", "children": [
         {"name": "mmcblk1p1", "maj:min": "179:17", "size": 15931539456, "type": "part", "fstype": "vfat",
          "mountpoint": None}]},
    {"name": "loop0", "path": "/dev/loop0", "maj:min": "7:0", "size": 1000, "type": "loop"},
    {"name": "zram0", "path": "/dev/zram0", "maj:min": "252:0", "size": 8 * 10 ** 9, "type": "disk",
     "mountpoint": "[SWAP]"},
]}


def test_normalize_lsblk_and_assess():
    devs = {d["id"]: d for d in T.normalize_lsblk(LSBLK, protect={(8, 33): "/media/u/STICK/ubuntu.img.xz"})}
    assert set(devs) == {"nvme0n1", "sda", "sdb", "sdc", "mmcblk0", "mmcblk1"}
    res = {k: T.assess_device(v) for k, v in devs.items()}
    assert res["sdb"]["ok"], res["sdb"]
    assert res["mmcblk1"]["ok"], res["mmcblk1"]  # built-in SD slot
    assert devs["sdb"]["model"] == "Generic STORAGE DEVICE"
    assert [p["label"] for p in devs["sdb"]["partitions"]] == ["system-boot", "writable"]
    assert not res["nvme0n1"]["ok"] and "/" in devs["nvme0n1"]["system_mounts"]
    assert devs["nvme0n1"]["holders"] == ["lvm:vg-root"]
    assert not res["sda"]["ok"]  # USB HDD: fixed media and > 512 GiB
    assert not res["sdc"]["ok"] and devs["sdc"]["protected_hits"]  # holds the image file
    assert devs["mmcblk0"]["emmc"] and not res["mmcblk0"]["ok"]


WIN_DISKS = [
    {"number": 0, "friendly": "NVMe SSD", "size": 1024209543168, "bus": "NVMe", "is_system": True, "is_boot": True,
     "readonly": False, "media_type": "Fixed hard disk media",
     "partitions": [{"number": 1, "size": 1, "letter": "", "is_system": True, "type": "System"},
                    {"number": 3, "size": 2, "letter": "C", "is_boot": True, "type": "Basic"}]},
    {"number": 1, "friendly": "Second SATA", "size": 256 * 10 ** 9, "bus": "SATA", "is_system": False,
     "is_boot": False, "readonly": False, "media_type": "Fixed hard disk media",
     "partitions": [{"number": 1, "size": 2, "letter": "D", "type": "Basic"}]},
    {"number": 2, "friendly": "Generic- SD/MMC USB Device", "size": 31914983424, "bus": "USB", "is_system": False,
     "is_boot": False, "readonly": False, "media_type": "Removable Media",
     "partitions": [{"number": 1, "size": 268435456, "letter": "E", "type": "FAT32"}]},
    {"number": 3, "friendly": "WD Passport", "size": 500 * 10 ** 9, "bus": "USB", "is_system": False,
     "is_boot": False, "readonly": False, "media_type": "External hard disk media", "partitions": []},
    {"number": 4, "friendly": "SD in USB reader", "size": 63864569856, "bus": 7, "is_system": False,
     "is_boot": False, "readonly": False, "media_type": "Removable Media",
     "partitions": [{"number": 1, "size": 63864569856, "letter": "F", "type": "Basic"}]},
    {"number": 5, "friendly": "Locked SD", "size": 15931539456, "bus": "SD", "is_system": False,
     "is_boot": False, "readonly": True, "media_type": "", "partitions": []},
]


def test_normalize_windows_and_assess():
    devs = T.normalize_windows(WIN_DISKS, "C:", protect_paths=["F:\\images\\ubuntu.img.xz"])
    by = {d["id"]: (d, T.assess_device(d)) for d in devs}
    assert by["2"][1]["ok"] and by["2"][0]["path"] == "\\\\.\\PhysicalDrive2"
    assert not by["0"][1]["ok"] and "C:\\" in by["0"][0]["system_mounts"]
    assert not by["1"][1]["ok"]  # SATA
    assert not by["3"][1]["ok"]  # external HDD (fixed media over USB)
    assert not by["4"][1]["ok"] and by["4"][0]["protected_hits"]  # holds the image
    assert not by["5"][1]["ok"]  # read-only
    # the system drive letter is refused even if Windows does not flag the disk as system
    odd = dict(WIN_DISKS[2], partitions=[{"number": 1, "size": 1, "letter": "E", "type": "Basic"}])
    d = T.normalize_windows([odd], "E:")[0]
    assert not T.assess_device(d)["ok"]


MAC_LIST = {"AllDisksAndPartitions": [
    {"DeviceIdentifier": "disk0", "Size": 500277790720, "Partitions": [
        {"DeviceIdentifier": "disk0s1", "Size": 524288000, "Content": "EFI"},
        {"DeviceIdentifier": "disk0s2", "Size": 499000000000, "Content": "Apple_APFS"}]},
    {"DeviceIdentifier": "disk4", "Size": 31914983424, "Partitions": [
        {"DeviceIdentifier": "disk4s1", "Size": 268435456, "Content": "Windows_FAT_32", "VolumeName": "system-boot",
         "MountPoint": "/Volumes/system-boot"}]},
    {"DeviceIdentifier": "disk5", "Size": 1000204886016, "Partitions": [
        {"DeviceIdentifier": "disk5s1", "Size": 1000204886016, "MountPoint": "/Volumes/Backup"}]},
    {"DeviceIdentifier": "disk6", "Size": 63864569856, "Partitions": [
        {"DeviceIdentifier": "disk6s1", "Size": 63864569856, "MountPoint": "/Volumes/WORK"}]},
]}
MAC_INFO = {
    "disk0": {"Internal": True, "BusProtocol": "Apple Fabric", "TotalSize": 500277790720, "RemovableMedia": False,
              "MediaName": "APPLE SSD", "WritableMedia": True, "VirtualOrPhysical": "Physical"},
    "disk4": {"Internal": False, "BusProtocol": "USB", "TotalSize": 31914983424, "RemovableMedia": True,
              "MediaName": "SD Card Reader", "WritableMedia": True, "VirtualOrPhysical": "Physical"},
    "disk5": {"Internal": False, "BusProtocol": "USB", "TotalSize": 1000204886016, "RemovableMedia": False,
              "MediaName": "Ext SSD", "WritableMedia": True},
    "disk6": {"Internal": False, "BusProtocol": "USB", "TotalSize": 63864569856, "RemovableMedia": True,
              "MediaName": "Reader", "WritableMedia": True},
}


def test_normalize_macos_and_assess():
    devs = {d["id"]: d for d in T.normalize_macos(MAC_LIST, MAC_INFO, protect_paths=["/Volumes/WORK/img.xz"])}
    assert devs["disk4"]["path"] == "/dev/rdisk4"
    assert T.assess_device(devs["disk4"])["ok"]
    assert not T.assess_device(devs["disk0"])["ok"]
    assert not T.assess_device(devs["disk5"])["ok"]
    assert not T.assess_device(devs["disk6"])["ok"]


def test_guard_device(monkeypatch):
    good = card()
    collector = lambda protect=(): [good, card(id="sda", path="/dev/sda", bus="sata", removable=False)]  # noqa: E731
    g = T.guard_device("/dev/sdb", good["size"], collector=collector)
    assert g["assessment"]["ok"]
    for target, size in (("/dev/sda", None), ("/dev/sdb1", None), ("/dev/sdx", None), ("/dev/sdb", 1)):
        with pytest.raises(T.ToolError) as ei:
            T.guard_device(target, size, collector=collector)
        assert ei.value.code == T.EXIT_DEVICE
    mac = lambda protect=(): [card(id="disk4", path="/dev/rdisk4")]  # noqa: E731
    assert T.guard_device("/dev/disk4", collector=mac)["id"] == "disk4"


def test_assess_devices_cli(monkeypatch, tmp_path):
    raw = json.dumps([card(), card(id="sda", is_system=True)]).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    res = tmp_path / "a.json"
    assert T.main(["--result", str(res), "assess-devices"]) == 0
    out = json.loads(res.read_text(encoding="utf-8"))["devices"]
    assert [d["assessment"]["ok"] for d in out] == [True, False]


def test_list_and_check_device_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "collect_devices", lambda protect=(): [card()])
    res = tmp_path / "l.json"
    assert T.main(["--result", str(res), "list-devices", "--max-size-gb", "16"]) == 0
    d = json.loads(res.read_text(encoding="utf-8"))["devices"][0]
    assert not d["assessment"]["ok"]  # 32 GB card above the lowered 16 GB ceiling
    assert T.main(["check-device", "--target", "/dev/sdb", "--expect-size", str(card()["size"])]) == 0
    assert T.main(["check-device", "--target", "/dev/sdb", "--expect-size", "123"]) == T.EXIT_DEVICE


# ------------------------------------------------------------------------------------------
# boot partition copy / scrub
# ------------------------------------------------------------------------------------------
def _fake_boot(tmp_path: Path) -> Path:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "config.txt").write_text("[all]\n")
    (boot / "cmdline.txt").write_text("console=tty1\n")
    (boot / "user-data").write_text("#cloud-config\n# image default\n")
    return boot


def test_copy_to_boot_and_scrub(tmp_path):
    proj = make_project(tmp_path / "proj")
    st = tmp_path / "stage"
    s = T.stage(base_params(), st, proj, generated_at="t")
    boot = _fake_boot(tmp_path)
    r = T.copy_to_boot(st, boot)
    assert r["ok"] and set(r["copied"]) == set(s["files"])
    for n, digest in s["files"].items():
        assert T.sha256_file(boot / n) == digest
    assert not list(boot.glob("*.tmp"))
    removed = T.scrub_stage(st)
    assert set(removed) == {"user-data", "network-config", "meta-data"}
    assert not (st / "user-data").exists() and (st / T.BUNDLE_NAME).exists()


def test_copy_to_boot_refusals(tmp_path):
    proj = make_project(tmp_path / "proj")
    st = tmp_path / "stage"
    T.stage(base_params(), st, proj, generated_at="t")
    notboot = tmp_path / "random"
    notboot.mkdir()
    with pytest.raises(T.ToolError) as ei:
        T.copy_to_boot(st, notboot)
    assert ei.value.code == T.EXIT_DEVICE
    with pytest.raises(T.ToolError):
        T.copy_to_boot(st, tmp_path / "missing")
    boot = _fake_boot(tmp_path)
    (st / "user-data").write_text("tampered")
    with pytest.raises(T.ToolError) as ei:
        T.copy_to_boot(st, boot)
    assert ei.value.code == T.EXIT_CHECKSUM
    with pytest.raises(T.ToolError):
        T.copy_to_boot(tmp_path / "nostage", boot)


# ------------------------------------------------------------------------------------------
# image resolve / download / verify (network mocked)
# ------------------------------------------------------------------------------------------
SUMS = "\n".join([
    "a" * 64 + " *ubuntu-24.04-preinstalled-server-arm64+raspi.img.xz",
    "b" * 64 + " *ubuntu-24.04.3-preinstalled-server-arm64+raspi.img.xz",
    "c" * 64 + " *ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
    "d" * 64 + " *ubuntu-24.04.3-preinstalled-desktop-arm64+raspi.img.xz",
    "e" * 64 + " *ubuntu-24.04.3-live-server-amd64.iso",
])


def test_resolve_image_picks_latest_server(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_http_get", lambda url, timeout=60: calls.append(url) or SUMS.encode())
    r = T.resolve_image("https://example.invalid/releases/24.04/release")
    assert r["name"] == "ubuntu-24.04.3-preinstalled-server-arm64+raspi.img.xz"
    assert r["sha256"] == "b" * 64 and r["url"].endswith("/" + r["name"])
    assert calls == ["https://example.invalid/releases/24.04/release/SHA256SUMS"]
    with pytest.raises(T.ToolError):
        T.pick_raspi_image({"foo.iso": "a" * 64})


def test_http_get_refuses_plain_http():
    with pytest.raises(T.ToolError):
        T._http_get("http://cdimage.ubuntu.com/SHA256SUMS")
    with pytest.raises(T.ToolError):
        T.download("http://x/y.img.xz", Path("unused"), "0" * 64)


class _FakeResp(io.BytesIO):
    status = 200

    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_download_verifies_sha256(monkeypatch, tmp_path):
    payload = b"fake image payload" * 1000
    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(T.urllib.request, "urlopen", lambda req, timeout=60: _FakeResp(payload))
    r = T.download("https://example.invalid/x.img.xz", tmp_path / "x.img.xz", good)
    assert r["sha256"] == good and not r["reused"]
    assert T.download("https://example.invalid/x.img.xz", tmp_path / "x.img.xz", good)["reused"]
    with pytest.raises(T.ToolError) as ei:
        T.download("https://example.invalid/y.img.xz", tmp_path / "y.img.xz", "0" * 64)
    assert ei.value.code == T.EXIT_CHECKSUM
    assert (tmp_path / "y.img.xz.part.bad").exists() and not (tmp_path / "y.img.xz").exists()


def test_verify_image(tmp_path):
    img = tmp_path / "ubuntu-24.04.3-preinstalled-server-arm64+raspi.img.xz"
    img.write_bytes(b"data")
    digest = hashlib.sha256(b"data").hexdigest()
    sums = f"{digest} *{img.name}\n"
    assert T.verify_image(img, None, sums)["ok"]
    assert T.verify_image(img, digest.upper(), None)["ok"]
    with pytest.raises(T.ToolError) as ei:
        T.verify_image(img, "1" * 64, None)
    assert ei.value.code == T.EXIT_CHECKSUM
    with pytest.raises(T.ToolError):
        T.verify_image(img, None, "0" * 64 + " other.img.xz\n")
    with pytest.raises(T.ToolError):
        T.verify_image(img, "nothex", None)


# ------------------------------------------------------------------------------------------
# wrapper / first-boot scripts (static checks; nothing is executed against a device)
# ------------------------------------------------------------------------------------------
SH_FILES = [SDCARD / "flash_sdcard.sh", SDCARD / "firstboot" / "facedoor-firstboot.sh"]


def _bash():
    for c in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if c and os.path.isfile(c):
            return c
    return None


@pytest.mark.parametrize("path", SH_FILES, ids=lambda p: p.name)
def test_shell_scripts_lf_and_syntax(path):
    data = path.read_bytes()
    assert b"\r" not in data and data.startswith(b"#!/usr/bin/env bash\n")
    bash = _bash()
    if not bash:
        pytest.skip("bash not available")
    r = subprocess.run([bash, "-n", "-"], input=data, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


def test_wrappers_have_no_skip_confirmation_option():
    sh = (SDCARD / "flash_sdcard.sh").read_text(encoding="utf-8")
    assert "ERASE" in sh and "DRY_RUN=1" in sh  # dry run is the default
    assert '-y|--yes|--force|--no-confirm) die' in sh
    ps = (SDCARD / "flash_sdcard.ps1").read_text(encoding="ascii")  # ASCII: PowerShell 5.1 reads BOM-less files as ANSI
    param_block = ps[ps.index("param("): ps.index(")\n\nSet-StrictMode")]
    for bad in ("$Yes", "$Force", "$NoConfirm", "$Confirm"):
        assert bad not in param_block
    assert '-cne "ERASE"' in ps and "$IsDry = -not $Write" in ps
    assert "IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)" in ps
    assert "AsSecureString" in ps and "AsPlainText" not in ps


def test_firstboot_never_starts_service_or_touches_gpio():
    fb = (SDCARD / "firstboot" / "facedoor-firstboot.sh").read_text(encoding="utf-8")
    code = [ln for ln in fb.splitlines() if not ln.lstrip().startswith("#") and "log " not in ln]
    joined = "\n".join(code)
    assert "--start" not in joined
    assert "hw_selftest" not in joined and "gpioset" not in joined and "lgpio" not in joined
    assert "systemctl start" not in joined and "systemctl restart" not in joined
    assert "sha256sum" in joined and "firstboot.done" in fb and "MANIFEST.sha256" in joined
    assert 'source "$ENV_FILE"' not in fb and ". \"$ENV_FILE\"" not in fb


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 parser check runs on Windows only")
def test_ps1_parses_in_windows_powershell():
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps:
        pytest.skip("powershell not available")
    path = str(SDCARD / "flash_sdcard.ps1")
    cmd = ("$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:FD_PS1,"
           "[ref]$t,[ref]$e);exit $e.Count")
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, timeout=120,
                       env=dict(os.environ, FD_PS1=path))
    assert r.returncode == 0, r.stdout.decode(errors="replace")
