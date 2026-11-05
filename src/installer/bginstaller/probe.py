"""Разбор описания дисков из вывода системных утилит.

Здесь нет вызовов этих утилит: разбор чистый и тестируется на заготовленном
выводе, в том числе на машине разработчика без Linux. Исполнение — задача CLI.
"""

from __future__ import annotations

import json
import re

from bginstaller.errors import ProbeError
from bginstaller.models import (
    BootMode,
    Disk,
    FSType,
    OsKind,
    Partition,
    PartitionTable,
    WindowsState,
)

_FS_MAP = {
    "vfat": FSType.FAT32,
    "fat32": FSType.FAT32,
    "fat16": FSType.UNKNOWN,
    "ntfs": FSType.NTFS,
    "btrfs": FSType.BTRFS,
    "ext4": FSType.UNKNOWN,
    "ext2": FSType.UNKNOWN,
    "swap": FSType.SWAP,
    "exfat": FSType.EXFAT,
    "squashfs": FSType.UNKNOWN,
}

_RECOVERY_LABELS = {"windowsrecovery", "recovery", "winre", "dell", "hp_service", "oem"}
_ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec915"
_MSR_GUID = "e3c9e316-0b45-4580-9f2b-d1234a7bf014"
_BASIC_GUID = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
_LINUX_GUID = "0fc63daf-8483-477e-8e3d-0177f19e1874"


