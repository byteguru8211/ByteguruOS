"""Окно настроек ByteguruOS.

Два источника истины, и окно честно их разделяет:

1. `bgshell.settings` (shell.ini) — то, что принадлежит оболочке: темы,
   раскладки клавиатуры, поведение загрузчика, политика NTFS. Виджеты
   строятся из СХЕМЫ: добавил настройку в схему — она появилась в интерфейсе,
   и наоборот, интерфейс не может предложить значение, которое не пройдёт
   проверку;
2. композитор (через bgshell.ctl) — то, что принадлежит сеансу: раскладка
   окон, промежутки, рамка фокуса, анимации, таймаут бездействия, имена и
   акценты потоков. Это не дубль первой группы: `bgctl set` меняет живой
   сеанс, а bgwl при старте читает тот же bgwl.conf, который оболочка
   обновляет через bgshell.themes.

Раздела «Windows-приложения» нет, если подсистема WinCompat не установлена:
система без неё полная, и показывать группу с тремя переключателями, которые
ничего не включают, значит обещать то, чего не будет.
"""

from __future__ import annotations

import os
from pathlib import Path

from bgshell import settings as settings_module
from bgshell import themes
from bgshell.errors import SettingError
from bgshell.ctl import Compositor, CompositorError
from bgshell.ui import bggui

# Группы shell.ini. Порядок — порядок вкладок, а не алфавит: человек ищет
# «как выглядит», потом «как себя ведёт», потом «как загружается».
INI_GROUPS = (
    ("Внешний вид", ("theme.name", "theme.mode", "theme.font-scale",
        "panel.position", "panel.autohide")),
    ("Клавиатура", ("session.keyboard-layouts", "session.hotkey-layout-toggle",
        "session.default-terminal")),
    ("Загрузка и соседство с Windows", ("boot.default-os",
        "boot.timeout-seconds", "boot.clock-policy", "storage.ntfs-write")),
    ("Приватность", ("privacy.telemetry",)),
)

# Композиторские настройки: ключ bgwl.conf → (подпись, вид, данные виджета).
COMPOSITOR_SETTINGS = (
    ("layout", "Раскладка окон по умолчанию", "choice",
        ("float", "tile", "even")),
    ("gap-inner", "Промежуток между окнами, px", "int", (0, 40)),
    ("gap-outer", "Отступ от края экрана, px", "int", (0, 40)),
    ("border-width", "Толщина рамки фокуса, px", "int", (0, 8)),
    ("animations", "Анимации при смене потока", "bool", None),
    ("anim-duration", "Длительность анимации, мс", "int", (0, 400)),
    ("focus-follows-mouse", "Фокус следует за курсором", "bool", None),
    ("idle-timeout", "Гасить экран и блокировать через, с", "int", (0, 3600)),
)

ACCENT_CHOICES = ("#f2a33d", "#4a9eff", "#3ecf8e", "#ff5c7a", "#b98cff",
    "#ffd166")
STREAM_LAYOUTS = ("float", "tile", "even")


def _row(child) -> object:
    Gtk = bggui.Gtk
    row = Gtk.ListBoxRow()
    row.set_child(child)
    return row


