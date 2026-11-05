"""Планировщик разметки: сколько, где и в каком порядке создавать.

План строится без обращения к диску: на входе описание (Disk + WindowsState),
на выходе упорядоченный список действий. Исполнение — отдельный шаг, поэтому
планирование можно репетировать и обсуждать до записи на носитель.

Главное ограничение, которое здесь учитывается: раздел обязан быть непрерывным.
Сумма свободных промежутков не равна доступному месту — щель в 1 ГиБ не станет
частью 50-ГиБ раздела.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from bginstaller.errors import PlanError
from bginstaller.models import (
    Action,
    BootMode,
    Disk,
    FSType,
    InstallPlan,
    Layout,
    Partition,
    Profile,
    WindowsState,
    GiB,
    MiB,
)
from bginstaller.safety import MIN_ESP_BYTES, blockers, evaluate, has_blocker

TAIL_RESERVE = 8 * MiB          # место под вторичную GPT-таблицу
WINDOWS_MINIMUM = 30 * GiB      # столько нельзя отнимать у Windows ни при каких условиях
HOME_FLOOR = 10 * GiB           # меньший /home профильные кэши съедят за месяц
MIN_USABLE_GAP = 1 * GiB        # промежуток меньше непригоден: наш минимальный раздел больше
SHRINK_STEP = 4 * GiB           # шаг подбора величины сжатия


@dataclass(frozen=True)
class Sizing:
    """Размеры для профиля. home_min — минимум, остальное добирается остатком."""

    root: int
    var: int
    home_min: int
    swap: int
    shared: int
    create_shared: bool = True


SIZINGS: dict[Profile, Sizing] = {
    # Офис с Windows-парком: /var растёт за счёт окружений WCV, поэтому запас большой.
    Profile.OFFICE_DUALBOOT: Sizing(30 * GiB, 80 * GiB, 40 * GiB, 8 * GiB, 50 * GiB),
    Profile.WORKSTATION: Sizing(30 * GiB, 60 * GiB, 40 * GiB, 8 * GiB, 16 * GiB),
    Profile.KIOSK: Sizing(20 * GiB, 20 * GiB, 0, 2 * GiB, 0, create_shared=False),
    Profile.SERVER: Sizing(20 * GiB, 40 * GiB, 0, 4 * GiB, 0, create_shared=False),
}


def gib(value: int) -> int:
    return int(value // GiB)


def partition_node(disk: Disk, part: Partition) -> str:
    """Имя блочного устройства раздела: у nvme и mmcblk нужен разделитель 'p'."""
    if disk.device and disk.device[-1].isdigit():
        return f"{disk.device}p{part.number}"
    return f"{disk.device}{part.number}"


def _usable_segments(disk: Disk) -> list[tuple[int, int]]:
    return [
        (start, length)
        for start, length in disk.free_segments(reserved_after_bytes=TAIL_RESERVE)
        if length >= MIN_USABLE_GAP
    ]


def _place(segments: list[tuple[int, int]], items: list[tuple[str, int]]) -> tuple[dict[str, int], list[tuple[int, int]]] | None:
    """First-fit-decreasing: крупные разделы раскладываем первыми.

    Иначе 8 ГиБ swap займут середину области, и для 80-ГиБ /var не останется
    непрерывного куска, хотя суммарно места хватало бы.
    """
    free = [[start, length] for start, length in segments if length > 0]
    free.sort(key=lambda seg: -seg[1])
    placed: dict[str, int] = {}
    for name, size in sorted(items, key=lambda item: -item[1]):
        for index, segment in enumerate(free):
            if segment[1] >= size:
                placed[name] = int(segment[0])
                segment[0] += size
                segment[1] -= size
                free = [item for item in free if item[1] > 0]
                free.sort(key=lambda seg: -seg[1])
                break
        else:
            return None
    return placed, [(int(start), int(length)) for start, length in free]


def _windows_ntfs(disk: Disk, windows: WindowsState) -> Partition:
    candidates = [p for p in disk.partitions if p.fs_type is FSType.NTFS and p.size_bytes > 10 * GiB]
    if windows.device_path:
        for part in candidates:
            if partition_node(disk, part) == windows.device_path:
                return part
    if not candidates:
        raise PlanError("сжимать нечего: NTFS-раздел с Windows не найден")
    return max(candidates, key=lambda part: part.size_bytes)


def _segments_after_shrink(disk: Disk, windows: WindowsState, shrink: int) -> list[tuple[int, int]]:
    """Свободные области с учётом освободившегося после сжатия NTFS."""
    segments = _usable_segments(disk)
    if shrink <= 0:
        return segments
    ntfs = _windows_ntfs(disk, windows)
    freed_start = ntfs.start_bytes + (ntfs.size_bytes - shrink)
    for index, (start, length) in enumerate(segments):
        if start == freed_start:
            segments[index] = (start, length + shrink)
            return segments
        if start > freed_start and freed_start + shrink == start:
            segments[index] = (freed_start, shrink + length)
            return segments
    if shrink >= MIN_USABLE_GAP:
        segments.append((freed_start, shrink))
    return segments


def _items(layout_items: list[tuple[str, int]], esp_cost: int) -> list[tuple[str, int]]:
    items = list(layout_items)
    if esp_cost:
        items.append(("esp", esp_cost))
    return [item for item in items if item[1] > 0]


def _esp_bytes(disk: Disk) -> int:
    esp = disk.esp
    if esp is not None and esp.size_bytes >= MIN_ESP_BYTES:
        return esp.size_bytes
    return 1 * GiB


def _allocate(disk: Disk, windows: WindowsState, sizing: Sizing, boot_mode: BootMode) -> Layout:
    """Подбираем раскладку: сначала свободное место, при нехватке — сжатие NTFS."""
    esp = disk.esp
    reuse_esp = esp is not None and esp.size_bytes >= MIN_ESP_BYTES and boot_mode is BootMode.UEFI
    esp_cost = 0 if reuse_esp else (_esp_bytes(disk) if boot_mode is BootMode.UEFI else 0)
    home_floor = max(sizing.home_min, HOME_FLOOR) if sizing.home_min else 0

    base_items = [
        ("root", sizing.root),
        ("var", sizing.var),
        ("swap", sizing.swap),
        ("shared", sizing.shared if sizing.create_shared else 0),
        ("home", home_floor),
    ]

    room = 0
    if windows.present:
        try:
            ntfs = _windows_ntfs(disk, windows)
            room = max(0, ntfs.size_bytes - WINDOWS_MINIMUM)
        except PlanError:
            room = 0

    shrink = 0
    while True:
        segments = _segments_after_shrink(disk, windows, shrink)
        result = _place(segments, _items(base_items, esp_cost))
        if result is not None:
            break
        if not windows.present:
            raise PlanError(
                f"не хватает непрерывного места под раскладку профиля;"
                f" свободно {gib(sum(length for _s, length in segments))} ГиБ,"
                f" требуется {gib(sum(size for _name, size in _items(base_items, esp_cost)))} ГиБ"
                " и соседней Windows для сжатия нет"
            )
        if shrink + SHRINK_STEP > room:
            raise PlanError(
                f"даже при сжатии Windows до {gib(WINDOWS_MINIMUM)} ГиБ непрерывного места не хватает"
            )
        shrink += SHRINK_STEP

    placed, leftover = result
    home_extra = max((length for _start, length in leftover), default=0)
    home_bytes = (home_floor + home_extra) if sizing.home_min else 0

    return Layout(
        root_bytes=sizing.root,
        var_bytes=sizing.var,
        home_bytes=home_bytes,
        swap_bytes=sizing.swap,
        esp_bytes=esp.size_bytes if reuse_esp else _esp_bytes(disk),
        shared_bytes=sizing.shared,
        reuse_esp=reuse_esp,
        shrink_windows_bytes=shrink,
        create_shared=sizing.create_shared,
    )


def _placements(disk: Disk, windows: WindowsState, layout: Layout, boot_mode: BootMode) -> dict[str, int]:
    """Начала новых разделов для готовой раскладки (тот же алгоритм, что и при подборе)."""
    segments = _segments_after_shrink(disk, windows, layout.shrink_windows_bytes)
    esp_cost = 0 if layout.reuse_esp or boot_mode is not BootMode.UEFI else layout.esp_bytes
    items = [
        ("root", layout.root_bytes),
        ("var", layout.var_bytes),
        ("home", layout.home_bytes),
        ("swap", layout.swap_bytes),
        ("shared", layout.shared_bytes if layout.create_shared else 0),
    ]
    result = _place(segments, _items(items, esp_cost))
    if result is None:
        raise PlanError("готовая раскладка не размещается в непрерывных областях")
    return result[0]


def _new_partitions(profile: Profile, layout: Layout) -> list[tuple[str, int, str, str]]:
    parts = [
        ("root", layout.root_bytes, "btrfs", "BG_ROOT"),
        ("var", layout.var_bytes, "btrfs", "BG_VAR"),
    ]
    if layout.home_bytes:
        parts.append(("home", layout.home_bytes, "btrfs", "BG_HOME"))
    if layout.swap_bytes:
        parts.append(("swap", layout.swap_bytes, "swap", "BG_SWAP"))
    if layout.create_shared and layout.shared_bytes:
        parts.append(("shared", layout.shared_bytes, "exfat", "BG_SHARE"))
    return [part for part in parts if part[1] > 0]


def _ordered_actions(
    disk: Disk,
    windows: WindowsState,
    layout: Layout,
    boot_mode: BootMode,
    profile: Profile,
    *,
    clean_layout: bool = False,
) -> list[Action]:
    actions: list[Action] = []

    def add(kind: str, target: str, *args: str, destructive: bool = False,
            confirm: bool = False, rollback: str = "") -> None:
        actions.append(
            Action(
                order=len(actions) + 1,
                kind=kind,
                target=target,
                arguments=args,
                destructive=destructive,
                requires_confirmation=confirm,
                rollback_hint=rollback,
            )
        )

    add("probe-disk", disk.device, "--json")
    if windows.present and not clean_layout:
        add("inspect-ntfs", disk.device, "--check-only")

    if layout.shrink_windows_bytes and windows.present and not clean_layout:
        ntfs = _windows_ntfs(disk, windows)
        add(
            "shrink-ntfs",
            partition_node(disk, ntfs),
            "--bytes",
            str(ntfs.size_bytes - layout.shrink_windows_bytes),
            "--dry-run",
            destructive=True,
            confirm=True,
            rollback="размер тома восстанавливается только из резервной копии",
        )

    placed = _placements(disk, windows, layout, boot_mode) if not clean_layout else {}
    if clean_layout:
        # Для затёртого диска вся ёмкость — одна область от начала резерва.
        single = [(disk.first_usable(TAIL_RESERVE), disk.size_bytes - TAIL_RESERVE - disk.first_usable(TAIL_RESERVE))]
        esp_cost = 0 if layout.reuse_esp or boot_mode is not BootMode.UEFI else layout.esp_bytes
        items = [
            ("root", layout.root_bytes),
            ("var", layout.var_bytes),
            ("home", layout.home_bytes),
            ("swap", layout.swap_bytes),
            ("shared", layout.shared_bytes if layout.create_shared else 0),
        ]
        result = _place(single, _items(items, esp_cost))
        if result is None:
            raise PlanError("раскладка не помещается на чистый диск")
        placed = result[0]

    if boot_mode is BootMode.UEFI and not layout.reuse_esp:
        add(
            "create-partition",
            disk.device,
            "--start",
            str(placed["esp"]),
            "--size",
            str(layout.esp_bytes),
            "--type",
            "esp",
            "--fs",
            "fat32",
            "--label",
            "BYTEGURU_EFI",
        )

    labels = {name: (size, fs, label) for name, size, fs, label in _new_partitions(profile, layout)}
    for name, _size, fs, label in _new_partitions(profile, layout):
        add(
            "create-partition",
            disk.device,
            "--start",
            str(placed[name]),
            "--size",
            str(labels[name][0]),
            "--type",
            "linux",
            "--fs",
            fs,
            "--label",
            label,
        )

    # Порядок здесь — не косметика: grub-install и efibootmgr работают только
    # тогда, когда целевая система уже смонтирована и скопирована. Installer,
    # который ставит загрузчик до переноса rootfs, оставляет машину без
    # загрузки, и «переустановить» — единственное лечение.
    add("mount-target", disk.device, "--root", "/mnt/target")
    add("install-system-image", "/mnt/target", "--profile", profile.value)
    add(
        "configure-boot",
        "/mnt/target",
        "--time-policy",
        "localtime-if-windows" if windows.present else "utc",
    )
    add("install-efi", disk.device, "--mode", boot_mode.value,
        "--reuse-esp", "yes" if layout.reuse_esp else "no")
    add("write-loader-entry", disk.device, "--name", "ByteguruOS", "--set-default", "yes")
    add("install-boot-repair", "/mnt/target", "--package", "byteguru-boot")
    add("finalize", "/mnt/target", "--sync", "--umount")
    return actions


def plan(
    disk: Disk,
    *,
    windows: WindowsState | None = None,
    profile: Profile = Profile.WORKSTATION,
    boot_mode: BootMode = BootMode.UEFI,
    mem_bytes: int = 8 * GiB,
    on_removable: bool = False,
    battery: bool = False,
    ignore_gates: bool = False,
) -> InstallPlan:
    """Собрать план установки. Блокирующие замечания не дают собрать план вовсе."""
    windows = windows or WindowsState()
    sizing = SIZINGS[profile]
    layout = _allocate(disk, windows, sizing, boot_mode)
    gates = evaluate(
        disk,
        windows,
        boot_mode,
        profile,
        mem_bytes=mem_bytes,
        required_bytes=layout.total_new_bytes(),
        on_removable=on_removable,
        battery=battery,
    )
    if has_blocker(gates) and not ignore_gates:
        raise PlanError("установка невозможна: " + "; ".join(gate.message for gate in blockers(gates)))
    notes = tuple(
        f"{gate.severity.value}: {gate.message}"
        for gate in gates
        if gate.severity.value != "block"
    )
    return InstallPlan(
        disk=disk,
        layout=layout,
        boot_mode=boot_mode,
        profile=profile,
        windows=windows,
        actions=tuple(_ordered_actions(disk, windows, layout, boot_mode, profile)),
        notes=notes,
    )


def plan_erase_and_install(disk: Disk, *, profile: Profile, boot_mode: BootMode = BootMode.UEFI) -> InstallPlan:
    """Полная затирка диска. Отдельная функция, чтобы её нельзя было вызвать по недосмотру."""
    if not disk.partitions:
        raise PlanError("нужен диск с существующими разделами: для чистого диска используйте plan()")
    clean = replace(disk, partitions=())
    sizing = SIZINGS[profile]
    esp_cost = _esp_bytes(clean) if boot_mode is BootMode.UEFI else 0
    usable = clean.size_bytes - esp_cost - TAIL_RESERVE - clean.first_usable(TAIL_RESERVE)
    fixed = sizing.root + sizing.var + sizing.swap + (sizing.shared if sizing.create_shared else 0)
    if usable < fixed + max(sizing.home_min, HOME_FLOOR if sizing.home_min else 0):
        raise PlanError(f"диск {gib(disk.size_bytes)} ГиБ слишком мал для профиля {profile.value}")
    home_bytes = usable - fixed if sizing.home_min else 0
    layout = Layout(
        root_bytes=sizing.root,
        var_bytes=sizing.var,
        home_bytes=home_bytes,
        swap_bytes=sizing.swap,
        esp_bytes=esp_cost,
        shared_bytes=sizing.shared,
        reuse_esp=False,
        shrink_windows_bytes=0,
        create_shared=sizing.create_shared,
    )
    tail = _ordered_actions(clean, WindowsState(), layout, boot_mode, profile, clean_layout=True)
    actions = [
        Action(
            order=1,
            kind="wipe-table",
            target=disk.device,
            destructive=True,
            requires_confirmation=True,
            rollback_hint="восстановление только из резервной копии таблицы разделов",
        )
    ] + [replace(action, order=index) for index, action in enumerate(tail[1:], start=2)]
    return InstallPlan(
        disk=clean,
        layout=layout,
        boot_mode=boot_mode,
        profile=profile,
        windows=WindowsState(),
        actions=tuple(actions),
        notes=("диск затирается целиком: все данные на нём будут потеряны",),
    )
