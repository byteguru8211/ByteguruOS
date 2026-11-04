"""Часы на двухзагрузочной машине.

Классическая поломка: Windows хранит time в BIOS как local time, Linux — как
UTC, и после переключения ОС часы расходятся на часовой пояс. Решение должно
быть принято установщиком осознанно, а не оставлено пользователю на второй
недель.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

LOCALTIME_REGISTRY_KEY = (
    r"HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
)
LOCALTIME_VALUE = "RealTimeIsUniversal"


class TimePolicy(str, Enum):
    UTC = "utc"
    LOCALTIME = "localtime"
    WINDOWS_UTC = "windows-utc"


@dataclass(frozen=True)
class ClockPlan:
    policy: TimePolicy
    rtc_setting: str
    explanation: str
    windows_commands: tuple[str, ...] = ()
    timedatectl_arguments: tuple[str, ...] = ()

    @property
    def needs_windows_change(self) -> bool:
        return bool(self.windows_commands)


def plan_for(windows_present: bool, user_choice: str | None = None) -> ClockPlan:
    """Что делать с часами. По умолчанию для dual boot — переводим Windows на UTC."""
    if not windows_present:
        return ClockPlan(
            policy=TimePolicy.UTC,
            rtc_setting="utc",
            explanation="Windows нет: стандартный UTC в rtc",
            timedatectl_arguments=("set-local-rtc", "0"),
        )
    if user_choice == "localtime":
        return ClockPlan(
            policy=TimePolicy.LOCALTIME,
            rtc_setting="local",
            explanation="Windows остаётся на local time, мы подстраиваемся: часы не разойдутся, "
            "но этот режим исторический и ломает часть софта",
            timedatectl_arguments=("set-local-rtc", "1"),
        )
    if user_choice == "utc":
        return ClockPlan(
            policy=TimePolicy.UTC,
            rtc_setting="utc",
            explanation="мы на UTC, Windows придётся переключить отдельно (см. команды)",
            timedatectl_arguments=("set-local-rtc", "0"),
            windows_commands=(
                f'reg add "{LOCALTIME_REGISTRY_KEY}" /v {LOCALTIME_VALUE} /t REG_DWORD /d 1 /f',
            ),
        )
    return ClockPlan(
        policy=TimePolicy.WINDOWS_UTC,
        rtc_setting="utc",
        explanation="обе системы работают в UTC: это единственный режим, в котором часы не "
        "расходятся после переключения между ОС",
        timedatectl_arguments=("set-local-rtc", "0"),
        windows_commands=(
            f'reg add "{LOCALTIME_REGISTRY_KEY}" /v {LOCALTIME_VALUE} /t REG_DWORD /d 1 /f',
            "w32tm /resync",
        ),
    )


def explain(plan: ClockPlan) -> str:
    lines = [f"политика: {plan.policy.value}", f"rtc — {plan.rtc_setting}", plan.explanation]
    if plan.timedatectl_arguments:
        lines.append(" timedatectl " + " ".join(plan.timedatectl_arguments))
    for command in plan.windows_commands:
        lines.append(f" в Windows (от администратора): {command}")
    if plan.policy is TimePolicy.WINDOWS_UTC:
        lines.append(
            " без этого шага Windows будет смещать время на величину часового пояса при каждой загрузке"
        )
    return "\n".join(lines)
