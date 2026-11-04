"""Единая модель приложений для оболочки.

Оболочка не должна знать, откуда приложение: Flatpak, окружение WinCompat или
системный образ. Дем приводит всё к одному описанию и честно сообщает, что
подтверждено, а что только заявлено.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AppKind(str, Enum):
    LINUX = "linux"           # Flatpak / системный пакет
    WINCOMPAT = "wincompat"   # WCV-окружение
    VM = "vm"                 # обходной путь L3
    REBOOT = "reboot"         # обходной путь L4: соседняя Windows
    WEB = "web"


@dataclass(frozen=True)
class Application:
    id: str
    name: str
    kind: AppKind
    version: str = ""
    publisher: str = ""
    icon: str = ""
    source: str = ""
    available: bool = True
    coverage: str = ""            # l0..l4 из реестра совместимости
    evidence: str = ""            # «подтверждено прогонами» или «не проверено»
    path: str = ""                # файл манифеста или desktop-файл
    tags: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def needs_windows(self) -> bool:
        return self.kind in (AppKind.WINCOMPAT, AppKind.VM, AppKind.REBOOT)

    @property
    def supported(self) -> bool:
        return self.available and self.coverage not in ("", "l0")

    def search_blob(self) -> str:
        return " ".join(
            filter(None, (self.name, self.publisher, self.id, " ".join(self.tags), self.version))
        ).lower()


def deduplicate(apps: list[Application]) -> list[Application]:
    """Один и тот же продукт может приходить из двух источников: оставляем предпочтительный.

    Предпочтение — нативному Linux-приложению: оно быстрее и стабильнее, чем
    то же самое в Wine. Дубликат WinCompat при этом не выбрасывается молча:
    он остаётся как явный вариант запуска.
    """
    by_id: dict[str, Application] = {}
    for app in apps:
        key = app.id.lower()
        previous = by_id.get(key)
        if previous is None:
            by_id[key] = app
            continue
        primary, secondary = _prefer(previous, app)
        by_id[key] = replace_extra(primary, secondary)
    return sorted(by_id.values(), key=lambda app: app.name.lower())


def _prefer(a: Application, b: Application) -> tuple[Application, Application]:
    """Нативное Linux-приложение всегдаprimary, вариант WinCompat — secondary."""
    if a.kind is AppKind.LINUX and b.kind is not AppKind.LINUX:
        return a, b
    if b.kind is AppKind.LINUX and a.kind is not AppKind.LINUX:
        return b, a
    # Оба не нативные: оставляем того, у кого подтверждено покрытие.
    if b.coverage and not a.coverage:
        return b, a
    return a, b


def replace_extra(primary: Application, secondary: Application) -> Application:
    """Слить варианты запуска в extra, сохранив основное приложение."""
    from dataclasses import replace

    variants = list(primary.extra.get("variants", ()))
    variants.append({"id": secondary.id, "kind": secondary.kind.value, "coverage": secondary.coverage})
    return replace(primary, extra={**primary.extra, "variants": variants})


def search(apps: list[Application], query: str, *, limit: int = 25) -> list[Application]:
    query = (query or "").strip().lower()
    if not query:
        return apps[:limit]
    scored = []
    for app in apps:
        blob = app.search_blob()
        if query not in blob:
            continue
        bonus = 0
        if app.name.lower().startswith(query):
            bonus = 2
        elif app.name.lower() == query:
            bonus = 3
        scored.append((bonus, app))
    scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
    return [app for _bonus, app in scored][:limit]


def group_by_kind(apps: list[Application]) -> dict[str, list[Application]]:
    groups: dict[str, list[Application]] = {}
    for app in apps:
        groups.setdefault(app.kind.value, []).append(app)
    return groups
