"""Источники приложений: Flatpak, манифесты WCV, реестр совместимости.

Разбор выводится на текст утилит, а не вызывает их: так код проверяется на
машине без Linux, а исполнение остаётся делом демона на целевой системе.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from bgd.model import AppKind, Application
from wcmgr.errors import ManifestError
from wcmgr.manifest import CoverageLevel, load_manifest_text
from wcmgr.registry import CompatibilityRegistry

FLATPAK_FIELDS = ("id", "application", "version", "name", "description", "icons")


def parse_flatpak_list(text: str) -> list[Application]:
    """Разбор `flatpak list --app --columns=application,version,name,description --json`."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    apps: list[Application] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        app_id = row.get("application") or row.get("id") or ""
        if not app_id:
            continue
        apps.append(
            Application(
                id=app_id,
                name=(row.get("name") or app_id.split(".")[-1]).strip(),
                kind=AppKind.LINUX,
                version=(row.get("version") or "").strip(),
                publisher=".".join(app_id.split(".")[:-1]),
                source="flatpak",
                path=row.get("ref", "") or app_id,
                tags=tuple(tag for tag in (row.get("description") or "").split("\t")[1:] if tag),
            )
        )
    return apps


def scan_wcv_directory(root: str | Path) -> list[Application]:
    """Манифесты WCV из каталога репозитория или /var/lib/byteguru/wcv."""
    directory = Path(root)
    apps: list[Application] = []
    if not directory.is_dir():
        return apps
    for candidate in sorted(directory.rglob("*.wcv")):
        try:
            manifest = load_manifest_text(candidate.read_text(encoding="utf-8"))
        except (ManifestError, OSError, UnicodeDecodeError):
            # Битый манифест не должен ронять список приложений: его покажем
            # отдельно через doctor.
            continue
        apps.append(application_from_manifest(manifest, path=str(candidate)))
    return apps


def application_from_manifest(manifest, *, path: str = "", proven: CoverageLevel | None = None,
                              evidence: str = "") -> Application:
    kind = AppKind.WINCOMPAT
    if manifest.expected_level is CoverageLevel.L3:
        kind = AppKind.VM
    elif manifest.expected_level is CoverageLevel.L4:
        kind = AppKind.REBOOT
    level = proven or manifest.expected_level
    return Application(
        id=manifest.id,
        name=manifest.name,
        kind=kind,
        version=manifest.version,
        publisher=manifest.publisher,
        source="wcv",
        path=path,
        coverage=level.value,
        evidence=evidence or (
            "подтверждено прогонами" if proven is not None else "не проверено на этой сборке"
        ),
        available=True,
        extra={
            "executable": manifest.executable,
            "runner": manifest.runner.value,
            "declared_level": manifest.expected_level.value,
            "fingerprint": manifest.fingerprint(),
            "requires_windows": manifest.expected_level.needs_windows_environment,
        },
    )


def with_registry_evidence(apps: Iterable[Application], registry: CompatibilityRegistry) -> list[Application]:
    """Подмешиваем в список реальный уровень покрытия из реестра."""
    from dataclasses import replace

    result = []
    for app in apps:
        entry = registry.get(app.id) if app.kind is not AppKind.LINUX else None
        if entry is None:
            result.append(app)
            continue
        result.append(
            replace(
                app,
                coverage=entry.published_level.value,
                evidence=entry.evidence,
                extra={**app.extra, "proven": entry.proven_level is not None},
            )
        )
    return list(result)


def installed_desktop_entries(directory: str | Path) -> list[Application]:
    """Системные .desktop-файлы: нужный источник для «всего, что стоит в системе»."""
    path = Path(directory)
    apps: list[Application] = []
    if not path.is_dir():
        return apps
    for candidate in sorted(path.glob("*.desktop")):
        fields = parse_desktop_file(candidate.read_text(encoding="utf-8", errors="replace"))
        if fields.get("NoDisplay", "").lower() == "true" or fields.get("X-Gui", "").lower() == "false":
            continue
        name = fields.get("Name") or candidate.stem
        apps.append(
            Application(
                id=fields.get("StartupWMClass") or candidate.stem,
                name=name,
                kind=AppKind.LINUX,
                source="desktop-entry",
                path=str(candidate),
                icon=fields.get("Icon", ""),
                tags=tuple(filter(None, (fields.get("Categories", "").split(";")))),
            )
        )
    return apps


def parse_desktop_file(text: str) -> dict[str, str]:
    """Минимальный парсер Desktop Entry: только ключевые пары из первой группы."""
    fields: dict[str, str] = {}
    in_group = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_group = line == "[Desktop Entry]"
            continue
        if not in_group or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        fields.setdefault(key.strip(), value.strip())
    return fields
