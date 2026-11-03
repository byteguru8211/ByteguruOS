"""Проверки подсистемы WinCompat: окружения, литеры, изоляция, уровни, запуск."""

from __future__ import annotations

import json
import posixpath
from dataclasses import replace

import pytest

from wcmgr import drives, fallback, prefix as prefix_mod, sandbox, spawn
from wcmgr.errors import LaunchError, PrefixError, RegistryError, SandboxError
from wcmgr.manifest import (
    CoverageLevel,
    Manifest,
    NetworkMode,
    Runner,
    dump_toml,
    load_manifest_text,
)

BASE = """
[wcv]
schema = 1
id = "test-app"
name = "Тестовое приложение"
version = "1.0"
executable = "app.exe"
runner = "wine"
expected_level = "l1"
wine = "9.0"
tested = true

[drives]
mappings = [
  { letter = "C", kind = "prefix", mode = "rw" },
  { letter = "Z", kind = "bind", path = "/srv/share", mode = "ro" },
]

[filesystem]
visible = ["/home/%USER%/docs"]

[network]
mode = "allowlist"
domains = ["server.local"]
"""


@pytest.fixture()
def manifest() -> Manifest:
    return load_manifest_text(BASE)



@pytest.fixture()
def posix_prefix() -> str:
    """Канонический POSIX-путь окружения: чистая логика не зависит от_disc_ хоста."""
    return "/var/lib/byteguru/wcv/test-app"

# ------------------------------------------------------------------ окружения
def test_create_prefix_layout(manifest, tmp_path):
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    path = store.create(manifest, version="20261103T000000Z")
    assert (path / "drive_c").is_dir()
    assert (path / "manifest.wcv").is_file()
    assert store.current_version(manifest.prefix_name) == "20261103T000000Z"
    assert store.verify(manifest.prefix_name, manifest)


def test_prefix_ids_are_validated(manifest, tmp_path):
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    for bad in ("../escape", "", "A", "with space", "тест"):
        with pytest.raises(PrefixError):
            store.path_for(bad)


def test_prefix_cannot_escape_storage_root(tmp_path):
    """Символьная ссылка из хранилища наружу не даёт путь вне root."""
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    store.root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("создание символьных ссылок недоступно на этой ОС без прав")
    with pytest.raises(PrefixError):
        store.path_for("link")


def test_double_create_is_refused(manifest, tmp_path):
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    store.create(manifest, version="20261103T000000Z")
    with pytest.raises(PrefixError):
        store.create(manifest, version="20261103T000001Z")


def test_update_keeps_history_and_rollback(manifest, tmp_path):
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    store.create(manifest, version="20261103T000000Z")
    updated = replace(manifest, version="2.0")
    store.update_manifest(updated, version="20261104T000000Z")
    assert store.versions(manifest.prefix_name) == [
        "20261103T000000Z",
        "20261104T000000Z",
    ]
    assert store.current_version(manifest.prefix_name) == "20261104T000000Z"
    rolled = store.rollback(manifest.prefix_name)
    assert rolled == "20261103T000000Z"
    assert store.verify(manifest.prefix_name, manifest)
    restored = load_manifest_text((store.path_for(manifest.prefix_name) / "manifest.wcv").read_text("utf-8"))
    assert restored.version == "1.0"


def test_rollback_without_history_fails(manifest, tmp_path):
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    store.create(manifest, version="20261103T000000Z")
    with pytest.raises(PrefixError):
        store.rollback(manifest.prefix_name)


def test_keep_data_removes_only_versions(manifest, tmp_path):
    store = prefix_mod.PrefixStore(tmp_path / "wcv")
    path = store.create(manifest, version="20261103T000000Z")
    (path / "drive_c" / "user.db").write_text("данные", encoding="utf-8")
    store.remove(manifest.id, keep_data=True)
    assert (path / "drive_c" / "user.db").is_file()
    assert store.versions(manifest.id) == []


# --------------------------------------------------------------------- литеры
def test_mount_plan_always_has_prefix_drive(manifest, posix_prefix):
    plan = drives.mount_plan(manifest, posix_prefix)
    assert plan[0].letter == "C"
    assert plan[0].options == ("bind", "rw")
    assert plan[1].letter == "Z"
    assert plan[1].read_only is True
    assert plan[1].source == "/srv/share"


