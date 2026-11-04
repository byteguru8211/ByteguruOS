"""Клиент управляющего сокета bgwl.

Оболочка не лезет в внутренности композитора и не парсит xdg: она читает то,
что композитор сам считает состоянием сеанса, и изменяет его теми же командами,
которыми их набирает человек в `nc -U`. Отсюда два правила:

- одно соединение на команды, другое на подписку. Команда ждёт ответ, события
  приходят когда хотят; в одном сокете они бы перемешались, и это источник
  классических гонок в панелях;
- ни одна команда не молчит. Не удалось — CompositorError, панель покажет
  причину, а не пустое место.

Путь сокета: $BGWL_CONTROL_SOCKET, иначе $XDG_RUNTIME_DIR/byteguru/compositor.sock.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


class CompositorError(RuntimeError):
    """Композитор недоступен или ответил ошибкой."""


SOCKET_NAME = "compositor.sock"


def socket_path() -> Path:
    explicit = os.environ.get("BGWL_CONTROL_SOCKET")
    if explicit:
        return Path(explicit)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(runtime) / "byteguru" / SOCKET_NAME


@dataclass
class Window:
    """Одно окно так, как его видит композитор.

    Поля — ровно то, что нужно списку окон в панели; всё остальное в state()
    остаётся доступным через raw, чтобы новый атрибут не требовал правки
    каждого потребителя.
    """

    id: int
    app_id: str
    title: str
    workspace: int
    pid: int
    focused: bool
    tiled: bool
    maximized: bool
    fullscreen: bool
    above: bool
    sticky: bool
    mapped: bool
    box: tuple[int, int, int, int]
    accent: str
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return self.title or self.app_id or "окно"

    @staticmethod
    def from_state(entry: dict) -> "Window":
        box = entry.get("box", {})
        return Window(
            id=entry["id"],
            app_id=entry.get("app_id", ""),
            title=entry.get("title", ""),
            workspace=entry.get("workspace", -1),
            pid=entry.get("pid", -1),
            focused=bool(entry.get("focused")),
            tiled=bool(entry.get("tiled")),
            maximized=bool(entry.get("maximized")),
            fullscreen=bool(entry.get("fullscreen")),
            above=bool(entry.get("above")),
            sticky=bool(entry.get("sticky")),
            mapped=bool(entry.get("mapped")),
            box=(box.get("x", 0), box.get("y", 0), box.get("width", 0),
                 box.get("height", 0)),
            accent=entry.get("accent", ""),
            raw=entry,
        )


@dataclass
class Stream:
    """Поток — рабочее пространство с собственным именем и акцентом."""

    index: int
    name: str
    layout: str
    active: bool
    windows: int
    accent: str
    master_percent: int
    master_count: int
    split: str

    @staticmethod
    def from_state(entry: dict) -> "Stream":
        return Stream(
            index=entry["index"],
            name=entry.get("name", ""),
            layout=entry.get("layout", "float"),
            active=bool(entry.get("active")),
            windows=int(entry.get("windows", 0)),
            accent=entry.get("accent", ""),
            master_percent=int(entry.get("master-percent", 55)),
            master_count=int(entry.get("master-count", 1)),
            split=entry.get("split", "right"),
        )


@dataclass
class Output:
    name: str
    description: str
    box: tuple[int, int, int, int]
    usable: tuple[int, int, int, int]
    scale: float
    enabled: bool
    refresh: int

    @staticmethod
    def from_state(entry: dict) -> "Output":
        def rect(key: str) -> tuple[int, int, int, int]:
            value = entry.get(key, {})
            return (value.get("x", 0), value.get("y", 0),
                    value.get("width", 0), value.get("height", 0))

        return Output(
            name=entry.get("name", ""),
            description=entry.get("description", ""),
            box=rect("box"),
            usable=rect("usable"),
            scale=float(entry.get("scale", 1.0)),
            enabled=bool(entry.get("enabled", True)),
            refresh=int(entry.get("refresh", 0)),
        )


@dataclass
class Snapshot:
    """Мгновенный снимок сеанса."""

    locked: bool
    panel_visible: bool
    streams: list[Stream]
    outputs: list[Output]
    windows: list[Window]
    settings: dict
    focused: int
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def active_stream(self) -> Stream | None:
        for stream in self.streams:
            if stream.active:
                return stream
        return None

    def stream(self, index: int) -> Stream | None:
        for stream in self.streams:
            if stream.index == index:
                return stream
        return None

    def windows_on(self, index: int) -> list[Window]:
        return [w for w in self.windows if w.workspace == index and w.mapped]

    @staticmethod
    def from_state(state: dict) -> "Snapshot":
        return Snapshot(
            locked=bool(state.get("locked")),
            panel_visible=bool(state.get("panel_visible", True)),
            streams=[Stream.from_state(e) for e in state.get("streams", [])],
            outputs=[Output.from_state(e) for e in state.get("outputs", [])],
            windows=[Window.from_state(e) for e in state.get("windows", [])],
            settings=dict(state.get("settings", {})),
            focused=int(state.get("focused", -1)),
            raw=state,
        )


class Compositor:
    """Синхронный клиент команд."""

    def __init__(self, path: Path | None = None):
        self.path = path or socket_path()

    def _ask(self, line: str) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3.0)
                sock.connect(str(self.path))
                sock.sendall((line + "\n").encode())
                # Ответ — один JSON-объект; события EV могут прийти раньше него
                # (подписанный клиент тот же), поэтому читаем строками.
                buffer = b""
                while True:
                    chunk = sock.recv(1 << 16)
                    if not chunk:
                        raise CompositorError("compositor closed the connection")
                    buffer += chunk
                    for candidate in buffer.split(b"\n"):
                        if candidate.startswith(b"{"):
                            return json.loads(candidate)
                    if len(buffer) > (1 << 20):
                        raise CompositorError("compositor reply is unbounded")
        except FileNotFoundError as error:
            raise CompositorError(f"no compositor socket at {self.path}") from error
        except OSError as error:
            raise CompositorError(f"compositor socket error: {error}") from error

    def state(self) -> Snapshot:
        return Snapshot.from_state(self._ask("state"))

    def command(self, line: str) -> dict:
        """Отправляет команду и возвращает ответ.

        Ошибку композитора (`{"type":"error"}`) поднимаем как CompositorError:
        молча проигнорированный `set accent=зелёный` — это настройки, которые
        «сохранились», но не подействовали.
        """
        reply = self._ask(line)
        if reply.get("type") == "error":
            raise CompositorError(reply.get("error", "unknown compositor error"))
        return reply

    def set(self, key: str, value: object) -> dict:
        return self.command(f"set {key} {value}")

    def switch_stream(self, index: int) -> dict:
        return self.command(f"workspace {index}")

    def send_window(self, index: int) -> dict:
        return self.command(f"send {index}")

    def run(self, action: str) -> dict:
        """Действие по имени (terminal, launcher, lock) или командная строка."""
        return self.command(f"run {action}")

    def lock(self) -> dict:
        return self.command("lock")

    def quit(self) -> dict:
        return self.command("quit")


class EventSource:
    """Подписка на события композитора для главного цикла GTK.

    Использует GLib.io_add_watch, а не свой поток: GTK-виджеты можно трогать
    только из цикла, который их создал, и это единственный способ получить
    событие из сокета в тот же цикл.
    """

    def __init__(self, on_event: Callable[[str], None],
                 path: Path | None = None):
        self.on_event = on_event
        self.path = path or socket_path()
        self._sock: socket.socket | None = None
        self._tag = None
        self._buffer = b""

    def start(self) -> bool:
        """True, если подписались. Без подписки панель всё равно работает:
        она опрашивает состояние по таймеру."""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.setblocking(False)
            sock.connect(str(self.path))
        except OSError:
            return False

        sock.sendall(b"subscribe\n")
        self._sock = sock
        try:
            from gi.repository import GLib  # noqa: PLC0415 — только в сеансе

            self._tag = GLib.io_add_watch(
                sock.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                self._handle)
        except (ImportError, AttributeError):  # pragma: no cover — без GTK
            return False
        return True

    def stop(self) -> None:
        from gi.repository import GLib  # noqa: PLC0415

        if self._tag is not None:
            GLib.source_remove(self._tag)
            self._tag = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _handle(self, _source, condition) -> bool:
        """Возвращает False, когда соединение закрыто: watch сам снимается."""
        from gi.repository import GLib  # noqa: PLC0415

        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self._sock = None
            return False
        try:
            chunk = self._sock.recv(1 << 16)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            return False
        if not chunk:
            self._sock = None
            return False

        self._buffer += chunk
        lines: Iterable[bytes] = self._buffer.split(b"\n")
        self._buffer = lines.pop()  # незавершённый хвост остаётся в буфере
        for line in lines:
            if line.startswith(b"EV "):
                self.on_event(line[3:].decode("utf-8", "replace").strip())
            elif line.startswith(b"{"):
                # Ответ команды, который мы не ждали: он относится к другому
                # соединению, но проглотить его безопаснее, чем разваливать
                # подписку.
                continue
        return True
