"""Общий слой GTK4 для приложений оболочки.

Здесь только то, что повторяется в каждом окне: подключение темы, доступ к
демону через bgd (сокета может не быть — тогда читаем источники напрямую),
помощники по иконкам и включение layer-shell. Никакой бизнес-логики: она в
bgshell и bgd.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from bgd.api import Context, Service
from bgd.model import Application
from bgshell import themes
from bgshell.settings import SettingsStore

# gtk4-layer-shell обязан быть пред-загружен (LD_PRELOAD), а GDK — выбрать
# wayland до первой инициализации. Оба решения принимаются окружением процесса,
# поэтому их делают обёртки bin/bgpanel и т. п., а не этот модуль: перезапуск
# себя же из Python терял командную строку и молча ломал сеанс. Здесь остаётся
# только проверка, которую панель честно показывает пользователю.
try:
    import gi
except ImportError:  # pragma: no cover — проверка на машине без GTK
    gi = None

# Порядок импорта здесь — не косметика. gtk4-layer-shell привязывает
# zwlr_layer_shell_v1 в конструкторе библиотеки, и конструктор должен отработать
# до того, как GTK откроет Wayland-дисплей; при обратном порядке GTK в
# пред-загруженном процессе не инициализируется («Gtk couldn't be initialized»).
# Поэтому сначала типelib слоя, потом Gtk.
LAYER_SHELL = None
if gi is not None:
    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as LAYER_SHELL  # noqa: F401
    except (ImportError, ValueError):
        LAYER_SHELL = None

    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: F401
    except (ImportError, ValueError):  # pragma: no cover — без графического сеанса
        gi = None
        Gtk = None
else:  # pragma: no cover
    Gtk = None

def layer_shell_supported() -> bool:
    return LAYER_SHELL is not None and bool(LAYER_SHELL.is_supported())


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
    def wincompat_enabled(self) -> bool:
        return bool(self.settings.get("wincompat.enabled"))

    @property
    def apps(self) -> list[Application]:
        """Видимые программы.

        Демон знает все источники — это его работа. Показывать же всё подряд
        решение продукта: пока Windows-подсистема выключена, её программы,
        обходные уровни через ВМ и перезагрузку в соседний раздел в списке
        быть не должно. Иначе «свой дистрибутив» выглядит как витрина Wine.
        """
        apps = self.service.apps()
        if self.wincompat_enabled:
            return apps
        return [app for app in apps if not app.needs_windows]

    def find(self, query: str) -> list[Application]:
        """Поиск по тем же видимым программам, что и список.

        Отдельный метод нужен ровно потому, что лаунчер ищет через
        `service.find`, а показывает — через `apps`: два разных источника
        дают список, где выключенная подсистема то появляется, то исчезает.
        """
        if self.wincompat_enabled:
            return self.service.find(query)
        from bgd.model import search  # noqa: PLC0415 — только при выключенной подсистеме

        return search(self.apps, query)


def load_theme(manager: object, theme_name: str) -> None:
    """Ставим CSS поверх системной темы. Если файла нет — молча остаётся базовая тема.

    Порядок providers (base → тема → порождённая палитра) важен: theme.css
    пишется из настройки accent и должен иметь последнее слово, иначе выбранный
    пользователем акцент перекрывается статическим CSS темы.
    """
    display = Gdk.Display.get_default()
    if display is None:
        # Нет дисплея — нет и StyleContext: в этом состоянии работают только
        # headless-проверки логики, и падать с Gtk-CRITICAL им незачем.
        return

    css_file = THEME_DIR / f"{theme_name}.css"
    if not css_file.is_file():
        css_file = THEME_DIR / "base.css"
    if not css_file.is_file():
        return

    provider = Gtk.CssProvider.new()
    provider.load_from_path(str(css_file))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # Тёмный режим просим у самого Adwaita, а не только своей CSS: иначе
    # стандартные виджеты (меню, диалоги, переключатели) остаются светлыми
    # посреди тёмного стола, и это видно в первом же окне настроек.
    settings = Gtk.Settings.get_default()
    if settings is not None:
        settings.set_property("gtk-application-prefer-dark-theme",
            "dark" in theme_name)

    generated = themes.THEME_CSS
    if generated.is_file():
        palette_provider = Gtk.CssProvider.new()
        palette_provider.load_from_path(str(generated))
        Gtk.StyleContext.add_provider_for_display(
            display, palette_provider, Gtk.STYLE_PROVIDER_PRIORITY_SETTINGS)


def build_window(application, title: str):
    """Окно приложения: одна точка, чтобы заголовок и класс были одинаковые."""
    window = Gtk.ApplicationWindow(application=application)
    window.set_title(title)
    return window


def has_icon(icon_name: str) -> bool:
    """Есть ли иконка в теме. Ответ нужен до создания Image: GTK рисует
    «пропавшую» иконку пустым квадратом, и панель выглядит сломанной."""
    display = Gdk.Display.get_default()
    if display is None:
        return False
    return bool(Gtk.IconTheme.get_for_display(display).has_icon(icon_name))


def icon_or_label(icon_name: str, text: str, size: int = 16):
    """Иконка темы, а если её нет — текст: панель не должна выглядеть сломанной
    на системе без иконочного пакета."""
    if not has_icon(icon_name):
        return Gtk.Label(label=text or icon_name[:2])
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_pixel_size(size)
    if text:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.append(image)
        box.append(Gtk.Label(label=text))
        return box
    return image


def run_command(command: str) -> int:
    """Запуск строки команды из конфига: split по shell-правилам, без shell."""
    import shlex  # noqa: PLC0415 — только здесь, чтобы не грузить модуль целиком

    try:
        argv = shlex.split(command)
    except ValueError:
        return 1
    if not argv:
        return 1
    return spawn(argv)


def connect_session(settings_path: str | None = None,
        registry_path: str | None = None) -> Session:
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
    """(текст, css-класс) для значка уровня покрытия в лаунчере.

    Значок — про Windows-программы: уровень покрытия описывает, насколько
    проверен перенос чужого .exe. Нативному приложению вешать «не
    поддерживается» бессмысленно, поэтому для него возвращаем пустую строку,
    а вызывающий код просто не рисует виджет.
    """
    if not app.needs_windows:
        return ("", "")
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


def prefer_wayland() -> None:
    """Заставляем GDK выбрать Wayland, если сеанс именно такой.

    Без этого на машине с запущенным X-сервером панель молча открывается на
    X11: layer-shell там недоступен, композитор её не видит, и выглядит это не
    как ошибка, а как «панели нет». В образе ByteguruOS X-сервера нет, но
    разработчик проверяет оболочку на машине с X — и должен видеть то же, что
    увидит пользователь.
    """
    if os.environ.get("GDK_BACKEND"):
        return
    if os.environ.get("WAYLAND_DISPLAY"):
        os.environ["GDK_BACKEND"] = "wayland"


def run_gtk(main_window_factory, argv: list[str] | None = None) -> int:
    """Точка входа: без GTK в reporting-only окружении просто выходим с подсказкой."""
    if Gtk is None:  # pragma: no cover
        print("не хватает GTK 4: оболочки запускаются только в графическом сеансе ByteguruOS")
        return 1
    prefer_wayland()
    application = Gtk.Application(application_id=f"{APP_ID_PREFIX}.{main_window_factory.__name__}")
    application.connect("activate", lambda app: main_window_factory(app))
    return application.run(argv or [])
