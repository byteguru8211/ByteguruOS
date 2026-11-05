"""Тесты десктопного слоя оболочки: палитра, системные индикаторы, клиент композитора.

Здесь нет GTK по той же причине, по какой он не нужен в bgshell.panel: проверяется
логика, а не отрисовка. Исключение — Compositor: он говорит по настоящему unix-
сокету, поэтому в тесте поднимается фейковый композитор на той же схеме «строка
команды → строка JSON».
"""

import json
import re
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / p) for p in
    ("src/shell", "src/bytegurud", "src/installer", "src/wincompat")]

from bgshell import systeminfo, themes  # noqa: E402
from bgshell.ctl import Compositor, Snapshot, socket_path  # noqa: E402


# ------------------------------------------------------------------- список программ
class _FakeService:
    """Минимум от bgd.Service: два метода, которыми пользуется оболочка."""

    def __init__(self, apps):
        self._apps = apps

    def apps(self):
        return list(self._apps)

    def find(self, query):
        return [app for app in self._apps if query.lower() in app.name.lower()]


def _app(name, kind_name, supported_coverage=""):
    from bgd.model import AppKind, Application

    return Application(id=name.lower(), name=name, kind=AppKind[kind_name],
        coverage=supported_coverage)


def test_windows_apps_hidden_until_subsystem_enabled(tmp_path):
    """Выключенная подсистема не показывается нигде — ни списком, ни поиском.

    Проверка нужна именно здесь, а не в bgd: демон обязан знать все источники,
    а решение «показывать или нет» принимает оболочка. Если фильтр оставить
    только в списке, поиск в лаунчере всё равно выдаст чужие программы, и
    «выключено» окажется словами.
    """
    from bgshell.settings import SettingsStore
    from bgshell.ui import bggui

    apps = [_app("Терминал", "LINUX"), _app("1С:Клиент", "WINCOMPAT", "l1"),
        _app("Режим ВМ", "VM", "l3"), _app("Перезагрузка в Windows", "REBOOT", "l4")]
    store = SettingsStore(path=tmp_path / "shell.ini")
    session = bggui.Session(settings=store, service=_FakeService(apps),
        theme_name="byteguru-dark")

    store.set("wincompat.enabled", False)
    assert [app.name for app in session.apps] == ["Терминал"]
    assert [app.name for app in session.find("1с")] == []

    store.set("wincompat.enabled", True)
    assert len(session.apps) == 4
    assert [app.name for app in session.find("1с")] == ["1С:Клиент"]


def test_coverage_badge_only_for_windows_apps():
    """Значок покрытия — про перенос чужих программ, а не про каждую иконку.

    Раньше «не поддерживается» висело на Firefox и LibreOffice: нативному
    приложению нечего переносить, и уровень покрытия у него отсутствует по
    определению.
    """
    from bgd.model import AppKind, Application
    from bgshell.ui import bggui

    native = Application(id="firefox", name="Firefox", kind=AppKind.LINUX)
    unproven = Application(id="1c", name="1С:Клиент", kind=AppKind.WINCOMPAT)
    proven = Application(id="nvim", name="Neovim", kind=AppKind.WINCOMPAT,
        coverage="l1", evidence="подтверждено прогонами")

    assert bggui.coverage_badge(native) == ("", "")
    assert bggui.coverage_badge(unproven)[0] == "не поддерживается"
    assert bggui.coverage_badge(proven)[0] == "L1"


# ------------------------------------------------------------------- палитра
def test_palette_derives_contrast_text():
    dark_accent = themes.Palette(accent="#1b3f8f", dark=True)   # тёмный синий
    bright_accent = themes.Palette(accent="#00ff88", dark=True)  # ядовитый зелёный
    # Текст на акценте выбирается по яркости подложки, а не «белый всегда».
    assert dark_accent.accent_fg == "#ffffff"
    assert bright_accent.accent_fg == "#101010"


def test_palette_rejects_malformed_accent():
    with pytest.raises(ValueError):
        themes.with_accent(themes.Palette(), "green")
    with pytest.raises(ValueError):
        themes.with_accent(themes.Palette(), "#12345")


def test_palette_css_contains_the_named_colors_the_shell_uses():
    css = themes.Palette(accent="#4a9eff").css()
    for marker in ("@define-color accent_bg_color #4a9eff;",
            ".bg-panel", ".bg-pill-active", "@define-color window_bg_color"):
        assert marker in css


