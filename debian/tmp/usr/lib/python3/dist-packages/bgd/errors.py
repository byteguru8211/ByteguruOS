"""Ошибки демона."""

from __future__ import annotations


class BgdError(Exception):
    """Базовая ошибка bytegurud."""


class SourceError(BgdError):
    """Источник приложений не читается: вывод утилиты или каталог манифестов."""
