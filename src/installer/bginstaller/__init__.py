"""Публичный API установщика ByteguruOS."""

from __future__ import annotations

from bginstaller.bootrepair import BootEntry, EfiState, diagnose, parse_efibootmgr, repair_commands
from bginstaller.clock import ClockPlan, TimePolicy, plan_for as clock_plan_for
from bginstaller.errors import (
    ApplyError,
    BootRepairError,
    InstallerError,
    PlanError,
    ProbeError,
)
from bginstaller.models import (
    Action,
    BootMode,
    Disk,
    FSType,
    InstallPlan,
    Layout,
    OsKind,
    Partition,
    PartitionTable,
    Profile,
    WindowsState,
    GiB,
    MiB,
)
from bginstaller.planner import plan, plan_erase_and_install

__version__ = "0.0.1"

__all__ = [
    "Action",
    "ApplyError",
    "BootEntry",
    "BootMode",
    "BootRepairError",
    "ClockPlan",
    "Disk",
    "EfiState",
    "FSType",
    "GiB",
    "InstallPlan",
    "InstallerError",
    "Layout",
    "MiB",
    "OsKind",
    "Partition",
    "PartitionTable",
    "PlanError",
    "ProbeError",
    "Profile",
    "TimePolicy",
    "WindowsState",
    "__version__",
    "clock_plan_for",
    "diagnose",
    "parse_efibootmgr",
    "plan",
    "plan_erase_and_install",
    "repair_commands",
]
