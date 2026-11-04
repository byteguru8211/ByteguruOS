"""Экран блокировки ByteguruOS (bglock).

Как это работает и почему именно так:

1. окно — layer-shell в слое OVERLAY на все края, с keyboard mode EXCLUSIVE.
   EXCLUSIVE здесь не косметика: он означает, что композитор отдаёт клавиатуру
   только нам, и ни одно приложение не узнает, что печатает пользователь;
2. сразу после появления окна мы говорим композитору `lock begin` — он гасит
   деревья рабочего стола и перестаёт обрабатывать горячие клавиши. Порядок
   важен: если сначала прятать стол, между скрытием и нашим map окно ввода
   осталось бы у предыдущего приложения;
3. аутентификация идёт через `sudo -S -v`. В образе нет привязок PAM для
   Python, а sudo стоит всегда и спрашивает пароль именно у PAM. После
   проверки билет сбрасывается (`sudo -k`), чтобы локер не оставлял
   пользователю права на root-команды без пароля;
4. при успешном вводе — `lock end` и выход. Если композитор умер или окно
   закрылось иначе, bgwl снимает блокировку сам (layer.c): зависший локер не
   имеет права оставить человека с чёрным экраном.

Никаких «безопасных» обещаний здесь нет: это блокировка сеанса от случайного
нажатия и от ушедшего в другой конец комнаты пользователя, а не защита от
того, у кого есть физический доступ к машине и загрузочная флешка.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime

from bgshell import panel as panel_model
from bgshell.ctl import Compositor, CompositorError
from bgshell.ui import bggui

LOCK_NAMESPACE = "bgwl-lock"
# Подсказка под полем: пароль проверяется через sudo, и пользователь должен
# видеть, ЧЕЙ именно пароль система ждёт.
MAX_ATTEMPTS_BEFORE_HINT = 2


def _layer_shell():
    return bggui.LAYER_SHELL


class Locker:
    def __init__(self, application):
        self.application = application
        self.compositor = Compositor()
        self.model = panel_model.PanelModel(clock=datetime.now())
        self.failed = 0
        self.window = bggui.build_window(application, "bglock")
        self.window.add_css_class("bg-lock")
        self._build()

    # ------------------------------------------------------------ вид
    def _build(self) -> None:
        Gtk = bggui.Gtk
        layer = _layer_shell()

        if layer is not None and bggui.layer_shell_supported():
            layer.init_for_window(self.window)
            layer.set_layer(self.window, layer.Layer.OVERLAY)
            for edge in (layer.Edge.TOP, layer.Edge.BOTTOM, layer.Edge.LEFT,
                    layer.Edge.RIGHT):
                layer.set_anchor(self.window, edge, True)
            # EXCLUSIVE: клавиатура не уходит ни одному приложению, пока мы
            # живы. Без этого пароль можно было бы ввести в чужое окно.
            layer.set_keyboard_mode(self.window, layer.KeyboardMode.EXCLUSIVE)
            layer.set_namespace(self.window, LOCK_NAMESPACE)
        else:
            # Не layer-shell (например, X11 для скриншота): остаёмся
            # полноэкранным окном — поведение то же, гарантия слабее.
            self.window.fullscreen()

        self.window.set_size_request(1, 1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_halign(Gtk.Align.CENTER)
        self.window.set_child(outer)

        clock = Gtk.Label(label=self.model.clock_text())
        clock.add_css_class("bg-lock-clock")
        outer.append(clock)
        self.widgets_clock = clock

        date = Gtk.Label(label=self.model.date_text())
        date.add_css_class("bg-muted")
        outer.append(date)

        user = self._display_name()
        who = Gtk.Label(label=user)
        who.add_css_class("title-3")
        outer.append(who)

        self.entry = Gtk.Entry()
        self.entry.set_visibility(False)
        self.entry.set_placeholder_text("пароль")
        self.entry.set_halign(Gtk.Align.CENTER)
        self.entry.set_width_chars(28)
        self.entry.connect("activate", lambda _e: self._try_unlock())
        outer.append(self.entry)

        self.status = Gtk.Label(label="")
        self.status.add_css_class("bg-muted")
        outer.append(self.status)

        button = Gtk.Button(label="разблокировать")
        button.set_halign(Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.connect("clicked", lambda _b: self._try_unlock())
        outer.append(button)

        # Композитор прячет рабочий стол только после того, как нам есть чем
        # занять клавиатуру: сначала map, потом lock begin.
        self.window.connect("map", lambda *_a: self._begin_lock())

    def _display_name(self) -> str:
        try:
            import pwd  # noqa: PLC0415 — только в POSIX-сеансе

            return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0] \
                or os.environ.get("USER", "пользователь")
        except (KeyError, OSError):
            return os.environ.get("USER", "пользователь")

    def _begin_lock(self) -> None:
        try:
            self.compositor.command("lock begin")
        except CompositorError as error:
            # Без композитора блокировка бессмысленна, а сидеть в мёртвом
            # сеансе — тем более: выходим с объяснением.
            print(f"bglock: композитор не принял блокировку: {error}",
                flush=True)
            self.application.quit()

    # ------------------------------------------------------------ проверка
    def _authenticate(self, password: str) -> bool:
        """Проверка пароля через sudo -S.

        `-v` обновляет билет PAM, `-k` снимает его до проверки (иначе sudo
        молча вернул бы успех на старом билете), после — снимаем и снова,
        чтобы локер не оставил сессионный кредит.
        """
        if not password:
            return False
        try:
            subprocess.run(["sudo", "-k"], check=False, timeout=5,
                capture_output=True)
            result = subprocess.run(
                ["sudo", "-S", "-k", "-v"],
                input=password + "\n", text=True, capture_output=True,
                timeout=10, check=False)
            subprocess.run(["sudo", "-k"], check=False, timeout=5,
                capture_output=True)
        except (OSError, subprocess.SubprocessError) as error:
            self.status.set_text(f"не удалось проверить пароль: {error}")
            return False
        return result.returncode == 0

    def _try_unlock(self) -> None:
        password = self.entry.get_text()
        self.entry.set_text("")
        if self._authenticate(password):
            self._release()
            self.application.quit()
            return

        self.failed += 1
        if self.failed >= MAX_ATTEMPTS_BEFORE_HINT:
            self.status.set_text(f"пароль {self._display_name()},"
                " ещё попытка")
        else:
            self.status.set_text("не тот пароль")
        # Короткая подсветка поля: человек должен понять, куда смотреть,
        # без модального окна поверх заблокированного экрана.
        self.entry.add_css_class("bg-lock-error")

        def clear_style() -> bool:
            self.entry.remove_css_class("bg-lock-error")
            return False

        bggui.GLib.timeout_add(600, clear_style)
        self.entry.grab_focus()

    def _release(self) -> None:
        try:
            self.compositor.command("lock end")
        except CompositorError:
            # Окно закрывается, layer-surface исчезнет — композитор снимет
            # блокировку по unmap. Это тот же путь, что и падение локера.
            pass


def build_locker(application) -> object:
    # Тема до первого кадра: экран блокировки в светлом Adwaita посреди тёмного
    # стола выглядит как чужое окно, а это первое, что человек видит утром.
    session = bggui.connect_session()
    bggui.load_theme(None, str(session.settings.get("theme.name")))
    locker = Locker(application)
    locker.window.present()
    return locker.window


def main(argv: list[str] | None = None) -> int:
    return bggui.run_gtk(build_locker, argv)


if __name__ == "__main__":
    raise SystemExit(main())
