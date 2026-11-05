"""Лаунчер приложений: поиск, сетка, значки уровня совместимости.

Показывает вместе с Linux-приложениями и те, что требуют Windows, но честно:
значок L1/L2 — работает у нас, L3 — через виртуальную машину, L4 — через
перезагрузку в соседний раздел.
"""

from __future__ import annotations

from bgshell.ui import bggui

if bggui.Gtk is None:  # pragma: no cover — окружение без графического сеанса
    Gtk = None
else:
    Gtk = bggui.Gtk


CELL_PIXELS = 96
MAX_COLUMNS = 8


def build_window(application) -> object:
    session = bggui.connect_session()
    bggui.load_theme(None, session.theme_name)

    window = Gtk.ApplicationWindow(application=application)
    window.set_title("Программы — ByteguruOS")
    window.set_default_size(880, 620)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    window.set_child(box)

    search = Gtk.SearchEntry()
    search.set_placeholder_text("Поиск программ")
    search.set_margin_top(12)
    search.set_margin_start(18)
    search.set_margin_end(18)
    box.append(search)

    scroller = Gtk.ScrolledWindow()
    scroller.set_vexpand(True)
    # Горизонтального скролла нет: столбцы делят ширину окна поровну, поэтому
    # сетка никогда не «уезжает» за край на узком экране.
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    box.append(scroller)

    grid = Gtk.Grid()
    grid.set_column_spacing(12)
    grid.set_row_spacing(12)
    grid.set_column_homogeneous(True)
    grid.set_margin_top(8)
    grid.set_margin_bottom(16)
    scroller.set_child(grid)

    def refill(query: str = "") -> None:
        while grid.get_first_child() is not None:
            grid.remove(grid.get_first_child())
        apps = session.find(query) if query else session.apps
        for index, app in enumerate(apps):
            button = _make_cell(session, app)
            grid.attach(button, index % MAX_COLUMNS, index // MAX_COLUMNS, 1, 1)

    search.connect("search-changed", lambda entry: refill(entry.get_text()))
    status = Gtk.Label(label=f"Найдено программ: {len(session.apps)}")
    status.set_margin_bottom(8)
    box.append(status)
    search.connect("activate", lambda entry: (window.close(), _launch_selected(session, entry.get_text())))
    refill()
    window.present()
    return window


def _make_cell(session, app) -> object:
    Gtk_ = bggui.Gtk
    tile = Gtk_.Button()
    tile.set_size_request(CELL_PIXELS, CELL_PIXELS)
    tile.add_css_class("app-tile")
    box = Gtk_.Box(orientation=Gtk_.Orientation.VERTICAL, spacing=4)
    image = Gtk_.Image.new_from_icon_name(bggui.app_icon(app))
    image.set_pixel_size(48)
    box.append(image)
    title = Gtk_.Label(label=app.name)
    title.set_wrap(True)
    title.set_max_width_chars(12)
    title.add_css_class("app-title")
    box.append(title)
    text, css = bggui.coverage_badge(app)
    if text:
        badge = Gtk_.Label(label=text)
        badge.add_css_class("badge")
        badge.add_css_class(css)
        box.append(badge)
    tile.set_child(box)
    tile.connect("clicked", lambda _b: _launch(session, app))
    tooltip = Gtk_.Tooltip()
    tile.set_tooltip_text(_describe(app))
    return tile


def _describe(app) -> str:
    lines = [app.name]
    if app.publisher:
        lines.append(app.publisher)
    if app.coverage:
        lines.append(f"уровень совместимости: {app.coverage.upper()} — {app.evidence}")
    if app.needs_windows:
        lines.append("требует Windows: запуск через виртуальную машину или перезагрузку в соседний раздел")
    return "\n".join(lines)


def _launch(session, app) -> int:
    """Запуск строго через план демона: оболочка не выдумывает команды сама."""
    plan = session.service.launch_plan(app.id)
    if not plan.get("ok"):
        _error(session, app, plan.get("error") or plan.get("reason") or "запуск невозможен")
        return 1
    command = plan.get("command") or plan.get("raw_command")
    if not command:
        _error(session, app, "для этого приложения нужен отдельный путь запуска, он ещё не реализован")
        return 1
    return bggui.spawn(list(command))


def _launch_selected(session, query: str) -> int:
    found = session.find(query)
    if not found:
        return 1
    return _launch(session, found[0])


def _error(session, app, message: str) -> None:
    if bggui.Gtk is None:  # pragma: no cover
        return
    dialog = bggui.Gtk.MessageDialog(
        transient_for=None,
        modal=True,
        message_type=bggui.Gtk.MessageType.WARNING,
        buttons=bggui.Gtk.ButtonsType.OK,
        text=f"{app.name}: {message}",
    )
    dialog.connect("response", lambda dialog, _r: dialog.destroy())
    dialog.present()


def main() -> int:
    return bggui.run_gtk(build_window)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
