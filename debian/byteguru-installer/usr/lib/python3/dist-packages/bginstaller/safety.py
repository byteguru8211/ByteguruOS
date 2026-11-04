"""Проверки, без которых установщик не имеет права трогать диск.

Философия простая: неизвестное состояние соседней Windows — это риск, а не
разрешение. Сжатие NTFS при включённом BitLocker или в гибернации означает
повреждение чужой системы, и здесь мы обязаны остановить процесс, а не «попробовать».
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from bginstaller.models import BootMode, Disk, FSType, PartitionTable, Profile, WindowsState, GiB, MiB


class Severity(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class Gate:
    name: str
    severity: Severity
    message: str
    remedy: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCK


MIN_ESP_BYTES = 512 * MiB
ABSOLUTE_MIN_ROOT = 12 * GiB


def _gates_for_windows(disk: Disk, windows: WindowsState) -> list[Gate]:
    result: list[Gate] = []
    if not windows.present:
        return result
    ntfs = [part for part in disk.partitions if part.fs_type is FSType.NTFS]
    if not ntfs:
        result.append(
            Gate(
                "windows-ntfs-detected",
                Severity.WARN,
                "Windows заявлена, но NTFS-раздел не найден: возможно, система на другом диске",
                "уточните, на каком диске стоит Windows",
            )
        )
        return result
    if windows.hibernated:
        result.append(
            Gate(
                "windows-shutdown",
                Severity.BLOCK,
                "раздел Windows в состоянии гибернации: монтирование на запись или изменение размера разрушат том",
                "загрузитесь в Windows и выполните полное завершение работы (shutdown /s /t 0)",
            )
        )
    if windows.dirty:
        result.append(
            Gate(
                "ntfs-consistency",
                Severity.BLOCK,
                "том NTFS помечен как грязный: требуется проверка перед любыми изменениями",
                "запустите chkdsk в Windows и завершите работу корректно",
            )
        )
    if windows.bitlocker_protected:
        result.append(
            Gate(
                "bitlocker",
                Severity.BLOCK,
                "том защищён BitLocker: изменение размера возможно только после приостановки защиты",
                "приостановите защиту (manage-bde -protectors -disable C:) на время установки",
            )
        )
    if windows.fast_startup is None:
        result.append(
            Gate(
                "fast-startup",
                Severity.WARN,
                "не удалось определить, выключен ли «быстрый запуск» — он оставляет том в смонтированном состоянии",
                "в Windows отключите быстрый запуск в электропитании",
            )
        )
    return result


def evaluate(
    disk: Disk,
    windows: WindowsState,
    boot_mode: BootMode,
    profile: Profile,
    *,
    mem_bytes: int = 4 * GiB,
    required_bytes: int = 0,
    on_removable: bool = False,
    battery: bool = False,
) -> list[Gate]:
    """Полный список замечаний. Порядок важен: блокирующие идут первыми."""
    gates: list[Gate] = []

    if boot_mode is BootMode.UEFI and disk.table is not PartitionTable.GPT:
        gates.append(
            Gate(
                "uefi-needs-gpt",
                Severity.BLOCK,
                f"загрузка UEFI требует GPT, а на {disk.device} таблица {disk.table.value}",
                "переведите диск в GPT (это пересобирает разметку целиком) или используйте Legacy",
            )
        )
    if boot_mode is BootMode.LEGACY:
        gates.append(
            Gate(
                "legacy-mode",
                Severity.WARN,
                "Legacy/CSM-загрузка поддерживается, но не является основной: обновления прошивки могут её убрать",
                "по возможности перейдите на UEFI",
            )
        )

    if disk.size_bytes < 60 * GiB and profile is not Profile.KIOSK:
        gates.append(
            Gate(
                "disk-size",
                Severity.WARN,
                f"диск {disk.size_bytes // GiB} ГиБ: места хватит, но запаса на рост /var почти нет",
                "уменьшите размер WinCompat-окружений или используйте больший диск",
            )
        )

    if required_bytes and disk.total_free_bytes() < required_bytes and not windows.present:
        gates.append(
            Gate(
                "free-space",
                Severity.BLOCK,
                f"нужно {required_bytes // GiB} ГиБ, свободно {disk.total_free_bytes() // GiB} ГиБ,"
                " а соседней Windows для сжатия нет",
                "освободите место или выберите другой диск",
            )
        )

    if boot_mode is BootMode.UEFI:
        esp = disk.esp
        if esp is not None and esp.size_bytes < MIN_ESP_BYTES:
            gates.append(
                Gate(
                    "esp-too-small",
                    Severity.WARN,
                    f"ESP {esp.size_bytes // MiB} МиБ: двух слотов образа и наших файлов может не хватить",
                    "придётся пересоздать ESP — это тронет загрузчик Windows, поэтому требуется резервная копия",
                )
            )

    if windows.hibernated and boot_mode is BootMode.UEFI:
        gates.append(
            Gate(
                "dual-boot-hibernation",
                Severity.INFO,
                "пока Windows в гибернации, мы смонтируем её том только на чтение — это норма, а не ошибка",
            )
        )

    if mem_bytes < 2 * GiB:
        gates.append(
            Gate(
                "memory",
                Severity.WARN,
                f"ОЗУ {mem_bytes // MiB} МиБ: подсистема WinCompat будет работать с ограничениями",
                "для Windows-приложений желательно не менее 8 ГиБ",
            )
        )
    elif mem_bytes > 32 * GiB:
        gates.append(
            Gate(
                "swap-policy",
                Severity.INFO,
                "ОЗУ достаточно, чтобы не выделять раздел swap: используется swapfile в /var",
            )
        )

    if on_removable:
        gates.append(
            Gate(
                "removable-media",
                Severity.WARN,
                f"{disk.device} съёмный: загрузочная запись на съёмном диске не переживет перенос между машинами",
                "установите на внутренний диск, съёмный оставьте как Live",
            )
        )
    if battery:
        gates.append(
            Gate(
                "power-source",
                Severity.WARN,
                "ноутбук работает от батареи: обрыв питания во время изменения разметки опасен для данных",
                "подключите питание",
            )
        )

    gates.extend(_gates_for_windows(disk, windows))
    return gates


def has_blocker(gates: list[Gate]) -> bool:
    return any(gate.blocking for gate in gates)


def blockers(gates: list[Gate]) -> list[Gate]:
    return [gate for gate in gates if gate.blocking]


def format_report(gates: list[Gate]) -> str:
    if not gates:
        return "замечаний нет"
    lines = []
    for gate in gates:
        mark = {"block": "ЗАПРЕТ", "warn": "ВНИМАНИЕ", "info": "инфо"}[gate.severity.value]
        text = f"[{mark}] {gate.message}"
        if gate.remedy:
            text += f"\n        → {gate.remedy}"
        lines.append(text)
    return "\n".join(lines)
