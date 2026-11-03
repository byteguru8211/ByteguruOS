"""Проверки схемы WCV-манифеста: то, что защищает от «тихо неверной политики»."""

from __future__ import annotations

import pytest

from wcmgr.errors import ManifestError
from wcmgr.manifest import (
    CoverageLevel,
    DriveKind,
    GraphPath,
    NetworkMode,
    PrintMode,
    Runner,
    dump_toml,
    load_manifest_text,
    validate_manifest_text,
)

MINIMAL = """
[wcv]
schema = 1
id = "notepad-plus"
name = "Notepad++"
version = "8.6.2"
executable = "notepad++.exe"
runner = "wine"
expected_level = "l1"
"""

FULL = """
[wcv]
schema = 1
id = "1c-client-8-3"
name = "1С:Предприятие 8.3 (тонкий клиент)"
version = "8.3.24.145"
publisher = "1C"
executable = "1cv8.exe"
runner = "wine"
expected_level = "l1"
wine = "9.0"
tested = true
tested_notes = "проверено на сборке 2026-11, сценарий: открытие справочников и печать отчёта"
installer_filename = "1cv8_8.3.24.145.exe"
installer_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[graph]
path = "dxvk"
dxvk_version = "2.3"

[drives]
mappings = [
  { letter = "C", kind = "prefix", mode = "rw" },
  { letter = "Z", kind = "bind", path = "/srv/Общий отдел", mode = "ro" },
]

[network]
mode = "allowlist"
domains = ["license.v8.1c.ru", "*.nalog.ru"]

[[network.ports]]
proto = "tcp"
port = 445

[devices]
audio = true
camera = false
printers = "queue-only"

[[devices.usb]]
vendor = "04e6"
product = "5203"
description = "USB-ключ Рутокен"

[filesystem]
visible = ["/home/%USER%/.1c", "/var/spool/byteguru/wcv/1c-client-8-3"]
hidden = ["/etc/shadow"]

[security]
apparmor = "enforce"
seccomp = "strict"
no_new_privs = true

[integration]
menu = true
mime = ["application/x-1c-erf"]

[update]
policy = "disabled"
"""


def test_minimal_manifest_uses_safe_defaults():
    manifest = load_manifest_text(MINIMAL)
    assert manifest.id == "notepad-plus"
    assert manifest.runner is Runner.WINE
    assert manifest.expected_level is CoverageLevel.L1
    # Безопасность по умолчанию: сеть закрыта, изоляция включена, обновления запрещены.
    assert manifest.network_mode is NetworkMode.BLOCKED
    assert manifest.no_new_privs is True
    assert manifest.update_policy.value == "disabled"
    assert manifest.printers is PrintMode.QUEUE_ONLY
    assert manifest.evidence_complete is False


def test_untested_manifest_is_not_evidence():
    manifest = load_manifest_text(MINIMAL)
    assert manifest.tested is False
    assert manifest.evidence_complete is False


def test_full_manifest_parses_every_section():
    manifest = load_manifest_text(FULL)
    assert manifest.installer_sha256.startswith("0123456789abcdef")
    assert manifest.graph_path is GraphPath.DXVK
    assert [d.letter for d in manifest.drives] == ["C", "Z"]
    assert manifest.drives[0].kind is DriveKind.PREFIX
    assert manifest.drives[1].path == "/srv/Общий отдел"
    assert manifest.ports[0].port == 445
    assert manifest.usb[0].vendor == "04e6"
    assert manifest.domains[0] == "license.v8.1c.ru"