def test_write_compositor_conf_round_trips_into_bgwl_parser(tmp_path):
    """Конфиг, который пишет оболочка, должен читать композитор.

    Это единственная проверка совместности двух реализаций формата без запуска
    C-бинарника: строки те же, что разбирает apply_config_line в util.c.
    """
    palette = themes.Palette(accent="#3ecf8e", dark=True)
    path = tmp_path / "bgwl.conf"
    themes.write_compositor_conf(palette, [(0, "код", "#3ecf8e"),
        (1, "сеть", "#4a9eff")], path=path)
    text = path.read_text(encoding="utf-8")

    assert "accent-color = #3ecf8e" in text
    assert "workspace = 1:код:#3ecf8e" in text
    assert "workspace = 2:сеть:#4a9eff" in text
    # Ключи, которые util.c не знает, попали бы в лог как «unknown key» и
    # остались бы незамеченными пользователем.
    known = ("terminal", "launcher", "file-manager", "screenshot",
        "lock-command", "lid-command", "cursor-theme", "xkb-layout",
        "workspace", "rule", "bind", "background-color", "accent-color",
        "border-color", "decor-color", "text-color", "background", "layout",
        "workspaces", "gap-inner", "gap-outer", "border-width", "cursor-size",
        "idle-timeout", "output-scale", "headless-size", "animations",
        "anim-duration", "focus-follows-mouse", "server-side-decoration")
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key = line.split("=")[0].strip()
        assert key in known, f"bgwl.conf writes unknown key {key!r}"


def test_apply_writes_files_and_tells_compositor(tmp_path, monkeypatch):
    monkeypatch.setattr(themes, "THEME_CSS", tmp_path / "theme.css")
    calls = []

    class FakeCompositor:
        def set(self, key, value):
            calls.append((key, value))
            return {"type": "state"}

    applied = themes.apply(themes.Palette(accent="#ff5c7a"),
        FakeCompositor(), streams=[(0, "код", "#ff5c7a")])
    assert (tmp_path / "theme.css").is_file()
    assert applied == ["css", "conf", "compositor"]
    assert calls == [("accent", "#ff5c7a")]


def test_apply_reports_compositor_failure_instead_of_hiding_it(tmp_path,
        monkeypatch):
    monkeypatch.setattr(themes, "THEME_CSS", tmp_path / "theme.css")

    class Broken:
        def set(self, key, value):
            raise RuntimeError("socket closed")

    applied = themes.apply(themes.Palette(), Broken())
    assert any(note.startswith("compositor:") for note in applied)


# ------------------------------------------------------- системные индикаторы
def test_network_icon_follows_kind_and_state():
    assert systeminfo.Network(connected=False).icon == \
        "network-wired-disconnected-symbolic"
    assert systeminfo.Network(connected=True, kind="wifi").icon == \
        "network-wireless-signal-good-symbolic"
    assert systeminfo.Network(connected=True, kind="wifi", strong=False).icon == \
        "network-wireless-signal-weak-symbolic"
    assert systeminfo.Network(connected=True, kind="ethernet").icon == \
        "network-wired-symbolic"


def test_battery_labels_and_thresholds():
    assert systeminfo.Battery(percent=10, charging=False, present=True).icon == \
        "battery-caution-symbolic"
    assert systeminfo.Battery(percent=10, charging=True, present=True).icon == \
        "battery-caution-charging-symbolic"
    assert systeminfo.Battery(percent=70, charging=False, present=True).icon == \
        "battery-good-symbolic"
    assert systeminfo.Battery(percent=50, charging=False, present=True,
        time_minutes=95).label == "50% · 1ч 35м"
    assert systeminfo.Battery(percent=0, charging=False, present=False).label == "сеть"


def test_collect_degrades_without_any_service(monkeypatch):
    """Ни NetworkManager, ни UPower, ни PipeWire: панель не падает."""
    for name in ("read_network", "read_battery", "read_audio", "read_brightness"):
        monkeypatch.setattr(systeminfo, name, lambda: None)
    info = systeminfo.collect()
    assert info.network is None and info.audio is None
    assert "network" in info.warnings and "audio" in info.warnings


def test_read_network_parses_nmcli_table(monkeypatch):
    # nmcli экранирует только тот ':', что входит в имя: вывод для SSID
    # «My SSID:5GHz» — wifi:My SSID\:5GHz:connected
    machine_line = "wifi:My SSID\\:5GHz:connected\n"
    monkeypatch.setattr(systeminfo, "_run",
        lambda args, timeout=2.0: machine_line
            if args[:2] == ["nmcli", "-t"] else None)
    network = systeminfo.read_network()
    assert network.kind == "wifi"
    assert network.description == "My SSID:5GHz"


