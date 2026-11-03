"""Проверки демона: источники, дедупликация, план запуска, протокол."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bgd import api, model, sources
from bgd.cli import main as cli_main
from wcmgr.fallback import Capabilities
from wcmgr.manifest import load_manifest
from wcmgr.registry import CompatibilityRegistry, RunRecord

DATA = Path(__file__).resolve().parents[1] / "data" / "wincompat"

FLATPAK_JSON = json.dumps(
    [
        {"application": "org.gimp.GIMP", "version": "2.10.36", "name": "GIMP", "description": "Града\tФото"},
        {"application": "org.libreoffice.LibreOffice", "version": "24.2", "name": "LibreOffice"},
        {"application": "notepad-plus", "version": "8.6.8", "name": "Notepad++ (Flatpak)"},
    ]
)


@pytest.fixture()
def wcv_dir(tmp_path) -> Path:
    target = tmp_path / "wcv"
    target.mkdir()
    for name in ("notepad-plus.wcv", "1c-client-83.wcv", "cad-legacy-2007.wcv", "doc-viewer-l2.wcv"):
        (target / name).write_text((DATA / name).read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture()
def service(tmp_path, wcv_dir) -> api.Service:
    context = api.Context(
        wcv_dir=str(wcv_dir),
        registry_path=str(tmp_path / "registry.db"),
        desktop_dirs=(),
        flatpak_list_json=FLATPAK_JSON,
        detect_capabilities=lambda: Capabilities(
            has_kvm=True, hypervisor_ready=True, has_windows_partition=True
        ),
    )
    instance = api.Service(context)
    yield instance
    instance.close()


def test_flatpak_list_parsed():
    apps = sources.parse_flatpak_list(FLATPAK_JSON)
    assert len(apps) == 3
    gimp = apps[0]
    assert gimp.id == "org.gimp.GIMP"
    assert gimp.kind is model.AppKind.LINUX
    assert gimp.version == "2.10.36"


def test_flatpak_empty_and_broken_inputs():
    assert sources.parse_flatpak_list("") == []
    assert sources.parse_flatpak_list("{не json") == []
    assert sources.parse_flatpak_list('{"не": "список"}') == []


def test_wcv_directory_scanned(wcv_dir):
    apps = sources.scan_wcv_directory(wcv_dir)
    assert {app.id for app in apps} == {"notepad-plus", "1c-client-83", "cad-legacy-2007", "doc-viewer-l2"}
    by_id = {app.id: app for app in apps}
    assert by_id["cad-legacy-2007"].kind is model.AppKind.REBOOT
    assert by_id["doc-viewer-l2"].coverage == "l2"
    # Ни один из примеров не подтверждён прогонами — демон обязан это показать.
    assert all("не проверено" in app.evidence for app in apps)


def test_broken_manifest_is_skipped_not_fatal(wcv_dir):
    (wcv_dir / "broken.wcv").write_text("[wcv]\nschema = 1\n", encoding="utf-8")
    apps = sources.scan_wcv_directory(wcv_dir)
    assert "broken" not in {app.id for app in apps}
    assert len(apps) == 4


def test_desktop_entry_minimal_parser(tmp_path):
    (tmp_path / "foot.desktop").write_text(
        "[Desktop Entry]\nName=foot\nExec=foot\nIcon=foot\nCategories=TerminalEmulator;System;\n",
        encoding="utf-8",
    )
    (tmp_path / "hidden.desktop").write_text(
        "[Desktop Entry]\nName=Скрытое\nNoDisplay=true\n", encoding="utf-8"
    )
    apps = sources.installed_desktop_entries(tmp_path)
    assert [app.name for app in apps] == ["foot"]
    assert "TerminalEmulator" in apps[0].tags


def test_dedup_prefers_native_linux(wcv_dir):
    apps = sources.parse_flatpak_list(FLATPAK_JSON) + sources.scan_wcv_directory(wcv_dir)
    merged = model.deduplicate(apps)
    notepad = next(app for app in merged if app.id == "notepad-plus")
    assert notepad.kind is model.AppKind.LINUX
    assert notepad.extra["variants"][0]["kind"] == "wincompat"


def test_search_matches_name_and_tags(wcv_dir):
    service = api.Service(
        api.Context(
            wcv_dir=str(wcv_dir),
            registry_path=str(Path(wcv_dir).parent / "r.db"),
            desktop_dirs=(),
            flatpak_list_json=FLATPAK_JSON,
        )
    )
    found = service.find("1с")
    assert found and found[0].id == "1c-client-83"
    assert service.find("сущее-нет") == []
    service.close()


def test_launch_plan_for_wine_app(service, wcv_dir):
    plan = service.launch_plan("1c-client-83")
    assert plan["ok"] is True
    assert plan["action"] == "launch-wine"
    assert plan["level"] == "l1"
    # aa-exec стоит первым: изоляция надевается до вызова Wine.
    assert plan["raw_command"][:4] == ["aa-exec", "-p", "wcv-1c-client-83", "--"]
    assert plan["raw_command"][4] == "wine"
    assert "C:\\1cv8.exe" in plan["raw_command"]
    assert plan["command"][:2] == ["systemd-run", "--user"]
    assert "Slice=wincompat.slice" in " ".join(plan["command"])
    assert plan["environment"]["BG_WCV_ID"] == "1c-client-83"
    assert any("wc-net" in warning for warning in plan["warnings"])
    service.close()


def test_launch_plan_for_reboot_level(service):
    plan = service.launch_plan("cad-legacy-2007")
    assert plan["action"] == "reboot-to-windows"
    assert plan["requires_reboot"] is True
    assert plan["command"][0] == "byteguru-boot-next"
    service.close()


def test_launch_plan_for_vm_level(tmp_path, wcv_dir):
    manifest = load_manifest(str(wcv_dir / "1c-client-83.wcv"))
    vm = replace(
        manifest,
        id="vm-only-app",
        prefix="vm-only-app",
        name="Приложение только через ВМ",
        expected_level=manifest.expected_level.__class__("l3"),
    )
    target = wcv_dir / "vm-app.wcv"
    from wcmgr.manifest import dump_toml

    target.write_text(dump_toml(vm), encoding="utf-8")
    service = api.Service(
        api.Context(
            wcv_dir=str(wcv_dir),
            registry_path=str(tmp_path / "r.db"),
            desktop_dirs=(),
            detect_capabilities=lambda: Capabilities(has_kvm=True, hypervisor_ready=True),
        )
    )
    plan = service.launch_plan(vm.id)
    assert plan["action"] == "launch-vm"
    service.close()


def test_launch_plan_unknown_app(service):
    plan = service.launch_plan("нет-такого")
    assert plan["ok"] is False
    service.close()


def test_registry_evidence_overrides_declared_level(service):
    registry = CompatibilityRegistry(Path(service.context.registry_path))
    manifest = load_manifest(str(Path(service.context.wcv_dir) / "1c-client-83.wcv"))
    registry.register(manifest)
    registry.record_run(RunRecord(manifest.id, ok=False, level=manifest.expected_level, detail="упал на старте"))
    registry.close()
    service.close()

    again = api.Service(
        api.Context(
            wcv_dir=service.context.wcv_dir,
            registry_path=service.context.registry_path,
            desktop_dirs=(),
            detect_capabilities=lambda: Capabilities(has_windows_partition=True),
        )
    )
    app = again.get(manifest.id)
    assert app.coverage == "l0"
    plan = again.launch_plan(manifest.id)
    assert plan["action"] in ("unsupported", "reboot-to-windows")
    again.close()


def test_handle_protocol_methods(service):
    response = service.handle({"method": "apps.search", "params": {"query": "notepad"}})
    assert response["ok"] is True
    assert response["result"][0]["id"] == "notepad-plus"
    assert service.handle({"method": "nope"})["ok"] is False
    assert service.handle({"method": "system.status"})["ok"] is True
    service.close()


def test_status_counts(service):
    status = service.status()
    assert status["apps_total"] >= 5
    assert status["wincompat"] >= 2
    assert status["unproven"] >= 1
    service.close()


def test_cli_apps_and_plan(tmp_path, wcv_dir, capsys):
    registry = str(tmp_path / "r.db")
    assert cli_main(["--wcv-dir", str(wcv_dir), "--registry", registry, "apps", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(item["id"] == "1c-client-83" for item in payload)

    assert cli_main(["--wcv-dir", str(wcv_dir), "--registry", registry, "plan", "1c-client-83", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["action"] in ("launch-wine", "launch-vm", "reboot-to-windows")

    assert cli_main(["--wcv-dir", str(wcv_dir), "--registry", registry, "plan", "нет-такого"]) == 1


def test_cli_doctor_reports_broken_manifest(tmp_path, wcv_dir, capsys):
    (wcv_dir / "broken.wcv").write_text("[wcv]\nschema = 1\nid = \"ok\"\n", encoding="utf-8")
    code = cli_main(
        ["--wcv-dir", str(wcv_dir), "--registry", str(tmp_path / "r.db"), "doctor", "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("broken.wcv" in item["file"] for item in payload["broken"])
