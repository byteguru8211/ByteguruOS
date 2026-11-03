"""Переключение между ОС из оболочке: команды, но безexecution без подтверждения."""

from __future__ import annotations

from dataclasses import dataclass

from bginstaller.bootrepair import EfiState, next_boot_command, repair_commands, windows_boot_index

OS_LABELS = {"byteguru": "ByteguruOS", "windows": "Windows", "last": "как в прошлый раз"}


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    commands: tuple[str, ...] = ()
    message: str = ""
    requires_reboot: bool = False


def reboot_into(target: str, state: EfiState) -> SwitchResult:
    """Однократная загрузка в другую ОС: не меняем постоянный порядок загрузки."""
    if target not in OS_LABELS:
        return SwitchResult(ok=False, message=f"неизвестная цель {target!r}")
    if target == "last":
        return SwitchResult(ok=False, message="«как в прошлый раз» — это настройка по умолчанию, а не разовая загрузка")
    try:
        argv = next_boot_command(OS_LABELS[target], state)
    except Exception as exc:  # запись может отсутствовать, если Windows не стоит
        return SwitchResult(ok=False, message=str(exc))
    return SwitchResult(
        ok=True,
        commands=tuple(argv),
        message=f"следующая загрузка — в {OS_LABELS[target]}; текущая сессия будет завершена",
        requires_reboot=True,
    )


def set_default(target: str, state: EfiState) -> SwitchResult:
    """Постоянный выбор ОС по умолчанию."""
    if target == "windows":
        index = windows_boot_index(state)
        if index is None:
            return SwitchResult(ok=False, message="запись Windows в EFI не найдена")
        order = [index] + [item for item in state.boot_order if item != index]
        return SwitchResult(
            ok=True, commands=("efibootmgr", "-o", ",".join(order)),
            message="Windows будет загружаться по умолчанию", requires_reboot=True,
        )
    if target == "byteguru":
        commands = repair_commands(state)
        if not commands:
            return SwitchResult(ok=False, message="нужно переустановить загрузчик")
        return SwitchResult(
            ok=True, commands=tuple(commands[0]),
            message="ByteguruOS будет загружаться по умолчанию", requires_reboot=True,
        )
    return SwitchResult(ok=False, message=f"неизвестная цель {target!r}")


def describe(result: SwitchResult) -> str:
    if not result.ok:
        return f"не выполнено: {result.message}"
    lines = [result.message, "будет выполнено: " + " ".join(result.commands)]
    if result.requires_reboot:
        lines.append("требуется перезагрузка: несохранённая работа в текущем сеансе будет потеряна")
    return "\n".join(lines)