class SettingsWindow:
    def __init__(self, application):
        self.application = application
        self.session = bggui.connect_session()
        self.store = self.session.settings
        self.compositor = Compositor()
        self.snapshot = None
        self.palette = themes.from_settings(self.store.effective())

        # Тема до построения виджетов: иначе окно открывается светлым
        # Adwaita посреди тёмного стола и первый кадр выглядит как баг темы.
        bggui.load_theme(None, str(self.store.get("theme.name")))

        self.window = bggui.build_window(application, "Настройки — ByteguruOS")
        self.window.set_default_size(880, 680)
        self._build()

    # ------------------------------------------------------------ каркас
    def _build(self) -> None:
        Gtk = bggui.Gtk

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.set_child(box)

        # Боковая панель вместо StackSwitcher: с русскими подписями
        # («Загрузка и соседство с Windows») горизонтальные вкладки делали окно
        # шире экрана, и виджеты настроек уезжали за правый край.
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        box.append(body)

        self.sidebar = Gtk.StackSidebar()
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.set_size_request(230, -1)
        body.append(self.sidebar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        body.append(self.stack)

        self.sidebar.set_stack(self.stack)

        self.status = Gtk.Label(label="")
        self.status.add_css_class("bg-muted")
        self.status.set_margin_bottom(6)
        box.append(self.status)

        self._add_ini_pages()
        self._add_desktop_page()
        self._add_streams_page()
        self._add_screens_page()
        if self._wincompat_available():
            self._add_wincompat_page()
        self._add_about_page()

        self.stack.set_visible_child_name(
            INI_GROUPS[0][0].replace(" ", "-"))

    def _page(self, title: str) -> object:
        Gtk = bggui.Gtk
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        list_box = Gtk.ListBox()
        list_box.add_css_class("settings-list")
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(list_box)
        # Имя страницы = подпись в боковой панели: её показывает StackSidebar.
        self.stack.add_titled(scroller, title.replace(" ", "-"), title)
        return list_box

    def _header(self, list_box, title: str, subtitle: str = "") -> None:
        Gtk = bggui.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for margin, value in (("top", 16), ("bottom", 6), ("start", 12),
                ("end", 12)):
            getattr(box, f"set_margin_{margin}")(value)
        label = Gtk.Label(label=title)
        label.add_css_class("group-title")
        label.set_xalign(0.0)
        box.append(label)
        if subtitle:
            hint = Gtk.Label(label=subtitle)
            hint.add_css_class("bg-muted")
            hint.set_xalign(0.0)
            hint.set_wrap(True)
            box.append(hint)
        list_box.append(_row(box))

    def _row(self, list_box, title: str, hint: str, widget) -> None:
        Gtk = bggui.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for margin, value in (("top", 8), ("bottom", 8), ("start", 12),
                ("end", 12)):
            getattr(box, f"set_margin_{margin}")(value)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        label = Gtk.Label(label=title)
        label.set_xalign(0.0)
        text.append(label)
        if hint:
            sub = Gtk.Label(label=hint)
            sub.add_css_class("bg-muted")
            sub.set_xalign(0.0)
            text.append(sub)
        box.append(text)
        if widget is not None:
            box.append(widget)
        list_box.append(_row(box))

    def _say(self, text: str) -> None:
        self.status.set_text(text)

    # ------------------------------------------------------------ shell.ini
    def _add_ini_pages(self) -> None:
        for title, keys in INI_GROUPS:
            list_box = self._page(title)
            self._header(list_box, title)
            for key in keys:
                setting = settings_module.SETTINGS.get(key)
                if setting is None:
                    continue
                self._row(list_box, setting.description, key,
                    self._ini_widget(setting))

    def _ini_widget(self, setting) -> object:
        Gtk = bggui.Gtk
        current = self.store.get(setting.key)

        def commit(value) -> None:
            try:
                self.store.set(setting.key, value)
                self.store.save()
            except SettingError as error:
                self._say(f"не сохранено: {error}")
                return
            self._say(f"сохранено: {setting.key}")
            if setting.key.startswith("theme."):
                self._apply_theme()

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
            dropdown.connect("notify::selected",
                lambda widget, _p: commit(setting.choices[widget.get_selected()]))
            return dropdown
        if setting.kind == "int":
            spin = Gtk.SpinButton.new_with_range(setting.minimum or 0,
                setting.maximum or 1000, 1)
            spin.set_value(int(current))
            # value-set у SpinButton — действие, а не сигнал; слушать надо
            # value-changed, иначе окно падает при первой же сборке виджета.
            spin.connect("value-changed",
                lambda widget: commit(int(widget.get_value())))
            return spin
        entry = Gtk.Entry()
        entry.set_text(str(current))
        entry.connect("activate", lambda widget: commit(widget.get_text()))
        return entry

    # ------------------------------------------------------------ композитор
    def _add_desktop_page(self) -> None:
        Gtk = bggui.Gtk
        list_box = self._page("Рабочий стол")
        self._header(list_box, "Рабочий стол",
            "Настройки живут в композиторе и применяются сразу. bgwl.conf "
            "записывает оболочка, поэтому выбор переживёт перезагрузку.")

        accents = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for color in ACCENT_CHOICES:
            swatch = Gtk.Button()
            swatch.set_size_request(28, 28)
            swatch.add_css_class("bg-swatch")
            swatch.set_tooltip_text(color)
            swatch.connect("clicked",
                lambda _b, value=color: self._set_accent(value))
            accents.append(swatch)
        self._row(list_box, "Акцентный цвет",
            "рамка фокуса, панель, кнопки оболочки", accents)

        self.refresh_compositor()
        for key, title, kind, extra in COMPOSITOR_SETTINGS:
            self._row(list_box, title, f"bgctl set {key}",
                self._compositor_widget(key, kind, extra))

    def _compositor_widget(self, key: str, kind: str, extra) -> object:
        Gtk = bggui.Gtk
        current = self.snapshot.settings.get(key) if self.snapshot else None

        def apply(value) -> None:
            self._ask(f"set {key} {value}")

        if kind == "bool":
            switch = Gtk.Switch()
            switch.set_active(bool(current))
            switch.connect("state-set",
                lambda _w, state: (apply("true" if state else "false"), False)[1])
            return switch
        if kind == "choice":
            dropdown = Gtk.DropDown.new_from_strings(list(extra))
            if current is not None:
                try:
                    dropdown.set_selected(list(extra).index(str(current)))
                except ValueError:
                    pass
            dropdown.connect("notify::selected",
                lambda widget, _p: apply(extra[widget.get_selected()]))
            return dropdown
        low, high = extra
        spin = Gtk.SpinButton.new_with_range(low, high, 1)
        spin.set_value(int(current) if current is not None else low)
        spin.connect("value-changed",
            lambda widget: apply(int(widget.get_value())))
        return spin

    def refresh_compositor(self) -> None:
        try:
            self.snapshot = self.compositor.state()
        except CompositorError as error:
            self._say(f"композитор недоступен: {error}")
            self.snapshot = None

    def _ask(self, command: str) -> None:
        try:
            self.compositor.command(command)
        except CompositorError as error:
            self._say(f"композитор отклонил «{command}»: {error}")
            return
        self.refresh_compositor()
        self._say(f"применено: {command}")

    def _set_accent(self, color: str) -> None:
        self._apply_palette(themes.Palette(accent=color,
            dark=self.palette.dark))

    def _apply_theme(self) -> None:
        self._apply_palette(themes.from_settings(self.store.effective()))

    def _apply_palette(self, palette: themes.Palette) -> None:
        self.palette = palette
        self.store.set("theme.mode", "dark" if palette.dark else "light")
        self.store.set("theme.accent", palette.accent)
        self.store.save()
        streams = [(stream.index, stream.name, stream.accent)
            for stream in (self.snapshot.streams if self.snapshot else [])]
        notes = themes.apply(palette, self.compositor, streams)
        self._say("тема применена: " + ", ".join(notes))
        bggui.load_theme(None, str(self.store.get("theme.name")))
        self.refresh_compositor()

    # ------------------------------------------------------------ потоки
    def _add_streams_page(self) -> None:
        list_box = self._page("Потоки")
        self._header(list_box, "Потоки",
            "Поток — рабочее место со своим именем, акцентом и раскладкой "
            "окон. Рамка фокуса окрашивается в акцент потока: по ней видно, "
            "где ты, не читая подпись.")
        self.streams_box = list_box
        self._fill_streams()

    def _fill_streams(self) -> None:
        Gtk = bggui.Gtk
        list_box = self.streams_box
        while list_box.get_first_child() is not None:
            list_box.remove(list_box.get_first_child())
        self._header(list_box, "Потоки",
            "Имя меняется у активного потока: переключение — один клик.")
        self.refresh_compositor()
        if self.snapshot is None:
            return

        for stream in self.snapshot.streams:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            for margin, value in (("top", 6), ("bottom", 6), ("start", 12),
                    ("end", 12)):
                getattr(row, f"set_margin_{margin}")(value)

            name = Gtk.Entry()
            name.set_text(stream.name)
            name.set_hexpand(True)
            name.connect("activate",
                lambda widget, index=stream.index: self._rename_stream(index,
                    widget.get_text()))
            row.append(name)

            layouts = Gtk.DropDown.new_from_strings(list(STREAM_LAYOUTS))
            try:
                layouts.set_selected(list(STREAM_LAYOUTS).index(stream.layout))
            except ValueError:
                pass
            layouts.connect("notify::selected",
                lambda widget, _p, index=stream.index:
                    self._set_stream_layout(index,
                        STREAM_LAYOUTS[widget.get_selected()]))
            row.append(layouts)

            open_button = Gtk.Button(label="перейти")
            open_button.set_sensitive(not stream.active)
            open_button.connect("clicked",
                lambda _b, index=stream.index: self._switch_stream(index))
            row.append(open_button)

            counter = Gtk.Label(label=str(stream.windows))
            counter.add_css_class("bg-muted")
            row.append(counter)

            list_box.append(_row(row))

    def _rename_stream(self, index: int, name: str) -> None:
        name = name.strip()
        if not name or self.snapshot is None:
            return
        active = self.snapshot.active_stream
        if active is None or active.index != index:
            self._say("имя меняет только активный поток — перейдите в него")
            return
        self._ask(f"set ws-name {name}")
        # Имя должно попасть в bgwl.conf, иначе после перезагрузки вернётся
        # заводское: применение темы перечитывает список потоков в конфиг.
        self._apply_palette(self.palette)

    def _set_stream_layout(self, index: int, layout: str) -> None:
        active = self.snapshot.active_stream if self.snapshot else None
        if active is None or active.index != index:
            self._ask(f"workspace {index}")
        self._ask(f"set ws-layout {layout}")

    def _switch_stream(self, index: int) -> None:
        self._ask(f"workspace {index}")
        self._fill_streams()

    # ------------------------------------------------------------ экраны
    def _add_screens_page(self) -> None:
        list_box = self._page("Экраны")
        self._header(list_box, "Экраны",
            "Разрешение и масштаб меняет портал xdg-desktop-portal; здесь — "
            "то, что видит композитор, включая область, оставленную панели.")
        self.screens_box = list_box
        self._fill_screens()

    def _fill_screens(self) -> None:
        list_box = self.screens_box
        while list_box.get_first_child() is not None:
            list_box.remove(list_box.get_first_child())
        self._header(list_box, "Экраны")
        self.refresh_compositor()
        if self.snapshot is None:
            return
        for output in self.snapshot.outputs:
            self._row(list_box,
                f"{output.name} · {output.description or 'без описания'}",
                f"размещение {output.box}, доступно {output.usable}, "
                f"масштаб {output.scale:g}, {output.refresh} мГц, "
                f"{'включён' if output.enabled else 'погашен'}", None)

    # ------------------------------------------------------------ wincompat
    def _wincompat_available(self) -> bool:
        try:
            import wcmgr.manifest  # noqa: F401
            return True
        except ImportError:
            return False

    def _add_wincompat_page(self) -> None:
        list_box = self._page("Windows-приложения")
        self._header(list_box, "Windows-приложения",
            "Подсистема WinCompat опциональна: без неё система остаётся "
            "полной, а этот раздел исчезает.")
        for key in ("wincompat.enabled", "wincompat.network-proxy",
                "wincompat.default-update"):
            setting = settings_module.SETTINGS.get(key)
            if setting is None:
                continue
            self._row(list_box, setting.description, key,
                self._ini_widget(setting))

    # ------------------------------------------------------------ о системе
    def _add_about_page(self) -> None:
        list_box = self._page("О системе")
        self._header(list_box, "ByteguruOS")
        self._row(list_box, "Версия", _read_version(), None)
        self._row(list_box, "Сеанс",
            f"{os.environ.get('XDG_SESSION_TYPE', '?')} на "
            f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')}", None)
        self._row(list_box, "Композитор",
            "bgwl — собственный, на wlroots. Потоки, раскладка окон, рамки и "
            "горячие клавиши принадлежат ему, а не чужому окружению", None)


def _read_version() -> str:
    for candidate in (Path("/usr/share/doc/byteguru-shell/version"),
            Path("VERSION")):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "1.0"


def build_window(application) -> object:
    window = SettingsWindow(application)
    window.window.present()
    return window.window


def main(argv: list[str] | None = None) -> int:
    return bggui.run_gtk(build_window, argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
