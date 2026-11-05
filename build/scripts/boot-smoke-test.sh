#!/usr/bin/env bash
#
# QEMU-прогон собранного ISO: проверка, что образ загружается до рабочего
# сеанса, а не только скачивается и монтируется.
#
# Зачем это нужно: `lb build` говорит «сборка успешна» про упаковку, а не про
# загрузку. Живой прогон ловит то, что статически не видно: ядро без нужного
# модуля, initrd без live-boot, сеанс, который падает на старте, — и всё это
# выглядит как «чёрный экран», пока кто-то не прочитал консоль.
#
# Ядро и initrd берутся из ISO и передаются QEMU напрямую, а не через эмуляцию
# прошивки: так у нас есть console=ttyS0, то есть serial-лог с самого первого
# сообщения ядра. Без этого прогон молчал бы до gdm-экрана, которого у нас нет.
#
# Использование: sudo bash build/scripts/boot-smoke-test.sh [путь.iso]

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_PREFIX="[boot-test]"
# 1200 с, а не «пара минут», по причине, которую легко проглядеть: без
# /dev/kvm QEMU работает на TCG, и гость живёт в разы медленнее часов. На
# машине без KVM (а GitHub-runner'ы все такие) загрузка live-образа до
# рабочего сеанса занимает десятки минут. Тест с маленьким таймаутом
# означал бы «красный CI без причины».
TIMEOUT_SECONDS="${BOOT_TIMEOUT:-1200}"
MEMORY_MB="${BOOT_MEMORY:-3072}"

log() { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
die() {
	printf '%s ОШИБКА: %s\n' "$LOG_PREFIX" "$*" >&2
	exit 1
}

[ "$(id -u)" -eq 0 ] || die "нужен root: монтирование ISO и loop-устройства"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 не установлен (apt-get install qemu-system-x86)"

ISO="${1:-}"
if [ -z "$ISO" ]; then
	ISO="$(ls -1t "$REPO_ROOT"/build/out/*.iso 2>/dev/null | head -1 || true)"
fi
[ -n "$ISO" ] && [ -f "$ISO" ] || die "ISO не найден: укажите путь аргументом или соберите образ"

WORK="$(mktemp -d /tmp/byteguru-boot-test.XXXXXX)"
MNT="$WORK/mnt"
LOG="$WORK/console.log"
trap 'umount "$MNT" 2>/dev/null || true; rm -rf "$WORK"' EXIT

log "образ: $ISO"
mkdir -p "$MNT"
mount -r -o loop "$ISO" "$MNT" || die "не удалось смонтировать ISO"

VMLINUZ="$(find "$MNT/live" -maxdepth 1 -name 'vmlinuz*' -print -quit 2>/dev/null || true)"
INITRD="$(find "$MNT/live" -maxdepth 1 -name 'initrd*' -print -quit 2>/dev/null || true)"
[ -n "$VMLINUZ" ] || die "в образе нет /live/vmlinuz: стадия binary не положила ядро"
[ -n "$INITRD" ] || die "в образе нет /live/initrd: без него live-boot не смонтирует rootfs"
cp "$VMLINUZ" "$WORK/vmlinuz"
cp "$INITRD" "$WORK/initrd.img"
umount "$MNT"

# console=ttyS0 — единственное, что делает прогон читаемым.
#
# nomodeset сюда намеренно НЕ добавлен: он выключает DRM, а DRM — единственный
# бэкенд, который bgwl умеет использовать на виртуальном железе. С nomodeset
# прогон падал не из-за образа, а из-за собственных параметров (проверено
# 2026-11-05: сеанс не поднимался, хотя образ грузился корректно).
APPEND="boot=live components hostname=byteguru username=byteguru console=ttyS0,115200"

log "запуск QEMU (до ${TIMEOUT_SECONDS} с), лог: $LOG"
# q35 вместо i440FX: на i440FX под TCG ядро периодически паникует
# «IO-APIC + timer doesn't work» — с тем же образом прогон то проходил, то
# падал на первой же секунде. Нестабильный тест хуже отсутствующего.
timeout --signal=SIGKILL "$TIMEOUT_SECONDS" qemu-system-x86_64 \
	-machine q35,accel=kvm:tcg -cpu max -smp 2 -m "$MEMORY_MB" \
	-vga std -display none -no-reboot -no-shutdown \
	-cdrom "$ISO" -boot d \
	-kernel "$WORK/vmlinuz" -initrd "$WORK/initrd.img" \
	-append "$APPEND" \
	-serial "file:$LOG" -monitor none \
	-netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
	>"$WORK/qemu.out" 2>&1
rc=$?

if [ ! -s "$LOG" ]; then
	tail -20 "$WORK/qemu.out" >&2 || true
	die "серийный лог пуст: QEMU не запустился (код $rc)"
fi

check() {
	if grep -q "$1" "$LOG"; then
		log "ok: $2"
		return 0
	fi
	log "НЕТ: $2"
	return 1
}

fail=0
check "Linux version" "ядро стартовало" || fail=1
check "Run /init as init process" "initrd передал управление live-boot" || fail=1
check "Reached target graphical.target" "systemd довёл сеанс до графической цели" || fail=1

if grep -q "BYTEGURU-SESSION-OK" "$LOG"; then
	log "ok: сеанс подтверждён само-проверкой образа"
elif grep -q "BYTEGURU-SESSION-FAIL" "$LOG"; then
	log "НЕТ: само-проверка сообщила о проблеме:"
	grep "BYTEGURU-SESSION-FAIL" "$LOG" | sed 's/^/[boot-test]   /'
	fail=1
else
	log "НЕТ: само-проверка не вышла на консоль за ${TIMEOUT_SECONDS} с"
	fail=1
fi

for fatal in "Kernel panic" "Attempted to kill init" "VFS: Unable to mount root"; do
	if grep -q "$fatal" "$LOG"; then
		log "НЕТ: фатальное сообщение ядра: $fatal"
		fail=1
	fi
done

log "хвост консоли:"
tail -25 "$LOG" | sed 's/^/[boot-test]   /'

if [ "$fail" -eq 0 ]; then
	log "прогон пройден: образ загружается и даёт рабочий сеанс"
	exit 0
fi
log "прогон провален: подробности в $LOG"
exit 1