def test_visible_roots_include_prefix_and_manifest_paths(manifest, posix_prefix):
    roots = drives.visible_roots(manifest, posix_prefix, user="ivan")
    assert any("/home/ivan/docs" in root for root in roots)
    assert any(root.endswith("/srv/share") for root in roots)
    assert all("/home" != root for root in roots)


def test_path_outside_visible_set_is_denied(manifest, posix_prefix):
    assert drives.is_path_allowed("/srv/share/otchet.xlsx", manifest, posix_prefix, user="ivan")
    assert not drives.is_path_allowed("/etc/passwd", manifest, posix_prefix, user="ivan")
    assert not drives.is_path_allowed("/home/other/secret", manifest, posix_prefix, user="ivan")


def test_symlink_escape_is_denied(manifest, posix_prefix):
    """Ссылка из разрешённого пути наружу не должна давать доступ."""
    resolved = {"/srv/share/link": "/etc/shadow"}
    assert not drives.is_path_allowed(
        "/srv/share/link", manifest, posix_prefix, user="ivan", resolve=lambda path: resolved.get(path, path)
    )


def test_hidden_paths_win_over_visible(manifest, posix_prefix):
    hidden = replace(manifest, hidden=("/srv/share/закрыто",))
    assert not drives.is_path_allowed("/srv/share/закрыто/файл", hidden, posix_prefix, user="ivan")


def test_windows_path_conversion_roundtrip(manifest, posix_prefix):
    posix = drives.windows_to_posix("Z:\\1C\\база.1c", manifest, posix_prefix, user="ivan")
    assert posix == "/srv/share/1C/база.1c"
    assert drives.posix_to_windows(posix, manifest, posix_prefix, user="ivan") == "Z:\\1C\\база.1c"
    assert drives.posix_to_windows(posixpath.join(posix_prefix, "drive_c", "windows"), manifest, posix_prefix) == "C:\\windows"


def test_unmapped_path_conversion_raises(manifest, posix_prefix):
    with pytest.raises(LaunchError):
        drives.posix_to_windows("/opt/other", manifest, posix_prefix)


# ------------------------------------------------------------------- изоляция
def test_sandbox_plan_blocks_system_writes(manifest, posix_prefix):
    plan = sandbox.build_plan(manifest, str(posix_prefix))
    profile = plan.apparmor_profile_text or ""
    assert plan.apparmor_profile_name == f"wcv-{manifest.id}"
    assert "deny file write -> /etc/**" in profile
    assert "deny file read -> /etc/shadow" in profile
    assert posix_prefix in profile


def test_blocked_network_drops_socket_syscalls(manifest, posix_prefix):
    blocked = replace(manifest, network_mode=NetworkMode.BLOCKED)
    payload = json.loads(sandbox.seccomp_profile(blocked))
    errno_names = set(next(rule for rule in payload["syscalls"] if rule["action"] == "SCMP_ACT_ERRNO")["names"])
    assert {"mount", "ptrace", "reboot", "bpf"} <= errno_names
    allow_rule = next(rule for rule in payload["syscalls"] if rule["action"] != "SCMP_ACT_ERRNO")
    assert "socket" not in allow_rule["names"]


def test_usb_permission_relaxes_private_devices(manifest, posix_prefix):
    with_usb = replace(manifest, camera=True)
    plan = sandbox.build_plan(with_usb, str(posix_prefix))
    props = dict(plan.systemd_properties)
    assert props["PrivateDevices"] == "yes"
    profile = plan.apparmor_profile_text or ""
    assert "/dev/video*" not in profile.replace("deny file -> /dev/video* *,", "")


def test_disabling_all_isolation_is_refused(manifest):
    naked = replace(manifest, apparmor=sandbox.AppArmorMode.OFF, seccomp=sandbox.SeccompMode.OFF)
    with pytest.raises(SandboxError):
        sandbox.build_plan(naked, "/tmp/prefix")


def test_allowlist_warns_about_proxy_dependency(manifest, posix_prefix):
    plan = sandbox.build_plan(manifest, str(posix_prefix))
    assert any("wc-net" in warning for warning in plan.warnings)