def test_read_network_without_nmcli_falls_back_to_dbus(monkeypatch):
    monkeypatch.setattr(systeminfo, "_run", lambda args, timeout=2.0: None)
    monkeypatch.setattr(systeminfo, "_dbus_get",
        lambda *a, **kw: "(variant boolean, true>)" if a[3] == "State" else None)
    network = systeminfo.read_network()
    assert network is not None and network.connected


def test_read_audio_handles_pactl_format(monkeypatch):
    replies = {
        ("pactl", "get-sink-volume"): "Volume: front-left: 45872 /  70% / -9.34 dB\n",
        ("pactl", "get-sink-mute"): "Muted: no\n",
    }

    def fake_run(args, timeout=2.0):
        return replies.get(tuple(args[:2]))

    monkeypatch.setattr(systeminfo, "_run", fake_run)
    audio = systeminfo.read_audio()
    assert audio.volume == 70 and audio.muted is False
    assert audio.icon == "audio-volume-medium-symbolic"


def test_needs_attention_only_on_low_unplugged_battery():
    info = systeminfo.SystemInfo(
        battery=systeminfo.Battery(percent=15, charging=False, present=True))
    assert info.needs_attention
    charged = systeminfo.SystemInfo(
        battery=systeminfo.Battery(percent=15, charging=True, present=True))
    assert not charged.needs_attention


# ------------------------------------------------------------- клиент bgwl
STATE_FIXTURE = {
    "type": "state", "version": 1, "compositor": "bgwl",
    "locked": False, "panel_visible": True,
    "settings": {"layout": "tile", "workspaces": 6, "gap-inner": 6,
        "gap-outer": 8, "border-width": 2, "animations": True,
        "anim-duration": 140, "focus-follows-mouse": False,
        "idle-timeout": 300, "accent": "#f2a33d", "terminal": "foot",
        "launcher": "bglauncher", "background": ""},
    "streams": [
        {"index": 0, "name": "поток 1", "layout": "tile", "active": True,
            "master-percent": 55, "master-count": 1, "split": "right",
            "windows": 2, "accent": "#f2a33d"},
        {"index": 1, "name": "код", "layout": "even", "active": False,
            "master-percent": 55, "master-count": 1, "split": "right",
            "windows": 0, "accent": "#4a9eff"},
    ],
    "outputs": [{"name": "eDP-1", "description": "Panel",
        "box": {"x": 0, "y": 0, "width": 1920, "height": 1200},
        "usable": {"x": 0, "y": 34, "width": 1920, "height": 1166},
        "scale": 1.5, "enabled": True, "refresh": 60000}],
    "windows": [
        {"id": 1, "app_id": "foot", "title": "сборка", "workspace": 0,
            "pid": 4242, "focused": True, "tiled": True, "maximized": False,
            "fullscreen": False, "above": False, "sticky": False,
            "decorated": True, "mapped": True, "accent": "#f2a33d",
            "box": {"x": 8, "y": 42, "width": 1050, "height": 1124}},
        {"id": 2, "app_id": "firefox", "title": "", "workspace": 0,
            "pid": 4243, "focused": False, "tiled": True, "maximized": False,
            "fullscreen": False, "above": True, "sticky": False,
            "decorated": True, "mapped": True, "accent": "#f2a33d",
            "box": {"x": 1066, "y": 42, "width": 846, "height": 1124}},
    ],
    "focused": 1,
}


class FakeCompositorServer:
    """Минимальная подделка bgwl_ctl: та же схема «строка → JSON»."""

    def __init__(self, path):
        self.path = path
        self.received = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                line = conn.recv(4096).split(b"\n")[0].decode()
                self.received.append(line)
                if line.startswith("set accent bad"):
                    conn.sendall(b'{"type":"error","error":"bad colour","key":"accent"}\n')
                elif line == "ping":
                    conn.sendall(b'{"type":"pong"}\n')
                else:
                    conn.sendall(json.dumps(STATE_FIXTURE).encode() + b"\n")

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.close()


@pytest.fixture
def fake_bgwl(tmp_path, monkeypatch):
    path = tmp_path / "compositor.sock"
    server = FakeCompositorServer(path)
    server.start()
    monkeypatch.setenv("BGWL_CONTROL_SOCKET", str(path))
    yield server
    server.stop()