@pytest.mark.parametrize(
    "text,expect",
    [
        ('[wcv]\nschema = 2\nid = "x1"\nname = "N"\nversion = "1"\nexecutable = "a.exe"\nrunner = "wine"\nexpected_level = "l1"', "schema"),
        ('[wcv]\nschema = 1\nid = "Нет ID"\nname = "N"\nversion = "1"\nexecutable = "a.exe"\nrunner = "wine"\nexpected_level = "l1"', "wcv.id"),
        ('[wcv]\nschema = 1\nid = "ok-id"\nname = "N"\nversion = "1"\nexecutable = "a.bat"\nrunner = "wine"\nexpected_level = "l1"', "executable"),
        ('[wcv]\nschema = 1\nname = "N"\nversion = "1"\nexecutable = "a.exe"\nrunner = "wine"\nexpected_level = "l1"', "не хватает"),
        ('[wcv]\nschema = 1\nid = "ok-id"\nname = "N"\nversion = "1"\nexecutable = "a.exe"\nrunner = "qemu"\nexpected_level = "l1"', "runner"),
        ('[wcv]\nschema = 1\nid = "ok-id"\nname = "N"\nversion = "1"\nexecutable = "a.exe"\nrunner = "wine"\nexpected_level = "l7"', "expected_level"),
        ('[wcv]\nschema = 1\nid = "ok-id"\nname = "N"\nversion = "1"\nexecutable = "a.exe"\nrunner = "wine"\nexpected_level = "l1"\nnetworke = 1', "неизвестный ключ"),
        ('[wcv]\nschema = 1\n', "не хватает"),
    ],
)
def test_bad_wcv_section_reported(text, expect):
    problems = validate_manifest_text(text)
    assert problems, "манифест обязан быть отвергнут"
    assert any(expect in problem for problem in problems), problems


def test_open_network_is_refused():
    problems = validate_manifest_text(
        MINIMAL + '\n[network]\nmode = "open"\n'
    )
    assert any("open" in problem for problem in problems)


def test_domains_require_allowlist():
    problems = validate_manifest_text(
        MINIMAL + '\n[network]\nmode = "blocked"\ndomains = ["a.ru"]\n'
    )
    assert any("allowlist" in problem for problem in problems)


def test_system_root_cannot_be_mapped_as_drive():
    problems = validate_manifest_text(
        MINIMAL + '\n[drives]\nmappings = [{ letter = "E", kind = "bind", path = "/" }]\n'
    )
    assert any("системным корнем" in problem for problem in problems)


def test_relative_and_traversing_paths_refused():
    for path in ("../etc", "C:\\Windows", "srv/share"):
        problems = validate_manifest_text(
            MINIMAL + f'\n[drives]\nmappings = [{{ letter = "E", kind = "bind", path = "{path}" }}]\n'
        )
        assert problems, path


def test_drive_letter_rules():
    problems = validate_manifest_text(
        MINIMAL + '\n[drives]\nmappings = [\n  { letter = "A", kind = "bind", path = "/srv/a" },\n  { letter = "E", kind = "prefix" },\n]\n'
    )
    assert any("зарезервирована" in problem for problem in problems)
    assert any("корневым префиксом" in problem for problem in problems)


def test_duplicate_letter_refused():
    problems = validate_manifest_text(
        MINIMAL + '\n[drives]\nmappings = [\n  { letter = "E", kind = "bind", path = "/srv/a" },\n  { letter = "E", kind = "bind", path = "/srv/b" },\n]\n'
    )
    assert any("дважды" in problem for problem in problems)


def test_no_new_privs_cannot_be_disabled():
    problems = validate_manifest_text(
        MINIMAL + "\n[security]\nno_new_privs = false\n"
    )
    assert any("no_new_privs" in problem for problem in problems)


def test_bad_sha256_and_usb_ids():
    problems = validate_manifest_text(
        MINIMAL.replace('expected_level = "l1"', 'expected_level = "l1"\ninstaller_sha256 = "ZZZ"')
    )
    assert any("installer_sha256" in problem for problem in problems)
    problems = validate_manifest_text(
        MINIMAL + '\n[devices]\nusb = [{ vendor = "04e", product = "5203" }]\n'
    )
    assert any("vendor" in problem for problem in problems)


def test_dump_roundtrip_is_stable():
    manifest = load_manifest_text(FULL)
    dumped = dump_toml(manifest)
    again = load_manifest_text(dumped)
    assert again.fingerprint() == manifest.fingerprint()
    assert dump_toml(again) == dumped


def test_l0_manifest_is_legal_but_marked_unsupported():
    manifest = load_manifest_text(
        MINIMAL.replace('expected_level = "l1"', 'expected_level = "l0"')
    )
    assert manifest.expected_level is CoverageLevel.L0
    assert manifest.expected_level.is_supported is False
    assert manifest.evidence_complete is False
