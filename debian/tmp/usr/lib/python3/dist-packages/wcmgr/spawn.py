"""Сборка командного вектора запуска WinCompat.

Здесь нет исполнения: функции чистые, чтобы политику запуска можно было
проверить тестами на любой машине. Реальный `exec` делает только CLI, с явным
флагом и только на Linux с установленным Wine.

Осознанное ограничение: никаких «волшебных» флагов Wine, которых не существует.
Контрольная сумма установщика проверяется вызывающим кодом ДО запуска (см.
`wcmgr.cli`), а не передаётся в wine как параметр.
"""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath

from wcmgr.errors import LaunchError
from wcmgr.manifest import CoverageLevel, DriveKind, GraphPath, Manifest, Mode, Runner
from wcmgr.sandbox import SandboxPlan

WINE_BINARY_DEFAULT = "wine"
# Wine попросит скачать Gecko/Mono при первом старте. В закрытом контуре это
# нежелательно, поэтому отключаем движки, если приложению они не нужны.
DLL_OVERRIDES_DEFAULT = "mscoree=disabled,mshtml=disabled"


def windows_exe_path(manifest: Manifest, prefix_path: str | Path) -> str:
    """Исполняемый файл в нотации Windows внутри префикса (C: = drive_c)."""
    executable = (manifest.executable or "").strip()
    if not executable:
        raise LaunchError(f"манифест {manifest.id} не указал исполняемый файл")
    if executable.startswith(("/", "\\")) or executable[:2].endswith(":"):
        raise LaunchError(
            f"wcv.executable обязан быть относительным путём в префиксе, получено {executable!r}"
        )
    tail = executable.replace("/", "\\")
    return f"C:\\{tail}"


def build_environment(
    manifest: Manifest,
    prefix_path: str | Path,
    *,
    wayland_display: str = "wayland-0",
    user: str = "",
) -> dict[str, str]:
    prefix = posixpath.normpath(str(prefix_path))
    env = {
        "WINEPREFIX": prefix,
        "WINEDLLOVERRIDES": DLL_OVERRIDES_DEFAULT,
        "WAYLAND_DISPLAY": wayland_display,
        "BG_WCV_ID": manifest.id,
        "BG_WCV_VERSION": manifest.version,
    }
    if user:
        env["BG_WCV_USER"] = user
    if manifest.graph_path is GraphPath.DXVK:
        # DXVK подхватывается самой сборкой Wine: переменные нужны только для
        # отладочных сценариев, поэтому здесь их нет.
        pass
    if manifest.graph_path is GraphPath.X11:
        env["GDK_BACKEND"] = "x11"
    return {key: value for key, value in env.items() if value}


def build_wine_command(
    manifest: Manifest,
    prefix_path: str | Path,
    *,
    wine_binary: str = WINE_BINARY_DEFAULT,
    extra_arguments: tuple[str, ...] = (),
    sandbox_plan: SandboxPlan | None = None,
) -> list[str]:
    if manifest.runner is not Runner.WINE:
        raise LaunchError(f"для runner={manifest.runner.value} команда Wine не строится")
    target = windows_exe_path(manifest, prefix_path)
    if manifest.executable.lower().endswith(".msi"):
        # Для пакета Wine вызывает msiexec отдельной программой:
        # `wine msiexec /i C:\пакет.msi`.
        argv = [wine_binary, "msiexec", "/i", target]
    else:
        argv = [wine_binary, target]
    argv.extend(extra_arguments)
    return _wrap(argv, sandbox_plan)


def build_installer_command(
    manifest: Manifest,
    installer_path: str | Path,
    *,
    wine_binary: str = WINE_BINARY_DEFAULT,
    sandbox_plan: SandboxPlan | None = None,
) -> list[str]:
    """Запуск установщика с внешнего носителя: путь передаём как есть."""
    source = PurePosixPath(str(installer_path))
    if not source.is_absolute():
        raise LaunchError("путь к установщику должен быть абсолютным POSIX-путём")
    if manifest.executable.lower().endswith(".msi"):
        argv = [wine_binary, "msiexec", "/i", str(source)]
    else:
        argv = [wine_binary, str(source)]
    return _wrap(argv, sandbox_plan)


def build_scope_prefix(
    manifest: Manifest,
    *,
    scope_name: str | None = None,
    properties: tuple[tuple[str, str], ...] | None = None,
) -> list[str]:
    """systemd-run обёртка: окружение попадает в wincompat.slice и отдельный scope."""
    name = scope_name or f"wcv-{manifest.id}"
    argv = [
        "systemd-run",
        "--user",
        "--scope",
        "--unit",
        name,
        "-p",
        "MemorySwapMax=0",
    ]
    for key, value in properties or ():
        argv += ["-p", f"{key}={value}"]
    return argv


def build_qemu_command(
    manifest: Manifest,
    *,
    image_path: str | Path,
    memory_mb: int = 4096,
    cpus: int = 2,
    shared_directory: str | Path | None = None,
    spice_port: int | None = None,
) -> list[str]:
    """Команда обходного пути L3: Windows в KVM с оконным доступом по SPICE."""
    if manifest.expected_level is CoverageLevel.L1 and manifest.runner is Runner.WINE:
        raise LaunchError("виртуальная машина не строится для приложений уровня L1")
    image = PurePosixPath(str(image_path))
    if not image.is_absolute():
        raise LaunchError("образ Windows задаётся абсолютным POSIX-путём")
    argv = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-m",
        str(memory_mb),
        "-smp",
        str(cpus),
        "-drive",
        f"if=virtio,format=qcow2,file={image}",
        "-netdev",
        "user,id=n0",
        "-device",
        "virtio-net-pci,netdev=n0",
    ]
    if shared_directory:
        # 9p-расшаривание: Windows видит папку как сетевой диск.
        argv += [
            "-virtfs",
            f"local,path={shared_directory},mount_tag=byteguru,security_model=mapped-xattr",
        ]
    if spice_port:
        argv += ["-spice", f"port={spice_port},disable-ticketing=on"]
    return argv


def _wrap(argv: list[str], sandbox_plan: SandboxPlan | None) -> list[str]:
    if sandbox_plan is None or sandbox_plan.apparmor_profile_text is None:
        return argv
    return ["aa-exec", "-p", sandbox_plan.apparmor_profile_name, "--", *argv]


def describe_command(manifest: Manifest, argv: list[str], env: dict[str, str]) -> str:
    """Что произойдёт — для подтверждающего диалога в оболочке."""
    lines = [f"{manifest.name} {manifest.version}", f"уровень: {manifest.expected_level.value}"]
    if manifest.graph_path is not GraphPath.AUTO:
        lines.append(f"графика: {manifest.graph_path.value}")
    lines.append("команда: " + " ".join(argv))
    for key in sorted(env):
        lines.append(f"  {key}={env[key]}")
    return "\n".join(lines)


def ro_letters(manifest: Manifest) -> tuple[str, ...]:
    return tuple(
        mapping.letter
        for mapping in manifest.drives
        if mapping.kind is DriveKind.BIND and mapping.mode is Mode.RO
    )