def test_socket_path_prefers_environment(fake_bgwl):
    assert socket_path() == Path(os.environ["BGWL_CONTROL_SOCKET"])


def test_state_snapshot_exposes_the_desktop_model(fake_bgwl):
    snapshot = Compositor().state()
    assert snapshot.active_stream.index == 0
    assert snapshot.stream(1).accent == "#4a9eff"
    assert [w.id for w in snapshot.windows_on(0)] == [1, 2]
    assert snapshot.windows_on(1) == []
    assert snapshot.outputs[0].usable == (0, 34, 1920, 1166)
    assert snapshot.settings["launcher"] == "bglauncher"

    foot, firefox = snapshot.windows
    assert foot.focused and foot.label == "сборка"
    assert firefox.label == "firefox", "пустой title не должен давать пустую строку"
    assert firefox.above and not firefox.focused


def test_command_returns_state_and_raises_on_error(fake_bgwl):
    compositor = Compositor()
    assert compositor.command("workspace 1")["type"] == "state"
    with pytest.raises(RuntimeError):
        compositor.set("accent", "bad")


def test_missing_socket_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BGWL_CONTROL_SOCKET", str(tmp_path / "nothing.sock"))
    with pytest.raises(RuntimeError):
        Compositor().state()


# ---------------------------------------------------------------------- упаковка
def test_every_launcher_is_installed_by_some_package():
    """Каждый скрипт из bin/ обязан попасть в .deb.

    dh_missing падает на этом сам, но только при сборке пакетов в контейнере,
    то есть постфактум. Здесь проверка стоит в обычном прогоне тестов:
    новый бинарник оболочки без строки в *.install — это «в образе команды
    нет», что пользователь замечает первым.
    """
    root = Path(__file__).resolve().parent.parent
    installed = set()
    for listing in (root / "debian").glob("*.install"):
        for line in listing.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                installed.add(line.split()[0])
    missing = sorted(
        f"usr/bin/{script.name}" for script in (root / "bin").iterdir()
        if script.is_file() and not script.name.endswith(".pyc")
        and f"usr/bin/{script.name}" not in installed)
    assert not missing, f"не указаны в debian/*.install: {missing}"