def test_systemd_properties_cover_isolation(manifest, tmp_path):
    props = dict(sandbox.systemd_properties(manifest))
    assert props["NoNewPrivileges"] == "yes"
    assert props["ProtectSystem"] == "strict"
    assert props["Slice"] == "wincompat.slice"
    assert props["AppArmorProfile"].startswith("wcv-")


# ------------------------------------------------------------------ уровни L0-L4
def test_decide_uses_proven_level_over_declared(manifest):
    caps = fallback.Capabilities(has_kvm=True, hypervisor_ready=True)
    decision = fallback.decide(manifest, CoverageLevel.L2, caps)
    assert decision.action == fallback.ACTION_WINE
    assert decision.level is CoverageLevel.L2


def test_decide_without_evidence_warns(manifest):
    decision = fallback.decide(manifest, None, fallback.Capabilities())
    assert decision.action == fallback.ACTION_WINE
    assert decision.notes


def test_l4_requires_windows_partition(manifest):
    l4 = replace(manifest, expected_level=CoverageLevel.L4)
    no_windows = fallback.decide(l4, CoverageLevel.L4, fallback.Capabilities())
    assert no_windows.action == fallback.ACTION_UNSUPPORTED
    with_windows = fallback.decide(
        l4, CoverageLevel.L4, fallback.Capabilities(has_windows_partition=True)
    )
    assert with_windows.action == fallback.ACTION_REBOOT
    assert with_windows.requires_reboot is True
    assert with_windows.commands[0] == "byteguru-boot-next"


def test_l3_falls_back_to_reboot_without_kvm(manifest):
    l3 = replace(manifest, expected_level=CoverageLevel.L3)
    decision = fallback.decide(
        l3, CoverageLevel.L3, fallback.Capabilities(has_windows_partition=True)
    )
    assert decision.action == fallback.ACTION_REBOOT
    assert decision.level is CoverageLevel.L4


def test_l0_is_never_executable(manifest):
    l0 = replace(manifest, expected_level=CoverageLevel.L0)
    decision = fallback.decide(l0, CoverageLevel.L0, fallback.Capabilities(has_kvm=True))
    assert decision.action == fallback.ACTION_UNSUPPORTED
    assert decision.is_executable is False


def test_native_runner_bypasses_wincompat():
    manifest = replace(load_manifest_text(BASE), runner=Runner.NATIVE)
    decision = fallback.decide(manifest, None, fallback.Capabilities())
    assert decision.action == fallback.ACTION_NATIVE


# ------------------------------------------------------------------ команды
def test_wine_command_uses_prefix_relative_path(manifest, posix_prefix):
    argv = spawn.build_wine_command(manifest, posix_prefix)
    assert argv == ["wine", "C:\\app.exe"]


def test_msi_uses_msiexec(manifest, posix_prefix):
    msi = replace(manifest, executable="setup.msi")
    argv = spawn.build_wine_command(msi, posix_prefix)
    assert argv[:3] == ["wine", "msiexec", "/i"]


def test_environment_carries_prefix_and_identity(manifest, posix_prefix):
    env = spawn.build_environment(manifest, posix_prefix, wayland_display="wayland-1", user="ivan")
    assert env["WINEPREFIX"] == str(posix_prefix)
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["BG_WCV_ID"] == manifest.id
    assert env["BG_WCV_VERSION"] == "1.0"
    assert env["BG_WCV_USER"] == "ivan"


def test_absolute_executable_is_refused(manifest, posix_prefix):
    bad = replace(manifest, executable="/usr/bin/app.exe")
    with pytest.raises(LaunchError):
        spawn.build_wine_command(bad, posix_prefix)


def test_sandbox_wraps_with_aa_exec(manifest, posix_prefix):
    plan = sandbox.build_plan(manifest, str(posix_prefix))
    argv = spawn.build_wine_command(manifest, posix_prefix, sandbox_plan=plan)
    assert argv[:4] == ["aa-exec", "-p", f"wcv-{manifest.id}", "--"]


