"""Проверки установщика: разметка, запреты, ремонт EFI-записей, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bginstaller import bootrepair, cli, clock, models, operations, planner, probe, safety
from bginstaller.apply import apply_plan, confirm_token
from bginstaller.errors import ApplyError, BootRepairError, PlanError, ProbeError
from bginstaller.models import BootMode, FSType, OsKind, Partition, PartitionTable, Profile, WindowsState, GiB, MiB

FIXTURES = Path(__file__).parent / "fixtures"


def _discard_writer(path: str, content: str) -> None:
    """В тестах файл писать не нужно, но он пишется: путь /mnt/target не
    принадлежит пользователю, и реальный writer упал бы на правах."""


def _disk(size_gib: int, partitions: tuple[Partition, ...], table=PartitionTable.GPT) -> models.Disk:
    return models.Disk(
        device="/dev/sda",
        size_bytes=size_gib * GiB,
        table=table,
        partitions=partitions,
    )


def _esp(number: int = 1, size: int = 512 * MiB) -> Partition:
    return Partition(number=number, start_bytes=1 * MiB, size_bytes=size, fs_type=FSType.FAT32,
                     label="EFI", flags=("esp",))


def _ntfs(number: int, start: int, size: int, label: str = "Windows") -> Partition:
    return Partition(number=number, start_bytes=start, size_bytes=size, fs_type=FSType.NTFS,
                     label=label, os_kind=OsKind.RECOVERY if label == "Recovery" else OsKind.WINDOWS)


# ----------------------------------------------------------------------- геометрия
def test_free_segments_skips_used_and_reserves_tail():
    disk = _disk(
        100,
        (
            _esp(1, 512 * MiB),
            _ntfs(2, 2 * GiB, 40 * GiB),
        ),
    )
    gaps = disk.free_segments(reserved_after_bytes=8 * MiB)
    assert gaps, "должна остаться свободная область"
    start, length = gaps[0]
    assert start >= 42 * GiB
    assert length <= 58 * GiB
    assert disk.size_bytes - (start + length) >= 8 * MiB - 1


def test_no_overlap_between_partitions_reported():
    disk = _disk(50, (_esp(1), _ntfs(2, 1 * MiB + 100 * MiB, 20 * GiB)))
    assert disk.partitions[0].overlaps(disk.partitions[1])


def test_partition_node_uses_p_separator_for_nvme():
    disk = models.Disk(device="/dev/nvme0n1", size_bytes=100 * GiB, table=PartitionTable.GPT)
    part = Partition(number=3, start_bytes=0, size_bytes=10 * GiB)
    assert planner.partition_node(disk, part) == "/dev/nvme0n1p3"


# ------------------------------------------------------------------------ планировщик
def test_plan_on_empty_disk_without_windows():
    disk = _disk(200, ())
    install_plan = planner.plan(disk, profile=Profile.WORKSTATION)
    assert install_plan.layout.shrink_windows_bytes == 0
    assert install_plan.layout.esp_bytes == 1 * GiB
    assert install_plan.layout.root_bytes == 30 * GiB
    assert install_plan.layout.home_bytes > 40 * GiB
    kinds = [action.kind for action in install_plan.actions]
    assert "create-partition" in kinds
    assert kinds.index("probe-disk") < kinds.index("install-efi")
    assert install_plan.requires_user_confirmation() is False


def test_plan_reuses_existing_esp():
    disk = _disk(200, (_esp(1, 512 * MiB),))
    install_plan = planner.plan(disk)
    assert install_plan.layout.reuse_esp is True
    assert not any(
        action.kind == "create-partition" and "--type" in action.arguments and "esp" in action.arguments
        for action in install_plan.actions
    )


def test_plan_creates_esp_when_too_small():
    disk = _disk(200, (_esp(1, 100 * MiB),))
    install_plan = planner.plan(disk)
    assert install_plan.layout.reuse_esp is False
    assert any("--type esp" in " ".join(action.arguments) for action in install_plan.actions)


def test_plan_shrinks_windows_and_requires_confirmation():
    disk = _disk(400, (_esp(1), _ntfs(2, 2 * GiB, 300 * GiB)))
    windows = WindowsState(present=True, version="Windows 11 Pro")
    install_plan = planner.plan(disk, windows=windows, profile=Profile.OFFICE_DUALBOOT)
    assert install_plan.layout.shrink_windows_bytes > 0
    shrink = next(action for action in install_plan.actions if action.kind == "shrink-ntfs")
    assert shrink.destructive is True
    assert shrink.requires_confirmation is True
    assert install_plan.requires_user_confirmation() is True
    assert confirm_token(install_plan) == "izmenit-razmetku"


def test_plan_refuses_to_leave_windows_too_small():
    disk = _disk(120, (_esp(1), _ntfs(2, 2 * GiB, 25 * GiB)))
    windows = WindowsState(present=True)
    with pytest.raises(PlanError, match="до 30 ГиБ"):
        planner.plan(disk, windows=windows, profile=Profile.OFFICE_DUALBOOT)


def test_plan_blocked_by_hibernation():
    disk = _disk(400, (_esp(1), _ntfs(2, 2 * GiB, 300 * GiB)))
    windows = WindowsState(present=True, hibernated=True)
    with pytest.raises(PlanError, match="гибернаци"):
        planner.plan(disk, windows=windows)


def test_plan_blocked_by_bitlocker():
    disk = _disk(400, (_esp(1), _ntfs(2, 2 * GiB, 300 * GiB)))
    windows = WindowsState(present=True, bitlocker_protected=True)
    with pytest.raises(PlanError, match="BitLocker"):
        planner.plan(disk, windows=windows)


def test_ignore_gates_allows_dry_plan_with_hibernated_windows():
    disk = _disk(200, (_esp(1), _ntfs(2, 2 * GiB, 60 * GiB)))
    install_plan = planner.plan(disk, windows=WindowsState(present=True, hibernated=True), ignore_gates=True)
    assert any("windows-shutdown" in note or "гибернаци" in note for note in install_plan.notes)


def test_kiosk_profile_has_no_home():
    disk = _disk(64, ())
    install_plan = planner.plan(disk, profile=Profile.KIOSK)
    assert install_plan.layout.home_bytes == 0
    assert install_plan.layout.create_shared is False


def test_server_profile_has_no_shared_volume():
    disk = _disk(200, ())
    install_plan = planner.plan(disk, profile=Profile.SERVER)
    assert install_plan.layout.shared_bytes == 0
    assert not any("BG_SHARE" in action.arguments for action in install_plan.actions)


def test_legacy_boot_on_mbr_is_blocked():
    disk = _disk(200, (), table=PartitionTable.MBR)
    with pytest.raises(PlanError, match="GPT"):
        planner.plan(disk, boot_mode=BootMode.UEFI)


def test_small_disk_scales_layout_instead_of_refusing():
    """40 ГиБ — это VM и старые ноутбуки; отказывать им нельзя.

    Раньше планировщик требовал ровно размеры профиля и отвечал «не хватает
    места» там, где система помещается. Теперь разделы уменьшаются, но не
    ниже минимума, при котором обновление не встанет.
    """
    install_plan = planner.plan(_disk(40, ()), profile=Profile.WORKSTATION)
    layout = install_plan.layout
    assert layout.root_bytes < planner.SIZINGS[Profile.WORKSTATION].root
    assert layout.root_bytes >= planner.ROOT_FLOOR
    assert layout.var_bytes >= planner.VAR_FLOOR
    assert layout.total_new_bytes() <= 40 * GiB


def test_tiny_disk_raises():
    with pytest.raises(PlanError):
        planner.plan(_disk(8, ()), profile=Profile.WORKSTATION)


def test_erase_plan_starts_with_wipe_and_demands_token():
    disk = _disk(200, (_esp(1), _ntfs(2, 2 * GiB, 100 * GiB)))
    install_plan = planner.plan_erase_and_install(disk, profile=Profile.WORKSTATION)
    assert install_plan.actions[0].kind == "wipe-table"
    assert install_plan.actions[0].destructive is True
    assert confirm_token(install_plan) == "udalit-dannye"
    assert any("затирается" in note for note in install_plan.notes)


def test_windows_partition_is_never_marked_for_deletion():
    disk = _disk(200, (_esp(1), _ntfs(2, 2 * GiB, 100 * GiB)))
    install_plan = planner.plan(disk, windows=WindowsState(present=True))
    for action in install_plan.actions:
        assert action.kind != "delete-partition"
        assert "/dev/sda2" != action.target or action.kind == "shrink-ntfs"


# --------------------------------------------------------------------------- safety
def test_gates_warn_about_unknown_fast_startup():
    disk = _disk(200, (_esp(1), _ntfs(2, 2 * GiB, 60 * GiB)))
    gates = safety.evaluate(disk, WindowsState(present=True), BootMode.UEFI, Profile.WORKSTATION)
    assert any(gate.name == "fast-startup" for gate in gates)
    assert safety.has_blocker(gates) is False


def test_gates_report_small_esp_as_warning():
    disk = _disk(200, (_esp(1, 100 * MiB),))
    gates = safety.evaluate(disk, WindowsState(), BootMode.UEFI, Profile.WORKSTATION)
    assert any(gate.name == "esp-too-small" for gate in gates)


def test_battery_and_removable_are_warnings_not_blockers():
    disk = _disk(200, ())
    gates = safety.evaluate(
        disk, WindowsState(), BootMode.UEFI, Profile.WORKSTATION, on_removable=True, battery=True
    )
    assert not safety.has_blocker(gates)
    assert {gate.name for gate in gates} >= {"removable-media", "power-source"}


# --------------------------------------------------------------------------- probe
def test_parse_lsblk_fixture():
    disks = probe.parse_lsblk((FIXTURES / "lsblk-dualboot.json").read_text(encoding="utf-8"))
    assert len(disks) == 2
    nvme = disks[0]
    assert nvme.device == "/dev/nvme0n1"
    assert nvme.table is PartitionTable.GPT
    assert len(nvme.partitions) == 4
    assert nvme.partitions[0].is_esp is True
    assert nvme.partitions[1].flags == ("msr",)
    assert nvme.windows_partitions[0].label == "Windows"
    assert disks[1].partitions == ()


def test_enrich_with_sfdisk_fills_partition_starts():
    disks = probe.parse_lsblk((FIXTURES / "lsblk-dualboot.json").read_text(encoding="utf-8"))
    enriched = probe.enrich_with_sfdisk(disks[0], (FIXTURES / "sfdisk-dualboot.json").read_text(encoding="utf-8"))
    assert enriched.partitions[0].start_bytes == 2048 * 512
    assert enriched.partitions[2].start_bytes == 239616 * 512
    assert enriched.partitions[3].start_bytes > enriched.partitions[2].end_bytes - 10 * GiB


def test_probe_rejects_garbage():
    with pytest.raises(ProbeError):
        probe.parse_lsblk("не json вообще")
    assert probe.parse_lsblk('{"blockdevices": []}') == ()


def test_detect_windows_from_flags():
    disks = probe.parse_lsblk((FIXTURES / "lsblk-dualboot.json").read_text(encoding="utf-8"))
    state = probe.detect_windows(
        disks[0], "Windows 11 Pro", {"C0FFEE00": "BitLocker, dirty"}
    )
    assert state.present is True
    assert state.version == "Windows 11 Pro"
    assert state.bitlocker_protected is True
    assert state.hibernated is True
    assert state.device_path == "/dev/nvme0n1p3"


def test_boot_mode_from_efivar():
    assert probe.boot_mode_from_efivar(["Boot0001", "BootOrder"]) is BootMode.UEFI
    assert probe.boot_mode_from_efivar(["SomethingElse"]) is BootMode.LEGACY


# ---------------------------------------------------------------------- bootrepair
def test_parse_efibootmgr_entries_and_order():
    state = bootrepair.parse_efibootmgr((FIXTURES / "efibootmgr-after-windows.txt").read_text(encoding="utf-8"))
    assert state.boot_order == ("0001", "0000", "0002", "0003")
    assert state.boot_next == "0001"
    assert len(state.entries) == 4
    assert state.windows_entries[0].index == "0000"
    assert state.byteguru_entries[0].label == "ByteguruOS"


def test_diagnose_detects_default_not_ours():
    state = bootrepair.parse_efibootmgr((FIXTURES / "efibootmgr-after-windows.txt").read_text(encoding="utf-8"))
    problems = bootrepair.diagnose(state)
    assert any("по умолчанию загружается" in problem for problem in problems)


def test_repair_when_entry_lost_creates_new_record():
    state = bootrepair.parse_efibootmgr((FIXTURES / "efibootmgr-entry-lost.txt").read_text(encoding="utf-8"))
    assert state.byteguru_entries == ()
    commands = bootrepair.repair_commands(state, disk="/dev/nvme0n1", esp_number=1)
    assert commands[0][:2] == ["efibootmgr", "-c"]
    assert r"\EFI\byteguru\systemd-bootx64.efi" in commands[0]
    assert "nvme0n1p1" in " ".join(commands[1])


def test_repair_when_entry_exists_only_reorders():
    state = bootrepair.parse_efibootmgr((FIXTURES / "efibootmgr-after-windows.txt").read_text(encoding="utf-8"))
    commands = bootrepair.repair_commands(state)
    assert commands[0][:2] == ["efibootmgr", "-o"]
    assert commands[0][2].startswith("0003")


def test_next_boot_command_and_errors():
    state = bootrepair.parse_efibootmgr((FIXTURES / "efibootmgr-after-windows.txt").read_text(encoding="utf-8"))
    assert bootrepair.next_boot_command("windows", state) == ["efibootmgr", "-n", "0000"]
    with pytest.raises(BootRepairError):
        bootrepair.next_boot_command("haiku", state)


def test_empty_efibootmgr_output_is_refused():
    with pytest.raises(BootRepairError):
        bootrepair.parse_efibootmgr("")


# -------------------------------------------------------------------------- clock
def test_clock_default_for_dual_boot_moves_windows_to_utc():
    plan = clock.plan_for(True)
    assert plan.policy is clock.TimePolicy.WINDOWS_UTC
    assert plan.needs_windows_change is True
    assert "RealTimeIsUniversal" in plan.windows_commands[0]


def test_clock_without_windows_is_plain_utc():
    plan = clock.plan_for(False)
    assert plan.policy is clock.TimePolicy.UTC
    assert plan.timedatectl_arguments == ("set-local-rtc", "0")
    assert plan.needs_windows_change is False


def test_clock_localtime_choice_is_respected():
    plan = clock.plan_for(True, "localtime")
    assert plan.policy is clock.TimePolicy.LOCALTIME
    assert plan.timedatectl_arguments == ("set-local-rtc", "1")


# -------------------------------------------------------------------------- apply
def test_profile_follows_disk_size():
    """Маленький диск не должен делать установку невозможной.

    Профиль — это размеры разделов, а не ярлык: на 24 ГиБ помещается kiosk, и
    отказ «недостаточно места» был бы багом установщика, а не диска.
    """
    assert planner.suggest_profile(_disk(24, ())) is Profile.KIOSK
    assert planner.suggest_profile(_disk(120, ())) is Profile.SERVER
    assert planner.suggest_profile(_disk(250, ())) is Profile.WORKSTATION
    # Офисный профиль с огромным /var оправдан только ради WinCompat.
    assert planner.suggest_profile(_disk(250, ()), windows_present=True) \
        is Profile.OFFICE_DUALBOOT


def test_small_disk_still_produces_a_plan():
    install_plan = planner.plan(_disk(24, ()),
        profile=planner.suggest_profile(_disk(24, ())))
    kinds = [action.kind for action in install_plan.actions]
    assert "create-partition" in kinds and "finalize" in kinds


def test_dry_run_executes_nothing():
    disk = _disk(200, ())
    install_plan = planner.plan(disk)
    calls: list[tuple[str, ...]] = []
    report = apply_plan(install_plan, apply=False,
        runner=lambda argv, stdin=None: calls.append(tuple(argv)) or 0)
    assert report.dry_run is True
    assert calls == []
    assert report.executed == 0


def test_apply_runs_in_order_and_stops_on_failure():
    disk = _disk(200, ())
    install_plan = planner.plan(disk)
    seen: list[str] = []

    def runner(argv, stdin=None):
        seen.append(argv[0])
        return 0 if argv[0] != "rsync" else 3

    report = apply_plan(install_plan, apply=True, runner=runner)
    assert report.ok is False
    assert "parted" in seen
    assert seen[-1] == "rsync"
    assert any(step.failed and step.returncode == 3 for step in report.steps)


def test_destructive_step_skipped_without_confirmation():
    disk = _disk(400, (_esp(1), _ntfs(2, 2 * GiB, 300 * GiB)))
    install_plan = planner.plan(disk, windows=WindowsState(present=True))
    executed: list[str] = []
    report = apply_plan(install_plan, apply=True, writer=_discard_writer,
        runner=lambda argv, stdin=None: executed.append(" ".join(argv)) or 0)
    assert report.ok is True
    assert not any("ntfsresize" in line for line in executed)
    assert any(step.skipped and "явное подтверждение" in step.note for step in report.steps)


def test_confirmed_ntfs_shrink_runs_real_resize():
    disk = _disk(400, (_esp(1), _ntfs(2, 2 * GiB, 300 * GiB)))
    install_plan = planner.plan(disk, windows=WindowsState(present=True))
    shrink = next(action for action in install_plan.actions if action.kind == "shrink-ntfs")
    executed: list[str] = []
    report = apply_plan(install_plan, apply=True, writer=_discard_writer,
        confirmations={shrink.order},
        runner=lambda argv, stdin=None: executed.append(" ".join(argv)) or 0)
    assert report.ok is True
    assert any("ntfsresize --force" in line for line in executed)


def test_configure_boot_writes_fstab_and_identity(tmp_path):
    """Файлы установленной системы: без них fstab/часы/пользователь живут только
    в голове установщика, а первая же перезагрузка это проверяет."""
    disk = _disk(200, ())
    install_plan = planner.plan(disk)
    ctx = operations.Context(target=str(tmp_path), hostname="guru-os",
        username="guru", password="pw", timezone="Europe/Moscow")
    written: dict[str, str] = {}

    def writer(path, content):
        written[path[len(str(tmp_path)):]] = content

    report = apply_plan(install_plan, apply=True, writer=writer, context=ctx,
        runner=lambda argv, stdin=None: 0)
    assert report.ok is True
    assert "/etc/hostname" in written
    assert written["/etc/hostname"] == "guru-os\n"
    fstab = written["/etc/fstab"]
    assert "LABEL=BG_ROOT" in fstab and "/home" in fstab
    assert written["/etc/adjtime"].endswith("UTC\n")


def test_unknown_action_type_fails_loudly():
    disk = _disk(200, ())
    install_plan = planner.plan(disk)
    broken = models.InstallPlan(
        disk=install_plan.disk,
        layout=install_plan.layout,
        boot_mode=install_plan.boot_mode,
        profile=install_plan.profile,
        windows=install_plan.windows,
        actions=(models.Action(order=1, kind="telepathy", target="/dev/sda"),),
    )
    report = apply_plan(broken, apply=False)
    assert report.ok is False
    with pytest.raises(ApplyError):
        operations.commands_for(models.Action(order=1, kind="telepathy", target="x"),
            install_plan, operations.Context(), operations.State(disk=install_plan.disk))


def test_bootloader_installation_follows_system_image():
    """Загрузчик ставится в уже скопированную систему.

    Порядок «grub-install до переноса rootfs» оставляет машину без загрузки:
    grub-install в пустой /mnt/target не находит ни /boot, ни модули, и
    установка «проходит успешно» до первой же перезагрузки.
    """
    install_plan = planner.plan(_disk(200, ()))
    kinds = [action.kind for action in install_plan.actions]
    assert kinds.index("install-system-image") < kinds.index("install-efi")
    assert kinds.index("configure-boot") < kinds.index("install-efi")
    assert kinds.index("install-efi") < kinds.index("finalize")


def test_password_reaches_chpasswd_through_stdin_not_argv():
    """Пароль в списке аргументов видно в /proc и в логе установки."""
    disk = _disk(200, ())
    install_plan = planner.plan(disk)
    ctx = operations.Context(username="guru", password="секрет-пароль")
    state = operations.State(disk=install_plan.disk)
    action = next(a for a in install_plan.actions if a.kind == "configure-boot")
    jobs = operations.commands_for(action, install_plan, ctx, state)
    flat = " ".join(" ".join(job.argv) for job in jobs)
    assert "секрет-пароль" not in flat
    passwd_job = next(job for job in jobs if job.stdin)
    assert passwd_job.argv[0] == "chroot"
    assert passwd_job.stdin == "guru:секрет-пароль\n"


# -------------------------------------------------------------------------- cli
def test_cli_plan_from_fixture(tmp_path, capsys):
    description = tmp_path / "disk.json"
    description.write_text(
        json.dumps(
            {
                "disk": {
                    "device": "/dev/sda",
                    "size_bytes": 500 * GiB,
                    "table": "gpt",
                    "partitions": [
                        {"number": 1, "start_bytes": MiB, "size_bytes": 512 * MiB, "fs_type": "fat32",
                         "flags": ["esp"]},
                        {"number": 2, "start_bytes": 2 * GiB, "size_bytes": 80 * GiB, "fs_type": "ntfs",
                         "label": "Windows", "os_kind": "windows"},
                    ],
                },
                "windows": {"present": True, "version": "Windows 11 Pro", "fast_startup": False},
            }
        ),
        encoding="utf-8",
    )
    code = cli.main(["plan", "--disk-file", str(description), "--profile", "workstation"])
    out = capsys.readouterr().out
    assert code == 0
    assert "диск: /dev/sda" in out
    assert "create-partition" in out


def test_cli_check_returns_nonzero_on_blocker(tmp_path, capsys):
    description = tmp_path / "disk.json"
    description.write_text(
        json.dumps(
            {
                "disk": {
                    "device": "/dev/sda",
                    "size_bytes": 200 * GiB,
                    "table": "gpt",
                    "partitions": [
                        {"number": 1, "start_bytes": MiB, "size_bytes": 512 * MiB, "fs_type": "fat32",
                         "flags": ["esp"]},
                        {"number": 2, "start_bytes": 2 * GiB, "size_bytes": 80 * GiB, "fs_type": "ntfs",
                         "label": "Windows", "os_kind": "windows"},
                    ],
                },
                "windows": {"present": True, "hibernated": True},
            }
        ),
        encoding="utf-8",
    )
    code = cli.main(["check", "--disk-file", str(description)])
    assert code == 1
    assert "ЗАПРЕТ" in capsys.readouterr().out


def test_cli_boot_repair_prints_commands(tmp_path, capsys):
    fixture = FIXTURES / "efibootmgr-entry-lost.txt"
    code = cli.main(["boot-repair", "--efibootmgr", str(fixture), "--disk", "/dev/sda"])
    out = capsys.readouterr().out
    assert code == 0
    assert "efibootmgr -c" in out
    assert "отсутствует" in out
