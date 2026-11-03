"""CLI установщика: probe / plan / check / apply / boot-repair / clock.

Все команды читаемы без root. Реальные изменения требуют `--apply`, и даже
тогда сначала печатают план на согласование.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bginstaller.apply import apply_plan, confirm_token
from bginstaller.bootrepair import diagnose, next_boot_command, parse_efibootmgr, repair_commands, windows_boot_index
from bginstaller.clock import explain, plan_for
from bginstaller.errors import InstallerError
from bginstaller.models import BootMode, Disk, Partition, PartitionTable, Profile, WindowsState, GiB
from bginstaller.planner import plan, plan_erase_and_install
from bginstaller.probe import boot_mode_from_efivar, detect_windows, enrich_with_sfdisk, parse_lsblk
from bginstaller.safety import evaluate, format_report


def _load_description(path: Path) -> tuple[Disk, WindowsState | None]:
    """Описание диска вместе с известным состоянием Windows (для прогонов на файлах)."""
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "disk" in payload:
        return _disk_from_dict(payload["disk"]), _windows_from_dict(payload.get("windows", {}))
    if isinstance(payload, dict) and "blockdevices" in payload:
        disks = parse_lsblk(raw)
        if not disks:
            raise InstallerError("в списке блочных устройств нет дисков")
        return disks[0], None
    disks = parse_lsblk(raw)
    if not disks:
        raise InstallerError("в файле нет ни одного диска")
    return disks[0], None


def _disk_from_dict(data: dict) -> Disk:
    partitions = tuple(
        Partition(
            number=int(item["number"]),
            start_bytes=int(item.get("start_bytes", 0)),
            size_bytes=int(item.get("size_bytes", 0)),
            fs_type=_fs(item.get("fs_type", "unknown")),
            label=item.get("label", ""),
            uuid=item.get("uuid", ""),
            os_kind=_os(item.get("os_kind", "none")),
            flags=tuple(item.get("flags", ())),
        )
        for item in data.get("partitions", ())
    )
    return Disk(
        device=data.get("device", "/dev/sda"),
        size_bytes=int(data.get("size_bytes", 0)),
        table=PartitionTable(data.get("table", "gpt")),
        model=data.get("model", ""),
        rotational=bool(data.get("rotational", True)),
        partitions=partitions,
    )


def _windows_from_dict(data: dict) -> WindowsState:
    return WindowsState(
        present=bool(data.get("present", False)),
        version=data.get("version", ""),
        bitlocker_protected=bool(data.get("bitlocker_protected", False)),
        hibernated=bool(data.get("hibernated", False)),
        fast_startup=data.get("fast_startup"),
        dirty=bool(data.get("dirty", False)),
        secure_boot_enabled=data.get("secure_boot_enabled"),
        device_path=data.get("device_path", ""),
    )


def _fs(value: str):
    from bginstaller.models import FSType

    return FSType(value)


def _os(value: str):
    from bginstaller.models import OsKind

    return OsKind(value)


def cmd_probe(args: argparse.Namespace) -> int:
    text = Path(args.lsblk).read_text(encoding="utf-8")
    disks = parse_lsblk(text)
    if args.sfdisk:
        disks = tuple(
            enrich_with_sfdisk(disk, Path(args.sfdisk).read_text(encoding="utf-8")) for disk in disks
        )
    payload = {
        "disks": [
            {
                "device": disk.device,
                "size_gib": disk.size_bytes // GiB,
                "table": disk.table.value,
                "free_gib": disk.total_free_bytes() // GiB,
                "windows": disk.windows_partitions[0].label if disk.windows_partitions else None,
                "partitions": [
                    {
                        "number": part.number,
                        "size_gib": part.size_bytes // GiB,
                        "fs": part.fs_type.value,
                        "label": part.label,
                        "kind": part.os_kind.value,
                    }
                    for part in disk.partitions
                ],
            }
            for disk in disks
        ]
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    disk, hinted = _load_description(Path(args.disk_file))
    windows = hinted or detect_windows(disk)
    boot_mode = BootMode(args.boot)
    install_plan = (
        plan_erase_and_install(disk, profile=Profile(args.profile), boot_mode=boot_mode)
        if args.erase
        else plan(
            disk,
            windows=windows,
            profile=Profile(args.profile),
            boot_mode=boot_mode,
            mem_bytes=args.memory_gib * GiB,
            on_removable=args.removable,
            battery=args.battery,
            ignore_gates=args.allow_blocked,
        )
    )
    print(install_plan.summary())
    if install_plan.notes:
        print("\nзамечания:")
        for note in install_plan.notes:
            print(f"  - {note}")
    if install_plan.requires_user_confirmation():
        print(f"\nдля необратимых действий нужен токен: {confirm_token(install_plan)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    disk, hinted = _load_description(Path(args.disk_file))
    windows = hinted or detect_windows(disk)
    gates = evaluate(
        disk,
        windows,
        BootMode(args.boot),
        Profile(args.profile),
        mem_bytes=args.memory_gib * GiB,
        required_bytes=args.need_gib * GiB,
        on_removable=args.removable,
        battery=args.battery,
    )
    print(format_report(gates))
    return 1 if any(gate.blocking for gate in gates) else 0


def cmd_apply(args: argparse.Namespace) -> int:
    disk, hinted = _load_description(Path(args.disk_file))
    windows = hinted or detect_windows(disk)
    install_plan = plan(
        disk,
        windows=windows,
        profile=Profile(args.profile),
        boot_mode=BootMode(args.boot),
        mem_bytes=args.memory_gib * GiB,
    )
    if not args.apply:
        print("только проба: изменения потребуют --apply и токен подтверждения")
    confirmed = set()
    if args.confirm_token == confirm_token(install_plan):
        confirmed = {action.order for action in install_plan.actions if action.requires_confirmation}
    report = apply_plan(install_plan, apply=args.apply, confirmations=confirmed)
    print(report.as_text())
    return 0 if report.ok else 1


def cmd_boot_repair(args: argparse.Namespace) -> int:
    text = Path(args.efibootmgr).read_text(encoding="utf-8")
    state = parse_efibootmgr(text)
    problems = diagnose(state)
    for problem in problems:
        print(f"! {problem}")
    commands = repair_commands(state, disk=args.disk, esp_number=args.esp_partition)
    print("\nкоманды восстановления:")
    for argv in commands:
        print("  " + " ".join(argv))
    if args.apply:
        import subprocess

        for argv in commands:
            code = subprocess.run(argv, check=False).returncode
            if code:
                print(f"команда завершилась кодом {code}: {' '.join(argv)}", file=sys.stderr)
                return code
    return 0


def cmd_boot_next(args: argparse.Namespace) -> int:
    state = parse_efibootmgr(Path(args.efibootmgr).read_text(encoding="utf-8"))
    print(" ".join(next_boot_command(args.target, state)))
    if args.target.lower() in ("windows", "win"):
        index = windows_boot_index(state)
        if index:
            print(f"# запись Windows найдена: Boot{index}", file=sys.stderr)
    return 0


def cmd_clock(args: argparse.Namespace) -> int:
    print(explain(plan_for(bool(args.windows), args.policy)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bginstaller", description="Установщик ByteguruOS")
    parser.add_argument("--version", action="store_true", help="показать версию")
    sub = parser.add_subparsers(dest="command")

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--disk-file", required=True, help="JSON/lsblk-описание диска (см. tests/fixtures)")
        target.add_argument("--boot", choices=[item.value for item in BootMode], default="uefi")
        target.add_argument("--profile", choices=[item.value for item in Profile], default="workstation")
        target.add_argument("--memory-gib", type=int, default=8)

    probe = sub.add_parser("probe", help="разобрать вывод lsblk/sfdisk")
    probe.add_argument("--lsblk", required=True)
    probe.add_argument("--sfdisk")
    probe.set_defaults(func=cmd_probe)

    plan_cmd = sub.add_parser("plan", help="показать план разметки")
    common(plan_cmd)
    plan_cmd.add_argument("--erase", action="store_true", help="полная затирка диска")
    plan_cmd.add_argument("--removable", action="store_true")
    plan_cmd.add_argument("--battery", action="store_true")
    plan_cmd.add_argument("--allow-blocked", action="store_true", help="построить план несмотря на запреты")
    plan_cmd.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="проверить готовность диска к установке")
    common(check)
    check.add_argument("--need-gib", type=int, default=0)
    check.add_argument("--removable", action="store_true")
    check.add_argument("--battery", action="store_true")
    check.set_defaults(func=cmd_check)

    apply_cmd = sub.add_parser("apply", help="исполнить план (по умолчанию — проба)")
    common(apply_cmd)
    apply_cmd.add_argument("--apply", action="store_true", help="выполнить команды реально")
    apply_cmd.add_argument("--confirm-token", default="")
    apply_cmd.set_defaults(func=cmd_apply)

    repair = sub.add_parser("boot-repair", help="восстановить загрузочную запись после Windows")
    repair.add_argument("--efibootmgr", required=True, help="файл с выводом efibootmgr -v")
    repair.add_argument("--disk", default="/dev/nvme0n1")
    repair.add_argument("--esp-partition", type=int, default=1)
    repair.add_argument("--apply", action="store_true")
    repair.set_defaults(func=cmd_boot_repair)

    next_cmd = sub.add_parser("boot-next", help="команда однократной перезагрузки в другую ОС")
    next_cmd.add_argument("--efibootmgr", required=True)
    next_cmd.add_argument("--target", default="windows")
    next_cmd.set_defaults(func=cmd_boot_next)

    clock = sub.add_parser("clock", help="политика часов на двухзагрузочной машине")
    clock.add_argument("--windows", action="store_true")
    clock.add_argument("--policy", choices=["utc", "localtime", ""], default="")
    clock.set_defaults(func=cmd_clock)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False) or not args.command:
        from bginstaller import __version__

        print(f"bginstaller {__version__}")
        if not args.command:
            parser.print_help()
        return 0
    try:
        return args.func(args)
    except InstallerError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
