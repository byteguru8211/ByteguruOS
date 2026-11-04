"""Unix-socket демон: одна строка JSON на запрос, одна на ответ.

Протокол сознательно простой: оболочка, панель и утилиты говорят по одному и
тому же API, а не размножают логику. Никакого shell в обработчике нет.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from bgd.api import Context, Service

MAX_MESSAGE = 1024 * 1024


def default_socket_path() -> Path:
    """Сокет живёт в XDG_RUNTIME_DIR: он существует только у активного сеанса."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return Path(runtime) / "byteguru" / "apps.sock"


class Daemon:
    def __init__(self, service: Service, socket_path: str | Path) -> None:
        self.service = service
        self.socket_path = Path(socket_path)
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        self.socket_path.chmod(0o660)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_MESSAGE:
                    response = {"ok": False, "error": "слишком длинный запрос"}
                else:
                    response = self._respond(line)
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()

    def _respond(self, raw: bytes) -> dict:
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "error": "запрос должен быть JSON-объектом в одной строке"}
        if not isinstance(request, dict):
            return {"ok": False, "error": "запрос должен быть JSON-объектом"}
        try:
            return self.service.handle(request)
        except Exception as exc:  # демон не должен падать от одного кривого запроса
            return {"ok": False, "error": f"внутренняя ошибка: {type(exc).__name__}: {exc}"}


async def _amain(socket_path: str, context: Context) -> None:
    service = Service(context)
    daemon = Daemon(service, socket_path)
    await daemon.start()
    try:
        assert daemon._server is not None
        await daemon._server.serve_forever()
    finally:
        await daemon.stop()
        service.close()


def run(socket_path: str | None = None, context: Context | None = None) -> int:
    target = str(socket_path or default_socket_path())
    try:
        asyncio.run(_amain(target, context or Context()))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"не удалось поднять сокет {target}: {exc}")
        return 1
    return 0
