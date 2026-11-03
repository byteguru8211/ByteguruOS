"""Окно настроек: список по группам с типизированными виджетами.

Виджеты строятся из схемы bgshell.settings, а не из отдельного.hand-сода:
добавил настройку в схему — она появилась в интерфейсе, и наоборот, интерфейс не
может предложить значение, которое не пройдёт проверку.
"""

from __future__ import annotations

from bgshell import settings as settings_module
from bgshell.errors import SettingError
from bgshell.ui import bggui


GROUPS = (
    ("Внешний вид", ("theme.name", "theme.font-scale", "panel.position", "panel.autohide")),
    ("Клавиатура и язык", ("session.keyboard-layouts", "session.hotkey-layout-toggle", "session.default-terminal")),
    ("Windows-приложения", ("wincompat.enabled", "wincompat.network-proxy", "wincompat.default-update")),
    ("Загрузка и соседство с Windows", ("boot.default-os", "boot.timeout-seconds", "boot.clock-policy", "storage.ntfs-write")),
    ("Приватность", ("privacy.telemetry",)),
)


def build_window(application) -> object:
    Gtk = bggui.Gtk
    session = bggui.connect_session()
    bggui.load_theme(None, session.theme_name)

    window = Gtk.ApplicationWindow(application=application)
    window.set_title("Настройки — ByteguruOS")
    window.set_default_size(720, 640)

    scroller = Gtk.ScrolledWindow()
    scroller.set_vexpand(True)
    window.set_child(scroller)

    list_box = Gtk.ListBox()
    list_box.add_css_class("settings-list")
    list_box.set_selection_mode(Gtk.SelectionMode.NONE)
    scroller.set_child(list_box)

    notice = Gtk.Label()
    notice.add_css_class("notice")
    for title, keys in GROUPS:
        list_box.append(_group_header(title))
        for key in keys:
            row = _setting_row(session.settings, key, notice)
            if row is not None:
                list_box.append(row)
    window.present()
    return window


def _group_header(title: str) -> object:
    Gtk = bggui.Gtk
    label = Gtk.Label(label=title)
    label.add_css_class("group-title")
    label.set_xalign(0.0)
    row = Gtk.ListBoxRow()
    row.set_child(label)
    return row


def _setting_row(store, key: str, notice) -> object:
    Gtk = bggui.Gtk
    setting = settings_module.SETTINGS.get(key)
    if setting is None:
        return None
    row = Gtk.ListBoxRow()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_top(8)
    box.set_margin_bottom(8)
    box.set_margin_start(12)
    box.set_margin_end(12)

    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    text.set_hexpand(True)
    title = Gtk.Label(label=setting.description)
    title.set_xalign(0.0)
    text.append(title)
    hint = Gtk.Label(label=f"{key} · {'требуются права администратора' if setting.requires_root else 'пользовательская'}")
    hint.add_css_class("dim")
    hint.set_xalign(0.0)
    text.append(hint)
    box.append(text)

    current = store.get(key)
    widget = _value_widget(setting, current, store, notice)
    if widget is None:
        return None
    box.append(widget)
    row.set_child(box)
    return row


def _value_widget(setting, current, store, notice) -> object:
    Gtk = bggui.Gtk

    def commit(value) -> None:
        try:
            store.set(setting.key, value)
            store.save()
            notice.set_text(f"сохранено: {setting.key}")
        except SettingError as exc:
            notice.set_text(f"не сохранено: {exc}")

    if setting.kind == "bool":
        switch = Gtk.Switch()
        switch.set_active(bool(current))
        switch.connect("state-set", lambda _w, state: (commit(state), False)[1])
        return switch
    if setting.kind == "choice":
        dropdown = Gtk.DropDown.new_from_strings(list(setting.choices))
        try:
            dropdown.set_selected(list(setting.choices).index(str(current)))
        except ValueError:
            dropdown.set_selected(0)
        dropdown.connect(
            "notify::selected",
            lambda widget, _param: commit(setting.choices[widget.get_selected()]),
        )
        return dropdown
    if setting.kind == "int":
        spin = Gtk.SpinButton.new_with_range(
            setting.minimum or 0, setting.maximum or 1000, 1
        )
        spin.set_value(int(current))
        spin.connect("value-set", lambda widget: commit(int(widget.get_value())))
        return spin
    entry = Gtk.Entry()
    entry.set_text(str(current))
    entry.connect("activate", lambda widget: commit(widget.get_text()))
    return entry


def main() -> int:
    return bggui.run_gtk(build_window)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
