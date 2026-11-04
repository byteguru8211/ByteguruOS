"""Оболочка ByteguruOS: модель состояния, настройки, мастер первого запуска."""

from __future__ import annotations

from bgshell.boot import SwitchResult, describe, reboot_into, set_default
from bgshell.errors import SettingError, ShellError, WizardError
from bgshell.firstboot import Prompt, Step, SystemFacts, Wizard
from bgshell.panel import Notification, NotifKind, PanelModel, WindowInfo
from bgshell.settings import Scope, Setting, SettingsStore, validate

__version__ = "0.0.1"

__all__ = [
    "Notification",
    "NotifKind",
    "PanelModel",
    "Prompt",
    "Scope",
    "Setting",
    "SettingsStore",
    "ShellError",
    "Step",
    "SwitchResult",
    "SystemFacts",
    "SettingError",
    "WindowInfo",
    "Wizard",
    "WizardError",
    "__version__",
    "describe",
    "reboot_into",
    "set_default",
    "validate",
]
