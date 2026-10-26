"""Unit tests for the generic UF2 uploader (builder/tools/uf2upload.py).

Covers the trigger-port policy (never guess an unrelated serial port) and
the Linux mount fallback for systems without a desktop automounter.
"""

import argparse
import pathlib
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("serial")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "builder" / "tools"))
import uf2upload  # noqa: E402

XIAO = (0x2886, 0x00C5)


def _ports(*items):
    return lambda: [SimpleNamespace(device=dev, vid=vid, pid=pid) for dev, vid, pid in items]


@pytest.fixture
def comports(monkeypatch):
    def install(*items):
        monkeypatch.setattr("serial.tools.list_ports.comports", _ports(*items))
    return install


@pytest.fixture(autouse=True)
def fresh_mount_reports(monkeypatch):
    monkeypatch.setattr(uf2upload, "_reported_mount_failures", set())


def test_parse_hwid_accepts_prefixed_and_bare_hex():
    assert uf2upload._parse_hwid("0x2886:0x00C5") == XIAO
    assert uf2upload._parse_hwid("2886:00c5") == XIAO
    with pytest.raises(argparse.ArgumentTypeError):
        uf2upload._parse_hwid("2886")


def test_explicit_port_is_used_without_listing(comports):
    comports()
    assert uf2upload.resolve_trigger_port("/dev/ttyACM5", [XIAO]) == "/dev/ttyACM5"


def test_only_port_matching_board_hwid_is_selected(comports):
    comports(("/dev/ttyUSB9", 0x1A86, 0x7523), ("/dev/ttyACM17", *XIAO))
    assert uf2upload.resolve_trigger_port(None, [XIAO]) == "/dev/ttyACM17"


def test_unrelated_serial_bridge_is_never_touched(comports):
    # Board already in the bootloader: only a CH340 is enumerated.
    comports(("/dev/ttyUSB9", 0x1A86, 0x7523))
    assert uf2upload.resolve_trigger_port(None, [XIAO]) is None


def test_no_port_and_no_hwid_skips_trigger(comports):
    comports(("/dev/ttyUSB9", 0x1A86, 0x7523))
    assert uf2upload.resolve_trigger_port(None, []) is None


def test_unmounted_drive_is_mounted_with_udisksctl(monkeypatch):
    calls = []
    mounted = []
    monkeypatch.setattr(uf2upload.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        uf2upload.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or mounted.append(True)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        uf2upload, "_linux_mountpoint",
        lambda dev: "/run/media/u/XIAOC5BOOT" if mounted else None,
    )
    assert uf2upload._mount_linux("/dev/sdg") == "/run/media/u/XIAOC5BOOT"
    assert calls == [["udisksctl", "mount", "-b", "/dev/sdg", "--no-user-interaction"]]


def test_mount_failure_is_reported_once_and_skips_root_mount(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(uf2upload.shutil, "which", lambda name: None)
    monkeypatch.setattr(uf2upload.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(uf2upload.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert uf2upload._mount_linux("/dev/sdg") is None
    assert uf2upload._mount_linux("/dev/sdg") is None
    out = capsys.readouterr().out
    assert out.count("could not be mounted") == 1
    assert "udisksctl not found" in out
    assert calls == []
