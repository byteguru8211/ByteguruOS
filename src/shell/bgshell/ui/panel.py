"""Панель задач: часы, раскладка, индикаторы, меню запуска ОС, уведомления.

Панель — layer-shell поверх композитора bgwl. Если сеанс обычный (X11/XWayland
или другой композитор), она разворачивается в обычное окно: оболочка не должна
зависеть от наличия конкретного протокола, чтобы её можно было показать на
скриншотах и в демо.
"""

from __future__ import annotations

from datetime import datetime

from bgshell import panel as panel_model
from bgshell.ui import bggui

def build_window(application, now: datetime | None = None) -> object:
    Gtk = bggui.Gtk
    session = bggui.connect_session()
    bggui.load_theme(None, session.theme_name)
    model = panel_model.PanelModel(apps=session.apps, clock=now or datetime.now())
    model.default_os = str(session.settings.get("boot.default-os"))
    model.layout_name = (str(session.settings.get("session.keyboard-layouts")).split(",") or ["us"])[-1]

    window = Gtk.ApplicationWindow(application=application)
    window.set_title("Панель ByteguruOS")
    window.add_css_class("panel-window")

    bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    bar.set_margin_start(8)
    bar.set_margin_end(8)
    window.set_child(bar)

    launcher_button = Gtk.Button(label="Программы")
    launcher_button.add_css_class("flat")
    launcher_button.connect("clicked", lambda _b: _open_application(application, "bglauncher"))
    bar.append(launcher_button)

    tasks = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    tasks.set_hexpand(True)
    bar.append(tasks)

    indicators = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    bar.append(indicators)

    wincompat = Gtk.Label()
    if model.wincompat_running:
        wincompat.set_text(f"WinCompat: {model.wincompat_running}")
        wincompat.add_css_class("indicator-wincompat")
    indicators.append(wincompat)

    network = Gtk.Label(label="сеть" if model.network_connected else "нет сети")
    network.add_css_class("online" if model.network_connected else "offline")
    indicators.append(network)

    integrity = Gtk.Label(label="целостность OK" if model.integrity_ok else "система изменена")
    integrity.add_css_class("ok" if model.integrity_ok else "bad")
    indicators.append(integrity)

    clock_label = Gtk.Label(label=f"{model.clock_text()} · {model.date_text()}")
    clock_label.add_css_class("clock")
    indicators.append(clock_label)

    menu = Gtk.MenuButton()
    menu.set_label("Меню")
    menu.set_popover(_session_popover(window, session, model))
    bar.append(menu)
    window.present()
    return window


def _session_popover(parent, session, model):
    Gtk = bggui.Gtk
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.set_margin_top(8)
    box.set_margin_bottom(8)
    box.set_margin_start(8)
    box.set_margin_end(8)

    heading = Gtk.Label(label="Загрузка по умолчанию")
    heading.add_css_class("heading")
    box.append(heading)

    for key, label in model.boot_menu_items():
        button = Gtk.Button(label=label)
        button.add_css_class("flat")
        button.connect("clicked", lambda _b, value=key: _set_default_os(session, value))
        box.append(button)

    box.append(Gtk.Separator())
    for label, action in (
        ("Проверить целостность системы", "verify"),
        ("Восстановить загрузчик после обновления Windows", "boot-repair"),
        ("Выход из сеанса", "logout"),
        ("Перезагрузка", "reboot"),
        ("Завершение работы", "poweroff"),
    ):
        button = Gtk.Button(label=label)
        button.add_css_class("flat")
        button.connect("clicked", lambda _b, value=action: _system_action(value))
        box.append(button)
    popover = Gtk.Popover()
    popover.set_child(box)
    return popover


def _set_default_os(session, value: str) -> None:
    session.settings.set("boot.default-os", value)
    session.settings.save()


def _system_action(action: str) -> None:
    commands = {
        "verify": ["byteguru-verify"],
        "boot-repair": ["pkexec", "byteguru-boot-repair"],
        "logout": ["bg-session", "exit"],
        "reboot": ["systemctl", "reboot"],
        "poweroff": ["systemctl", "poweroff"],
    }
    argv = commands.get(action)
    if argv:
        bggui.spawn(argv)


def _open_application(application, name: str) -> int:
    return bggui.spawn([name])


def main() -> int:
    return bggui.run_gtk(build_window)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
