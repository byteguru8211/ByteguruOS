"""Guru Line — палитра действий ByteguruOS (Super+Esc).

Чем она не «ещё один лаунчер»: одна строка вводит и приложения, и потоки, и
действия системы. «разложить тайлом», «поток 3», «заблокировать», «снимок
области», «тёмная тема» — это команды того же сеанса, и искать их в меню
настроек дольше, чем написать три буквы.

Состав:

- приложения — из демона bgd (он объединяет .desktop, Flatpak и WCV и знает
  уровень покрытия; без демона читаем .desktop сами);
- потоки и раскладки — из композитора через bgshell.ctl;
- действия сеанса — блокировка, снимок, терминал, завершение;
- переключатели — тема, акцент, фокус-за-курсором, анимации.

Окно — layer-shell OVERLAY с EXCLUSIVE-клавиатурой: пока палитра открыта,
ни одно приложение не получает клавиши. Esc закрывает, Enter выполняет,
стрелки и PgUp/PgDn двигают выделение.

Ничего не выполняется «втихую»: в подписи результата видно команду или
D-Bus-метод, который будет вызов. Это тот же принцип, что и в bgd: пользователь
вправе знать, что именно запускается.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from bgshell import fuzzy, themes
from bgshell.ctl import Compositor, CompositorError
from bgshell.ui import bggui

PALETTE_NAMESPACE = "bgwl-palette"
PALETTE_WIDTH = 620
MAX_RESULTS = 12
# Строка палитры: 8 + 8 отступов, 20 пикселей иконки и две строки текста.
ROW_PIXELS = 56
# Откуда приложение — единственное, что интересно во второй строке. У
# системного .desktop источника нет, поэтому для него оставляем пустоту, а не
# служебное слово из демона.
SOURCE_LABELS = {"flatpak": "Flatpak", "wcv": "WinCompat"}


def _layer_shell():
    return bggui.LAYER_SHELL


@dataclass
class Action:
    ident: str
    title: str
    subtitle: str
    kind: str
    payload: str = ""

    def as_candidate(self, keywords: tuple[str, ...] = ()) -> fuzzy.Candidate:
        return fuzzy.Candidate(ident=self.ident, title=self.title,
            subtitle=self.subtitle, keywords=keywords, kind=self.kind,
            payload=self.payload or self.ident)


class GuruLine:
    def __init__(self, application, session):
        self.application = application
        self.session = session
        self.compositor = Compositor()
        self.snapshot = None
        self.results: list[fuzzy.Candidate] = []
        self.candidates: list[fuzzy.Candidate] = []

        self.window = bggui.build_window(application, "bglauncher")
        self.window.add_css_class("bg-palette")
        self._build()
        self._collect()
        self._refill()

    # ------------------------------------------------------------ каркас
    def _build(self) -> None:
        Gtk = bggui.Gtk
        layer = _layer_shell()

        self.window.set_size_request(PALETTE_WIDTH, -1)
        if layer is not None and bggui.layer_shell_supported():
            layer.init_for_window(self.window)
            layer.set_layer(self.window, layer.Layer.OVERLAY)
            for edge in (layer.Edge.TOP, layer.Edge.BOTTOM, layer.Edge.LEFT,
                    layer.Edge.RIGHT):
                layer.set_anchor(self.window, edge, False)
            layer.set_keyboard_mode(self.window, layer.KeyboardMode.EXCLUSIVE)
            layer.set_namespace(self.window, PALETTE_NAMESPACE)
            layer.set_margin(self.window, layer.Edge.TOP, 120)
        else:
            self.window.set_default_size(PALETTE_WIDTH, 480)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("bg-palette-body")
        self.window.set_child(box)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("приложение, поток, действие…")
        self.entry.set_has_frame(False)
        self.entry.add_css_class("bg-palette-entry")
        self.entry.connect("changed", lambda _w: self._refill())
        self.entry.connect("activate", lambda _w: self._activate_selected())
        box.append(self.entry)

        self.list = Gtk.ListBox()
        self.list.add_css_class("bg-palette-list")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated",
            lambda _l, row: self._activate_row(row))

        # Высота ограничена: без этого палитра на 51 приложении растягивается
        # на весь экран, и вместо «строки поверх окон» получается вторая
        # панель задач. ScrolledWindow с natural height даёт карточке расти
        # ровно до 12 строк.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(MAX_RESULTS * ROW_PIXELS)
        scroller.set_child(self.list)
        box.append(scroller)

        self.hint = Gtk.Label(label="Enter — выполнить · Esc — закрыть · "
            "↑↓ — выбор")
        self.hint.add_css_class("bg-muted")
        box.append(self.hint)

        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._on_key)
        self.window.add_controller(controller)

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        Gtk = bggui.Gtk
        if keyval == Gdk_keyval("Escape"):
            self.application.quit()
            return True
        if keyval in (Gdk_keyval("Down"), Gdk_keyval("Page_Down")):
            self._move(1)
            return True
        if keyval in (Gdk_keyval("Up"), Gdk_keyval("Page_Up")):
            self._move(-1)
            return True
        if keyval == Gdk_keyval("Tab"):
            self._move(1)
            return True
        if keyval in (Gdk_keyval("Return"), Gdk_keyval("KP_Enter")):
            self._activate_selected()
            return True
        return False

    # ------------------------------------------------------------ содержимое
    def _collect(self) -> None:
        candidates: list[fuzzy.Candidate] = []
        candidates.extend(self._app_candidates())
        candidates.extend(self._stream_candidates())
        candidates.extend(self._layout_candidates())
        candidates.extend(self._session_candidates())
        candidates.extend(self._toggle_candidates())
        self.candidates = candidates

    def _app_candidates(self) -> list[fuzzy.Candidate]:
        out: list[fuzzy.Candidate] = []
        try:
            apps = list(self.session.apps)
        except Exception:  # noqa: BLE001 — демон может быть не запущен
            apps = []
        for app in apps:
            badge, _css = bggui.coverage_badge(app)
            # Подпись не повторяет название. В палитре она отвечает на вопрос
            # «откуда это», поэтому издатель (или источник) и уровень покрытия
            # полезнее второй строки с тем же текстом.
            origin = app.publisher or SOURCE_LABELS.get(app.source, "")
            details = " · ".join(filter(None, (origin, badge)))
            out.append(fuzzy.Candidate(
                ident=f"app:{app.id}",
                title=app.name,
                subtitle=details,
                keywords=tuple(filter(None, (app.id, *app.tags))),
                kind="app",
                payload=app.id,
                icon=bggui.app_icon(app),
            ))
        return out

    def _stream_candidates(self) -> list[fuzzy.Candidate]:
        out: list[fuzzy.Candidate] = []
        if self.snapshot is None:
            return out
        for stream in self.snapshot.streams:
            out.append(fuzzy.Candidate(
                ident=f"stream:{stream.index}",
                title=f"поток: {stream.name}",
                subtitle=f"{stream.layout}, окон {stream.windows}",
                keywords=("поток", "stream", "рабочее место", "переключить"),
                kind="stream",
                payload=str(stream.index),
            ))
        return out

    def _layout_candidates(self) -> list[fuzzy.Candidate]:
        return [
            fuzzy.Candidate(ident=f"layout:{name}",
                title=f"разложить: {title}",
                subtitle=f"bgctl set ws-layout {name}",
                keywords=("раскладка", "окна", "layout", "тайл", "плавающие"),
                kind="layout", payload=name)
            for name, title in (("tile", "тайлом (мастер + стек)"),
                ("even", "равной сеткой"), ("float", "плавающими"))
        ]

    def _session_candidates(self) -> list[fuzzy.Candidate]:
        return [
            fuzzy.Candidate(ident="action:terminal", title="терминал",
                subtitle="bgctl run terminal", keywords=("консоль", "shell", "foot"),
                kind="action", payload="terminal"),
            fuzzy.Candidate(ident="action:lock", title="заблокировать экран",
                subtitle="bgctl lock", keywords=("лок", "пароль", "экран"),
                kind="action", payload="lock"),
            fuzzy.Candidate(ident="action:screenshot",
                title="снимок области экрана", subtitle="grim + slurp",
                keywords=("скриншот", "фото", "screen"), kind="action",
                payload="screenshot"),
            fuzzy.Candidate(ident="action:files", title="файлы",
                subtitle="bgctl run file-manager", keywords=("thunar", "папки"),
                kind="action", payload="file-manager"),
            fuzzy.Candidate(ident="action:quit", title="завершить сеанс",
                subtitle="bgctl quit", keywords=("выход", "logout"), kind="action",
                payload="quit"),
        ]

    def _toggle_candidates(self) -> list[fuzzy.Candidate]:
        dark = self.session.settings.get("theme.mode") != "light"
        return [
            fuzzy.Candidate(ident="toggle:theme",
                title=("светлая тема" if dark else "тёмная тема"),
                subtitle="theme.mode + акцент композитора",
                keywords=("тема", "тёмная", "светлая", "dark", "light"),
                kind="toggle", payload="theme"),
            fuzzy.Candidate(ident="toggle:animations",
                title="анимации: переключить", subtitle="bgctl set animations",
                keywords=("анимация", "motion"), kind="toggle",
                payload="animations"),
            fuzzy.Candidate(ident="toggle:follows",
                title="фокус следует за курсором: переключить",
                subtitle="bgctl set focus-follows-mouse",
                keywords=("фокус", "курсор"), kind="toggle", payload="follows"),
        ]

    def _refill(self) -> None:
        Gtk = bggui.Gtk
        query = self.entry.get_text()
        try:
            self.snapshot = self.compositor.state()
        except CompositorError:
            self.snapshot = None

        self.results = fuzzy.rank(self.candidates, query, MAX_RESULTS)
        while self.list.get_first_child() is not None:
            self.list.remove(self.list.get_first_child())
        for candidate in self.results:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(12)
            box.set_margin_end(12)
            mark = Gtk.Image.new_from_icon_name(
                candidate.icon or KIND_ICONS.get(candidate.kind,
                    "application-x-executable-symbolic"))
            mark.set_pixel_size(20)
            mark.add_css_class("bg-palette-kind")
            box.append(mark)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.set_hexpand(True)
            title = Gtk.Label(label=candidate.title)
            title.set_xalign(0.0)
            title.add_css_class("heading")
            text.append(title)
            if candidate.subtitle:
                subtitle = Gtk.Label(label=candidate.subtitle)
                subtitle.set_xalign(0.0)
                subtitle.add_css_class("bg-muted")
                text.append(subtitle)
            box.append(text)
            row.set_child(box)
            row.candidate = candidate  # type: ignore[attr-defined]
            self.list.append(row)

        first = self.list.get_row_at_index(0)
        if first is not None:
            self.list.select_row(first)

    # ------------------------------------------------------------ запуск
    def _move(self, step: int) -> None:
        current = self.list.get_selected_row()
        index = current.get_index() if current is not None else 0
        target = max(0, min(self.list.get_last_child_index(), index + step))
        row = self.list.get_row_at_index(target)
        if row is not None:
            self.list.select_row(row)

    def _activate_selected(self) -> None:
        self._activate_row(self.list.get_selected_row())

    def _activate_row(self, row) -> None:
        candidate = getattr(row, "candidate", None) if row is not None else None
        if candidate is None:
            return
        if self._run(candidate):
            self.application.quit()

    def _run(self, candidate: fuzzy.Candidate) -> bool:
        kind = candidate.kind
        if kind == "app":
            return self._launch_app(candidate.payload)
        if kind == "stream":
            self._ask(f"workspace {candidate.payload}")
            return True
        if kind == "layout":
            self._ask(f"set ws-layout {candidate.payload}")
            return True
        if kind == "action":
            # Команда идёт композитору, а не в подпроцесс bgctl: палитра уже
            # знает сокет, а лишний fork на каждый запуск терминала — это
            # ещё и зависимость от PATH.
            self._ask(f"run {candidate.payload}")
            return True
        if kind == "toggle":
            return self._toggle(candidate.payload)
        return False

    def _toggle(self, what: str) -> bool:
        if what == "theme":
            dark = self.session.settings.get("theme.mode") != "light"
            palette = themes.Palette(
                accent=str(self.session.settings.get("theme.accent")
                    or themes.DEFAULT_ACCENT), dark=not dark)
            themes.apply(palette, self.compositor)
            self.session.settings.set("theme.mode",
                "dark" if palette.dark else "light")
            self.session.settings.set("theme.name",
                "byteguru-dark" if palette.dark else "byteguru-light")
            self.session.settings.save()
            return True
        if what == "animations":
            current = bool(self.snapshot.settings.get("animations")) \
                if self.snapshot else True
            self._ask(f"set animations {'false' if current else 'true'}")
            return True
        if what == "follows":
            current = bool(self.snapshot.settings.get("focus-follows-mouse")) \
                if self.snapshot else False
            self._ask(f"set focus-follows-mouse {'false' if current else 'true'}")
            return True
        return False

    def _launch_app(self, app_id: str) -> bool:
        """Запуск по плану bgd.

        План возвращает сам демон (flatpak run / gtk-launch / wine-скрипт), и
        именно его мы исполняем: палитра не должна знать, как устроено
        приложение, иначе две реализации одного запуска разойдутся.
        """
        import shlex  # noqa: PLC0415

        try:
            plan = self.session.service.launch_plan(app_id)
        except Exception:  # noqa: BLE001 — демон мог не подняться
            return self._launch_desktop_fallback(app_id)
        if not plan.get("ok"):
            self.hint.set_text(plan.get("error", "план запуска пуст"))
            return False
        command = plan.get("command") or ""
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            return False
        try:
            subprocess.Popen(argv, start_new_session=True)
        except OSError as error:
            self.hint.set_text(f"не удалось запустить: {error}")
            return False
        return True

    def _launch_desktop_fallback(self, app_id: str) -> bool:
        try:
            subprocess.Popen(["gtk-launch", app_id], start_new_session=True)
        except OSError:
            return False
        return True

    def _ask(self, command: str) -> None:
        try:
            self.compositor.command(command)
        except CompositorError as error:
            self.hint.set_text(f"композитор отклонил «{command}»: {error}")


KIND_ICONS = {
    "app": "application-x-executable-symbolic",
    "stream": "view-list-symbolic",
    "layout": "view-grid-symbolic",
    "action": "system-run-symbolic",
    "toggle": "go-jump-symbolic",
}


def Gdk_keyval(name: str) -> int:
    from gi.repository import Gdk  # noqa: PLC0415 — только в сеансе

    return Gdk.keyval_from_name(name)


def build_palette(application) -> object:
    session = bggui.connect_session()
    bggui.load_theme(None, str(session.settings.get("theme.name")))
    palette = GuruLine(application, session)
    palette.window.present()
    palette.entry.grab_focus()
    return palette.window


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else [])
    if "--grid" in argv:
        from bgshell.ui import launcher  # noqa: PLC0415

        return launcher.main([a for a in argv if a != "--grid"])
    return bggui.run_gtk(build_palette, argv)


if __name__ == "__main__":
    raise SystemExit(main())
