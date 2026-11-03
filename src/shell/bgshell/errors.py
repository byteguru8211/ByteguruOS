"""Ошибки оболочки."""

from __future__ import annotations


class ShellError(Exception):
    """Базовая ошибка оболочки ByteguruOS."""


class SettingError(ShellError):
    """Настройка не существует или значение не проходит проверку."""


class WizardError(ShellError):
    """Мастер первого запуска получил ответ, которого не может быть на текущем шаге."""