def test_scope_prefix_carries_properties(manifest):
    argv = spawn.build_scope_prefix(manifest, properties=sandbox.systemd_properties(manifest))
    assert argv[:4] == ["systemd-run", "--user", "--scope", "--unit"]
    assert "Slice=wincompat.slice" in argv


def test_qemu_command_only_for_vm_levels(manifest):
    with pytest.raises(LaunchError):
        spawn.build_qemu_command(manifest, image_path="/var/lib/win.qcow2")
    l3 = replace(manifest, expected_level=CoverageLevel.L3)
    argv = spawn.build_qemu_command(
        l3, image_path="/var/lib/win.qcow2", shared_directory="/srv/share", spice_port=5930
    )
    assert "-enable-kvm" in argv
    assert any("mount_tag=byteguru" in item for item in argv)
    assert any(item.startswith("port=5930") for item in argv)


def test_installer_command_requires_absolute_path(manifest):
    with pytest.raises(LaunchError):
        spawn.build_installer_command(manifest, "setup.exe")
    argv = spawn.build_installer_command(manifest, "/media/usb/setup.exe")
    assert argv == ["wine", "/media/usb/setup.exe"]


# ------------------------------------------------------------------- реестр
def test_registry_requires_registration_before_runs(tmp_path):
    from wcmgr.registry import CompatibilityRegistry, RunRecord

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    with pytest.raises(RegistryError):
        registry.record_run(RunRecord("nope", True, CoverageLevel.L1))
    registry.close()


def test_registry_level_comes_from_runs_not_manifest(manifest, tmp_path):
    from wcmgr.registry import CompatibilityRegistry, RunRecord

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    registry.register(manifest)
    assert registry.get(manifest.id).proven_level is None
    registry.record_run(RunRecord(manifest.id, ok=False, level=CoverageLevel.L1, detail="упал"))
    assert registry.get(manifest.id).proven_level is CoverageLevel.L0
    registry.record_run(RunRecord(manifest.id, ok=True, level=CoverageLevel.L2))
    entry = registry.get(manifest.id)
    assert entry.proven_level is CoverageLevel.L2
    assert entry.runs_total == 2
    assert "1/2" in entry.evidence
    registry.close()


def test_registry_does_not_let_bypass_prove_wine(manifest, tmp_path):
    from wcmgr.registry import CompatibilityRegistry, RunRecord

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    registry.register(manifest)
    registry.record_run(RunRecord(manifest.id, ok=True, level=CoverageLevel.L3))
    assert registry.get(manifest.id).proven_level is None
    registry.note_bypass(manifest.id, CoverageLevel.L4, "нужен античит-совместимый драйвер")
    entry = registry.get(manifest.id)
    assert entry.bypass_level is CoverageLevel.L4
    registry.close()


def test_registry_rejects_bypass_level_for_wine(manifest, tmp_path):
    from wcmgr.registry import CompatibilityRegistry

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    registry.register(manifest)
    with pytest.raises(RegistryError):
        registry.note_bypass(manifest.id, CoverageLevel.L1)
    registry.close()


def test_registry_update_changes_fingerprint(manifest, tmp_path):
    from wcmgr.registry import CompatibilityRegistry

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    assert registry.register(manifest) is True
    assert registry.register(manifest) is False
    changed = replace(manifest, version="1.1")
    assert registry.register(changed) is True
    assert registry.get(manifest.id).manifest.version == "1.1"
    registry.close()


def test_registry_export_lists_evidence(manifest, tmp_path):
    from wcmgr.registry import CompatibilityRegistry

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    registry.register(manifest)
    payload = json.loads(registry.export_json())
    assert payload["apps"][0]["id"] == manifest.id
    assert payload["apps"][0]["proven_level"] is None
    assert payload["apps"][0]["evidence"] == "не проверено на этой сборке"
    registry.close()


def test_registry_rejects_foreign_schema(tmp_path):
    from wcmgr.registry import CompatibilityRegistry

    registry = CompatibilityRegistry(tmp_path / "reg.db")
    registry.close()
    import sqlite3

    connection = sqlite3.connect(str(tmp_path / "reg.db"))
    connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema'")
    connection.commit()
    connection.close()
    with pytest.raises(RegistryError):
        CompatibilityRegistry(tmp_path / "reg.db")
