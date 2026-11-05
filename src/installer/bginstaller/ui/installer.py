"""Графический установщик ByteguruOS.

Окно намеренно тоньше планировщика: оно собирает ответы человека (что за диск,
кто будет работать, соседство с Windows или затирка) и показывает то, что уже
умеет `bginstaller.plan` + `bginstaller.apply`. Никакой второй логики разметки
здесь нет — иначе GUI и CLI расходятся, а расходится установщик всегда в
сторону испорченного диска.

Три вещи, которые отличают это окно от «ещё одного мастера»:

- перед кнопкой «Установить» видны реальные команды, а не обещание;
- необратимое действие требует слово-токен, а не галочку;
- установка идёт в фоновом потоке, и окно не зависает на rsync-е — пользователь
  видит, на каком шаге встала машина, если встала.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from bginstaller import operations, probe
from bginstaller.apply import apply_plan, confirm_token
from bginstaller.errors import InstallerError, PlanError, ProbeError
from bginstaller.models import BootMode, GiB, Profile, WindowsState
from bginstaller.planner import plan, plan_erase_and_install

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError):  # pragma: no cover — сборка без графического сеанса
    gi = None
    Gtk = None

STEPS = ("язык", "диск", "пользователь", "план", "установка", "готово")

LANGUAGES = (("Русский", "ru_RU.UTF-8"), ("English", "en_US.UTF-8"))
KEYBOARDS = (("Русская + английская", "ru,us"), ("Только английская", "us"),
    ("Украинская + английская", "ua,us"))
TIMEZONES = ("Europe/Moscow", "Europe/Kyiv", "Europe/Berlin", "Asia/Almaty",
    "Asia/Tashkent", "America/New_York", "UTC")


def _gtk():
    return Gtk


def detect_disks() -> list:
    """Живые диски через lsblk.

    Разбор делает `bginstaller.probe` — тот же код, что и в CLI, поэтому
    «что видит окно» и «что считает планировщик» не могут разойтись.
    """
    try:
        text = subprocess.run(["lsblk", "-J", "-b", "-o",
            "NAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL,TRAN"],
            check=False, capture_output=True, text=True).stdout
        return [disk for disk in probe.parse_lsblk(text) if disk.size_bytes > 0]
    except (OSError, ProbeError, ValueError):
        return []


class Installer:
    def __init__(self, application):
        self.application = application
        self.disks = detect_disks()
        self.context = operations.Context()
        self.erase = False
        self.install_plan = None
        self.report = None
        self.worker: threading.Thread | None = None
        self.log_lines: list[str] = []

        self.window = Gtk.ApplicationWindow(application=application)
        self.window.set_title("Установка ByteguruOS")
        self.window.set_default_size(880, 660)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.switcher = Gtk.StackSwitcher()
        self.switcher.set_stack(self.stack)
        self.switcher.add_css_class("bg-installer-tabs")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.set_child(root)
        root.append(self._banner())
        root.append(self.stack)
        root.append(self._footer())

        self.pages = {
            "язык": self._page_language(),
            "диск": self._page_disk(),
            "пользователь": self._page_user(),
            "план": self._page_plan(),
            "установка": self._page_progress(),
            "готово": self._page_done(),
        }
        for name, page in self.pages.items():
            self.stack.add_titled(page, name, name)

        self._update_state()

    # --------------------------------------------------------------- каркас
    def _banner(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(10)
        box.set_margin_start(18)
        box.set_margin_end(18)
        title = Gtk.Label()
        title.set_markup("<b>Установка ByteguruOS</b>")
        title.add_css_class("title-2")
        box.append(title)
        self.status = Gtk.Label(label="")
        self.status.add_css_class("dim-label")
        self.status.set_hexpand(True)
        self.status.set_xalign(1.0)
        box.append(self.status)
        return box

    def _footer(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(14)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(self.switcher)
        self.back = Gtk.Button(label="Назад")
        self.back.connect("clicked", lambda _b: self._step(-1))
        self.forward = Gtk.Button(label="Далее")
        self.forward.add_css_class("suggested-action")
        self.forward.connect("clicked", lambda _b: self._step(1))
        box.append(self.forward)
        self.back.set_halign(Gtk.Align.START)
        return box

    def _step(self, delta: int) -> None:
        if delta > 0 and self.stack.get_visible_child_name() == "план":
            self._start_install()
            return
        names = list(self.pages)
        index = names.index(self.stack.get_visible_child_name())
        target = max(0, min(len(names) - 1, index + delta))
        self.stack.set_visible_child_name(names[target])

    # --------------------------------------------------------------- страницы
    def _page_language(self) -> Gtk.Widget:
        box = self._page_box("Язык и клавиатура")
        self.language = self._choice(box, "Язык системы",
            [name for name, _code in LANGUAGES])
        self.keyboard = self._choice(box, "Раскладка клавиатуры",
            [name for name, _spec in KEYBOARDS])
        self.timezone = self._choice(box, "Часовой пояс", list(TIMEZONES))
        box.append(self._note(
            "Часы: если на машине стоит Windows, установщик договорится с ней "
            "о единой политике времени — иначе системы будут спорить за часы "
            "при каждой перезагрузке."))
        return box

    def _page_disk(self) -> Gtk.Widget:
        box = self._page_box("Куда устанавливаем")
        self.disk_list = Gtk.ListBox()
        self.disk_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.disk_list.connect("row-selected", lambda *_a: self._update_state())
        for disk in self.disks:
            row = Gtk.ListBoxRow()
            row.disk = disk  # type: ignore[attr-defined]
            label = Gtk.Label(label=self._disk_text(disk), xalign=0.0)
            row.set_child(label)
            self.disk_list.append(row)
        if not self.disks:
            self.disk_list.append(self._info_row(
                "Диски не найдены: установщик работает без прав администратора "
                "или на машине без носителя."))
        box.append(self._scroller(self.disk_list))

        self.mode_along = Gtk.CheckButton(label="Оставить то, что на диске, и "
            "поставить ByteguruOS рядом")
        self.mode_erase = Gtk.CheckButton(label="Стереть диск целиком")
        self.mode_erase.set_group(self.mode_along)
        self.mode_along.set_active(True)
        self.mode_along.connect("toggled", lambda *_a: self._update_state())
        box.append(self.mode_along)
        box.append(self.mode_erase)
        box.append(self._note(
            "Сжатие NTFS установщик делает только после проверки тома; если "
            "Windows не выключилась чисто или включён BitLocker, установка "
            "будет остановлена, а не «продолжена на свой страх и риск»."))
        return box

    def _page_user(self) -> Gtk.Widget:
        box = self._page_box("Кто будет работать")
        self.full_name = self._entry(box, "Имя пользователя")
        self.username = self._entry(box, "Логин")
        self.password = self._entry(box, "Пароль")
        self.password.set_visibility(False)
        self.password.set_invisible_char("*")
        self.hostname = self._entry(box, "Имя компьютера")
        self.hostname.set_text("byteguru")
        self.autologin = Gtk.CheckButton(label="Входить автоматически (live-режим и "
            "личная машина)")
        self.autologin.set_active(True)
        box.append(self.autologin)
        box.append(self._note(
            "Пароль не попадает ни в одну команду: он передаётся в stdin "
            "утилиты chpasswd внутри установленной системы."))
        return box

    def _page_plan(self) -> Gtk.Widget:
        box = self._page_box("Что будет сделано")
        self.plan_text = Gtk.Label(label="Выберите диск — здесь появится план.",
            xalign=0.0)
        self.plan_text.set_wrap(True)
        box.append(self.plan_text)

        self.commands = Gtk.TextView()
        self.commands.set_editable(False)
        self.commands.set_monospace(True)
        self.commands.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        box.append(self._scroller(self.commands, expand=True))

        self.token = Gtk.Entry()
        self.token.set_placeholder_text("слово-подтверждение"
            " (udalit-dannye / izmenit-razmetku)")
        self.token.connect("changed", lambda *_a: self._update_state())
        box.append(self.token)

        self.confirm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.elevate = Gtk.Button(label="Перезапустить с правами администратора")
        self.elevate.connect("clicked", lambda _b: self._relaunch_privileged())
        self.confirm_row.append(self.elevate)
        box.append(self.confirm_row)
        return box

    def _page_progress(self) -> Gtk.Widget:
        box = self._page_box("Установка")
        self.progress = Gtk.ProgressBar()
        box.append(self.progress)
        self.log = Gtk.TextView()
        self.log.set_editable(False)
        self.log.set_monospace(True)
        self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        box.append(self._scroller(self.log, expand=True))
        return box

    def _page_done(self) -> Gtk.Widget:
        box = self._page_box("Готово")
        self.done_text = Gtk.Label(label="", xalign=0.0)
        self.done_text.set_wrap(True)
        box.append(self.done_text)
        reboot = Gtk.Button(label="Перезагрузить сейчас")
        reboot.add_css_class("suggested-action")
        reboot.connect("clicked", lambda _b: self._reboot())
        box.append(reboot)
        return box

    # --------------------------------------------------------------- помощники
    def _page_box(self, title: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(18)
        box.set_margin_end(18)
        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("title-3")
        box.append(heading)
        return box

    def _choice(self, box: Gtk.Box, label: str, items: list[str]) -> Gtk.DropDown:
        box.append(self._field_label(label))
        dropdown = Gtk.DropDown.new_from_strings(items)
        box.append(dropdown)
        return dropdown

    def _entry(self, box: Gtk.Box, label: str) -> Gtk.Entry:
        box.append(self._field_label(label))
        entry = Gtk.Entry()
        box.append(entry)
        return entry

    def _field_label(self, label: str) -> Gtk.Label:
        widget = Gtk.Label(label=label, xalign=0.0)
        widget.add_css_class("dim-label")
        return widget

    def _note(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0.0)
        label.set_wrap(True)
        label.add_css_class("dim-label")
        return label

    def _info_row(self, text: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        label = Gtk.Label(label=text, xalign=0.0)
        label.set_wrap(True)
        row.set_child(label)
        return row

    def _scroller(self, child: Gtk.Widget, expand: bool = False) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        if expand:
            scroller.set_vexpand(True)
        return scroller

    def _disk_text(self, disk) -> str:
        windows = any(part.holds_windows for part in disk.partitions)
        kind = "есть Windows" if windows else "без Windows"
        return (f"{disk.device} · {disk.size_bytes // GiB} ГиБ · "
            f"{disk.table.value} · {kind}")

    @property
    def selected_disk(self):
        row = self.disk_list.get_selected_row()
        return getattr(row, "disk", None) if row is not None else None

    # --------------------------------------------------------------- состояние
    def _update_state(self) -> None:
        privileged = os.geteuid() == 0
        self.status.set_text("права администратора есть" if privileged
            else "только просмотр: не хватает прав администратора")
        self.elevate.set_visible(not privileged)
        # Кнопка «Далее» на странице плана — это уже не «дальше», а «начать
        # необратимое»; подписывать её одинаково на всех страницах нельзя.
        self.forward.set_label("Установить"
            if self.stack.get_visible_child_name() == "план" else "Далее")
        self.forward.set_sensitive(self._can_continue(privileged))
        self._rebuild_plan()

    def _can_continue(self, privileged: bool) -> bool:
        name = self.stack.get_visible_child_name()
        if name == "диск":
            return self.selected_disk is not None
        if name == "пользователь":
            return bool(self.username.get_text().strip())
        if name == "план":
            if not privileged or self.install_plan is None:
                return False
            # Токен нужен только когда план действительно необратим.
            if not self.install_plan.requires_user_confirmation():
                return True
            return self.token.get_text().strip() == confirm_token(self.install_plan)
        return name != "установка"

    def _collect_context(self) -> operations.Context:
        return operations.Context(
            hostname=self.hostname.get_text().strip() or "byteguru",
            username=self.username.get_text().strip(),
            real_name=self.full_name.get_text().strip(),
            password=self.password.get_text(),
            autologin=self.autologin.get_active(),
            language=LANGUAGES[self.language.get_selected()][1],
            keyboard=KEYBOARDS[self.keyboard.get_selected()][1],
            timezone=TIMEZONES[self.timezone.get_selected()],
        )

    def _rebuild_plan(self) -> None:
        disk = self.selected_disk
        if disk is None:
            self.plan_text.set_text("Выберите диск — здесь появится план.")
            self._set_commands("")
            return
        try:
            if self.mode_erase.get_active():
                self.install_plan = plan_erase_and_install(disk,
                    profile=Profile.WORKSTATION, boot_mode=BootMode.UEFI)
            else:
                self.install_plan = plan(disk,
                    windows=WindowsState(present=any(
                        part.holds_windows for part in disk.partitions)),
                    profile=Profile.WORKSTATION, boot_mode=BootMode.UEFI)
        except (PlanError, InstallerError) as exc:
            self.install_plan = None
            self.plan_text.set_text(f"установка на {disk.device} невозможна: {exc}")
            self._set_commands("")
            self.forward.set_sensitive(False)
            return

        layout = self.install_plan.layout
        self.plan_text.set_text(
            f"{disk.device}: корень {layout.root_bytes // GiB} ГиБ, "
            f"/var {layout.var_bytes // GiB} ГиБ, "
            f"/home {layout.home_bytes // GiB} ГиБ, "
            f"подкачка {layout.swap_bytes // GiB} ГиБ"
            + (f" · сжатие Windows на {layout.shrink_windows_bytes // GiB} ГиБ"
                if layout.shrink_windows_bytes else ""))
        preview = apply_plan(self.install_plan, apply=False,
            context=self._collect_context())
        self._set_commands("\n".join(
            f"#{step.action.order:>2} {step.action.kind}\n"
            + "\n".join(f"   $ {line}" for line in step.commands)
            for step in preview.steps))

    def _set_commands(self, text: str) -> None:
        self.commands.get_buffer().set_text(text)

    # --------------------------------------------------------------- установка
    def _start_install(self) -> None:
        if self.install_plan is None or self.worker is not None:
            return
        self.context = self._collect_context()
        self.stack.set_visible_child_name("установка")
        self.forward.set_sensitive(False)
        self.back.set_sensitive(False)
        self.worker = threading.Thread(target=self._run_install, daemon=True)
        self.worker.start()

    def _run_install(self) -> None:
        """Прогон плана в рабочем потоке.

        Лог пишется построчно и сразу в окно: установка на реальный диск
        занимает минуты, и «окно думает» без единой строки — это то, из-за
        чего человек выдёргивает флешку.
        """
        def runner(argv, stdin=None):
            line = " ".join(argv)
            self._log(f"$ {line}")
            process = subprocess.run(list(argv), input=stdin, text=True,
                capture_output=True, check=False)
            for output in (process.stdout, process.stderr):
                for chunk in (output or "").splitlines():
                    self._log(f"   {chunk}")
            if process.returncode:
                self._log(f"   код {process.returncode}")
            return process.returncode

        def writer(path, content):
            self._log(f"# запись {path}")
            operations_write(path, content)

        # Прогресс считаем по количеству команд, а не по числу действий:
        # «install-system-image» занимает девять десятых времени, и полоса,
        # идущая по действиям, стояла бы на 20% всю копировалку.
        preview = apply_plan(self.install_plan, apply=False, context=self.context)
        total = sum(len(step.jobs) for step in preview.steps) or 1
        done = 0

        def tick() -> None:
            nonlocal done
            done += 1
            self._progress(min(0.98, done / total))

        def counting_runner(argv, stdin=None):
            result = runner(argv, stdin)
            tick()
            return result

        def counting_writer(path, content):
            writer(path, content)
            tick()

        report = apply_plan(self.install_plan, apply=True,
            runner=counting_runner, writer=counting_writer, context=self.context,
            confirmations={action.order for action in self.install_plan.actions
                if action.requires_confirmation})
        self._progress(1.0)
        GLib.idle_add(self._finish_install, report)

    def _log(self, line: str) -> None:
        self.log_lines.append(line)
        GLib.idle_add(self._append_log, line)

    def _append_log(self, line: str) -> bool:
        buffer = self.log.get_buffer()
        end = buffer.get_end_iter()
        buffer.insert(end, line + "\n")
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        self.log.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        buffer.delete_mark(mark)
        return False

    def _progress(self, fraction: float) -> None:
        GLib.idle_add(lambda: (self.progress.set_fraction(fraction), False)[1])

    def _finish_install(self, report) -> bool:
        self.report = report
        self.forward.set_sensitive(True)
        self.back.set_sensitive(True)
        if report.ok:
            self.done_text.set_text(
                "Система установлена. Выньте носитель и перезагрузитесь; "
                "ByteguruOS появится в списке загрузки первой.\n"
                + "\n".join(self.install_plan.notes[:3]))
            self.stack.set_visible_child_name("готово")
        else:
            failed = next((step for step in report.steps if step.failed), None)
            self.plan_text.set_text(
                f"остановлено на шаге «{failed.action.kind}»: {failed.note}"
                if failed else "остановлено")
            self.stack.set_visible_child_name("план")
        return False

    def _reboot(self) -> None:
        subprocess.run(["systemctl", "reboot"], check=False)

    def _relaunch_privileged(self) -> None:
        # Не `pkexec bginstaller`, а обёртка: pkexec выбрасывает окружение, и
        # окно установщика, поднятое им, не находит Wayland-сеанс.
        code = subprocess.run(["bginstaller-gui"], check=False).returncode
        if code not in (0, 1):
            self.status.set_text("не удалось получить права администратора")


def operations_write(path: str, content: str) -> None:
    from pathlib import Path  # noqa: PLC0415 — только на записи файла

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def build_window(application) -> Gtk.Widget:
    installer = Installer(application)
    installer.window.present()
    return installer.window


def main(argv: list[str] | None = None) -> int:
    if gi is None:  # pragma: no cover — без GTK окно не собрать
        print("для графического установщика нужны python3-gi и GTK 4",
            file=sys.stderr)
        return 2
    application = Gtk.Application(application_id="ru.byteguru.installer")
    application.connect("activate", lambda app: build_window(app))
    return int(application.run(argv or []))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
