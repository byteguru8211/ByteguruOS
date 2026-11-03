"""CLI WinCompat: проверка манифестов, установка окружений, запуск, журнал прогонов.

Реальный запуск Wine возможен только в Linux-сеансе и только с явным `--run`:
по умолчанию команда печатает план. Так одну и ту же утилиту можно использовать
для проверки политик на машине разработчика.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from wcmgr.errors import WcvError
from wcmgr.fallback import Capabilities, decide
from wcmgr.manifest import dump_toml, load_manifest, validate_manifest_text
from wcmgr.prefix import PrefixStore
from wcmgr.registry import CompatibilityRegistry, RunRecord
from wcmgr.sandbox import build_plan
from wcmgr.spawn import build_environment, build_scope_prefix, build_wine_command, describe_command

DEFAULT_STORAGE = "/var/lib/byteguru/wcv"
DEFAULT_REGISTRY = "/var/lib/byteguru/compat-registry.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wcmgr", description="Подсистема запуска Windows-приложений")
    parser.add_argument("--storage", default=DEFAULT_STORAGE, help="корень хранилища окружений WCV")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="файл реестра совместимости")
    parser.add_argument("--user", default="", help="имя пользователя сеанса для подстановки %%USER%%")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="проверить манифесты по схеме")
    check.add_argument("paths", nargs="+")
    check.set_defaults(func=cmd_check)

    install = sub.add_parser("install", help="создать окружение по манифесту")
    install.add_argument("manifest")
    install.add_argument("--version", default="", help="метка версии окружения")
    install.set_defaults(func=cmd_install)

    launch = sub.add_parser("launch", help="показать или выполнить запуск")
    launch.add_argument("app_id")
    launch.add_argument("arguments", nargs="*", help="аргументы передаваемые приложению")
    launch.add_argument("--run", action="store_true", help="выполнить реально (только в Linux)")
    launch.add_argument("--no-sandbox", action="store_true", help="отладка: без политик изоляции")
    launch.set_defaults(func=cmd_launch)

    apps = sub.add_parser("list", help="список установленных окружений")
    apps.set_defaults(func=cmd_list)

    record = sub.add_parser("record", help="записать результат прогона в реестр")
    record.add_argument("app_id")
    record.add_argument("--ok", action="store_true")
    record.add_argument("--fail", action="store_true")
    record.add_argument("--level", default="")
    record.add_argument("--detail", default="")
    record.add_argument("--build", default="")
    record.set_defaults(func=cmd_record)

    rollback = sub.add_parser("rollback", help="откатить окружение на предыдущую версию")
    rollback.add_argument("app_id")
    rollback.add_argument("--to", default="", help="явная версия метки")
    rollback.set_defaults(func=cmd_rollback)

    export = sub.add_parser("export", help="выгрузить реестр совместимости в JSON")
    export.add_argument("--output", default="", help="файл (по умолчанию stdout)")
    export.set_defaults(func=cmd_export)

    doctor = sub.add_parser("doctor", help="что есть в системе для работы WinCompat")
    doctor.set_defaults(func=cmd_doctor)
    return parser


# ------------------------------------------------------------------- команды
def cmd_check(args: argparse.Namespace) -> int:
    failed = 0
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            files = sorted(path.rglob("*.wcv"))
        else:
            files = [path]
        for item in files:
            try:
                text = item.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"{item}: не читается ({exc})")
                failed += 1
                continue
            problems = validate_manifest_text(text)
            if problems:
                failed += 1
                print(f"{item}: ОТКАЗ")
                for problem in problems:
                    print(f"    - {problem}")
            else:
                manifest = load_manifest(str(item))
                mark = "подтверждён" if manifest.evidence_complete else "не проверен прогонами"
                print(f"{item}: ok ({manifest.id} {manifest.version}, {mark})")
    return 1 if failed else 0


def cmd_install(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except (WcvError, OSError) as exc:
        print(f"манифест не принят: {exc}", file=sys.stderr)
        return 2
    store = PrefixStore(args.storage)
    try:
        if store.exists(manifest.prefix_name):
            path = store.update_manifest(manifest, version=args.version or None)
            action = "обновлено"
        else:
            path = store.create(manifest, version=args.version or None)
            action = "создано"
    except WcvError as exc:
        print(f"окружение не собрано: {exc}", file=sys.stderr)
        return 1
    with CompatibilityRegistry(args.registry) as registry:
        registry.register(manifest)
    print(f"{action} окружение {manifest.id} в {path}")
    print(f"версия: {store.current_version(manifest.prefix_name)}")
    if not manifest.tested:
        print("пометка: манифест не подтверждён прогоном — в реестре он останется «не проверено»")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    store = PrefixStore(args.storage)
    try:
        info = store.current(args.app_id)
    except WcvError:
        info = None
    if info is None:
        print(
            f"окружение {args.app_id!r} не установлено или его идентификатор некорректен "
            "(wcmgr install <манифест>)",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = load_manifest(str(info.path / "manifest.wcv"))
    except WcvError as exc:
        print(f"манифест окружения повреждён: {exc}", file=sys.stderr)
        return 2

    with CompatibilityRegistry(args.registry) as registry:
        registry.register(manifest)
        proven = registry.proven_level(manifest.id)
    plan = build_plan(manifest, str(info.path)) if not args.no_sandbox else None
    capabilities = _capabilities()
    decision = decide(manifest, proven, capabilities)

    if decision.action != "launch-wine":
        print(describe_command(manifest, list(decision.commands), {}))
        print(f"\nдействие: {decision.action}")
        print(f"причина: {decision.reason}")
        if decision.hint:
            print(f"подсказка: {decision.hint}")
        return 0 if decision.is_executable else 1

    argv = build_wine_command(manifest, str(info.path), sandbox_plan=plan,
                              extra_arguments=tuple(args.arguments))
    scoped = build_scope_prefix(manifest, properties=plan.systemd_properties if plan else ()) + ["--"] + argv
    env = build_environment(manifest, str(info.path), user=args.user)
    print(describe_command(manifest, scoped, env))
    if plan:
        print("\nпредупреждения изоляции:")
        for warning in plan.warnings:
            print(f"  - {warning}")

    if not args.run:
        print("\nпроба: ничего не запущено. Добавьте --run для выполнения.")
        return 0
    if platform.system() != "Linux":
        print("--run доступен только в Linux-сеансе", file=sys.stderr)
        return 2
    try:
        return subprocess.run(scoped, env={**_environ(), **env}).returncode
    except OSError as exc:
        print(f"запуск не удался: {exc}", file=sys.stderr)
        return 127


def cmd_list(args: argparse.Namespace) -> int:
    store = PrefixStore(args.storage)
    root = Path(args.storage)
    if not root.is_dir():
        print(f"хранилище пусто: {root}")
        return 0
    rows = []
    for candidate in sorted(root.iterdir()):
        if not (candidate / "manifest.wcv").is_file():
            continue
        try:
            manifest = load_manifest(str(candidate / "manifest.wcv"))
        except WcvError as exc:
            rows.append({"id": candidate.name, "status": f"битый манифест: {exc}"})
            continue
        info = store.current(candidate.name)
        rows.append(
            {
                "id": manifest.id,
                "name": manifest.name,
                "version": manifest.version,
                "prefix_version": info.version if info else "",
                "level": manifest.expected_level.value,
                "tested": manifest.tested,
                "size_bytes": store.size_bytes(candidate.name),
            }
        )
    print(json.dumps({"storage": str(root), "environments": rows}, ensure_ascii=False, indent=2))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    if args.ok == args.fail:
        print("нужно ровно одно из --ok или --fail", file=sys.stderr)
        return 2
    manifest_level = args.level or ""
    with CompatibilityRegistry(args.registry) as registry:
        app = registry.get(args.app_id)
        if app is None:
            print(f"{args.app_id!r} не зарегистрирован", file=sys.stderr)
            return 2
        from wcmgr.manifest import CoverageLevel

        level = CoverageLevel(manifest_level) if manifest_level else app.expected_level
        registry.record_run(
            RunRecord(args.app_id, ok=args.ok, level=level, detail=args.detail, build=args.build)
        )
        entry = registry.get(args.app_id)
    print(f"записано: ok={args.ok} level={level.value}; опубликованный уровень теперь {entry.published_level.value}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    store = PrefixStore(args.storage)
    try:
        version = store.rollback(args.app_id, to_version=args.to or None)
    except WcvError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"окружение {args.app_id} откачено к версии {version}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with CompatibilityRegistry(args.registry) as registry:
        payload = registry.export_json()
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"реестр выгружен в {args.output}")
        return 0
    print(payload)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = {
        "os": platform.system(),
        "wine": _which("wine"),
        "wine64": _which("wine64"),
        "aa_exec": _which("aa-exec"),
        "apparmor_parser": _which("apparmor_parser"),
        "systemd_run": _which("systemd-run"),
        "wayland_display": bool(_environ().get("WAYLAND_DISPLAY")),
        "kvm": Path("/dev/kvm").exists(),
        "storage_writable": _writable(args.storage),
    }
    missing = [name for name, value in checks.items() if not value]
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if missing:
        print("не хватает: " + ", ".join(sorted(missing)))
        return 1
    return 0


# ------------------------------------------------------------------ служебное
def _which(name: str) -> str:
    import shutil

    return shutil.which(name) or ""


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _writable(path: str) -> bool:
    target = Path(path)
    try:
        if target.exists():
            return target.is_dir() and _access_writable(target)
        parent = target.parent
        return parent.is_dir() and _access_writable(parent)
    except OSError:
        return False


def _access_writable(path: Path) -> bool:
    import os

    return os.access(str(path), os.W_OK)


def _capabilities() -> Capabilities:
    from bginstaller.probe import parse_lsblk  # noqa: F401 — опциональная проверка соседства

    kvm = Path("/dev/kvm").exists()
    hypervisor = False
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        hypervisor = "vmx" in cpuinfo or "svm" in cpuinfo
    except OSError:
        hypervisor = False
    return Capabilities(
        has_kvm=kvm,
        hypervisor_ready=hypervisor,
        has_windows_partition=_windows_present(),
        has_gpu_acceleration=Path("/dev/dri").is_dir(),
    )


def _windows_present() -> bool:
    """Проверяем наличие NTFS-тома. Без привилегий это может не удаться — тогда False."""
    try:
        result = subprocess.run(
            ["lsblk", "--json", "--bytes", "--output", "NAME,SIZE,FSTYPE,TYPE"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        from bginstaller.probe import parse_lsblk

        return any(disk.windows_partitions for disk in parse_lsblk(result.stdout))
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except WcvError as exc:
        print(f"ошибка WinCompat: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
