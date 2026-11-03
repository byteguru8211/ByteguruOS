"""Модель панели задач и рабочего стола.

Отвязано от GTK: здесь только состояние и его переходы, поэтому панель
проверяется тестами без дисплея. GTK-слой (`bgshell.ui`) reads именно отсюда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from bgd.model import AppKind, Application


class NotifKind(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    UPDATE = "update"


WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


@dataclass(frozen=True)
class Notification:
    id: str
    kind: NotifKind
    title: str
    body: str = ""
    action_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "body": self.body,
            "action_id": self.action_id,
        }


@dataclass(frozen=True)
class WindowInfo:
    app_id: str
    title: str
    focused: bool = False
    minimized: bool = False


@dataclass
class PanelModel:
    """Всё, что панель показывает в одну секунду времени."""

    apps: list[Application] = field(default_factory=list)
    windows: list[WindowInfo] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)
    favorites: list[str] = field(default_factory=list)
    default_os: str = "byteguru"
    clock: datetime | None = None
    layout_name: str = "us"
    volume: int = 60
    muted: bool = False
    battery_percent: int | None = None
    charging: bool = True
    network_connected: bool = True
    integrity_ok: bool = True
    wincompat_running: int = 0
    windows_partition_present: bool = False
    search_query: str = ""

    # ------------------------------------------------------------------ часы
    def clock_text(self) -> str:
        if self.clock is None:
            return "--:--"
        return self.clock.strftime("%H:%M")

    def date_text(self) -> str:
        if self.clock is None:
            return ""
        return f"{WEEKDAYS[self.clock.weekday()]}, {self.clock.day} {MONTHS[self.clock.month - 1]}"

    # -------------------------------------------------------------- приложения
    def launcher_apps(self) -> list[Application]:
        """Порядок в лаунчере: избранные первыми, затем алфавит, затем «нужна Windows»."""
        ranked = sorted(
            self.apps,
            key=lambda app: (
                0 if app.id in self.favorites else 1,
                1 if app.kind in (AppKind.REBOOT, AppKind.VM) else 0,
                app.name.lower(),
            ),
        )
        return ranked

    def search(self, query: str | None = None) -> list[Application]:
        text = self.search_query if query is None else query
        if not text.strip():
            return self.launcher_apps()
        lowered = text.strip().lower()
        matched = [
            app
            for app in self.launcher_apps()
            if lowered in app.search_blob()
        ]
        return matched

    def running_windows(self) -> list[WindowInfo]:
        return [window for window in self.windows]

    def focused_window(self) -> WindowInfo | None:
        for window in self.windows:
            if window.focused:
                return window
        return None

    def taskbar_entries(self) -> list[tuple[WindowInfo, Application | None]]:
        by_id = {app.id.lower(): app for app in self.apps}
        return [
            (window, by_id.get(window.app_id.lower()))
            for window in self.windows
        ]

    # ------------------------------------------------------------ уведомления
    def notify(self, notification: Notification) -> None:
        self.notifications = [item for item in self.notifications if item.id != notification.id]
        self.notifications.insert(0, notification)
        self.notifications = self.notifications[:12]

    def dismiss(self, notification_id: str) -> bool:
        before = len(self.notifications)
        self.notifications = [item for item in self.notifications if item.id != notification_id]
        return len(self.notifications) < before

    def unread(self) -> int:
        return len(self.notifications)

    def critical(self) -> list[Notification]:
        return [item for item in self.notifications if item.kind is NotifKind.CRITICAL]

    # ----------------------------------------------------------- переключение ОС
    def boot_menu_items(self) -> list[tuple[str, str]]:
        items = [("byteguru", "ByteguruOS"), ("windows", "Windows")]
        if self.default_os not in ("byteguru", "windows", "last"):
            items.append((self.default_os, self.default_os))
        items.append(("last", "Как при последнем запуске"))
        return items

    def shows_windows_switcher(self) -> bool:
        """Переключатель имеет смысл, только если на диске реально найдена Windows."""
        return self.windows_partition_present

    def status_summary(self) -> dict[str, object]:
        return {
            "clock": self.clock_text(),
            "date": self.date_text(),
            "layout": self.layout_name,
            "volume": self.volume,
            "muted": self.muted,
            "network": "онлайн" if self.network_connected else "офлайн",
            "battery": self.battery_percent,
            "charging": self.charging,
            "integrity": "ok" if self.integrity_ok else "modified",
            "wincompat_running": self.wincompat_running,
            "notifications": len(self.notifications),
            "default_os": self.default_os,
        }