def parse_lsblk(text: str) -> tuple[Disk, ...]:
    """lsblk --json --bytes --paths --output NAME,SIZE,FSTYPE,PARTTYPENAME,PARTFLAGS,LABEL,UUID,MOUNTPOINT,TYPE,MODEL,TRAN,ROTA"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"вывод lsblk не является JSON: {exc}") from exc
    blocks = payload.get("blockdevices")
    if not isinstance(blocks, list):
        raise ProbeError("в выводе lsblk нет списка blockdevices")
    disks = []
    for item in blocks:
        if item.get("type") != "disk":
            continue
        disks.append(_disk_from_lsblk(item))
    return tuple(disks)


def _partition_table(value) -> PartitionTable:
    """Таблица разделов по PTTYPE (lsblk) или label (sfdisk).

    Пустое значение — диска без таблицы, то есть свежего. Раньше оно попадало
    в ветку MBR, и проверка безопасности отказывала такому диску в UEFI:
    «требуется GPT, а таблица mbr» — на диске, где нет ничего.
    """
    name = str(value or "").strip().lower()
    if not name:
        return PartitionTable.NONE
    if name == "gpt":
        return PartitionTable.GPT
    if name in ("dos", "mbr", "msdos"):
        return PartitionTable.MBR
    return PartitionTable.UNKNOWN


def _disk_from_lsblk(item: dict) -> Disk:
    children = item.get("children") or []
    partitions = tuple(_partition_from_lsblk(child, index + 1) for index, child in enumerate(children))
    table = _partition_table(item.get("pttype"))
    size = _as_int(item.get("size"))
    if size <= 0:
        raise ProbeError(f"диск {item.get('name')!r} имеет нечитаемый размер")
    return Disk(
        device=f"/dev/{item['name']}" if not str(item.get("name", "")).startswith("/") else item["name"],
        size_bytes=size,
        table=table,
        model=(item.get("model") or "").strip(),
        rotational=str(item.get("rota", "")).lower() in ("true", "1"),
        partitions=partitions,
    )


def _partition_from_lsblk(child: dict, fallback_number: int) -> Partition:
    name = str(child.get("name", ""))
    number = _as_int(child.get("partno")) or fallback_number
    fstype = _FS_MAP.get((child.get("fstype") or "").lower(), FSType.UNKNOWN)
    flags: list[str] = []
    parttype = (child.get("parttypename") or "").lower()
    partuuid = (child.get("partuuid") or "").lower()
    if "system" in parttype or partuuid == _ESP_GUID:
        flags.append("esp")
    if partuuid == _MSR_GUID or "microsoft reserved" in parttype:
        flags.append("msr")
    if "bios" in parttype:
        flags.append("bios_boot")
    label = (child.get("label") or "").strip()
    return Partition(
        number=int(number),
        start_bytes=0,  # lsblk не даёт начало раздела: берём из sfdisk, см. enrich_with_sfdisk()
        size_bytes=_as_int(child.get("size")),
        fs_type=fstype,
        label=label,
        uuid=(child.get("uuid") or "").strip(),
        mount_point=child.get("mountpoint"),
        os_kind=_os_kind(fstype, label, parttype, name),
        flags=tuple(flags),
    )


def enrich_with_sfdisk(disk: Disk, text: str) -> Disk:
    """sfdisk --json <disk>: добавляет начала разделов, которых нет в lsblk."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"вывод sfdisk не является JSON: {exc}") from exc
    rows = payload.get("partitiontable") or {}
    first_lba = _as_int(rows.get("firstlba")) or 0
    partitions: list[Partition] = []
    by_number: dict[int, tuple[int, int]] = {}
    for row in rows.get("partitions") or []:
        number = int(_as_int(row.get("partno")) or 0)
        if not number:
            # В выводе sfdisk часть может быть описана только узлом: номер в конце имени.
            match = re.search(r"p?(\d+)$", str(row.get("node", "")))
            number = int(match.group(1)) if match else 0
        start = _as_int(row.get("start"))
        length = _as_int(row.get("size"))
        if not number:
            raise ProbeError(f"в записи sfdisk нет номера раздела: {row!r}")
        # sfdisk выдаёт адреса в секторах по 512 байт независимо от размера сектора.
        by_number[number] = (start * 512, length * 512)
    existing = {part.number: part for part in disk.partitions}
    all_numbers = sorted(set(list(by_number) + list(existing)))
    for number in all_numbers:
        start, length = by_number.get(number, (existing.get(number).start_bytes if number in existing else 0,
                                                existing.get(number).size_bytes if number in existing else 0))
        base = existing.get(number)
        if base is None:
            base = Partition(number=number, start_bytes=start, size_bytes=length)
        partitions.append(
            Partition(
                number=number,
                start_bytes=start,
                size_bytes=length or base.size_bytes,
                fs_type=base.fs_type,
                label=base.label,
                uuid=base.uuid,
                mount_point=base.mount_point,
                os_kind=base.os_kind,
                flags=base.flags,
            )
        )
    # sfdisk знает label лучше, но «не gpt» от него не переведёт GPT в
    # неизвестность: если lsblk уже видел GPT, спорить с ним не будем.
    table = _partition_table(rows.get("label"))
    if table is PartitionTable.NONE:
        table = disk.table
    return Disk(
        device=disk.device,
        size_bytes=disk.size_bytes,
        table=table,
        model=disk.model,
        rotational=disk.rotational,
        sector_size=_as_int(rows.get("unit")) or 512,
        partitions=tuple(partitions),
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return 0


def _os_kind(fstype: FSType, label: str, parttype: str, name: str) -> OsKind:
    lowered = label.lower()
    if fstype is FSType.NTFS:
        if lowered in _RECOVERY_LABELS or "recover" in lowered:
            return OsKind.RECOVERY
        if lowered in ("msr-reserved",) or "microsoft reserved" in parttype:
            return OsKind.OTHER
        return OsKind.WINDOWS
    if fstype is FSType.FAT32 and lowered in ("efi", "boot", "system", "byteguru_efi"):
        return OsKind.OTHER
    if "byteguru" in lowered or "bg_" in lowered:
        return OsKind.BYTEGURU
    if "basic data" in parttype and fstype is FSType.UNKNOWN:
        return OsKind.OTHER
    return OsKind.NONE if fstype in (FSType.UNKNOWN, FSType.NONE) else OsKind.OTHER


def detect_windows(disk: Disk, os_prober_text: str = "", blkid_flags: dict[str, str] | None = None) -> WindowsState:
    """Собираем сведения о соседней Windows из prober-вывода и флагов blkid.

    Всё, что не подтверждено выводом утилиты, остаётся неизвестным: установщик
    обязан относиться к неизвестному как к риску (см. bginstaller.safety).
    """
    flags = blkid_flags or {}
    ntfs = [part for part in disk.partitions if part.fs_type is FSType.NTFS and part.size_bytes > 10 * 1024**3]
    if not ntfs:
        return WindowsState(present=False)
    biggest = max(ntfs, key=lambda part: part.size_bytes)
    state = flags.get(biggest.uuid) or flags.get(_guess_node(disk, biggest)) or ""
    bitlocker = "bitlocker" in state.lower() or any(
        value.lower().find("bitlocker") >= 0 for value in flags.values() if value
    )
    hibernated = any(
        token in state.lower() for token in ("hibernated", "dirty", "suspended")
    ) or "hiberfil" in os_prober_text.lower()
    version = ""
    match = re.search(r"(Windows\s+(?:10|11)(?:\s+[A-Z]+)?)", os_prober_text, re.IGNORECASE)
    if match:
        version = match.group(1)
    return WindowsState(
        present=True,
        version=version,
        bitlocker_protected=bitlocker,
        hibernated=hibernated,
        dirty="dirty" in state.lower(),
        device_path=_guess_node(disk, biggest),
    )


def _guess_node(disk: Disk, part: Partition) -> str:
    if disk.device and disk.device[-1].isdigit():
        return f"{disk.device}p{part.number}"
    return f"{disk.device}{part.number}"


def boot_mode_from_efivar(variables: list[str]) -> BootMode:
    """Есть ли в EFI-переменных загрузочные записи — признак UEFI-загрузки."""
    joined = " ".join(variables).lower()
    return BootMode.UEFI if ("boot####" in joined or "bootorder" in joined) else BootMode.LEGACY
