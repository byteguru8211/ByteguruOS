"""Ошибки подсистемы WinCompat.

Иераррия сознательно мелкая: CLI различает «манифест составлен неверно»
(вина автора манифеста, чинится правкой файла) и «система не может выполнить
действие» (вина окружения, чинится установкой/правами).
"""

from __future__ import annotations


class WcvError(Exception):
    """Базовая ошибка WinCompat."""


class ManifestError(WcvError):
    """Манифест не проходит схему: неизвестные ключи, типы, значения."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) or "manifest не валиден")


class PrefixError(WcvError):
    """Проблема с окружением WCV (создание, откат, целостность)."""


class SandboxError(WcvError):
    """Политика изоляции не может быть построена для запрошенного манифеста."""


class LaunchError(WcvError):
    """Запуск refused до обращения к ядру: не хватает данных или небезопасно."""


class RegistryError(WcvError):
    """Реестр совместимости повреждён или используется не по назначению."""
