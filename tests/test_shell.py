"""Проверки оболочки: настройки, панель, мастер первого запуска, переключение ОС."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from bgd.model import AppKind, Application
from bginstaller.bootrepair import parse_efibootmgr, windows_boot_index
from bgshell import boot, firstboot, panel, settings
from bgshell.errors import SettingError, WizardError

FIXTURES = None

APPS = [
    Application("firefox", "Firefox", AppKind.LINUX),
    Application("1c-client-83", "1С:Предприятие 8.3", AppKind.WINCOMPAT, coverage="l1"),
    Application("cad-legacy-2007", "САПР локальная", AppKind.REBOOT, coverage="l4"),
    Application("org.gimp.GIMP", "GIMP", AppKind.LINUX),
]


# ---------------------------------------------------------------------- настройки
def test_settings_have_defaults_for_every_key():
    values = settings.SettingsStore(path=Path("не используется до save")).effective()
    # Марочный выбор «тёмная тема по умолчанию» закреплён в схеме, а не в этом
    # тесте: тест проверяет, что значение есть и совпадает со схемой.
    assert values["theme.name"] == settings.SETTINGS["theme.name"].default
    assert values["theme.mode"] == "dark"
    # Windows-подсистема выключена по умолчанию: ByteguruOS — своя система,
    # а не обёртка над Wine, и список программ не должен начинаться с чужих.
    assert values["wincompat.enabled"] is False
    assert values["storage.ntfs-write"] is False
    # Ни одна настройка не должна оставаться без значения по умолчанию.
    assert set(values) == set(settings.SETTINGS)


def test_unknown_setting_key_is_rejected(tmp_path):
    store = settings.SettingsStore.load(tmp_path / "config")
    with pytest.raises(SettingError):
        store.get("netwrok.enabled")
    with pytest.raises(SettingError):
        store.set("panel.position", "middle")


def test_bool_and_int_validation(tmp_path):
    store = settings.SettingsStore.load(tmp_path / "config")
    store.set("theme.font-scale", "150")
    assert store.get("theme.font-scale") == 150
    store.set("panel.autohide", "да")
    assert store.get("panel.autohide") is True
    with pytest.raises(SettingError):
        store.set("theme.font-scale", 5000)


def test_save_roundtrip_keeps_only_changes(tmp_path):
    path = tmp_path / "config"
    store = settings.SettingsStore.load(path)
    store.set("boot.default-os", "windows")
    # Значение, совпадающее с заводским, в changed() не попадает — это и есть
    # смысл «сохраняем только изменения», поэтому выбираем заведомо другое.
    store.set("theme.name", "byteguru-high-contrast")
    store.save()
    reloaded = settings.SettingsStore.load(path)
    assert reloaded.get("boot.default-os") == "windows"
    assert reloaded.get("theme.name") == "byteguru-high-contrast"
    assert set(reloaded.changed()) == {"boot.default-os", "theme.name"}
    assert reloaded.pending_reboot() == ["boot.default-os"]


def test_unknown_keys_in_file_are_reported_not_fatal(tmp_path):
    path = tmp_path / "config"
    path.write_text("[panel]\nposition = middle\nliquid = true\n", encoding="utf-8")
    store = settings.SettingsStore.load(path)
    assert "panel.liquid" in store.unknown_keys
    # Значение вне choices отбрасывается к заводскому, а не к строке из файла.
    # Сверяемся со схемой, а не с литералом: дефолт панели — часть дизайна,
    # и он имеет право поменяться, не ломая этот тест.
    assert store.get("panel.position") == settings.SETTINGS["panel.position"].default


def test_scope_listing():
    boot_settings = settings.settings_for_scope(settings.Scope.BOOT)
    assert {"boot.default-os", "boot.timeout-seconds", "boot.clock-policy"} <= {s.key for s in boot_settings}


# -------------------------------------------------------------------------- панель
def test_clock_and_date_in_russian():
    model = panel.PanelModel(clock=datetime(2026, 11, 4, 14, 5), apps=APPS)
    assert model.clock_text() == "14:05"
    assert model.date_text() == "среда, 4 ноября"


def test_clock_without_time_is_placeholder():
    assert panel.PanelModel(apps=[]).clock_text() == "--:--"


def test_launcher_orders_favorites_then_windows_apps():
    model = panel.PanelModel(apps=APPS, favorites=("org.gimp.GIMP",))
    order = [app.id for app in model.launcher_apps()]
    assert order[0] == "org.gimp.GIMP"
    # Приложения, требующие Windows, не должны первым идти в общем списке.
    assert order.index("cad-legacy-2007") > order.index("firefox")


def test_search_matches_cyrillic_and_latin():
    model = panel.PanelModel(apps=APPS)
    assert [app.id for app in model.search("1с")] == ["1c-client-83"]
    assert [app.id for app in model.search("gimp")] == ["org.gimp.GIMP"]
    assert [app.id for app in model.search("сапр")] == ["cad-legacy-2007"]
    assert model.search("полностью-нет-такого") == []


def test_notifications_dedupe_by_id_and_cap():
    model = panel.PanelModel(apps=APPS)
    first = panel.Notification("upd-1", panel.NotifKind.UPDATE, "Доступно обновление")
    model.notify(first)
    model.notify(panel.Notification("upd-1", panel.NotifKind.UPDATE, "Доступно обновление 1.1"))
    assert len(model.notifications) == 1
    assert model.notifications[0].title.endswith("1.1")
    for index in range(20):
        model.notify(panel.Notification(f"n{index}", panel.NotifKind.INFO, "заголовок"))
    assert len(model.notifications) == 12
    # Храним 12 последних: самые старые вытесняются, а не копятся бесконечно.
    assert {item.id for item in model.notifications} == {f"n{index}" for index in range(8, 20)}
    assert model.dismiss("n19") is True
    assert model.dismiss("n0") is False


def test_taskbar_maps_windows_to_apps():
    model = panel.PanelModel(
        apps=APPS,
        windows=[
            panel.WindowInfo("1c-client-83", "Справочник номенклатуры", focused=True),
            panel.WindowInfo("unknown-app", "Окно"),
        ],
    )
    entries = model.taskbar_entries()
    assert entries[0][1] is APPS[1]
    assert entries[1][1] is None
    assert model.focused_window().app_id == "1c-client-83"


def test_windows_switcher_only_when_partition_present():
    without = panel.PanelModel(apps=APPS, windows_partition_present=False)
    with_partition = panel.PanelModel(apps=APPS, windows_partition_present=True)
    assert without.shows_windows_switcher() is False
    assert with_partition.shows_windows_switcher() is True
    assert [item[0] for item in with_partition.boot_menu_items()] == ["byteguru", "windows", "last"]


def test_status_summary_marks_broken_integrity():
    model = panel.PanelModel(apps=APPS, integrity_ok=False, wincompat_running=2)
    status = model.status_summary()
    assert status["integrity"] == "modified"
    assert status["wincompat_running"] == 2


# ---------------------------------------------------------------------- мастер
def _wizard(windows=True, hypervisor=False) -> firstboot.Wizard:
    facts = firstboot.SystemFacts(
        windows_partition_present=windows,
        windows_version="Windows 11 Pro",
        wincompat_available=True,
        hypervisor_available=hypervisor,
        ram_gib=16,
    )
    return firstboot.Wizard(facts)


def test_welcome_step_and_progression():
    wizard = _wizard()
    assert wizard.current is firstboot.Step.WELCOME
    assert wizard.can_next() is True
    wizard.next()
    assert wizard.current is firstboot.Step.REGION


def test_dualboot_step_skipped_without_windows():
    with_windows = _wizard(windows=True)
    without = _wizard(windows=False)
    assert firstboot.Step.DUALBOOT in with_windows.steps()
    assert firstboot.Step.DUALBOOT not in without.steps()
    assert len(without.steps()) == len(with_windows.steps()) - 1


def test_dualboot_prompts_ask_about_default_os_and_clock():
    wizard = _wizard()
    wizard.next()
    wizard.next()  # keyboard
    wizard.next()  # dualboot
    assert wizard.current is firstboot.Step.DUALBOOT
    keys = {prompt.key for prompt in wizard.prompts()}
    assert keys == {"boot_default", "clock_shared", "ntfs_readonly"}


def _goto(wizard: firstboot.Wizard, step: firstboot.Step) -> firstboot.Wizard:
    for _ in range(8):
        if wizard.current is step:
            return wizard
        wizard.next()
    raise AssertionError(f"шаг {step.value} не достигнут")


def test_hypervisor_option_appears_only_when_available():
    plain = _goto(_wizard(hypervisor=False), firstboot.Step.WINCOMPAT)
    vm_capable = _goto(_wizard(hypervisor=True), firstboot.Step.WINCOMPAT)
    assert "wincompat_vm" not in {prompt.key for prompt in plain.prompts()}
    assert "wincompat_vm" in {prompt.key for prompt in vm_capable.prompts()}


def test_wizard_rejects_answer_not_on_current_step():
    wizard = _wizard()
    with pytest.raises(WizardError):
        wizard.answer("boot_default", "windows")


def test_wizard_rejects_invalid_choice_and_empty_string():
    wizard = _wizard()
    wizard.next()
    with pytest.raises(WizardError):
        wizard.answer("locale", "de_DE")
    with pytest.raises(WizardError):
        wizard.answer("timezone", "   ")


def test_wizard_can_next_requires_answers():
    wizard = _wizard()
    wizard.next()
    assert wizard.can_next() is False
    wizard.answer_all_defaults()
    assert wizard.can_next() is True


def test_wizard_actions_and_summary():
    wizard = _wizard()
    while wizard.current is not firstboot.Step.SUMMARY:
        wizard.answer_all_defaults()
        wizard.next()
    actions = {action["kind"] for action in wizard.actions()}
    assert {"set-timezone", "set-locale", "set-keyboard-layouts", "set-boot-default-os",
            "set-clock-policy", "set-ntfs-read-only", "set-wincompat", "set-wincompat-proxy",
            "set-telemetry"} <= actions
    assert wizard.needs_root() is True
    assert wizard.requires_reboot() is True
    text = wizard.summary()
    assert "set-boot-default-os" in text
    assert "нужна перезагрузка" in text


def test_wizard_finish_sets_done():
    wizard = _wizard()
    while wizard.current is not firstboot.Step.SUMMARY:
        wizard.answer_all_defaults()
        wizard.next()
    assert wizard.next() is firstboot.Step.DONE
    assert wizard.can_next() is False
    assert wizard.prompts() == []


def test_ntfs_readonly_can_be_disabled_explicitly():
    wizard = _wizard()
    while wizard.current is not firstboot.Step.DUALBOOT:
        wizard.next()
    wizard.answer("boot_default", "windows")
    wizard.answer("clock_shared", False)
    wizard.answer("ntfs_readonly", False)
    actions = {action["kind"]: action["value"] for action in wizard.actions()}
    assert actions["set-boot-default-os"] == "windows"
    assert "set-clock-policy" not in actions
    assert actions["set-ntfs-read-only"] is False


# ------------------------------------------------------------------- переключение
def _efi_state() -> object:
    from pathlib import Path

    text = (Path(__file__).parent / "fixtures" / "efibootmgr-after-windows.txt").read_text(encoding="utf-8")
    return parse_efibootmgr(text)


def test_reboot_into_windows_uses_bootnext():
    result = boot.reboot_into("windows", _efi_state())
    assert result.ok is True
    assert result.commands == ("efibootmgr", "-n", "0000")
    assert result.requires_reboot is True
    assert "несохранённая работа" in boot.describe(result)


def test_reboot_into_unknown_target_fails():
    result = boot.reboot_into("haiku", _efi_state())
    assert result.ok is False
    assert "неизвестная цель" in result.message


def test_set_default_to_byteguru_reorders_boot():
    result = boot.set_default("byteguru", _efi_state())
    assert result.ok is True
    assert result.commands[0:2] == ("efibootmgr", "-o")
    assert result.commands[2].startswith("0003")


def test_set_default_to_missing_windows_reports():
    from pathlib import Path

    text = (Path(__file__).parent / "fixtures" / "efibootmgr-entry-lost.txt").read_text(encoding="utf-8")
    state = parse_efibootmgr(text)
    # В этой фикстуре запись Windows есть, а вот ByteguruOS отсутствует.
    assert boot.windows_boot_index(state) == "0000"
    result = boot.set_default("byteguru", state)
    assert result.ok is True
    assert result.commands[1] == "-c"
