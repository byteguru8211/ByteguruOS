"""Панель ByteguruOS.

Слоистое окно (wlr-layer-shell) поверх композитора bgwl. Её namespace
начинается с BGWL_PANEL_NAMESPACE_PREFIX — только так композитор признаёт в ней
панель и отдаёт ей эксклюзивную зону, чтобы окна её не перекрывали. Если
layer-shell недоступен (X11, другой композитор, съёмка скриншота для демо)
окно остаётся обычным: оболочка не должна зависеть от одного протокола, чтобы
её можно было показать человеку.

Что панель делает:

- показывает состояние композитора (потоки и окна) и меняет его теми же
  командами, которые набирает человек в `bgctl`;
- собирает индикаторы машины (сеть, батарея, звук, яркость) из уже запущенных
  демонов;
- даёт быстрые настройки: тёмное/светлое, акцент, раскладка окон, промежутки,
  громкость, яркость, блокировка.

Чего она не делает:

- не хранит состояние. Единственный источник истины — композитор и демоны; всё,
  что панель помнит между кадрами, — последний снимок;
- не показывает то, чего не может знать. Индикатора раскладки клавиатуры здесь
  нет намеренно: клиент Wayland не видит активную группу xkb без отдельного
  протокола, а рисовать «угаданную» раскладку — значит врать пользователю.

Весь ввод идёт из цикла GTK: события сокета через EventSource, часы и индикаторы
по таймерам. Отдельных потоков нет: виджеты GTK нельзя трогать из чужого потока,
а гонка в панели — это упавший сеанс.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from bgshell import panel as panel_model
from bgshell import systeminfo, themes
from bgshell.ctl import Compositor, CompositorError, EventSource, Snapshot
from bgshell.ui import bggui

PANEL_NAMESPACE = "bgwl-panel"
PANEL_HEIGHT = 34
ACCENT_CHOICES = ("#f2a33d", "#4a9eff", "#3ecf8e", "#ff5c7a", "#b98cff", "#ffd166")
LAYOUTS = ("float", "tile", "even")
# История уведомлений принадлежит bgnote, который пишет этот файл; панель его
# только читает. Свой D-Bus-сервер внутри панели означал бы, что уведомления
# исчезают вместе с панелью.
NOTIFICATIONS_FILE = Path.home() / ".local/share/byteguru/notifications.json"
STATUS_INTERVAL_SECONDS = 5
CLOCK_INTERVAL_SECONDS = 10


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


class Panel:
    def __init__(self, application, session: bggui.Session):
        self.application = application
        self.session = session
        self.settings = session.settings
        self.compositor = Compositor()
        self.model = panel_model.PanelModel(clock=datetime.now())
        self.snapshot: Snapshot | None = None
        self.info = systeminfo.SystemInfo()
        self.events: EventSource | None = None
        self.widgets: dict[str, object] = {}
        self.palette = themes.from_settings(self.settings.effective())
        self._pending_refresh = False
        self._missing_since = 0.0
        self._dot_css_installed = ""

        # Тема применяется до первого кадра: иначе панель на несколько секунд
        # становится светлой полосой на тёмном столе — ровно то, что видно на
        # скриншотах первого запуска.
        themes.apply(self.palette, self.compositor)
        bggui.load_theme(None, str(self.settings.get("theme.name")))

        self.window = bggui.build_window(application, "bgpanel")
        self.window.add_css_class("bg-panel")
        self._build()
        self.poll_status()
        self.refresh()
        self.tick_clock()
        self._start_sources()

    # ------------------------------------------------------------ построение
    def _build(self) -> None:
        Gtk = bggui.Gtk
        layer = bggui.LAYER_SHELL

        self.window.set_size_request(-1, PANEL_HEIGHT)
        if layer is not None and bggui.layer_shell_supported():
            layer.init_for_window(self.window)
            layer.set_layer(self.window, layer.Layer.TOP)
            layer.set_anchor(self.window, layer.Edge.TOP, True)
            layer.set_anchor(self.window, layer.Edge.LEFT, True)
            layer.set_anchor(self.window, layer.Edge.RIGHT, True)
            # Эксклюзивная зона — то, из-за чего панель вообще слой: композитор
            # вычитает её из usable-области, и окна под неё не заезжают.
            layer.set_exclusive_zone(self.window, PANEL_HEIGHT)
            layer.set_namespace(self.window, PANEL_NAMESPACE)
            layer.set_keyboard_mode(self.window, layer.KeyboardMode.ON_DEMAND)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        self.window.set_child(bar)

        guru = Gtk.Button()
        guru.set_child(bggui.icon_or_label("open-menu-symbolic", "Byteguru", 14))
        guru.add_css_class("flat")
        guru.add_css_class("bg-guru")
        guru.set_tooltip_text("Guru Line — поиск приложений и действий")
        guru.connect("clicked", lambda _b: self._open_launcher())
        bar.append(guru)

        streams = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.append(streams)
        self.widgets["streams"] = streams

        tasks = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        tasks.set_hexpand(True)
        tasks.set_halign(Gtk.Align.START)
        bar.append(tasks)
        self.widgets["tasks"] = tasks

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.append(status)
        self.widgets["network"] = self._status_button(status,
            "network-wired-disconnected-symbolic", self._open_network_menu)
        self.widgets["audio"] = self._status_button(status,
            "audio-volume-muted-symbolic", self._open_quick_settings)
        self.widgets["battery"] = self._status_button(status,
            "battery-missing-symbolic", self._open_quick_settings)

        clock = Gtk.Button()
        clock.add_css_class("flat")
        clock.add_css_class("bg-clock")
        clock.set_child(Gtk.Label(label="--:--"))
        clock.connect("clicked", lambda _b: self._open_calendar())
        status.append(clock)
        self.widgets["clock"] = clock

        self.widgets["bell"] = self._status_button(status,
            "notifications-symbolic", self._open_notifications)
        self.widgets["power"] = self._status_button(status,
            "system-shutdown-symbolic", self._open_power_menu)

    def _status_button(self, parent, icon_name: str, handler) -> object:
        Gtk = bggui.Gtk
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("bg-status")
        button.set_child(bggui.icon_or_label(icon_name, ""))
        button.connect("clicked", lambda _b: handler(button))
        parent.append(button)
        return button

    # ------------------------------------------------------------ источники
    def _start_sources(self) -> None:
        GLib = bggui.GLib
        self.events = EventSource(self._on_compositor_event)
        self.events.start()
        GLib.timeout_add_seconds(CLOCK_INTERVAL_SECONDS, self._on_clock_timer)
        GLib.timeout_add_seconds(STATUS_INTERVAL_SECONDS, self._on_status_timer)

    def _on_clock_timer(self) -> bool:
        self.tick_clock()
        return True

    def _on_status_timer(self) -> bool:
        self.poll_status()
        return True

    def _on_compositor_event(self, _kind: str) -> None:
        """Композитор сообщил, что что-то изменилось.

        Тип события не разбирается: снимок читается целиком, а поддерживать два
        описания одного состояния — это путь к расхождению панели и экрана.
        """
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        if self._pending_refresh:
            return
        self._pending_refresh = True
        bggui.GLib.idle_add(self._refresh_when_idle)

    def _refresh_when_idle(self) -> bool:
        self._pending_refresh = False
        self.refresh()
        return False

    # ------------------------------------------------------------ содержимое
    def refresh(self) -> None:
        try:
            self.snapshot = self.compositor.state()
        except CompositorError as error:
            self._show_compositor_missing(str(error))
            return
        self._missing_since = 0.0
        self._install_dot_css()
        self._render_streams()
        self._render_tasks()

    def _show_compositor_missing(self, reason: str) -> None:
        """Панель переживает смерть композитора: часы и индикаторы работают,
        вместо граф — одна понятная плашка. Молча опустевшая панель выглядит
        как зависшая система."""
        if time.monotonic() - self._missing_since < 2.0:
            return
        self._missing_since = time.monotonic()

        Gtk = bggui.Gtk
        for name in ("streams", "tasks"):
            box = self.widgets[name]
            while box.get_first_child() is not None:
                box.remove(box.get_first_child())
        label = Gtk.Label(label="композитор не отвечает")
        label.add_css_class("bg-warn")
        label.set_tooltip_text(reason)
        self.widgets["streams"].append(label)

    def _install_dot_css(self) -> None:
        """Цвета точек потоков — один CSS-провайдер на весь список.

        GTK 4 не отдаёт per-widget CssProvider так, чтобы это было дешёвым, а
        поток у нас максимум десять: класс bg-dot-<i> на каждый индекс
        перерисовывается одним провайдером при смене палитры.
        """
        Gtk = bggui.Gtk
        rules = []
        for stream in self.snapshot.streams if self.snapshot else []:
            rules.append(f".bg-dot-{stream.index} {{ background-color: "
                f"{stream.accent or self.palette.accent}; }}")
        css = "\n".join(rules)
        if css == self._dot_css_installed:
            return
        self._dot_css_installed = css
        provider = Gtk.CssProvider.new()
        provider.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            bggui.Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _render_streams(self) -> None:
        Gtk = bggui.Gtk
        box = self.widgets["streams"]
        while box.get_first_child() is not None:
            box.remove(box.get_first_child())
        if self.snapshot is None:
            return

        for stream in self.snapshot.streams:
            button = Gtk.Button()
            button.add_css_class("bg-pill")
            if stream.active:
                button.add_css_class("bg-pill-active")

            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            dot = Gtk.Box()
            dot.set_size_request(8, 8)
            dot.set_valign(Gtk.Align.CENTER)
            dot.add_css_class("bg-stream-dot")
            dot.add_css_class(f"bg-dot-{stream.index}")
            content.append(dot)
            content.append(Gtk.Label(label=shorten(stream.name, 14)))
            if stream.windows:
                count = Gtk.Label(label=str(stream.windows))
                count.add_css_class("bg-muted")
                content.append(count)
            button.set_child(content)
            button.set_tooltip_text(
                f"{stream.name} · {stream.layout} · окон {stream.windows}\n"
                "правая кнопка — имя, раскладка, акцент")
            button.connect("clicked",
                lambda _b, index=stream.index: self._ask(f"workspace {index}"))
            self._attach_right_click(button,
                lambda _gesture, _x, _y, s=stream, b=button:
                    self._open_stream_menu(s, b))
            box.append(button)

    def _render_tasks(self) -> None:
        Gtk = bggui.Gtk
        box = self.widgets["tasks"]
        while box.get_first_child() is not None:
            box.remove(box.get_first_child())
        if self.snapshot is None:
            return
        active = self.snapshot.active_stream
        if active is None:
            return

        for window in self.snapshot.windows_on(active.index):
            button = Gtk.Button()
            button.add_css_class("flat")
            button.add_css_class("bg-task")
            if window.focused:
                button.add_css_class("bg-task-focused")
            marks = "".join(filter(None, (
                "⧉" if window.above else "",
                "❐" if window.maximized else "",
                "⛶" if window.fullscreen else "",
                "◆" if window.sticky else "",
            )))
            prefix = f"{marks} " if marks else ""
            button.set_child(Gtk.Label(label=f"{prefix}{shorten(window.label, 24)}"))
            button.set_tooltip_text(f"{window.app_id} · {window.title}")
            button.connect("clicked",
                lambda _b, wid=window.id: self._ask(f"focus {wid}"))
            self._attach_right_click(button,
                lambda _g, _x, _y, w=window, b=button:
                    self._open_window_menu(w, b))
            box.append(button)

    def poll_status(self) -> None:
        self.info = systeminfo.collect()
        self._render_status()

    def _render_status(self) -> None:
        network, battery, audio = self.info.network, self.info.battery, self.info.audio

        self._set_icon("network", network.icon if network
            else "network-wired-disconnected-symbolic")
        self.widgets["network"].set_tooltip_text(
            network.label if network else "сеть недоступна")

        self._set_icon("audio", audio.icon if audio else "audio-volume-muted-symbolic")
        self.widgets["audio"].set_tooltip_text(
            f"громкость {audio.volume}%" + (" · заглушено" if audio.muted else "")
            if audio else "звук недоступен")

        battery_button = self.widgets["battery"]
        if battery is not None and battery.present:
            battery_button.set_visible(True)
            self._set_icon("battery", battery.icon)
            battery_button.set_tooltip_text(battery.label)
        else:
            battery_button.set_visible(False)

        unread = len(self._load_notifications())
        bell = self.widgets["bell"]
        bell.set_tooltip_text(f"уведомления: {unread}" if unread else "уведомлений нет")
        if unread:
            bell.add_css_class("bg-bell-active")
        else:
            bell.remove_css_class("bg-bell-active")

    def _set_icon(self, name: str, icon_name: str) -> None:
        button = self.widgets.get(name)
        if button is None:
            return
        image = self._find_image(button)
        if image is not None:
            image.set_from_icon_name(icon_name)
            return
        # Иконки в теме нет: заменяем виджет текстом, чтобы вместо пустого
        # квадрата пользователь видел хотя бы две буквы.
        if bggui.has_icon(icon_name):
            return
        fallback = Gtk.Label(label=icon_name.split("-")[0][:3])
        fallback.add_css_class("bg-muted")
        button.set_child(fallback)

    def _find_image(self, widget):
        """Первое Image где-то в виджете: панель живёт в Gtk.Box любой глубины."""
        if hasattr(widget, "set_from_icon_name"):
            return widget
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child is not None:
            found = self._find_image(child)
            if found is not None:
                return found
            child = child.get_next_sibling()
        return None

    def tick_clock(self) -> None:
        self.model.clock = datetime.now()
        label = self.widgets["clock"].get_child()
        if label is not None and hasattr(label, "set_text"):
            label.set_text(self.model.clock_text())
        self.widgets["clock"].set_tooltip_text(self.model.date_text())

    # ------------------------------------------------------------ команды
    def _ask(self, command: str) -> None:
        try:
            reply = self.compositor.command(command)
        except CompositorError as error:
            self._toast(f"композитор отклонил команду: {error}")
            return
        self.snapshot = Snapshot.from_state(reply)
        self._install_dot_css()
        self._render_streams()
        self._render_tasks()

    def _open_launcher(self) -> None:
        launcher = "bglauncher"
        if self.snapshot is not None:
            launcher = str(self.snapshot.settings.get("launcher") or launcher)
        bggui.run_command(launcher)

    def _attach_right_click(self, widget, handler) -> None:
        """GTK 4 не даёт сигнал right-click на кнопке: жест — единственный
        способ отличить контекстное меню от обычного нажатия."""
        gesture = bggui.Gtk.GestureClick.new()
        gesture.set_button(3)
        gesture.connect("pressed", handler)
        widget.add_controller(gesture)

    def _toast(self, text: str) -> None:
        """Короткое сообщение без модального диалога: панель не имеет права
        блокировать работу человека ради одной ошибки."""
        Gtk = bggui.Gtk
        popover = Gtk.Popover.new(self.widgets["power"])
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_max_width_chars(48)
        label.add_css_class("bg-muted")
        popover.set_child(label)
        popover.present()
        bggui.GLib.timeout_add_seconds(5, popover.dismiss)

    # ------------------------------------------------------------ меню
    def _popover(self, relative_to, builder) -> None:
        Gtk = bggui.Gtk
        popover = Gtk.Popover.new(relative_to)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(10)
        content.set_margin_end(10)
        builder(content)
        popover.set_child(content)
        popover.present()

    def _open_stream_menu(self, stream, button) -> None:
        Gtk = bggui.Gtk

        def build(content) -> None:
            title = Gtk.Label(label=f"поток «{stream.name}»")
            title.add_css_class("heading")
            content.append(title)

            entry = Gtk.Entry()
            entry.set_text(stream.name)
            entry.set_placeholder_text("имя потока")
            content.append(entry)
            rename = Gtk.Button(label="переименовать")
            rename.connect("clicked",
                lambda _b: self._ask(f"set ws-name {entry.get_text().strip()}")
                if entry.get_text().strip() else None)
            content.append(rename)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for layout in LAYOUTS:
                item = Gtk.Button(label=layout)
                item.add_css_class("bg-pill")
                if layout == stream.layout:
                    item.add_css_class("bg-pill-active")
                item.connect("clicked",
                    lambda _b, value=layout: self._ask(f"set ws-layout {value}"))
                row.append(item)
            content.append(row)

            accents = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for color in ACCENT_CHOICES:
                swatch = Gtk.Button()
                swatch.set_size_request(22, 22)
                swatch.add_css_class("bg-swatch")
                swatch.set_tooltip_text(color)
                swatch.connect("clicked",
                    lambda _b, value=color: self._ask(f"set ws-accent {value}"))
                accents.append(swatch)
            content.append(accents)

            send = Gtk.Button(label="перенести сюда активное окно")
            send.connect("clicked",
                lambda _b: self._ask(f"send {stream.index}"))
            content.append(send)

        self._popover(button, build)

    def _open_window_menu(self, window, button) -> None:
        Gtk = bggui.Gtk
        actions = (
            ("закрыть", "close"),
            ("развернуть / свернуть", "maximize"),
            ("во весь экран", "fullscreen"),
            ("прикрепить сверху", "pin"),
            ("перенести в следующий поток", "send next"),
        )

        def build(content) -> None:
            title = Gtk.Label(label=shorten(window.label, 40))
            title.add_css_class("heading")
            content.append(title)
            for label, command in actions:
                item = Gtk.Button(label=label)
                item.add_css_class("flat")
                item.connect("clicked", lambda _b, cmd=command: self._ask(cmd))
                content.append(item)

        self._popover(button, build)

    def _open_quick_settings(self, button) -> None:
        Gtk = bggui.Gtk

        def build(content) -> None:
            mode = Gtk.ToggleButton(label="тёмное оформление")
            mode.set_active(self.palette.dark)
            mode.connect("toggled", lambda toggle: self.apply_palette(
                themes.Palette(accent=self.palette.accent,
                    dark=toggle.get_active())))
            content.append(mode)

            accents = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for color in ACCENT_CHOICES:
                swatch = Gtk.Button()
                swatch.set_size_request(24, 24)
                swatch.add_css_class("bg-swatch")
                swatch.set_tooltip_text(color)
                swatch.connect("clicked", lambda _b, value=color: self.apply_palette(
                    themes.Palette(accent=value, dark=self.palette.dark)))
                accents.append(swatch)
            content.append(self._titled("Акцент", accents))

            layouts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            current_layout = ""
            if self.snapshot is not None:
                active = self.snapshot.active_stream
                current_layout = active.layout if active else ""
            for layout in LAYOUTS:
                item = Gtk.Button(label=layout)
                item.add_css_class("bg-pill")
                if layout == current_layout:
                    item.add_css_class("bg-pill-active")
                item.connect("clicked",
                    lambda _b, value=layout: self._ask(f"set ws-layout {value}"))
                layouts.append(item)
            content.append(self._titled("Раскладка окон в потоке", layouts))

            if self.snapshot is not None:
                gaps = Gtk.SpinButton.new_with_range(0, 40, 2)
                gaps.set_value(int(self.snapshot.settings.get("gap-inner", 6)))
                gaps.connect("value-changed",
                    lambda spin: self._ask(f"set gap-inner {int(spin.get_value())}"))
                content.append(self._titled("Промежутки", gaps))

            if self.info.audio is not None:
                volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                    0, 100, 5)
                volume.set_value(self.info.audio.volume)
                volume.connect("value-changed",
                    lambda scale: self._set_volume(int(scale.get_value())))
                mute = Gtk.Button(label="выключить звук")
                mute.connect("clicked", lambda _b: (
                    systeminfo.toggle_audio_mute(), self.poll_status()))
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                row.append(volume)
                row.append(mute)
                content.append(self._titled("Звук", row))

            if self.info.brightness is not None:
                brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                    5, 100, 5)
                brightness.set_value(self.info.brightness)
                brightness.connect("value-changed",
                    lambda scale: systeminfo.set_brightness(int(scale.get_value())))
                content.append(self._titled("Яркость", brightness))

            lock = Gtk.Button(label="заблокировать экран")
            lock.connect("clicked", lambda _b: self._ask("lock"))
            content.append(lock)

        self._popover(button, build)

    def _set_volume(self, percent: int) -> None:
        systeminfo.set_audio_volume(percent)
        self.poll_status()

    def _titled(self, label: str, child) -> object:
        Gtk = bggui.Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=label)
        title.add_css_class("bg-muted")
        title.set_xalign(0.0)
        box.append(title)
        box.append(child)
        return box

    def _open_power_menu(self, button) -> None:
        items = (
            ("заблокировать экран", lambda: self._ask("lock")),
            ("выйти из сеанса", lambda: self._ask("quit")),
            ("перезагрузить", lambda: bggui.run_command("systemctl reboot")),
            ("выключить", lambda: bggui.run_command("systemctl poweroff")),
        )

        def build(content) -> None:
            Gtk = bggui.Gtk
            for label, handler in items:
                item = Gtk.Button(label=label)
                item.add_css_class("flat")
                item.connect("clicked", lambda _b: handler())
                content.append(item)

        self._popover(button, build)

    def _open_network_menu(self, button) -> None:
        Gtk = bggui.Gtk

        def build(content) -> None:
            network = self.info.network
            title = Gtk.Label(label=network.label if network else "сеть недоступна")
            title.add_css_class("heading")
            content.append(title)
            editor = Gtk.Button(label="настроить подключения")
            editor.connect("clicked",
                lambda _b: bggui.run_command("nm-connection-editor"))
            content.append(editor)

        self._popover(button, build)

    def _open_calendar(self) -> None:
        Gtk = bggui.Gtk

        def build(content) -> None:
            title = Gtk.Label(label=self.model.date_text())
            title.add_css_class("heading")
            content.append(title)
            clock = Gtk.Label(label=self.model.clock_text())
            clock.add_css_class("large-title")
            content.append(clock)
            battery = self.info.battery
            if battery is not None and battery.present:
                content.append(Gtk.Label(label=battery.label))

        self._popover(self.widgets["clock"], build)

    def _open_notifications(self, button) -> None:
        Gtk = bggui.Gtk

        def build(content) -> None:
            entries = self._load_notifications()
            if not entries:
                content.append(Gtk.Label(label="уведомлений нет"))
                return
            for entry in list(reversed(entries))[:12]:
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                title = Gtk.Label(label=str(entry.get("title", "")))
                title.add_css_class("heading")
                title.set_xalign(0.0)
                body = Gtk.Label(label=str(entry.get("body", "")))
                body.set_xalign(0.0)
                body.set_wrap(True)
                body.add_css_class("bg-muted")
                row.append(title)
                row.append(body)
                content.append(row)
            clear = Gtk.Button(label="очистить")
            clear.connect("clicked", lambda _b: self._clear_notifications())
            content.append(clear)

        self._popover(button, build)

    def _load_notifications(self) -> list[dict]:
        try:
            data = json.loads(NOTIFICATIONS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _clear_notifications(self) -> None:
        try:
            NOTIFICATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            NOTIFICATIONS_FILE.write_text("[]", encoding="utf-8")
        except OSError:
            return
        self._render_status()

    # ------------------------------------------------------------ палитра
    def apply_palette(self, palette: themes.Palette) -> None:
        """Меняет тему целиком: настройки, CSS, конфиг и акцент композитора.

        Порядок важен: bgwl.set accent рисует рамку фокуса немедленно, а CSS
        подхватывается при следующем подключении providers.
        """
        self.palette = palette
        self.settings.set("theme.mode", "dark" if palette.dark else "light")
        self.settings.set("theme.accent", palette.accent)
        self.settings.set("theme.name",
            "byteguru-dark" if palette.dark else "byteguru-light")
        self.settings.save()

        streams = [(stream.index, stream.name, stream.accent)
            for stream in (self.snapshot.streams if self.snapshot else [])]
        for note in themes.apply(palette, self.compositor, streams):
            if note.startswith("compositor:"):
                self._toast(note)

        bggui.load_theme(None, str(self.settings.get("theme.name")))
        if self.snapshot is not None:
            self._install_dot_css()


def build_panel(application) -> object:
    session = bggui.connect_session()
    panel = Panel(application, session)
    panel.window.present()
    return panel.window


def main(argv: list[str] | None = None) -> int:
    return bggui.run_gtk(build_panel, argv)

if __name__ == "__main__":
    raise SystemExit(main())