# ------------------------------------------------------------------------ bgctl
def _load_bgctl():
    """bgctl — исполняемый файл без .py: spec сам его не распознаёт."""
    import importlib.machinery
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "bin" / "bgctl"
    loader = importlib.machinery.SourceFileLoader("bgctl_module", str(path))
    spec = importlib.util.spec_from_loader("bgctl_module", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_bgctl_json_flag_works_on_both_sides_of_the_subcommand():
    """`bgctl --json status` обязан печатать JSON, а не текст.

    argparse с parents+subparsers коварен: подкоманда переписывает значение
    флага, указанное до неё. Без default=SUPPRESS флаг работал только в форме
    `bgctl status --json`, то есть ровно наоборот тому, как его пишут в
    скриптах.
    """
    bgctl = _load_bgctl()
    parser = bgctl.build_parser()
    assert bgctl._as_json(parser.parse_args(["--json", "status"])) is True
    assert bgctl._as_json(parser.parse_args(["status", "--json"])) is True
    assert bgctl._as_json(parser.parse_args(["status"])) is False


def test_bgctl_exposes_run_action():
    """Действия сеанса доступны и человеку, а не только палитре."""
    bgctl = _load_bgctl()
    args = bgctl.build_parser().parse_args(["run", "launcher"])
    assert args.template == "run {}"


def test_every_man_page_is_installed_by_some_package():
    """Справочная страница, не попавшая в пакет, равна её отсутствию.

    В live-сессии `man bginstaller` — единственный способ прочитать про
    токены подтверждения и флаги apply, а ISO пересобирается не ради
    документации. Значит страница обязана быть в .deb, и проверять это
    нужно в обычном прогоне тестов.
    """
    root = Path(__file__).resolve().parent.parent
    installed = set()
    for listing in (root / "debian").glob("*.install"):
        for line in listing.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                installed.add(line.split()[0])
    pages = sorted(p.name for p in (root / "docs" / "man").iterdir())
    missing = [name for name in pages
        if f"usr/share/man/man{name[-1]}/{name}" not in installed]
    assert not missing, f"страницы не установлены ни одним пакетом: {missing}"


# ------------------------------------------------------------- документация
def _docs_files(root: Path):
    return [p for p in [root / "README.md", root / "CONTRIBUTING.md",
        *sorted((root / "docs").rglob("*.md"))] if p.is_file()]


def test_man_references_point_to_existing_pages():
    """`man bginstaller` в тексте обязан вести на существующую страницу.

    Документация обещает справку в терминале; ссылка на несуществующую
    страницу — это ровно то, что пользователь проверяет первым и что ломается
    незаметно, потому что текст при этом читается правильно.
    """
    root = Path(__file__).resolve().parent.parent
    pages = {p.name for p in (root / "docs" / "man").iterdir()}
    stems = {name.rsplit(".", 1)[0] for name in pages}
    missing = set()
    for doc in _docs_files(root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        for reference in re.findall(r"`man ([a-z0-9-]+)`", text):
            if reference not in stems:
                missing.add(f"{doc.relative_to(root)}: man {reference}")
    assert not missing, sorted(missing)


def test_commands_in_documented_examples_exist():
    """Команды из блоков `sh` в документации должны существовать.

    Проверка по всей прозе была бы шумом: там осознанно упоминаются имена,
    которых нет (история дефекта про bg-mount-target читается правильно).
    А пример из code fence копируют в терминал — выдуманная команда в нём
    стоит читателю потерянного вечера.
    """
    root = Path(__file__).resolve().parent.parent
    ours = {p.name for p in (root / "bin").iterdir()}
    ours |= {p.name for p in (root / "iso" / "overlay" / "usr" / "local"
        / "bin").iterdir()}
    ours |= {p.name for p in (root / "iso" / "overlay" / "usr" / "local"
        / "sbin").iterdir()}
    # Внешние утилиты, которые документация имеет право показывать: они
    # приходят с Debian, а не с этим репозиторием.
    external = {"apt-get", "bash", "python", "python3", "pip", "pytest", "git",
        "ls", "lsblk", "lsmod", "meson", "ninja", "pkg-config", "efibootmgr",
        "grub-install", "mount", "umount", "sudo", "mkdir", "cat", "pgrep",
        "journalctl", "man", "systemctl", "systemd-detect-virt", "pkexec",
        "grim", "notify-send", "udisksctl", "partprobe", "udevadm",
        "sha256sum", "Get-FileHash"}
    unknown = set()
    for doc in _docs_files(root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        for block in re.findall(r"```(?:sh|bash|powershell)\n(.*?)```", text, re.S):
            for line in block.splitlines():
                line = line.strip().lstrip("$").strip()
                if not line or line.startswith("#"):
                    continue
                # Префиксы окружения (PYTHONPATH=... python -m ...) — не команда.
                while re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
                    parts = line.split(None, 1)
                    line = parts[1] if len(parts) == 2 else ""
                if not line:
                    continue
                first = line.split()[0].rstrip("\\")
                if first.startswith(("-", "|", "&&")):
                    continue
                if first not in ours | external:
                    unknown.add(f"{doc.relative_to(root)}: {first}")
    assert not unknown, sorted(unknown)


def test_session_says_out_loud_when_there_is_no_screen(tmp_path):
    """Сеанс без экрана выглядит для человека как зависшая загрузка.

    Композитор без KMS уходит на headless-вывод: сокет есть, всё отвечает, а
    на мониторе остаётся текстовая консоль. Молчать в этом случае — значит
    отправить человека разбирать «зависание» вместо одной строчки на экране.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir()
    # Настоящий unix-сокет: скрипт проверяет именно тип файла, а не наличие.
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(runtime / "wayland-0"))
    listener.listen(1)

    (stubs / "bgwl").write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    # Один и тот же bgctl отвечает и на status, и на что угодно ещё — только
    # headless-выводом.
    (stubs / "bgctl").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"compositor\":\"bgwl\",\"outputs\":"
        '[{"name":"HEADLESS-1","headless":true}]}"\n',
        encoding="utf-8")
    for stub in stubs.iterdir():
        stub.chmod(0o755)

    script = Path(__file__).resolve().parents[1] / "bin" / "bgwl-session"
    env = dict(os.environ,
        PATH=f"{stubs}:/usr/bin:/bin",
        XDG_RUNTIME_DIR=str(runtime),
        HOME=str(tmp_path),
        XDG_STATE_HOME=str(tmp_path / "state"),
        WAYLAND_DISPLAY="wayland-0",
        BGWL_SESSION_SCREEN_TRIES="1")
    try:
        proc = subprocess.run(["/bin/sh", str(script)], env=env,
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL)
    finally:
        listener.close()
    assert "ни одного экрана" in proc.stderr
