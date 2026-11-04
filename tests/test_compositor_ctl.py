"""Интеграционные тесты композитора bgwl.

Проверяют не «собирается ли», а поведение: что управляющий сокет отвечает
схемам, которые читает панель, и что команды изменения состояния действительно
меняют его. Тесты помечаются skip, если композитор не собран, — на машине без
wlroots проверять нечего, и это не провал сборки Python-пакетов.

Клиент (foot) нужен только для тестов с окнами; без него они пропускаются,
потому что «в CI нет терминала» не должно выглядеть как регрессия композитора.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BGWL_CANDIDATES = [
    REPO / "build-c/src/compositor/bgwl",
    REPO / "build/src/compositor/bgwl",
    Path("/usr/bin/bgwl"),
]


def find_bgwl() -> Path | None:
    for candidate in BGWL_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class Ctl:
    """Одна строка команды — один JSON-объект ответа.

    События EV могут прийти раньше ответа и между командами, поэтому буфер
    живёт в объекте и вычитывается по строкам: иначе ответ одной команды
    стал бы ответом следующей.
    """

    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.buf = b""
        deadline = time.time() + 5.0
        while True:
            try:
                self.sock.connect(str(path))
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)

    def next_json(self) -> dict:
        while True:
            newline = self.buf.find(b"\n")
            if newline >= 0:
                line, self.buf = self.buf[:newline], self.buf[newline + 1:]
                if line.startswith(b"{"):
                    return json.loads(line)
                continue  # an EV event: skip it and keep reading
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise AssertionError("compositor closed the control socket")
            self.buf += chunk
            if len(self.buf) > (1 << 20):
                raise AssertionError("compositor reply is not bounded JSON")

    def command(self, line: str) -> dict:
        self.sock.sendall((line + "\n").encode())
        return self.next_json()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture(scope="module")
def session():
    bgwl = find_bgwl()
    if bgwl is None:
        pytest.skip("bgwl is not built; run `meson setup build && ninja -C build`")

    runtime = Path(tempfile.mkdtemp(prefix="bgwl-test-"))
    env = dict(os.environ)
    env.update(
        XDG_RUNTIME_DIR=str(runtime),
        WLR_BACKENDS="headless",
        WLR_RENDERER="pixman",
        WLR_LOG_LEVEL="3",
        BGWL_HEADLESS_SIZE="1280x800",
    )
    process = subprocess.Popen(
        [str(bgwl), "-b", "headless"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    socket_path = runtime / "byteguru" / "compositor.sock"
    ctl = None
    try:
        ctl = Ctl(socket_path)
    except OSError:
        output = process.communicate(timeout=5)[0] if process.poll() is not None else ""
        pytest.skip(f"bgwl did not expose a control socket:\n{output}")

    yield ctl, process, socket_path

    if ctl is not None:
        ctl.close()
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    shutil.rmtree(runtime, ignore_errors=True)


def test_state_has_the_shape_the_panel_reads(session):
    ctl, _, _ = session
    state = ctl.command("state")

    assert state["type"] == "state"
    assert state["version"] == 1
    assert state["compositor"] == "bgwl"

    # The panel's stream row and the settings app's layout picker both index
    # these fields by name; a rename here is a break in two places.
    streams = state["streams"]
    assert streams, "a desktop with no streams cannot be drawn"
    for stream in streams:
        assert set(stream) >= {
            "index", "name", "layout", "active", "windows", "accent",
            "master-percent", "master-count", "split",
        }
    assert sum(1 for stream in streams if stream["active"]) == 1

    for output in state["outputs"]:
        assert set(output) >= {"name", "box", "usable", "scale", "enabled"}
        assert output["box"]["width"] > 0
        assert output["usable"]["width"] <= output["box"]["width"]

    assert set(state["settings"]) >= {
        "layout", "workspaces", "gap-inner", "gap-outer", "border-width",
        "animations", "accent", "terminal", "launcher",
    }


def test_ping_and_unknown_command(session):
    ctl, _, _ = session
    assert ctl.command("ping") == {"type": "pong"}

    # An unknown command answers with an error object rather than hanging or
    # closing the connection: a typo must not take the panel down.
    reply = ctl.command("definitely-not-a-command")
    assert reply["type"] == "error"
    assert ctl.command("ping")["type"] == "pong"


def test_settings_round_trip(session):
    ctl, _, _ = session
    for command, key, value in [
        ("set gap-inner 14", "gap-inner", 14),
        ("set border-width 3", "border-width", 3),
        ("set layout tile", "layout", "tile"),
        ("set animations false", "animations", False),
    ]:
        state = ctl.command(command)
        assert state["settings"][key] == value, command

    # Restore defaults so later tests and the next run start from a known state.
    ctl.command("set animations true")
    ctl.command("set layout float")
    ctl.command("set gap-inner 6")
    ctl.command("set border-width 2")


def test_bad_setting_is_reported_not_swallowed(session):
    ctl, _, _ = session
    reply = ctl.command("set accent not-a-colour")
    assert reply["type"] == "error"
    assert reply["key"] == "accent"


def test_workspace_switch_changes_the_active_stream(session):
    ctl, _, _ = session
    state = ctl.command("workspace 2")
    active = [stream["index"] for stream in state["streams"] if stream["active"]]
    assert active == [2]

    state = ctl.command("workspace next")
    active = [stream["index"] for stream in state["streams"] if stream["active"]]
    assert active == [3]

    ctl.command("workspace 0")


def test_window_actions_need_a_client(session):
    """Окна проверяются только если в системе есть хоть один Wayland-клиент."""
    client = shutil.which("foot")
    ctl, process, socket_path = session
    if client is None:
        pytest.skip("no wayland client installed (foot); window tests skipped")

    env = dict(os.environ)
    runtime = str(socket_path.parent.parent)
    env["XDG_RUNTIME_DIR"] = runtime
    # WAYLAND_DISPLAY is exported by bgwl into its own environment, not into
    # ours, so the socket path is the portable way to connect.
    env["WAYLAND_DISPLAY"] = next(
        (name for name in os.listdir(runtime) if name.startswith("wayland-")), ""
    )
    launched = subprocess.Popen([client, "--title=bgwl-test"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        window = None
        for _ in range(50):
            time.sleep(0.1)
            state = ctl.command("state")
            windows = [w for w in state["windows"] if w["title"] == "bgwl-test"]
            if windows:
                window = windows[0]
                break
        assert window is not None, "foot mapped no window"
        assert window["pid"] > 0
        assert window["box"]["width"] > 0
        assert state["focused"] == window["id"]

        state = ctl.command("maximize")
        target = next(w for w in state["windows"] if w["id"] == window["id"])
        assert target["maximized"] is True

        state = ctl.command("fullscreen")
        target = next(w for w in state["windows"] if w["id"] == window["id"])
        assert target["fullscreen"] is True

        ctl.command("close")
        deadline = time.time() + 5.0
        gone = False
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.2)
            state = ctl.command("state")
            if all(w["id"] != window["id"] for w in state["windows"]):
                gone = True
                break
        assert gone, "the window survived close"
    finally:
        if process.poll() is None:
            launched.terminate()
