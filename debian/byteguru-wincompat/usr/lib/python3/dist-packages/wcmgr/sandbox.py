"""Политики изоляции Windows-приложения: AppArmor, seccomp, свойства юнита.

Причина, по которой этот файл существует: WinCompat исполняет чужой бинарный код,
которому мы не можем доверять. Манифест описывает, что разрешено, а генератор
превращает это в форму, понятную ядру, и не даёт разрешить лишнее «по привычке».
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from wcmgr.errors import SandboxError
from wcmgr.manifest import (
    AppArmorMode,
    DriveKind,
    Manifest,
    Mode,
    NetworkMode,
    SeccompMode,
)

PROFILE_PREFIX = "wcv-"
# Системные пути, закрытые даже при ошибке в манифесте: приложение не должно
# иметь возможности их прочитать ни при каких значениях политик.
HARD_DENY_READ = (
    "/etc/shadow",
    "/etc/gshadow",
    "/root/**",
    "/var/lib/byteguru/secrets/**",
    "/home/**/.ssh/**",
    "/home/**/.gnupg/**",
)
HARD_DENY_WRITE = (
    "/boot/**",
    "/etc/**",
    "/usr/**",
    "/bin/**",
    "/sbin/**",
    "/lib/**",
    "/lib64/**",
    "/var/lib/byteguru/system/**",
    "/sys/**",
    "/proc/sys/**",
    "/dev/**",
)
# Вызовы, запрещённые всегда: они позволяют выйти из песочницы или переписать
# загрузку, а Windows-приложению они не нужны.
HARD_DENY_SYSCALLS = (
    "reboot",
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "swapon",
    "swapoff",
    "personality",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "keyctl",
    "add_key",
    "request_key",
    "setns",
    "unshare",
    "ptrace",
    "acct",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
)
# Минимальный набор, без которого Wine не стартует. Списки правил по всем
# 300+ вызовам здесь нет сознательно: он требует проверки на реальном Wine.
ALLOWED_BASE_SYSCALLS = (
    "read",
    "write",
    "open",
    "openat",
    "close",
    "stat",
    "fstat",
    "lstat",
    "poll",
    "lseek",
    "mmap",
    "munmap",
    "brk",
    "rt_sigaction",
    "rt_sigprocmask",
    "access",
    "pipe",
    "dup",
    "dup2",
    "clone",
    "fork",
    "vfork",
    "execve",
    "exit",
    "wait4",
    "fcntl",
    "flock",
    "fsync",
    "getdents64",
    "ioctl",
    "readlink",
    "readlinkat",
    "getuid",
    "getgid",
    "geteuid",
    "getegid",
    "getpid",
    "getppid",
    "socket",
    "connect",
    "bind",
    "listen",
    "accept",
    "sendto",
    "recvfrom",
    "setsockopt",
    "getsockopt",
    "nanosleep",
    "clock_gettime",
    "futex",
)


@dataclass(frozen=True)
class SandboxPlan:
    """Всё, что нужно исполнителю для подъёма изолированного окружения."""

    apparmor_profile_name: str
    apparmor_profile_text: str | None
    seccomp_profile_text: str | None
    systemd_properties: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apparmor_profile_name": self.apparmor_profile_name,
            "apparmor_profile_text": self.apparmor_profile_text,
            "seccomp_profile_text": self.seccomp_profile_text,
            "systemd_properties": [list(item) for item in self.systemd_properties],
            "warnings": list(self.warnings),
        }


def profile_name(manifest: Manifest) -> str:
    return f"{PROFILE_PREFIX}{manifest.id}"


def _writable_roots(manifest: Manifest, prefix_path: str) -> list[str]:
    roots = [f"{prefix_path.rstrip('/')}/**"]
    for mapping in manifest.drives:
        if mapping.kind is DriveKind.BIND and mapping.mode is Mode.RW and mapping.path:
            roots.append(f"{mapping.path.rstrip('/')}/**")
    return roots


def _readable_roots(manifest: Manifest, prefix_path: str) -> list[str]:
    roots = [f"{prefix_path.rstrip('/')}/**", "/usr/**", "/lib/**", "/lib64/**", "/etc/ld.so.cache"]
    for entry in manifest.visible:
        roots.append(f"{entry.rstrip('/')}/**")
    for mapping in manifest.drives:
        if mapping.kind is DriveKind.BIND and mapping.path:
            roots.append(f"{mapping.path.rstrip('/')}/**")
    return roots


def apparmor_profile(manifest: Manifest, prefix_path: str) -> str:
    """Текст профиля. Это рабочий шаблон, а не аттестованный набор политик:
    реальные профили дорабатываются под конкретное приложение при проверке."""
    lines = [
        f"profile {profile_name(manifest)} flags=(attach_disconnected) {{",
        "  #include <abstractions/base>",
        "  #include <abstractions/nameservice>",
        "",
        "  capability net_bind_service",
        "  network,",
        "  unix,",
        "",
    ]
    for root in _readable_roots(manifest, prefix_path):
        lines.append(f"  read file -> {root},")
    for root in _writable_roots(manifest, prefix_path):
        lines.append(f"  rw file -> {root},")
    lines.append("")
    for path in HARD_DENY_READ:
        lines.append(f"  deny file read -> {path},")
    for path in HARD_DENY_WRITE:
        lines.append(f"  deny file write -> {path},")
    if not manifest.camera:
        lines.append("  deny file -> /dev/video* *,")
    if manifest.printers.value == "none":
        lines.append("  deny file -> /dev/lp*,")
    if not manifest.usb:
        lines.append("  deny file -> /dev/bus/usb/**,")
    if manifest.network_mode is NetworkMode.BLOCKED:
        lines.append("  deny network,")
    lines.append("}")
    return "\n".join(lines) + "\n"


def seccomp_profile(manifest: Manifest) -> str:
    allowed = list(ALLOWED_BASE_SYSCALLS)
    if manifest.network_mode is NetworkMode.BLOCKED:
        for syscall in ("socket", "connect", "bind", "listen", "accept", "sendto", "recvfrom"):
            if syscall in allowed:
                allowed.remove(syscall)
    payload = {
        "defaultAction": "SCMP_ACT_ALLOW",
        "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
        "syscalls": [
            {
                "names": sorted(set(allowed)),
                "action": "SCMP_ACT_NOTIFY" if manifest.seccomp is SeccompMode.STRICT else "SCMP_ACT_ALLOW",
            },
            {"names": sorted(HARD_DENY_SYSCALLS), "action": "SCMP_ACT_ERRNO"},
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def systemd_properties(manifest: Manifest, *, app_id: str | None = None) -> tuple[tuple[str, str], ...]:
    identifier = app_id or manifest.id
    props = [
        ("Description", f"ByteguruOS WinCompat: {manifest.name}"),
        ("Slice", "wincompat.slice"),
        ("NoNewPrivileges", "yes" if manifest.no_new_privs else "no"),
        ("PrivateDevices", "yes" if not manifest.usb else "no"),
        ("ProtectSystem", "strict"),
        ("ProtectKernelModules", "yes"),
        ("ProtectKernelTunables", "yes"),
        ("ProtectControlGroups", "yes"),
        ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6" if manifest.network_mode is not NetworkMode.BLOCKED else "AF_UNIX"),
        ("RestrictNamespaces", "yes"),
        ("MemoryDenyWriteExecute", "no"),
        ("RestrictSUIDSGID", "yes"),
        ("User", "%U"),
        ("AppArmorProfile", f"{profile_name(manifest)}" if manifest.apparmor is not AppArmorMode.OFF else "-"),
    ]
    if manifest.apparmor is AppArmorMode.COMPLAIN:
        props[-1] = ("AppArmorProfile", f"{profile_name(manifest)}//complain")
    return tuple(props)


def build_plan(manifest: Manifest, prefix_path: str) -> SandboxPlan:
    if manifest.apparmor is AppArmorMode.OFF and manifest.seccomp is SeccompMode.OFF:
        raise SandboxError(
            f"манифест {manifest.id} отключает обе изоляции: запуск разрешён только "
            "в локальной отладке с явным флагом --no-sandbox"
        )
    warnings: list[str] = []
    if manifest.apparmor is AppArmorMode.COMPLAIN:
        warnings.append("AppArmor в режиме complain: нарушения журналируются, но не блокируются")
    if manifest.seccomp is SeccompMode.OFF:
        warnings.append("seccomp отключён")
    if manifest.network_mode is NetworkMode.ALLOWLIST:
        warnings.append(
            "allowlist доменов исполняется на уровне прокси wc-net, а не AppArmor: "
            "без запущенного прокси приложение получит неограниченный доступ в сеть"
        )
    if manifest.usb:
        warnings.append("разрешён прямой доступ к USB: устройство видно процессу без посредника")
    return SandboxPlan(
        apparmor_profile_name=profile_name(manifest),
        apparmor_profile_text=apparmor_profile(manifest, prefix_path)
        if manifest.apparmor is not AppArmorMode.OFF
        else None,
        seccomp_profile_text=seccomp_profile(manifest)
        if manifest.seccomp is not SeccompMode.OFF
        else None,
        systemd_properties=systemd_properties(manifest),
        warnings=tuple(warnings),
    )
