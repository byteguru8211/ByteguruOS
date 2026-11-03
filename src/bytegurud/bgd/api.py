"""API демона: единая точка, из которой оболочка получает приложения и планы запуска.

Здесь встречаются WinCompat, реестр совместимости и установщик: план запуска
включает и команду, и объяснение пользователю, и предупреждения изоляции.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bgd.model import Application, AppKind, deduplicate, group_by_kind, search
from bgd.sources import (
    installed_desktop_entries,
    parse_flatpak_list,
    scan_wcv_directory,
    with_registry_evidence,
)
from wcmgr.errors import WcvError
from wcmgr.fallback import Capabilities, decide
from wcmgr.manifest import CoverageLevel, load_manifest
from wcmgr.registry import CompatibilityRegistry
from wcmgr.sandbox import build_plan as build_sandbox_plan
from wcmgr.spawn import build_environment, build_scope_prefix, build_wine_command


@dataclass
class Context:
    """Откуда брать данные. В тестам подсовываются файлы-фикстуры, в системе — реальные пути."""

    wcv_dir: str = "/usr/share/byteguru/wincompat"
    registry_path: str = "/var/lib/byteguru/compat-registry.db"
    desktop_dirs: tuple[str, ...] = ("/usr/share/applications",)
    flatpak_list_json: str = ""
    detect_capabilities: Callable[[], Capabilities] | None = None
    extra_manifests: tuple[str, ...] = ()


@dataclass
class Service:
    context: Context
    _registry: CompatibilityRegistry | None = None

    def registry(self) -> CompatibilityRegistry:
        if self._registry is None:
            self._registry = CompatibilityRegistry(self.context.registry_path)
        return self._registry

    def apps(self) -> list[Application]:
        apps: list[Application] = []
        apps.extend(parse_flatpak_list(self.context.flatpak_list_json))
        apps.extend(scan_wcv_directory(self.context.wcv_dir))
        for path in self.context.extra_manifests:
            try:
                manifest = load_manifest(path)
            except WcvError:
                continue
            apps.append(_app_from_manifest_file(manifest, path))
        for directory in self.context.desktop_dirs:
            apps.extend(installed_desktop_entries(directory))
        apps = with_registry_evidence(apps, self.registry())
        return deduplicate(apps)

    def find(self, query: str) -> list[Application]:
        return search(self.apps(), query)

    def get(self, app_id: str) -> Application | None:
        for app in self.apps():
            if app.id.lower() == (app_id or "").lower():
                return app
        return None

    def capabilities(self) -> Capabilities:
        if self.context.detect_capabilities is not None:
            return self.context.detect_capabilities()
        return detect_capabilities()

    def launch_plan(self, app_id: str) -> dict:
        """Что именно выполнить, чтобы приложение открылось. Ничего не запускается."""
        app = self.get(app_id)
        if app is None:
            return {"ok": False, "error": f"приложение {app_id!r} не найдено"}
        if app.kind is AppKind.LINUX:
            return {
                "ok": True,
                "app": app.id,
                "action": "flatpak-run" if app.source == "flatpak" else "desktop-launch",
                "command": _linux_command(app),
                "warnings": [],
            }
        manifest_path = app.path
        try:
            manifest = load_manifest(manifest_path)
        except (WcvError, OSError) as exc:
            return {"ok": False, "app": app.id, "error": f"манифест не читается: {exc}"}
        prefix = str(Path("/var/lib/byteguru/wcv") / manifest.prefix_name)
        try:
            sandbox = build_sandbox_plan(manifest, prefix)
        except WcvError as exc:
            sandbox = None
            sandbox_error = str(exc)
        else:
            sandbox_error = ""
        decision = decide(manifest, self.registry().proven_level(manifest.id), self.capabilities())
        payload = {
            "ok": decision.is_executable,
            "app": manifest.id,
            "name": manifest.name,
            "action": decision.action,
            "level": decision.level.value,
            "reason": decision.reason,
            "hint": decision.hint,
            "requires_reboot": decision.requires_reboot,
            "warnings": list(sandbox.warnings) if sandbox else [sandbox_error],
            "notes": list(decision.notes),
        }
        if decision.action == "launch-wine" and sandbox is not None:
            argv = build_wine_command(manifest, prefix, sandbox_plan=sandbox)
            payload["command"] = build_scope_prefix(manifest, properties=sandbox.systemd_properties) + ["--"] + argv
            payload["raw_command"] = argv
            payload["environment"] = build_environment(manifest, prefix)
        elif decision.commands:
            payload["command"] = list(decision.commands)
        return payload

    def status(self) -> dict:
        apps = self.apps()
        return {
            "apps_total": len(apps),
            "wincompat": len([app for app in apps if app.kind is AppKind.WINCOMPAT]),
            "needs_windows": len([app for app in apps if app.needs_windows]),
            "unproven": len([app for app in apps if app.kind is not AppKind.LINUX and "не проверено" in app.evidence]),
            "coverage": registry_summary(self.registry()),
            "capabilities": {
                "kvm": self.capabilities().has_kvm,
                "hypervisor_ready": self.capabilities().hypervisor_ready,
                "windows_partition": self.capabilities().has_windows_partition,
            },
        }

    def handle(self, request: dict) -> dict:
        """Единая точка для протокола демона: {"method": ..., "params": {...}}."""
        method = request.get("method", "")
        params = request.get("params") or {}
        if method == "apps.list":
            return {"ok": True, "result": [_app_to_dict(app) for app in self.apps()]}
        if method == "apps.search":
            return {"ok": True, "result": [_app_to_dict(app) for app in self.find(params.get("query", ""))]}
        if method == "apps.get":
            app = self.get(params.get("id", ""))
            return {"ok": app is not None, "result": _app_to_dict(app) if app else None}
        if method == "apps.launch_plan":
            return self.launch_plan(params.get("id", ""))
        if method == "system.status":
            return {"ok": True, "result": self.status()}
        return {"ok": False, "error": f"неизвестный метод {method!r}"}

    def close(self) -> None:
        if self._registry is not None:
            self._registry.close()
            self._registry = None


def registry_summary(registry: CompatibilityRegistry) -> dict[str, int]:
    return registry.summary()


def detect_capabilities() -> Capabilities:
    """Определяем окружение по факту. На не-Linux возвращаем «ничего нет» — без исключений.

    Наличие Windows-раздела здесь не проверяется: это задача bginstaller.probe,
    которому нужны привилегии; демон читает результат из кэша установщика.
    """
    has_kvm = Path("/dev/kvm").exists()
    hypervisor_ready = False
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        hypervisor_ready = "vmx" in cpuinfo or "svm" in cpuinfo
    except OSError:
        hypervisor_ready = False
    return Capabilities(
        has_kvm=has_kvm,
        hypervisor_ready=hypervisor_ready,
        has_windows_partition=_windows_partition_cached(),
        has_gpu_acceleration=Path("/dev/dri").is_dir(),
    )


WINDOWS_CACHE_FILE = Path("/run/byteguru/installer/dualboot.json")


def _windows_partition_cached() -> bool:
    """Читаем кэш установщика: демон не имеет права сканировать диски сам."""
    try:
        import json

        payload = json.loads(WINDOWS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(payload.get("windows_present", False))


def _app_from_manifest_file(manifest, path: str) -> Application:
    from bgd.sources import application_from_manifest

    return application_from_manifest(manifest, path=path)


def _app_to_dict(app: Application | None) -> dict | None:
    if app is None:
        return None
    return {
        "id": app.id,
        "name": app.name,
        "kind": app.kind.value,
        "version": app.version,
        "publisher": app.publisher,
        "icon": app.icon,
        "source": app.source,
        "coverage": app.coverage,
        "evidence": app.evidence,
        "available": app.available,
        "needs_windows": app.needs_windows,
        "supported": app.supported,
        "tags": list(app.tags),
        "extra": app.extra,
    }


def _linux_command(app: Application) -> list[str]:
    if app.source == "flatpak":
        return ["flatpak", "run", app.id]
    return ["gtk-launch", app.id]
