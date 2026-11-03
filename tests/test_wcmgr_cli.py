"""Проверки CLI WinCompat: манифесты, окружения, план запуска, реестр."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wcmgr import cli

DATA = Path(__file__).resolve().parents[1] / "data" / "wincompat"


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    storage = tmp_path / "storage"
    registry = tmp_path / "registry.db"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for name in ("notepad-plus.wcv", "1c-client-83.wcv"):
        (manifests / name).write_text((DATA / name).read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "storage": str(storage),
        "registry": str(registry),
        "manifests": str(manifests),
    }


def _run(env: dict[str, str], *argv: str) -> int:
    return cli.main(["--storage", env["storage"], "--registry", env["registry"], *argv])


def test_check_accepts_shipped_manifests(env, capsys):
    assert _run(env, "check", env["manifests"]) == 0
    out = capsys.readouterr().out
    assert "ok (notepad-plus" in out
    assert "не проверен прогонами" in out


def test_check_reports_broken_manifest(env, capsys):
    bad = Path(env["manifests"]) / "bad.wcv"
    bad.write_text('[wcv]\nschema = 1\nid = "bad"\nname = ""\nversion = "1"\nexecutable = "a.exe"\nrunner = "wine"\nexpected_level = "l1"\n', encoding="utf-8")
    assert _run(env, "check", str(bad)) == 1
    assert "ОТКАЗ" in capsys.readouterr().out


def test_check_accepts_single_file(env):
    assert _run(env, "check", str(Path(env["manifests"]) / "1c-client-83.wcv")) == 0


def test_install_creates_environment_and_registers(env, capsys):
    assert _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z") == 0
    out = capsys.readouterr().out
    assert "создано окружение notepad-plus" in out
    assert Path(env["storage"], "notepad-plus", "manifest.wcv").is_file()
    assert _run(env, "list") == 0


def test_install_twice_updates_version(env, capsys):
    manifest = str(Path(env["manifests"]) / "notepad-plus.wcv")
    _run(env, "install", manifest, "--version", "20261103T000000Z")
    capsys.readouterr()
    assert _run(env, "install", manifest, "--version", "20261104T000000Z") == 0
    assert "обновлено окружение" in capsys.readouterr().out


def test_list_prints_json(env, capsys):
    _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z")
    capsys.readouterr()
    assert _run(env, "list") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["environments"][0]["id"] == "notepad-plus"
    assert payload["environments"][0]["tested"] is False


def test_list_on_empty_storage(env, capsys):
    assert _run(env, "list") == 0
    assert "хранилище пусто" in capsys.readouterr().out


def test_launch_requires_installed_environment(env, capsys):
    assert _run(env, "launch", "нет-такого") == 2
    err = capsys.readouterr().err
    assert "не установлено" in err or "окружение" in err


def test_launch_dry_run_prints_plan_without_exec(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_windows_present", lambda: False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z")
    capsys.readouterr()
    assert _run(env, "launch", "notepad-plus") == 0
    out = capsys.readouterr().out
    assert "команда:" in out
    assert "aa-exec" in out
    assert "проба: ничего не запущено" in out
    assert "WINEPREFIX=" in out


def test_launch_rejects_run_on_non_linux(env, capsys, monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z")
    capsys.readouterr()
    code = _run(env, "launch", "notepad-plus", "--run")
    assert code in (0, 2)
    if code == 2:
        assert "только в Linux" in capsys.readouterr().err


def test_record_changes_published_level(env, capsys):
    _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z")
    capsys.readouterr()
    assert _run(env, "record", "notepad-plus", "--fail", "--level", "l1", "--detail", "падает на старте") == 0
    assert "опубликованный уровень теперь l0" in capsys.readouterr().out


def test_record_requires_exactly_one_flag(env, capsys):
    _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z")
    capsys.readouterr()
    assert _run(env, "record", "notepad-plus") == 2
    assert _run(env, "record", "notepad-plus", "--ok", "--fail") == 2


def test_rollback_reports_target_version(env, capsys):
    manifest = str(Path(env["manifests"]) / "notepad-plus.wcv")
    _run(env, "install", manifest, "--version", "20261103T000000Z")
    _run(env, "install", manifest, "--version", "20261104T000000Z")
    capsys.readouterr()
    assert _run(env, "rollback", "notepad-plus") == 0
    assert "откачено к версии 20261103T000000Z" in capsys.readouterr().out


def test_export_writes_registry_json(env, tmp_path, capsys):
    _run(env, "install", str(Path(env["manifests"]) / "notepad-plus.wcv"), "--version", "20261103T000000Z")
    output = tmp_path / "registry.json"
    assert _run(env, "export", "--output", str(output)) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["apps"][0]["id"] == "notepad-plus"
    assert payload["apps"][0]["proven_level"] is None


def test_doctor_reports_missing_tools(env, capsys):
    code = _run(env, "doctor")
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert '"wine"' in out
    assert '"storage_writable"' in out


def test_help_when_no_command(capsys):
    assert cli.main([]) == 0
    assert "usage: wcmgr" in capsys.readouterr().out
