"""Ошибки установщика."""

from __future__ import annotations


class InstallerError(Exception):
    """Базовая ошибка установщика ByteguruOS."""


class PlanError(InstallerError):
    """Запрошенную раскладку невозможно собрать без потери данных."""


class ProbeError(InstallerError):
    """Вывод системной утилиты не разобрать: доверять такому описанию диска нельзя."""


class BootRepairError(InstallerError):
    """Ремонт загрузки невозможен в текущем состоянии EFI."""


class ApplyError(InstallerError):
    """Исполнение плана остановлено: условие не выполнено или команда недоступна."""
