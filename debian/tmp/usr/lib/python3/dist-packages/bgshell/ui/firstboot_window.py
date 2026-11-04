"""Мастер первого запуска — GTK-обёртка над bgshell.firstboot.

Логика шагов, ответы и список действий живут в модели и покрыты тестами; здесь
только отриска текущего шага и применениеresult через хранилище настроек.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bgshell import firstboot, settings as settings_module
from bgshell.errors import SettingError, WizardError
from bgshell.ui import bggui

TITLES = {
    firstboot.Step.WELCOME: "Добро пожаловать в ByteguruOS",
    firstboot.Step.REGION: "Регион и язык",
    firstboot.Step.KEYBOARD: "Клавиатура",
    firstboot.Step.DUALBOOT: "Соседство с Windows",
    firstboot.Step.WINCOMPAT: "Приложения Windows",
    firstboot.Step.PRIVACY: "Приватность",
    firstboot.Step.SUMMARY: "Проверьте, что будет сделано",
    firstboot.Step.DONE: "Готово",
}

# Ответ мастера → ключ настройки. Ответы без пары применяются вне конфигурации
# оболочки (например, часовой пояс — через timedatectl).
SETTING_BY_ANSWER = {
    "layouts": "session.keyboard-layouts",
    "boot_default": "boot.default-os",
    "clock_shared": "boot.clock-policy",
    "ntfs_readonly": "storage.ntfs-write",
    "wincompat_enabled": "wincompat.enabled",
    "wincompat_strict_network": "wincompat.network-proxy",
    "wincompat_vm": "wincompat.vm-enabled",
    "telemetry": "privacy.telemetry",
}


def detect_facts() -> firstboot.SystemFacts:
    """Собираем факты о машине. Не сумели посмотреть — значит «нет», а не «да»."""
    windows_present = False
    hypervisor = Path("/dev/kvm").exists()
    try:
        from bginstaller.probe import parse_lsblk

        result = subprocess.run(
            ["lsblk", "--json", "--bytes", "--output", "NAME,SIZE,FSTYPE,LABEL,TYPE,PARTTYPENAME"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            windows_present = any(disk.windows_partitions for disk in parse_lsblk(result.stdout))
    except (OSError, ValueError):
        windows_present = False
    return firstboot.SystemFacts(
        windows_partition_present=windows_present,
        hypervisor_available=hypervisor,
    )


def build_window(application, facts: firstboot.SystemFacts | None = None) -> object:
    Gtk = bggui.Gtk
    session = bggui.connect_session()
    bggui.load_theme(None, session.theme_name)
    wizard = firstboot.Wizard(facts or detect_facts())

    window = Gtk.ApplicationWindow(application=application)
    window.set_title("Первый запуск — ByteguruOS")
    window.set_default_size(660, 520)

    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    root.set_margin_top(20)
    root.set_margin_bottom(20)
    root.set_margin_start(22)
    root.set_margin_end(22)
    window.set_child(root)

    title = Gtk.Label()
    title.add_css_class("title-2")
    title.set_wrap(True)
    title.set_xalign(0.0)
    root.append(title)

    progress = Gtk.Label()
    progress.add_css_class("dim")
    progress.set_xalign(0.0)
    root.append(progress)

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    root.append(body)

    status = Gtk.Label()
    status.add_css_class("notice")
    status.set_wrap(True)
    status.set_xalign(0.0)
    root.append(status)

    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    footer.set_halign(Gtk.Align.END)
    root.append(footer)

    back = Gtk.Button(label="Назад")
    back.add_css_class("flat")
    footer.append(back)
    forward = Gtk.Button(label="Далее")
    forward.add_css_class("suggested-action")
    footer.append(forward)

    def render() -> None:
        while body.get_first_child() is not None:
            body.remove(body.get_first_child())
        step = wizard.current
        title.set_text(TITLES[step])
        total = len(wizard.steps())
        position = min(wizard.index + 1, total)
        progress.set_text(f"шаг {position} из {total}")

        if step is firstboot.Step.SUMMARY:
            summary = Gtk.Label(label=wizard.summary())
            summary.set_xalign(0.0)
            summary.set_wrap(True)
            summary.add_css_class("mono")
            body.append(summary)
        else:
            for prompt in wizard.prompts():
                if prompt.help:
                    hint = Gtk.Label(label=prompt.help)
                    hint.add_css_class("dim")
                    hint.set_wrap(True)
                    hint.set_xalign(0.0)
                    body.append(hint)
                body.append(_prompt_widget(wizard, prompt, status))

        forward.set_sensitive(wizard.can_next())
        back.set_sensitive(wizard.index > 0)
        forward.set_label("Применить и завершить" if step is firstboot.Step.SUMMARY else "Далее")

    def on_forward(*_args) -> bool:
        if wizard.current is firstboot.Step.SUMMARY:
            apply_answers(session, wizard, status)
            return True
        wizard.next()
        render()
        return True

    def on_back(*_args) -> bool:
        wizard.back()
        render()
        return True

    forward.connect("clicked", on_forward)
    back.connect("clicked", on_back)
    render()
    window.present()
    return window


def _prompt_widget(wizard, prompt, status) -> object:
    Gtk = bggui.Gtk
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    label = Gtk.Label(label=prompt.text)
    label.set_xalign(0.0)
    label.set_hexpand(True)
    label.set_wrap(True)
    row.append(label)

    def commit(value) -> None:
        try:
            wizard.answer(prompt.key, value)
            status.set_text(f"отмечено: {prompt.key} = {value}")
        except WizardError as exc:
            status.set_text(str(exc))

    if prompt.kind == "bool":
        switch = Gtk.Switch()
        switch.set_active(bool(prompt.default))
        switch.connect("state-set", lambda _w, state: (commit(state), False)[1])
        row.append(switch)
    elif prompt.kind == "choice":
        dropdown = Gtk.DropDown.new_from_strings(list(prompt.choices))
        try:
            dropdown.set_selected(list(prompt.choices).index(str(prompt.default)))
        except ValueError:
            dropdown.set_selected(0)
        dropdown.connect("notify::selected", lambda widget: commit(prompt.choices[widget.get_selected()]))
        row.append(dropdown)
    else:
        entry = Gtk.Entry()
        entry.set_text(str(prompt.default or ""))
        # Применяем по Enter, а не по каждому нажатию: иначе неполный путь
        # «E:» улетит в валидацию раньше, чем пользователь допишет слово.
        entry.connect("activate", lambda widget: commit(widget.get_text()))
        row.append(entry)
    return row


def apply_answers(session, wizard: firstboot.Wizard, status) -> None:
    """Пишем ответы в настройки; ответы без пары остаются на совести системных утилит."""
    applied = []
    skipped = []
    for answer, value in wizard.answers.items():
        key = SETTING_BY_ANSWER.get(answer)
        if key is None:
            skipped.append(answer)
            continue
        if key not in settings_module.SETTINGS:
            skipped.append(answer)
            continue
        if key == "storage.ntfs-write":
            value = not bool(value)  # ответ «только для чтения» → флаг записи наоборот
        try:
            session.settings.set(key, value)
            applied.append(key)
        except SettingError as exc:
            skipped.append(f"{key} ({exc})")
    session.settings.save()
    text = f"сохранено {len(applied)} настроек"
    if skipped:
        text += "; пропущено: " + ", ".join(skipped)
    if wizard.requires_reboot():
        text += "; часть вступит в силу после перезагрузки"
    status.set_text(text)


def main() -> int:
    return bggui.run_gtk(build_window)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
