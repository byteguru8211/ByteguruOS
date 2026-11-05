"""ByteguruOS installer — установка, соседство с Windows и ремонт загрузки.

Код написан так, чтобы решения о разметке можно было проверить без.root-доступа
и без реального диска: разбор вывода утилит отделён от планирования, а команды
строятся, но не исполняются до явного `--apply`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

SECTOR = 512
MIB_ALIGN = MiB  # все границы кратны 1 МиБ: так делают sfdisk и parted по умолчанию


class PartitionTable(str, Enum):
    # Пустое значение lsblk — это не «mbr». Диск из коробки не имеет таблицы
    # вовсе, и смешивать его с реальной MBR означало отказывать ему в UEFI.
    NONE = "none"
    GPT = "gpt"
    MBR = "mbr"
    # Непривычная метка (mac, sun, aix…): считать её совместимой с UEFI
    # нельзя, а молча перемаркировать — значит уничтожить данные, которых мы
    # не видели.
    UNKNOWN = "unknown"


class BootMode(str, Enum):
    UEFI = "uefi"
    LEGACY = "legacy"


class FSType(str, Enum):
    FAT32 = "fat32"
    NTFS = "ntfs"
    BTRFS = "btrfs"
    EXFAT = "exfat"
    SWAP = "swap"
    UNKNOWN = "unknown"
    NONE = "none"


class OsKind(str, Enum):
    WINDOWS = "windows"
    BYTEGURU = "byteguru"
    RECOVERY = "recovery"
    OTHER = "other"
    NONE = "none"


class Profile(str, Enum):
    """Профили раскладки из раздела 6.2 плана разработки."""

    WORKSTATION = "workstation"
    OFFICE_DUALBOOT = "office-dualboot"
    KIOSK = "kiosk"
    SERVER = "server"


@dataclass(frozen=True)
class Partition:
    number: int
    start_bytes: int
    size_bytes: int
    fs_type: FSType = FSType.UNKNOWN
    label: str = ""
    uuid: str = ""
    mount_point: str | None = None
    os_kind: OsKind = OsKind.NONE
    flags: tuple[str, ...] = ()

    @property
    def end_bytes(self) -> int:
        return self.start_bytes + self.size_bytes

    @property
    def is_esp(self) -> bool:
        return "esp" in self.flags or (self.fs_type is FSType.FAT32 and self.size_bytes <= 2 * GiB and self.number == 1)

    @property
    def is_windows_recovery(self) -> bool:
        return self.os_kind is OsKind.RECOVERY or self.label.lower() in (
            "windowsrecovery",
            "recovery",
            "winre",
        )

    @property
    def holds_windows(self) -> bool:
        return self.os_kind is OsKind.WINDOWS or (self.fs_type is FSType.NTFS and self.size_bytes > 10 * GiB)

    def overlaps(self, other: "Partition") -> bool:
        return self.start_bytes < other.end_bytes and other.start_bytes < self.end_bytes


@dataclass(frozen=True)
class Disk:
    device: str
    size_bytes: int
    table: PartitionTable
    model: str = ""
    rotational: bool = True
    sector_size: int = SECTOR
    partitions: tuple[Partition, ...] = ()

    @property
    def esp(self) -> Partition | None:
        for part in self.partitions:
            if part.is_esp:
                return part
        return None

    @property
    def windows_partitions(self) -> tuple[Partition, ...]:
        return tuple(part for part in self.partitions if part.holds_windows)

    @property
    def byteguru_partitions(self) -> tuple[Partition, ...]:
        return tuple(part for part in self.partitions if part.os_kind is OsKind.BYTEGURU)

    def free_segments(self, *, reserved_after_bytes: int = 8 * MiB) -> tuple[tuple[int, int], ...]:
        """Незанятые области (start, length), отсортированные по убыванию размера.

        Хвост диска оставляем свободным: при росте размера раздела ядру нужен
        запас, и sfdisk иначе жалуется на нехватку места для вторичной GPT.
        """
        used = sorted((p.start_bytes, p.end_bytes) for p in self.partitions)
        gaps: list[tuple[int, int]] = []
        cursor = self.first_usable(reserved_after_bytes)
        for start, end in used:
            if start > cursor:
                gaps.append((cursor, start - cursor))
            cursor = max(cursor, end)
        limit = self.size_bytes - reserved_after_bytes
        if cursor < limit:
            gaps.append((cursor, limit - cursor))
        return tuple(sorted((g for g in gaps if g[1] >= MiB), key=lambda g: -g[1]))

    def first_usable(self, reserved_after_bytes: int = 8 * MiB) -> int:
        if self.table is PartitionTable.GPT:
            return max(1 * MiB, reserved_after_bytes)
        return MiB

    def total_free_bytes(self) -> int:
        return sum(length for _start, length in self.free_segments())


@dataclass(frozen=True)
class WindowsState:
    """Что известно про соседнюю Windows. Незнание трактуем как риск, не как разрешение."""

    present: bool = False
    version: str = ""
    bitlocker_protected: bool = False
    hibernated: bool = False
    fast_startup: bool | None = None
    dirty: bool = False
    secure_boot_enabled: bool | None = None
    device_path: str = ""

    @property
    def unknown_risks(self) -> tuple[str, ...]:
        risks = []
        if self.present and self.fast_startup is None:
            risks.append("неизвестно, выключен ли «быстрый запуск» Windows")
        if self.present and self.secure_boot_enabled is None:
            risks.append("неизвестен режим Secure Boot")
        return tuple(risks)


@dataclass(frozen=True)
class Action:
    """Одно упорядоченное действие плана. Исполняется только с --apply и только на Linux."""

    order: int
    kind: str
    target: str
    arguments: tuple[str, ...] = ()
    destructive: bool = False
    rollback_hint: str = ""
    requires_confirmation: bool = False

    def describe(self) -> str:
        flags = " [необратимо]" if self.destructive else ""
        return f"{self.order:>3}. {self.kind} {self.target}{' '.join(' ' + a for a in self.arguments)}{flags}"


@dataclass(frozen=True)
class Layout:
    """Итоговая раскладка: что и где лежит после установки."""

    root_bytes: int
    var_bytes: int
    home_bytes: int
    swap_bytes: int
    esp_bytes: int
    shared_bytes: int
    reuse_esp: bool
    shrink_windows_bytes: int
    create_shared: bool

    def total_new_bytes(self) -> int:
        return self.root_bytes + self.var_bytes + self.home_bytes + self.swap_bytes + (
            self.shared_bytes if self.create_shared else 0
        ) + (0 if self.reuse_esp else self.esp_bytes)

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "root_bytes": self.root_bytes,
            "var_bytes": self.var_bytes,
            "home_bytes": self.home_bytes,
            "swap_bytes": self.swap_bytes,
            "esp_bytes": self.esp_bytes,
            "shared_bytes": self.shared_bytes,
            "reuse_esp": self.reuse_esp,
            "shrink_windows_bytes": self.shrink_windows_bytes,
            "create_shared": self.create_shared,
        }


@dataclass(frozen=True)
class InstallPlan:
    disk: Disk
    layout: Layout
    boot_mode: BootMode
    profile: Profile
    windows: WindowsState
    actions: tuple[Action, ...]
    notes: tuple[str, ...] = ()

    def destructive_actions(self) -> Iterable[Action]:
        return tuple(action for action in self.actions if action.destructive)

    def requires_user_confirmation(self) -> bool:
        return any(action.requires_confirmation for action in self.actions)

    def summary(self) -> str:
        lines = [
            f"диск: {self.disk.device} ({self.disk.size_bytes // GiB} ГиБ, {self.disk.table.value})",
            f"профиль: {self.profile.value}, загрузка: {self.boot_mode.value}",
        ]
        if self.windows.present:
            lines.append(f"соседняя Windows: {self.windows.version or 'версия не определена'}")
        lines.append(f"новая разметка: root {self.layout.root_bytes // GiB} ГиБ,"
                     f" /var {self.layout.var_bytes // GiB} ГиБ,"
                     f" /home {self.layout.home_bytes // GiB} ГиБ")
        if self.layout.shrink_windows_bytes:
            lines.append(
                f"сжатие NTFS на {self.layout.shrink_windows_bytes // GiB} ГиБ (только после проверки диска)"
            )
        lines.extend(action.describe() for action in self.actions)
        return "\n".join(lines)
