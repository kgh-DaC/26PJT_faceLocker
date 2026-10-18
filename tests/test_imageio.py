"""imageio.ascii_safe_path platform behaviour (QA C-1). Platform is mocked via sys.platform."""

from __future__ import annotations

from pathlib import Path

import pytest

from facedoor import imageio
from facedoor.errors import CameraError, FaceDoorError, ModelError


@pytest.fixture
def korean_file(tmp_path) -> Path:
    d = tmp_path / "얼굴인식"
    d.mkdir()
    f = d / "model.onnx"
    f.write_bytes(b"\x00model-bytes" * 10)
    return f


@pytest.fixture
def ascii_cache(tmp_path, monkeypatch) -> Path:
    c = tmp_path / "cache"
    assert imageio._is_ascii(str(c)), "tmp_path must be ASCII for this test"
    monkeypatch.setattr(imageio, "_cache_dir", lambda: c)
    return c


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_returns_original_path_without_copy(korean_file, monkeypatch, platform):
    monkeypatch.setattr(imageio.sys, "platform", platform)

    def boom():
        raise AssertionError("cache dir must not be touched off Windows")

    monkeypatch.setattr(imageio, "_cache_dir", boom)
    assert imageio.ascii_safe_path(korean_file) == korean_file.resolve()


def test_non_windows_permission_problem_cannot_happen(korean_file, monkeypatch):
    """systemd ProtectHome=read-only: mkdir would fail, but on Linux nothing is created."""
    monkeypatch.setattr(imageio.sys, "platform", "linux")

    def no_mkdir(*a, **k):
        raise PermissionError("read-only home")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    assert imageio.ascii_safe_path(korean_file) == korean_file.resolve()


def test_windows_non_ascii_copies_to_ascii_cache(korean_file, ascii_cache, monkeypatch):
    monkeypatch.setattr(imageio.sys, "platform", "win32")
    dst = imageio.ascii_safe_path(korean_file)
    assert dst.parent == ascii_cache and imageio._is_ascii(str(dst))
    assert dst.suffix == ".onnx" and dst.read_bytes() == korean_file.read_bytes()
    assert imageio.ascii_safe_path(korean_file) == dst  # cached, idempotent


def test_windows_ascii_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(imageio.sys, "platform", "win32")
    f = tmp_path / "m.onnx"
    f.write_bytes(b"x")
    assert imageio.ascii_safe_path(f) == f.resolve()


def test_windows_copy_permission_error_becomes_model_error(korean_file, ascii_cache, monkeypatch):
    monkeypatch.setattr(imageio.sys, "platform", "win32")

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(imageio.shutil, "copyfile", denied)
    with pytest.raises(ModelError) as ei:
        imageio.ascii_safe_path(korean_file)
    assert isinstance(ei.value, FaceDoorError) and "PermissionError" in str(ei.value)
    assert isinstance(ei.value.__cause__, PermissionError)


def test_windows_mkdir_failure_uses_error_cls(korean_file, ascii_cache, monkeypatch):
    monkeypatch.setattr(imageio.sys, "platform", "win32")

    def no_mkdir(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    with pytest.raises(CameraError):
        imageio.ascii_safe_path(korean_file, error_cls=CameraError)


def test_windows_missing_non_ascii_file_is_model_error(tmp_path, ascii_cache, monkeypatch):
    monkeypatch.setattr(imageio.sys, "platform", "win32")
    with pytest.raises(ModelError):
        imageio.ascii_safe_path(tmp_path / "없음" / "model.onnx")
