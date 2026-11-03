"""Общий слой GTK4 для приложений оболочки.

Здесь только то, что повторяется в каждом окне: подключение темы, доступ к
демону через bgd (сокета может не быть — тогда читаем источники напрямую) и
помощники по иконкам. Никакой бизнес-логики: она в bgshell и bgd.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:  # GTK доступен только в графическом сеансе Linux
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: F401
except (ImportError, ValueError):  # pragma: no cover — проверка на машине без GUI
    gi = None
    Gtk = None

from bgd.api import Context, Service
from bgd.model import Application
from bgshell.settings import SettingsStore

APP_ID_PREFIX = "ru.byteguru"
THEME_DIR = Path(__file__).resolve().parent / "theme"
ICONS_DIR = Path("/usr/share/icons/byteguru")
FALLBACK_ICON = "application-x-executable"


@dataclass(frozen=True)
class Session:
    """Что нужно любому окну оболочки: настройки, модель приложений и права."""

    settings: SettingsStore
    service: Service
    theme_name: str = "byteguru-light"
    is_session_owner: bool = True

    @property
    def apps(self) -> list[Application]:
        return self.service.apps()


def load_theme(manager: object, theme_name: str) -> None:
    """Ставим CSS поверх системной темы. Если файла нет — молча остаётся базовая тема."""
    css_file = THEME_DIR / f"{theme_name}.css"
    if not css_file.is_file():
        css_file = THEME_DIR / "base.css"
    if not css_file.is_file():
        return
    provider = Gtk.CssProvider.new()
    provider.load_from_path(str(css_file))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def connect_session(settings_path: str | None = None, registry_path: str | None = None) -> Session:
    """Открываем сеанс оболочки. Путь к реестру можно подменить для демонстрации."""
    store = SettingsStore.load(
        settings_path
        or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "byteguru" / "shell.ini"
    )
    context = Context(
        wcv_dir=os.environ.get("BG_WCV_DIR", "/usr/share/byteguru/wincompat"),
        registry_path=registry_path
        or os.environ.get("BG_REGISTRY", str(Path.home() / ".local/share/byteguru/compat.db")),
    )
    theme = str(store.get("theme.name"))
    return Session(settings=store, service=Service(context), theme_name=theme)


def app_icon(app: Application, size: int = 48) -> str:
    """Иконка приложения: манифест → тема → тематическая заглушка по типу."""
    if app.icon:
        return app.icon
    override = ICONS_DIR / "hicolor" / f"{size}x{size}" / "apps" / f"{app.id}.png"
    if override.exists():
        return str(override)
    if app.kind.name == "REBOOT":
        return "system-reboot-symbolic"
    if app.kind.name == "VM":
        return "vm-network-server-symbolic"
    if app.kind.name == "WINCOMPAT":
        return "drive-harddisk-symbolic"
    return FALLBACK_ICON


def coverage_badge(app: Application) -> tuple[str, str]:
    """(текст, css-класс) для значка уровня покрытия в лаунчере."""
    coverage = (app.coverage or "").lower()
    if not app.supported:
        return ("не поддерживается", "badge-unsupported")
    if "не проверено" in app.evidence:
        return (coverage.upper(), "badge-untested")
    return (coverage.upper(), "badge-proven")


def spawn(argv: list[str]) -> int:
    """Запуск команды от имени сеанса. Никакого shell: вектор аргументов как есть."""
    try:
        subprocess.Popen(argv, start_new_session=True)
    except (OSError, ValueError):
        return 1
    return 0


def run_gtk(main_window_factory, argv: list[str] | None = None) -> int:
    """Точка входа: без GTK на экране reporting-only окружении просто выходим с подсказкой."""
    if Gtk is None:  # pragma: no cover
        print("не хватает GTK 4: оболочки запускаются только в графическом сеансе ByteguruOS")
        return 1
    application = Gtk.Application(application_id=f"{APP_ID_PREFIX}.{main_window_factory.__name__}")
    application.connect("activate", lambda app: main_window_factory(app))
    return application.run(argv or [])
