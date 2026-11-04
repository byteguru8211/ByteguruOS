"""Хранилище настроек оболочки.

Настройки — это контракт между оболочкой, демоном и системой: у каждого ключа
есть тип, значение по умолчанию и область применения. Самопальные ключи
запрещены: «настройка, которой нет» должна быть ошибкой, а не тихим no-op.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from bgshell.errors import SettingError


class Scope(str, Enum):
    SESSION = "session"        # применяется немедленно
    BOOT = "boot"              # влияет на загрузку, нужен reboot
    WINCOMPAT = "wincompat"    # влияет на подсистему WinCompat
    SYSTEM = "system"          # требует прав администратора


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str                     # bool | int | choice | str
    default: object
    description: str
    scope: Scope
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    requires_root: bool = False


SETTINGS: dict[str, Setting] = {
    "session.default-terminal": Setting(
        "session.default-terminal", "str", "foot", "Терминал по умолчанию (Super+Enter)", Scope.SESSION
    ),
    "session.hotkey-layout-toggle": Setting(
        "session.hotkey-layout-toggle", "choice", "grp:alt_shift_toggle",
        "Переключение раскладки клавиатуры", Scope.SESSION,
        choices=("grp:alt_shift_toggle", "grp:ctrl_shift_toggle", "grp:win_space_toggle"),
    ),
    "session.keyboard-layouts": Setting(
        "session.keyboard-layouts", "str", "us,ru", "Раскладки клавиатуры через запятую",
        Scope.SESSION, requires_root=True,
    ),
    "theme.name": Setting(
        "theme.name", "choice", "byteguru-dark", "Тема оформления", Scope.SESSION,
        choices=("byteguru-light", "byteguru-dark", "byteguru-high-contrast"),
    ),
    "theme.mode": Setting(
        "theme.mode", "choice", "dark",
        "Режим палитры: влияет на фон оболочки и композитор", Scope.SESSION,
        choices=("dark", "light"),
    ),
    "theme.accent": Setting(
        "theme.accent", "str", "#f2a33d",
        "Акцентный цвет в форме #RRGGBB: рамка фокуса, потоки, кнопки", Scope.SESSION,
    ),
    "theme.font-scale": Setting(
        "theme.font-scale", "int", 100, "Масштаб шрифта, проценты", Scope.SESSION, minimum=75, maximum=200
    ),
    "panel.position": Setting(
        "panel.position", "choice", "top", "Положение панели", Scope.SESSION,
        choices=("top", "bottom"),
    ),
    "panel.autohide": Setting(
        "panel.autohide", "bool", False, "Скрывать панель при развороте окна на весь экран", Scope.SESSION
    ),
    "wincompat.enabled": Setting(
        "wincompat.enabled", "bool", True, "Подсистема запуска Windows-приложений",
        Scope.WINCOMPAT, requires_root=True,
    ),
    "wincompat.network-proxy": Setting(
        "wincompat.network-proxy", "bool", True,
        "Сетевые allowlist-политики исполняет прокси wc-net", Scope.WINCOMPAT, requires_root=True
    ),
    "wincompat.default-update": Setting(
        "wincompat.default-update", "choice", "disabled",
        "Разрешить обновления Windows-приложениям внутри окружения", Scope.WINCOMPAT,
        choices=("disabled", "manual", "auto"),
    ),
    "boot.default-os": Setting(
        "boot.default-os", "choice", "byteguru", "Какая ОС загружается по умолчанию", Scope.BOOT,
        choices=("byteguru", "windows", "last"), requires_root=True,
    ),
    "boot.timeout-seconds": Setting(
        "boot.timeout-seconds", "int", 5, "Задержка меню загрузки", Scope.BOOT,
        minimum=0, maximum=60, requires_root=True,
    ),
    "boot.clock-policy": Setting(
        "boot.clock-policy", "choice", "windows-utc", "Часы на двухзагрузочной машине", Scope.BOOT,
        choices=("utc", "localtime", "windows-utc"), requires_root=True,
    ),
    "storage.ntfs-write": Setting(
        "storage.ntfs-write", "bool", False,
        "Монтировать тома Windows на запись (риск при гибернации)", Scope.SYSTEM, requires_root=True
    ),
    "privacy.telemetry": Setting(
        "privacy.telemetry", "choice", "off",
        "Обезличенная телеметрия совместимости", Scope.SESSION, choices=("off", "crashes", "full")
    ),
}


@dataclass
class SettingsStore:
    """Читаемые/пишемые значения поверх файла конфигурации пользователя."""

    path: Path
    values: dict[str, object] = field(default_factory=dict)
    unknown_keys: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str | Path) -> "SettingsStore":
        file_path = Path(path)
        parser = configparser.ConfigParser()
        unknown: list[str] = []
        values: dict[str, object] = {}
        if file_path.is_file():
            try:
                parser.read(file_path, encoding="utf-8")
            except configparser.Error as exc:
                raise SettingError(f"файл настроек повреждён: {exc}") from exc
            for section in parser.sections():
                for key, raw in parser.items(section):
                    full = f"{section}.{key}"
                    if full not in SETTINGS:
                        unknown.append(full)
                        continue
                    values[full] = _decode(SETTINGS[full], raw)
        return cls(path=file_path, values=values, unknown_keys=tuple(unknown))

    def effective(self) -> dict[str, object]:
        merged = {key: setting.default for key, setting in SETTINGS.items()}
        merged.update(self.values)
        return merged

    def get(self, key: str) -> object:
        setting = _require(key)
        return self.values.get(key, setting.default)

    def set(self, key: str, value: object) -> None:
        setting = _require(key)
        self.values[key] = validate(setting, value)

    def reset(self, key: str) -> None:
        self.values.pop(_require(key).key, None)

    def changed(self) -> dict[str, object]:
        """Только те ключи, что отличаются от значений по умолчанию."""
        defaults = {key: setting.default for key, setting in SETTINGS.items()}
        return {key: value for key, value in self.values.items() if defaults[key] != value}

    def save(self) -> None:
        parser = configparser.ConfigParser()
        for key, value in sorted(self.values.items()):
            section, _, name = key.rpartition(".")
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, name, _encode(value))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as stream:
            parser.write(stream)

    def pending_reboot(self) -> list[str]:
        return [
            key
            for key in self.changed()
            if SETTINGS[key].scope in (Scope.BOOT,)
        ]

    def requires_root(self, key: str) -> bool:
        return _require(key).requires_root


def _require(key: str) -> Setting:
    setting = SETTINGS.get(key)
    if setting is None:
        raise SettingError(f"неизвестная настройка {key!r}")
    return setting


def validate(setting: Setting, value: object) -> object:
    if setting.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "да", "нет"):
            return value.lower() in ("true", "1", "да")
        raise SettingError(f"{setting.key}: ожидается да/нет, получено {value!r}")
    if setting.kind == "int":
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise SettingError(f"{setting.key}: ожидается целое число") from exc
        if setting.minimum is not None and number < setting.minimum:
            raise SettingError(f"{setting.key}: меньше {setting.minimum}")
        if setting.maximum is not None and number > setting.maximum:
            raise SettingError(f"{setting.key}: больше {setting.maximum}")
        return number
    if setting.kind == "choice":
        text = str(value)
        if text not in setting.choices:
            raise SettingError(f"{setting.key}: значение {text!r} вне списка {list(setting.choices)}")
        return text
    text = str(value)
    if not text.strip():
        raise SettingError(f"{setting.key}: пустое значение недопустимо")
    return text


def _decode(setting: Setting, raw: str) -> object:
    try:
        return validate(setting, raw)
    except SettingError:
        return setting.default


def _encode(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def settings_for_scope(scope: Scope) -> list[Setting]:
    return [setting for setting in SETTINGS.values() if setting.scope is scope]
