"""Мастер первого запуска.

Ключевой сценарий миграции: пользователь впервые включает ByteguruOS на машине,
где стояла Windows. Мастер должен за три экрана решить, что делать с соседней
системой и подсистемой совместимости, — и обязательно объяснить, что он собрался
делать, до применения.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from bgshell.errors import WizardError


class Step(str, Enum):
    WELCOME = "welcome"
    REGION = "region"
    KEYBOARD = "keyboard"
    DUALBOOT = "dualboot"
    WINCOMPAT = "wincompat"
    PRIVACY = "privacy"
    SUMMARY = "summary"
    DONE = "done"


@dataclass(frozen=True)
class SystemFacts:
    """Что известно о машине. Неполные данные трактуем консервативно."""

    windows_partition_present: bool = False
    windows_version: str = ""
    wincompat_available: bool = True
    hypervisor_available: bool = False
    network_connected: bool = False
    suggested_timezone: str = "Europe/Moscow"
    suggested_locale: str = "ru_RU"
    ram_gib: int = 8


@dataclass(frozen=True)
class Prompt:
    key: str
    text: str
    kind: str                       # bool | choice | str
    choices: tuple[str, ...] = ()
    default: object = None
    help: str = ""


class Wizard:
    ORDER = (Step.WELCOME, Step.REGION, Step.KEYBOARD, Step.DUALBOOT, Step.WINCOMPAT, Step.PRIVACY, Step.SUMMARY)

    def __init__(self, facts: SystemFacts) -> None:
        self.facts = facts
        self.answers: dict[str, object] = {}
        self.index = 0

    # ------------------------------------------------------------- маршрутизация
    def steps(self) -> tuple[Step, ...]:
        """Пропускаем шаги, которые не о чем спрашивать на этой машине."""
        result = []
        for step in self.ORDER:
            if step is Step.DUALBOOT and not self.facts.windows_partition_present:
                continue
            if step is Step.WINCOMPAT and not self.facts.wincompat_available:
                continue
            result.append(step)
        return tuple(result)

    @property
    def current(self) -> Step:
        steps = self.steps()
        if self.index >= len(steps):
            return Step.DONE
        return steps[self.index]

    def can_next(self) -> bool:
        """Вперёд пускаем, только если на шаге есть ответ или шагу ответы не нужны."""
        step = self.current
        if step is Step.DONE:
            return False
        if step in (Step.WELCOME, Step.SUMMARY):
            return True
        return all(prompt.key in self.answers for prompt in self.prompts())

    def next(self) -> Step:
        if self.current is Step.SUMMARY:
            self.index = len(self.steps())
            return Step.DONE
        if self.index < len(self.steps()):
            self.index += 1
        return self.current

    def back(self) -> Step:
        self.index = max(0, self.index - 1)
        return self.current

    # -------------------------------------------------------------------- вопросы
    def prompts(self) -> list[Prompt]:
        step = self.current
        if step is Step.WELCOME:
            return [
                Prompt(
                    # kind="note": на первом экране нечего спрашивать. Пока тут
                    # был bool, мастер рисовал переключатель без подписи, а
                    # ответ на него не влиял ни на что.
                    key="welcome", kind="note",
                    text="ByteguruOS готов к работе. Несколько коротких шагов: "
                        "часовой пояс, раскладка, соседство с Windows и "
                        "приватность.",
                    help="Ни одно изменение не применится, пока вы не "
                        "подтвердите их на последнем экране",
                )
            ]
        if step is Step.REGION:
            return [
                Prompt("timezone", "Часовой пояс", "str", default=self.facts.suggested_timezone),
                Prompt("locale", "Язык интерфейса", "choice", choices=("ru_RU", "en_US"), default="ru_RU"),
            ]
        if step is Step.KEYBOARD:
            return [
                Prompt(
                    "layouts",
                    "Раскладки клавиатуры",
                    "choice",
                    choices=("us,ru", "ru", "us,ru,ua"),
                    default="us,ru",
                    help="переключение — Alt+Shift, как в Windows",
                )
            ]
        if step is Step.DUALBOOT:
            windows = self.facts.windows_version or "Windows"
            return [
                Prompt(
                    "boot_default",
                    f"На диске найдена {windows}. Какая система загружается по умолчанию?",
                    "choice",
                    choices=("byteguru", "windows", "last"),
                    default="byteguru",
                ),
                Prompt(
                    "clock_shared",
                    "Синхронизировать часы с Windows (обе системы в UTC)",
                    "bool",
                    default=True,
                    help="иначе после переключения между ОС часы будут расходиться на величину пояса",
                ),
                Prompt(
                    "ntfs_readonly",
                    "Раздел Windows монтировать только для чтения",
                    "bool",
                    default=True,
                    help="запись на том с гибернацией может повредить установленную систему",
                ),
            ]
        if step is Step.WINCOMPAT:
            prompts = [
                Prompt(
                    "wincompat_enabled",
                    "Включить запуск Windows-приложений (подсистема WinCompat)",
                    "bool",
                    default=True,
                ),
                Prompt(
                    "wincompat_strict_network",
                    "Ограничивать сеть каждого Windows-приложения списком разрешённых адресов",
                    "bool",
                    default=True,
                    help="без прокси wc-net приложение сможет обращаться в сеть свободно",
                ),
            ]
            if self.facts.hypervisor_available:
                prompts.append(
                    Prompt(
                        "wincompat_vm",
                        "Разрешить запуск тяжёлых приложений в виртуальной машине (L3)",
                        "bool",
                        default=False,
                    )
                )
            return prompts
        if step is Step.PRIVACY:
            return [
                Prompt(
                    "telemetry",
                    "Обезличенная статистика запусков для реестра совместимости",
                    "choice",
                    choices=("off", "crashes", "full"),
                    default="off",
                    help="содержимое документов не собирается; по умолчанию выключено",
                )
            ]
        if step is Step.SUMMARY:
            return [Prompt("confirm", "Применить настроенное?", "bool", default=False)]
        return []

    def answer(self, key: str, value: object) -> None:
        for prompt in self.prompts():
            if prompt.key != key:
                continue
            if prompt.kind == "bool":
                if isinstance(value, str):
                    value = value.lower() in ("1", "true", "да", "yes")
                if not isinstance(value, bool):
                    raise WizardError(f"{key}: ожидается да/нет")
            elif prompt.kind == "choice":
                if str(value) not in prompt.choices:
                    raise WizardError(f"{key}: значение {value!r} вне списка {list(prompt.choices)}")
            elif prompt.kind == "str":
                if not str(value).strip():
                    raise WizardError(f"{key}: пусто")
            self.answers[key] = value
            return
        raise WizardError(f"шаг «{self.current.value}» не спрашивает про {key!r}")

    def answer_all_defaults(self) -> None:
        for prompt in self.prompts():
            if prompt.default is not None:
                self.answers[prompt.key] = prompt.default

    # --------------------------------------------------------------------- вывод
    def actions(self) -> list[dict]:
        """Что мастер прикажет сделать. Каждое действие — отдельная запись с причиной."""
        actions: list[dict] = []
        answers = self.answers
        if "timezone" in answers:
            actions.append(
                {"kind": "set-timezone", "value": answers["timezone"], "scope": "system",
                 "reason": "выбран на шаге региона"}
            )
        if "locale" in answers:
            actions.append({"kind": "set-locale", "value": answers["locale"], "scope": "system"})
        if "layouts" in answers:
            actions.append({"kind": "set-keyboard-layouts", "value": answers["layouts"], "scope": "session"})
        if "boot_default" in answers:
            actions.append(
                {"kind": "set-boot-default-os", "value": answers["boot_default"], "scope": "boot",
                 "requires_reboot": True}
            )
        if answers.get("clock_shared"):
            actions.append({"kind": "set-clock-policy", "value": "windows-utc", "scope": "boot",
                            "requires_reboot": True,
                            "note": "Windows потребуется переключить на UTC из своей системы"})
        if "ntfs_readonly" in answers:
            actions.append(
                {"kind": "set-ntfs-read-only", "value": bool(answers["ntfs_readonly"]), "scope": "system"}
            )
        if "wincompat_enabled" in answers:
            actions.append({"kind": "set-wincompat", "value": bool(answers["wincompat_enabled"]), "scope": "wincompat"})
        if answers.get("wincompat_strict_network"):
            actions.append({"kind": "set-wincompat-proxy", "value": True, "scope": "wincompat"})
        if "wincompat_vm" in answers:
            actions.append({"kind": "set-wincompat-vm", "value": bool(answers["wincompat_vm"]), "scope": "wincompat"})
        if "telemetry" in answers:
            actions.append({"kind": "set-telemetry", "value": answers["telemetry"], "scope": "session"})
        return actions

    def needs_root(self) -> bool:
        return any(action.get("scope") in ("boot", "system", "wincompat") for action in self.actions())

    def requires_reboot(self) -> bool:
        return any(action.get("requires_reboot") for action in self.actions())

    def summary(self) -> str:
        lines = [f"шаг {self.index + 1 if self.current is not Step.DONE else len(self.steps())} из {len(self.steps())}: {self.current.value}"]
        for action in self.actions():
            lines.append(f"  - {action['kind']} = {action.get('value')}"
                         + (" (нужна перезагрузка)" if action.get("requires_reboot") else ""))
            if action.get("note"):
                lines.append(f"      {action['note']}")
        if self.needs_root():
            lines.append("  часть действий требует прав администратора")
        return "\n".join(lines)
