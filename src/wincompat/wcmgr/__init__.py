"""ByteguruOS WinCompat — подсистема запуска Windows-приложений.

Публичный API пакета стабилизирован только для этих имён; всё остальное —
внутренняя реализация, которую можно менять между релизами системы.
"""

from __future__ import annotations

from wcmgr.errors import (
    LaunchError,
    ManifestError,
    PrefixError,
    RegistryError,
    SandboxError,
    WcvError,
)
from wcmgr.manifest import (
    CoverageLevel,
    DriveMapping,
    Manifest,
    dump_toml,
    load_manifest,
    load_manifest_text,
    validate_manifest_text,
)
from wcmgr.registry import CompatibilityRegistry, RunRecord

__version__ = "0.0.1"

__all__ = [
    "CompatibilityRegistry",
    "CoverageLevel",
    "DriveMapping",
    "LaunchError",
    "Manifest",
    "ManifestError",
    "PrefixError",
    "RegistryError",
    "RunRecord",
    "SandboxError",
    "WcvError",
    "__version__",
    "dump_toml",
    "load_manifest",
    "load_manifest_text",
    "validate_manifest_text",
]
