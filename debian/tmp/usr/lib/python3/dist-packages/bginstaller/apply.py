"""Исполнение плана установки.

По умолчанию — только показ команд (`--apply` нужен явно). Каждая команда
передаётся вектором, без shell=True: путь с пробелом или кириллицей не должен
становиться способом выполнить что-то ещё.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from bginstaller.errors import ApplyError
from bginstaller.models import Action, InstallPlan

CommandRunner = Callable[[Sequence[str]], int]


@dataclass
class StepResult:
    action: Action
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    failed: bool = False
    skipped: bool = False
    note: str = ""

    @property
    def command_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass
class ApplyReport:
    steps: list[StepResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        return not any(step.failed for step in self.steps)

    @property
    def executed(self) -> int:
        return sum(1 for step in self.steps if not step.skipped and step.returncode is not None)

    def as_text(self) -> str:
        lines = ["ПРОБА (ничего не изменялось)" if self.dry_run else "ИСПОЛНЕНО"]
        for step in self.steps:
            mark = "skip" if step.skipped else ("FAIL" if step.failed else "ok")
            lines.append(f"[{mark:>4}] #{step.action.order} {step.command_line}")
            if step.note:
                lines.append(f"        {step.note}")
        return "\n".join(lines)


_ACTION_TO_TOOL = {
    "probe-disk": ("lsblk",),
    "inspect-ntfs": ("ntfsfix", "--dry-run"),
    "shrink-ntfs": ("ntfsresize",),
    "create-partition": ("sfdisk", "--append"),
    "install-efi": ("bg-boot-install",),
    "write-loader-entry": ("efibootmgr",),
    "mount-target": ("bg-mount-target",),
    "install-system-image": ("bg-image-install",),
    "configure-boot": ("bg-boot-config",),
    "install-boot-repair": ("bg-boot-repair-install",),
    "finalize": ("sync",),
    "wipe-table": ("sfdisk", "--wipe", "always"),
}


def argv_for(action: Action) -> tuple[str, ...]:
    """Собираем вектор аргументов для действия плана."""
    tool = _ACTION_TO_TOOL.get(action.kind)
    if tool is None:
        raise ApplyError(f"для действия {action.kind!r} нет исполнителя")
    argv = list(tool)
    if action.kind in {"probe-disk", "inspect-ntfs", "shrink-ntfs", "create-partition", "wipe-table", "write-loader-entry"}:
        argv.append(action.target)
    argv.extend(action.arguments)
    if action.kind == "finalize":
        argv = ["sh", "-c", "sync && umount -R /mnt/target"]
    return tuple(argv)


def default_runner(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def apply_plan(
    plan: InstallPlan,
    *,
    apply: bool = False,
    runner: CommandRunner | None = None,
    confirmations: set[int] | None = None,
) -> ApplyReport:
    """Пройти по плану. `apply=False` — только печать команд."""
    execute = runner or default_runner
    report = ApplyReport(dry_run=not apply)
    confirmations = confirmations or set()
    for action in plan.actions:
        try:
            argv = argv_for(action)
        except ApplyError as exc:
            report.steps.append(StepResult(action=action, argv=(), returncode=None, failed=True, note=str(exc)))
            continue
        if action.requires_confirmation and action.order not in confirmations:
            report.steps.append(
                StepResult(
                    action=action,
                    argv=argv,
                    returncode=None,
                    skipped=True,
                    note="требуется явное подтверждение (--confirm "
                    f"{action.order}): {action.rollback_hint or 'необратимое действие'}",
                )
            )
            continue
        if not apply:
            report.steps.append(StepResult(action=action, argv=argv, returncode=None, skipped=True,
                                          note="пробa"))
            continue
        try:
            code = execute(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            report.steps.append(StepResult(action=action, argv=argv, returncode=None, failed=True, note=str(exc)))
            break
        if code != 0:
            report.steps.append(
                StepResult(action=action, argv=argv, returncode=code, failed=True,
                           note=f"команда завершилась кодом {code}")
            )
            break
        report.steps.append(StepResult(action=action, argv=argv, returncode=code))
    return report


def confirm_token(plan: InstallPlan) -> str:
    """Токен подтверждения необратимых действий — чтобы вводили слово, а не «y»."""
    destructive = plan.destructive_actions()
    if not destructive:
        return "ok"
    return "udalit-dannye" if any(a.kind == "wipe-table" for a in destructive) else "izmenit-razmetku"
