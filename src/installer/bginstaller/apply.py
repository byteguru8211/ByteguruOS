"""Исполнение плана установки.

По умолчанию — только показ команд (`apply=True` нужен явно). Команды
собирает `bginstaller.operations`: здесь остаются порядок, подтверждения и учёт
результата, а не предметные детали разметки или загрузчика.

Никакого `shell=True`: вектор аргументов как есть, данные (пароль, содержимое
файла) уходят через stdin или запись файла. Путь с пробелом или кириллицей не
должен становиться способом выполнить что-то ещё.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bginstaller.errors import ApplyError
from bginstaller.models import Action, InstallPlan
from bginstaller.operations import Context, Job, State, commands_for

CommandRunner = Callable[..., int]
FileWriter = Callable[[str, str], None]


@dataclass
class StepResult:
    action: Action
    jobs: list[Job] = field(default_factory=list)
    returncode: int | None = None
    failed: bool = False
    skipped: bool = False
    note: str = ""

    @property
    def argv(self) -> tuple[str, ...]:
        """Первая реальная команда шага — для старых вызовов и тестов."""
        for job in self.jobs:
            if job.argv:
                return job.argv
        return ()

    @property
    def commands(self) -> list[str]:
        return [job.command_line for job in self.jobs]

    @property
    def command_line(self) -> str:
        return " ; ".join(self.commands) or "—"


@dataclass
class ApplyReport:
    steps: list[StepResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        return not any(step.failed for step in self.steps)

    @property
    def executed(self) -> int:
        return sum(1 for step in self.steps
            if not step.skipped and step.returncode == 0)

    def as_text(self) -> str:
        lines = ["ПРОБА (ничего не изменялось)" if self.dry_run else "ИСПОЛНЕНО"]
        for step in self.steps:
            mark = "skip" if step.skipped else ("FAIL" if step.failed else "ok")
            lines.append(f"[{mark:>4}] #{step.action.order} {step.action.kind}")
            for line in step.commands:
                lines.append(f"        $ {line}")
            if step.note:
                lines.append(f"        {step.note}")
        return "\n".join(lines)


def default_runner(argv: Sequence[str], stdin: str | None = None) -> int:
    return subprocess.run(list(argv), input=stdin, text=True,
        check=False).returncode


def default_writer(path: str, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def apply_plan(
    plan: InstallPlan,
    *,
    apply: bool = False,
    runner: CommandRunner | None = None,
    writer: FileWriter | None = None,
    context: Context | None = None,
    confirmations: set[int] | None = None,
) -> ApplyReport:
    """Пройти по плану. `apply=False` — только сборка и печать команд."""
    execute = runner or default_runner
    write = writer or default_writer
    ctx = context or Context()
    report = ApplyReport(dry_run=not apply)
    confirmations = confirmations or set()
    state = State(disk=plan.disk)

    for action in plan.actions:
        try:
            jobs = commands_for(action, plan, ctx, state)
        except ApplyError as exc:
            report.steps.append(StepResult(action=action, failed=True, note=str(exc)))
            continue

        if action.requires_confirmation and action.order not in confirmations:
            report.steps.append(StepResult(action=action, jobs=jobs, skipped=True,
                note="требуется явное подтверждение: "
                    f"{action.rollback_hint or 'необратимое действие'}"))
            continue
        if not apply:
            report.steps.append(StepResult(action=action, jobs=jobs, skipped=True,
                note="проба"))
            continue

        step = StepResult(action=action, jobs=jobs)
        for job in jobs:
            try:
                if job.write:
                    write(job.write[0], job.write[1])
                elif job.argv:
                    code = execute(list(job.argv), job.stdin or None)
                    step.returncode = code
                    if code != 0:
                        step.failed = True
                        step.note = (f"команда завершилась кодом {code}: "
                            f"{job.command_line}")
                        break
            except (OSError, ValueError) as exc:
                step.failed = True
                step.note = str(exc)
                break
        report.steps.append(step)
        if step.failed:
            break
    return report


def confirm_token(plan: InstallPlan) -> str:
    """Токен подтверждения необратимых действий — чтобы вводили слово, а не «y»."""
    destructive = plan.destructive_actions()
    if not destructive:
        return "ok"
    return "udalit-dannye" if any(a.kind == "wipe-table" for a in destructive) else "izmenit-razmetku"
