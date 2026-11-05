"""Настоящее исполнение действий плана.

До этого модуля apply.py собирал команды вида `bg-mount-target` — бинарников с
такими именами в природе нет, поэтому «установка» всегда разваливалась на
первом же привилегированном шаге. Здесь каждое действие плана превращается в
обычные утилиты, которые и должны стоять в live-образе: parted, mkfs.*, rsync,
grub-install, efibootmgr.

Три правила, которые держат весь модуль:

1. Никакого shell. Команда — вектор аргументов; данные (пароль, содержимое
   файла) уходят через stdin или запись файла, а не через подстановку в
   строку. Путь с пробелом или кириллицей не должен становиться способом
   выполнить что-то ещё.
2. Разделы ищем по метке, а не по номеру. `parted` добавляет разделы сам, и
   нумерация зависит от того, что уже лежало на диске; `/dev/disk/by-label`
   даёт то же имя и при создании, и при следующем монтировании.
3. Порядок действий задаёт планировщик, а не этот файл. Здесь только
   «как», а не «что за чем».
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field, replace

from bginstaller.errors import ApplyError
from bginstaller.models import BootMode, Disk, InstallPlan, Partition
from bginstaller.planner import partition_node

TARGET = "/mnt/target"
ESP_MOUNT = "/boot/efi"

# Что не переносим из live-системы в установленную: каталоги-монtовки,
# виртуальные ФС и кэш пакетов. Кривой перенос /proc в target — это
# «система, которая не грузится», и чинить её придётся с другой флешки.
IMAGE_EXCLUDES = (
    "/mnt", "/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp",
    "/var/cache/apt/archives", "/var/log/journal",
)

_BTRFS_MOUNT = "defaults,compress=zstd,ssd,discard=async"

# Тип раздела для parted: esp — отдельное имя, всё остальное — Linux FS data.
_PARTED_TYPE = {"esp": "esp", "linux": "linux"}


@dataclass(frozen=True)
class Context:
    """Параметры установки, которые выбирает человек в окне установщика.

    Значения по умолчанию — не «чтобы код компилировался»: CLI-путь
    `bginstaller apply` обязан оставаться рабочим без GUI, поэтому у каждого
    поля есть разумный дефолт, кроме пароля — его нельзя придумать за
    пользователя.
    """

    hostname: str = "byteguru"
    username: str = ""
    real_name: str = ""
    password: str = ""
    autologin: bool = True
    language: str = "ru_RU.UTF-8"
    keyboard: str = "ru"
    timezone: str = "Europe/Moscow"
    bootloader_name: str = "ByteguruOS"
    source_root: str = ""          # каталог-донор; пусто = текущая live-система
    image: str = ""                # squashfs с rootfs, если ставим из образа
    target: str = TARGET


@dataclass
class Job:
    """Один шаг действия: команда, запись файла или передача данных в stdin."""

    argv: tuple[str, ...] = ()
    stdin: str = ""
    write: tuple[str, str] = ()     # (путь, содержимое)
    note: str = ""

    @property
    def command_line(self) -> str:
        if self.write:
            return f"запись файла {self.write[0]}"
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass
class State:
    """Что уже сделано в этом проходе: нумерация разделов и метки."""

    disk: Disk
    next_partition: int = 0
    created: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.next_partition = len(self.disk.partitions) + 1

    def node_for_label(self, label: str) -> str:
        return f"/dev/disk/by-label/{label}"


def _value(arguments: tuple[str, ...], flag: str, default: str = "") -> str:
    """Значение после флага в векторе аргументов действия."""
    try:
        return arguments[arguments.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _chroot(root: str, *argv: str) -> tuple[str, ...]:
    return ("chroot", root, *argv)


def _node(device: str, number: int) -> str:
    """Узел раздела по номеру: у nvme и mmcblk перед цифрой нужен 'p'."""
    if device and device[-1].isdigit():
        return f"{device}p{number}"
    return f"{device}{number}"


def _mkdir(*paths: str) -> Job:
    return Job(argv=("mkdir", "-p", *paths))


def _windows_partition(plan: InstallPlan) -> Partition | None:
    for part in plan.disk.partitions:
        if part.holds_windows:
            return part
    return None


def _esp_node(plan: InstallPlan, state: State) -> str:
    """Узел ESP: существующий раздел или только что созданный."""
    esp = plan.disk.esp
    if esp is not None:
        return partition_node(plan.disk, esp)
    return state.node_for_label("BYTEGURU_EFI")


# --------------------------------------------------------------- действия

def _probe_disk(action, plan, ctx, state) -> list[Job]:
    # Разведка уже сделана: plan() не собрать без разобранного lsblk. Повторный
    # запуск здесь только замедлил бы установку.
    return [Job(note="разметка диска уже известна планировщику")]


def _wipe_table(action, plan, ctx, state) -> list[Job]:
    return [
        Job(argv=("parted", "--script", action.target, "mklabel", "gpt"),
            note="старая таблица разделов уничтожена"),
        Job(argv=("partprobe", action.target)),
        Job(argv=("udevadm", "settle", "--timeout=60")),
    ]


def _inspect_ntfs(action, plan, ctx, state) -> list[Job]:
    windows = _windows_partition(plan)
    if windows is None:
        return [Job(note="раздела NTFS не найдено — проверка не нужна")]
    node = partition_node(plan.disk, windows)
    # ntfsfix -n ничего не меняет, но отвечает на единственный важный вопрос:
    # том размонтирован «чисто» или Windows оставила его в hibryd-состоянии.
    return [Job(argv=("ntfsfix", "--dry-run", "--force", node),
                 note="читаем состояние тома, не изменяя его")]


def _shrink_ntfs(action, plan, ctx, state) -> list[Job]:
    node = action.target
    size = _value(action.arguments, "--bytes")
    if not size:
        raise ApplyError("shrink-ntfs без --bytes")
    return [
        Job(argv=("ntfsresize", "--info", "--force", node)),
        Job(argv=("ntfsresize", "--force", "--size", f"{size}B", node),
            note="том сжат; расширение обратно возможно только из копии"),
        Job(argv=("partprobe", plan.disk.device)),
        Job(argv=("udevadm", "settle", "--timeout=60")),
    ]


def _create_partition(action, plan, ctx, state) -> list[Job]:
    start = int(_value(action.arguments, "--start", "0"))
    size = int(_value(action.arguments, "--size", "0"))
    fs = _value(action.arguments, "--fs", "none")
    label = _value(action.arguments, "--label", "")
    kind = _value(action.arguments, "--type", "linux")
    if size <= 0:
        raise ApplyError("create-partition с нулевым размером")

    number = state.next_partition
    state.next_partition += 1
    node = _node(action.target, number)
    end = start + size - 1

    jobs = [Job(argv=("parted", "--script", action.target, "unit", "B",
        "mkpart", label or "byteguru", str(start), str(end)))]
    if kind == "esp":
        jobs.append(Job(argv=("parted", "--script", action.target, "set",
            str(number), "esp", "on")))
    jobs.append(Job(argv=("partprobe", action.target)))
    jobs.append(Job(argv=("udevadm", "settle", "--timeout=60")))

    if fs == "btrfs":
        jobs.append(Job(argv=("mkfs.btrfs", "--force", "--label", label, node)))
    elif fs == "fat32":
        jobs.append(Job(argv=("mkfs.fat", "-F", "32", "-n", label, node)))
    elif fs == "exfat":
        jobs.append(Job(argv=("mkfs.exfat", "-n", label, node)))
    elif fs == "swap":
        jobs.append(Job(argv=("mkswap", "--label", label, node)))
    elif fs not in ("none", ""):
        raise ApplyError(f"неизвестная файловая система {fs!r}")
    if label:
        state.created.append(label)
    return jobs


def _mount_target(action, plan, ctx, state) -> list[Job]:
    root = ctx.target
    labels = {part.label.upper() for part in plan.disk.partitions} | set(state.created)
    jobs = [_mkdir(root),
        Job(argv=("mount", "-o", _BTRFS_MOUNT, state.node_for_label("BG_ROOT"), root))]
    if "BG_VAR" in labels:
        jobs.append(_mkdir(f"{root}/var"))
        jobs.append(Job(argv=("mount", "-o", _BTRFS_MOUNT,
            state.node_for_label("BG_VAR"), f"{root}/var")))
    if "BG_HOME" in labels:
        jobs.append(_mkdir(f"{root}/home"))
        jobs.append(Job(argv=("mount", "-o", _BTRFS_MOUNT,
            state.node_for_label("BG_HOME"), f"{root}/home")))
    if "BG_SWAP" in labels:
        jobs.append(Job(argv=("swapon", state.node_for_label("BG_SWAP"))))

    if plan.boot_mode is BootMode.UEFI:
        esp = _esp_node(plan, state)
        jobs.append(_mkdir(f"{root}{ESP_MOUNT}"))
        jobs.append(Job(argv=("mount", esp, f"{root}{ESP_MOUNT}")))
    return jobs


def _install_system_image(action, plan, ctx, state) -> list[Job]:
    root = ctx.target
    if ctx.image:
        return [Job(argv=("unsquashfs", "-force", "-dest", root, ctx.image),
            note=f"rootfs распакован из {ctx.image}")]

    source = (ctx.source_root or "/").rstrip("/") or "/"
    argv = ["rsync", "--archive", "--xattrs", "--acls", "--one-file-system",
        "--info=progress2"]
    for exclude in IMAGE_EXCLUDES:
        argv.append(f"--exclude={exclude}")
    argv.extend([f"{source}/", f"{root}/"])
    return [Job(argv=tuple(argv),
        note=f"перенос live-системы из {source} в {root}")]


def _configure_boot(action, plan, ctx, state) -> list[Job]:
    """Содержимое установленной системы: fstab, имя хоста, язык, пользователь.

    Файлы пишем сами, а не `echo … > file`: без shell нельзя ни передать
    содержимое, ни гарантировать, что значение пользователя не станет частью
    команды.
    """
    root = ctx.target
    policy = _value(action.arguments, "--time-policy", "utc")
    jobs = [
        Job(write=(f"{root}/etc/fstab", _fstab(plan, ctx, policy, state))),
        Job(write=(f"{root}/etc/hostname", f"{ctx.hostname}\n")),
        Job(write=(f"{root}/etc/locale.conf", f"LANG={ctx.language}\n")),
        Job(write=(f"{root}/etc/vconsole.conf", f"KEYMAP={ctx.keyboard}\n")),
        Job(write=(f"{root}/etc/timezone", f"{ctx.timezone}\n")),
        Job(argv=("ln", "-sf", f"/usr/share/zoneinfo/{ctx.timezone}",
            f"{root}/etc/localtime")),
        # /etc/adjtime: ядро читает политику часов отсюда, и «localtime» при
        # соседстве с Windows — единственная причина, по которой две ОС не
        # дерутся за одни и те же часы.
        Job(write=(f"{root}/etc/adjtime", _adjtime(policy))),
        _mount_api(root, "dev"),
        _mount_api(root, "proc"),
        _mount_api(root, "sys"),
        _mount_api(root, "run"),
    ]
    if ctx.username:
        jobs.extend(_create_user(root, ctx))
    jobs.append(Job(argv=_chroot(root, "systemd-firstboot", "--root", "/",
        "--locale", ctx.language, "--keymap", ctx.keyboard,
        "--timezone", ctx.timezone, "--hostname", ctx.hostname),
        note="на случай, если unit-файлы перезаписали что-то из этого"))
    jobs.append(Job(argv=_chroot(root, "ldconfig")))
    jobs.append(Job(argv=_chroot(root, "update-initramfs", "-u", "-k", "all"),
        note="initramfs собран под новую разметку"))
    return jobs


def _mount_api(root: str, api: str) -> Job:
    return Job(argv=("mount", "--bind", f"/{api}", f"{root}/{api}"),
        note=f"привязка /{api} для chroot")


def _create_user(root: str, ctx: Context) -> list[Job]:
    groups = "sudo,input,audio,video,fuse,plugdev"
    jobs = [Job(argv=_chroot(root, "useradd", "--create-home", "--shell",
        "/bin/bash", "--groups", groups, "--comment", ctx.real_name or ctx.username,
        ctx.username))]
    # Пароль уходит в stdin утилиты chpasswd, а не в argv: в списке аргументов
    # он светился бы в /proc и в логе установки.
    jobs.append(Job(argv=_chroot(root, "chpasswd"),
        stdin=f"{ctx.username}:{ctx.password}\n"))
    if ctx.autologin:
        jobs.append(Job(write=(f"{root}/etc/systemd/system/getty@tty1.service.d"
            "/byteguru-autologin.conf",
            "[Service]\n"
            f"ExecStart=\nExecStart=-/sbin/agetty --autologin {ctx.username} "
            "--noclear %I $TERM\n")))
    return jobs


def _install_efi(action, plan, ctx, state) -> list[Job]:
    mode = _value(action.arguments, "--mode", plan.boot_mode.value)
    root = ctx.target
    if mode == BootMode.LEGACY.value:
        return [
            Job(argv=_chroot(root, "grub-install", "--target=i386-pc",
                "--boot-directory=/boot", action.target),
                note="загрузчик пишется в MBR диска изнутри установленной системы"),
            Job(argv=_chroot(root, "grub-mkconfig", "-o", "/boot/grub/grub.cfg")),
        ]
    return [
        Job(argv=_chroot(root, "grub-install", "--target=x86_64-efi",
            f"--efi-directory={ESP_MOUNT}", "--bootloader-id", ctx.bootloader_name,
            "--no-nvram"),
            note="файл загрузчика на ESP; запись в NVRAM делает следующий шаг"),
        Job(argv=_chroot(root, "grub-mkconfig", "-o", "/boot/grub/grub.cfg")),
    ]


def _write_loader_entry(action, plan, ctx, state) -> list[Job]:
    if plan.boot_mode is not BootMode.UEFI:
        return [Job(note="BIOS-загрузка: запись в NVRAM не нужна")]
    esp = _esp_node(plan, state)
    number = _number_from_node(action.target, esp)
    loader = f"\\EFI\\{ctx.bootloader_name}\\grubx64.efi"
    jobs = [Job(argv=("efibootmgr", "--create", "--disk", action.target,
        "--part", str(number), "--label", ctx.bootloader_name,
        "--loader", loader),
        note="запись в NVRAM; если прошивка её не принимает, грузимся по --removable")]
    if _value(action.arguments, "--set-default", "no") == "yes":
        jobs.append(Job(argv=("efibootmgr", "--verbose"),
            note="проверка: ByteguruOS должен быть в BootOrder"))
    return jobs


def _install_boot_repair(action, plan, ctx, state) -> list[Job]:
    """Служба «почини загрузку после обновления Windows».

    Windows умеет перезаписывать BootOrder, и пользователь остаётся один на
    один с BIOS. Unit в установленной системе сравнивает BootOrder при
    каждом запуске и возвращает ByteguruOS на первое место.
    """
    root = ctx.target
    unit = ("[Unit]\n"
        "Description=Проверка порядка загрузки UEFI (ByteguruOS)\n"
        "After=multi-user.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/bginstaller boot-repair --apply\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n")
    return [
        Job(write=(f"{root}/usr/lib/systemd/system/byteguru-boot-repair.service",
            unit)),
        Job(argv=_chroot(root, "systemctl", "enable",
            "byteguru-boot-repair.service")),
    ]


def _finalize(action, plan, ctx, state) -> list[Job]:
    root = ctx.target
    return [
        Job(argv=("sync",)),
        # umount -R снимает и bind-монтирования API, иначе chroot остаётся
        # «висящим» и target не размонтируется без перезагрузки.
        Job(argv=("umount", "-R", root)),
        Job(argv=("sync",)),
    ]


_EXECUTORS = {
    "probe-disk": _probe_disk,
    "inspect-ntfs": _inspect_ntfs,
    "shrink-ntfs": _shrink_ntfs,
    "create-partition": _create_partition,
    "mount-target": _mount_target,
    "install-system-image": _install_system_image,
    "configure-boot": _configure_boot,
    "install-efi": _install_efi,
    "write-loader-entry": _write_loader_entry,
    "install-boot-repair": _install_boot_repair,
    "finalize": _finalize,
    "wipe-table": _wipe_table,
}


def commands_for(action, plan: InstallPlan, ctx: Context,
        state: State) -> list[Job]:
    """Список шагов для одного действия плана."""
    executor = _EXECUTORS.get(action.kind)
    if executor is None:
        raise ApplyError(f"для действия {action.kind!r} нет исполнителя")
    return executor(action, plan, ctx, state)


# --------------------------------------------------------------- содержимое файлов

def _fstab(plan: InstallPlan, ctx: Context, policy: str, state: State) -> str:
    """fstab установленной системы.

    Метки берём из того, что реально создали в этом проходе (state.created), а
    не только из таблицы разделов плана: при установке на чистый диск план
    знает ноль разделов, и fstab без этого дополнения выходил бы без /var и
    /home — система грузилась бы, но без домашних каталогов.
    """
    labels = {part.label.upper() for part in plan.disk.partitions if part.label}
    labels |= {label.upper() for label in state.created}
    lines = [
        "# порождено установщиком ByteguruOS; метки переживают перестановку диска",
        "LABEL=BG_ROOT\t/\tbtrfs\t" + _BTRFS_MOUNT + "\t0 0",
    ]
    if "BG_VAR" in labels:
        lines.append("LABEL=BG_VAR\t/var\tbtrfs\t" + _BTRFS_MOUNT + "\t0 0")
    if "BG_HOME" in labels:
        lines.append("LABEL=BG_HOME\t/home\tbtrfs\t" + _BTRFS_MOUNT + "\t0 0")
    if "BG_SWAP" in labels:
        lines.append("LABEL=BG_SWAP\tnone\tswap\tsw\t0 0")
    if plan.boot_mode is BootMode.UEFI:
        lines.append(f"LABEL={_esp_label(plan)}\t{ESP_MOUNT}\tvfat\t"
            "umask=007,shortname=winnt\t0 2")
    lines.append("")
    return "\n".join(lines)


def _esp_label(plan: InstallPlan) -> str:
    esp = plan.disk.esp
    if esp is not None and esp.label:
        return esp.label.upper()
    return "BYTEGURU_EFI"


def _adjtime(policy: str) -> str:
    mode = "LOCAL" if policy == "localtime-if-windows" else "UTC"
    return ("0.0 0 0.0\n0.0 0 0.0\n0.0 0 0.0\n"
            f"{mode}\n")


def _number_from_node(disk_device: str, node: str) -> int:
    """Номер раздела по его узлу — нужен efibootmgr для --part."""
    tail = node[len(disk_device):] if node.startswith(disk_device) else node
    digits = "".join(char for char in tail if char.isdigit())
    return int(digits) if digits else 1
