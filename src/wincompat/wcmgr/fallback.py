"""Что делать, если приложение не работает в Wine: решение обходного пути.

Таблица решений маленькая и намеренно закрытая: варианты «придумать патч на
лету» нет. Если уровня L1 нет, система обязана предложить ровно один из
поддерживаемых путей и объяснить почему.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wcmgr.manifest import CoverageLevel, Manifest, Runner

ACTION_WINE = "launch-wine"
ACTION_VM = "launch-vm"
ACTION_REBOOT = "reboot-to-windows"
ACTION_UNSUPPORTED = "unsupported"
ACTION_NATIVE = "launch-native"

EXPLANATIONS = {
    CoverageLevel.L0: "Приложение требует Windows-код в ядре, античит или DRM. В WinCompat оно не заработает.",
    CoverageLevel.L1: "Полноценная работа в подсистеме WinCompat.",
    CoverageLevel.L2: "Работает базовый сценарий: просмотр, печать, простые операции. Сложное редактирование может быть недоступно.",
    CoverageLevel.L3: "Требуется настоящий Windows: доступен запуск в виртуальной машине с оконным режимом.",
    CoverageLevel.L4: "Требуется настоящая Windows с прямым доступом к железу: доступен перезапуск в соседний раздел.",
}


@dataclass(frozen=True)
class Capabilities:
    """Что есть в этой системе. Определяется окружением, а не желанием пользователя."""

    has_kvm: bool = False
    has_windows_partition: bool = False
    has_gpu_acceleration: bool = True
    hypervisor_ready: bool = False

    @property
    def vm_available(self) -> bool:
        return self.has_kvm and self.hypervisor_ready


@dataclass(frozen=True)
class Decision:
    action: str
    level: CoverageLevel
    reason: str
    hint: str = ""
    commands: tuple[str, ...] = ()
    requires_reboot: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def is_executable(self) -> bool:
        return self.action != ACTION_UNSUPPORTED


def reboot_commands(efi_boot_index: str | None = None) -> tuple[str, ...]:
    """Команды однократной перезагрузки в Windows через.efivar-запись."""
    if efi_boot_index is None:
        return ("byteguru-boot-next", "windows")
    return ("byteguru-boot-next", "windows", "--boot-index", efi_boot_index)


def decide(
    manifest: Manifest,
    proven_level: CoverageLevel | None,
    capabilities: Capabilities,
    *,
    efi_boot_index: str | None = None,
) -> Decision:
    """Лучший из доступных путей при текущем доказанном уровне покрытия."""
    declared = manifest.expected_level

    if manifest.runner is Runner.NATIVE:
        return Decision(
            action=ACTION_NATIVE,
            level=declared,
            reason="манифест помечен как нативное Linux-приложение: WinCompat не нужен",
        )

    effective = proven_level if proven_level is not None else declared

    if effective is CoverageLevel.L1:
        return Decision(
            action=ACTION_WINE,
            level=CoverageLevel.L1,
            reason="приложение подтверждённо работает в подсистеме WinCompat"
            if proven_level is CoverageLevel.L1
            else "уровень L1 заявлен в манифесте, но ещё не подтверждён прогоном на этой сборке",
            notes=() if proven_level is CoverageLevel.L1 else ("первый запуск считается проверкой",),
        )

    if effective is CoverageLevel.L2:
        return Decision(
            action=ACTION_WINE,
            level=CoverageLevel.L2,
            reason=EXPLANATIONS[CoverageLevel.L2],
            hint="если нужен полный функционал — используйте соседний раздел Windows",
        )

    if effective is CoverageLevel.L3:
        if capabilities.vm_available:
            return Decision(
                action=ACTION_VM,
                level=CoverageLevel.L3,
                reason=EXPLANATIONS[CoverageLevel.L3],
                hint="образ Windows и лицензии — ответственность организации",
            )
        if capabilities.has_windows_partition:
            return Decision(
                action=ACTION_REBOOT,
                level=CoverageLevel.L4,
                reason="виртуализация недоступна (нет KVM или hypervisor не поднята), но Windows стоит на этом же диске",
                hint="перезагрузка выполнится в Windows без входа в BIOS",
                commands=reboot_commands(efi_boot_index),
                requires_reboot=True,
            )
        return Decision(
            action=ACTION_UNSUPPORTED,
            level=CoverageLevel.L3,
            reason="нужен настоящий Windows, но ни виртуализация, ни соседний раздел недоступны",
            hint="включите виртуализацию в UEFI или установите Windows рядом с ByteguruOS",
        )

    if effective is CoverageLevel.L4:
        if capabilities.has_windows_partition:
            return Decision(
                action=ACTION_REBOOT,
                level=CoverageLevel.L4,
                reason=EXPLANATIONS[CoverageLevel.L4],
                commands=reboot_commands(efi_boot_index),
                requires_reboot=True,
            )
        return Decision(
            action=ACTION_UNSUPPORTED,
            level=CoverageLevel.L4,
            reason="приложению нужна настоящая Windows, а Windows-раздел на этом компьютере не найден",
            hint="установите Windows в соседний раздел (доступно из установщика ByteguruOS)",
        )

    return Decision(
        action=ACTION_UNSUPPORTED,
        level=CoverageLevel.L0,
        reason=EXPLANATIONS[CoverageLevel.L0],
        hint="подберите Linux-аналог или используйте соседний раздел Windows",
    )
