"""Диагностика и ремонт EFI-записей после обновлений Windows.

Самая частая жалоба на двухзагрузочной машине: «после обновления Windows
ByteguruOS исчез из меню». Причина — Windows перезаписывает BootOrder. Чинится
это одной командой efibootmgr, но только если понимать, что именно пропало.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bginstaller.errors import BootRepairError

BOOT_ENTRY_RE = re.compile(r"^Boot(\d{4})\*?\s*(.*?)\s*(RCU?\s*:\s*(.*))?$", re.MULTILINE)
BOOT_ORDER_RE = re.compile(r"BootOrder\s*:\s*([0-9A-Fa-f,]+)")
BOOT_NEXT_RE = re.compile(r"BootNext\s*:\s*([0-9A-Fa-f]{4})")
FILE_PATH_RE = re.compile(r"(HD|File)\(([^)]*)\)(?:[,/](Pc\([^)]*\)[^)]*)?)?", re.IGNORECASE)


@dataclass(frozen=True)
class BootEntry:
    index: str
    label: str
    raw: str
    active: bool

    @property
    def is_byteguru(self) -> bool:
        text = (self.label + " " + self.raw).lower()
        return "byteguru" in text or "systemd-boot" in text or r"\efi\byteguru" in text.replace("\\", "\\").lower()

    @property
    def is_windows(self) -> bool:
        text = (self.label + " " + self.raw).lower()
        return "windows" in text or "bootmgfw" in text.replace("\\", "\\").lower()

    @property
    def device_path(self) -> str:
        match = FILE_PATH_RE.search(self.raw)
        return match.group(0) if match else ""


@dataclass(frozen=True)
class EfiState:
    entries: tuple[BootEntry, ...]
    boot_order: tuple[str, ...]
    boot_next: str | None
    secure_boot: bool | None

    def by_index(self, index: str) -> BootEntry | None:
        for entry in self.entries:
            if entry.index == index:
                return entry
        return None

    @property
    def byteguru_entries(self) -> tuple[BootEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_byteguru)

    @property
    def windows_entries(self) -> tuple[BootEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_windows)

    @property
    def first(self) -> BootEntry | None:
        for index in self.boot_order:
            entry = self.by_index(index)
            if entry is not None:
                return entry
        return self.entries[0] if self.entries else None


def parse_efibootmgr(text: str) -> EfiState:
    """Разбор вывода `efibootmgr -v`."""
    if not text.strip():
        raise BootRepairError("вывод efibootmgr пуст: проверьте, что система загружена в UEFI-режиме")
    entries: list[BootEntry] = []
    for line in text.splitlines():
        match = re.match(r"^(Boot([0-9A-Fa-f]{4}))(\*?)\s+(.*)$", line.strip())
        if not match:
            continue
        index = match.group(2)
        active = match.group(3) != "*"
        label_and_raw = match.group(4)
        # efibootmgr отделяет метку от описания табом (иногда запятой).
        label = re.split(r"[\t,]", label_and_raw, maxsplit=1)[0].strip()
        entries.append(BootEntry(index=index.upper(), label=label, raw=label_and_raw, active=active))
    order_match = BOOT_ORDER_RE.search(text)
    order = tuple(item.strip().upper() for item in order_match.group(1).split(",")) if order_match else ()
    next_match = BOOT_NEXT_RE.search(text)
    secure = None
    secure_match = re.search(r"SecureBoot\s*:\s*([0-9a-fA-F])", text)
    if secure_match:
        secure = secure_match.group(1) == "1"
    return EfiState(entries=tuple(entries), boot_order=order, boot_next=next_match.group(1).upper() if next_match else None, secure_boot=secure)


def diagnose(state: EfiState) -> list[str]:
    """Что не так с загрузкой — человеческим языком, по убыванию серьёзности."""
    problems: list[str] = []
    ours = state.byteguru_entries
    if not ours:
        problems.append("запись ByteguruOS в EFI отсутствует: Windows её затёрла или установка не дошла до загрузчика")
    elif not any(entry.active for entry in ours):
        problems.append("запись ByteguruOS есть, но помечена неактивной (* = скрыта из меню)")
    first = state.first
    if first is not None and not first.is_byteguru and ours:
        problems.append(f"по умолчанию загружается «{first.label}», а не ByteguruOS")
    if state.boot_next:
        entry = state.by_index(state.boot_next)
        if entry is not None:
            problems.append(f"однократная загрузка назначена на «{entry.label}»")
    return problems


def repair_commands(state: EfiState, *, disk: str = "/dev/nvme0n1", esp_number: int = 1) -> list[list[str]]:
    """Команды восстановления. Возвращаются как векторы — без shell-инъекций."""
    ours = state.byteguru_entries
    commands: list[list[str]] = []
    if ours:
        active = [entry for entry in ours if entry.active] or ours
        best = active[0]
        order = [best.index] + [index for index in state.boot_order if index != best.index]
        commands.append(
            ["efibootmgr", "-o", ",".join(order)]
        )
        if not best.active:
            commands.append(["efibootmgr", "-b", best.index, "-A"])
        return commands
    device_part = f"{disk}p{esp_number}" if disk[-1].isdigit() else f"{disk}{esp_number}"
    commands.append(
        [
            "efibootmgr",
            "-c",
            "--disk",
            disk,
            "--part",
            str(esp_number),
            "--label",
            "ByteguruOS",
            "--loader",
            r"\EFI\byteguru\systemd-bootx64.efi",
        ]
    )
    commands.append(["efibootmgr", "-u", device_part, "-g"])
    return commands


def next_boot_command(target_label: str, state: EfiState) -> list[str]:
    """Однократная перезагрузка в указанную ОС через BootNext."""
    wanted = [entry for entry in state.entries if target_label.lower() in entry.label.lower()]
    if not wanted:
        raise BootRepairError(f"загрузочная запись «{target_label}» не найдена в EFI")
    return ["efibootmgr", "-n", wanted[0].index]


def windows_boot_index(state: EfiState) -> str | None:
    entries = state.windows_entries
    return entries[0].index if entries else None
