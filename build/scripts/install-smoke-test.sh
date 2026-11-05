#!/usr/bin/env bash
#
# End-to-end прогон установки: ISO → диск → загрузка с этого диска.
#
# Зачем, если есть boot-smoke-test: он отвечает на «образ грузится», а на
# «установщик ставит систему, которая потом грузится сама» — нет. Разница
# принципиальная: у install-пути свои монтирования, метки, fstab, загрузчик
# и NVRAM, и тестировались они только dry-run-ом, где исполнитель команд
# подменён заглушкой.
#
# Два прогона QEMU:
#   1. live-сессия с флагом byteguru.autoinstall и вторым виртуальным диском;
#      внутри образа byteguru-autoinstall размечает диск, переносит систему и
#      ставит загрузчик. У успехе — строка BYTEGURU-INSTALL-OK на консоли.
#   2. тот же диск, но без ISO: ждём BYTEGURU-SESSION-OK уже от установленной
#      системы. Это единственный способ доказать, что установка не «прошла
#      успешно», а оставила рабочую машину.
#
# Путь BIOS/legacy, а не UEFI: UEFI требует OVMF, а с ним нельзя передать
# ядру командную строку (нет -kernel), то есть не включить автоустановку.
# Для теста это осознанный размен — NVRAM-часть (efibootmgr, boot-repair)
# здесь не проверяется, и в отчёте об этом сказано отдельно.
#
# Использование: sudo bash build/scripts/install-smoke-test.sh [путь.iso]
# Переменные: INSTALL_TIMEOUT (сек, по умолчанию 3600),
#            SESSION_TIMEOUT (сек, 1800), DISK_GIB (24), KEEP=1 — не удалять диск.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_PREFIX="[install-test]"
INSTALL_TIMEOUT="${INSTALL_TIMEOUT:-3600}"
SESSION_TIMEOUT="${SESSION_TIMEOUT:-1800}"
DISK_GIB="${DISK_GIB:-24}"
MEMORY_MB="${BOOT_MEMORY:-3072}"

log() { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
die() {
	printf '%s ОШИБКА: %s\n' "$LOG_PREFIX" "$*" >&2
	exit 1
}

[ "$(id -u)" -eq 0 ] || die "нужен root: loop-монтирование ISO и qcow2"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 не установлен"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img не установлен (apt-get install qemu-utils)"

ISO="${1:-}"
if [ -z "$ISO" ]; then
	ISO="$(ls -1t "$REPO_ROOT"/build/out/*.iso 2>/dev/null | head -1 || true)"
fi
[ -n "$ISO" ] && [ -f "$ISO" ] || die "ISO не найден: укажите путь аргументом"

WORK="$(mktemp -d /tmp/byteguru-install-test.XXXXXX)"
MNT="$WORK/mnt"
DISK="$WORK/target.qcow2"
trap '[ -n "${KEEP:-}" ] || rm -rf "$WORK"; umount "$MNT" 2>/dev/null || true' EXIT

log "образ: $ISO"
mkdir -p "$MNT"
mount -r -o loop "$ISO" "$MNT" || die "не удалось смонтировать ISO"
VMLINUZ="$(find "$MNT/live" -maxdepth 1 -name 'vmlinuz*' -print -quit)"
INITRD="$(find "$MNT/live" -maxdepth 1 -name 'initrd*' -print -quit)"
[ -n "$VMLINUZ" ] && [ -n "$INITRD" ] || die "в образе нет /live/vmlinuz или initrd"
cp "$VMLINUZ" "$WORK/vmlinuz"; cp "$INITRD" "$WORK/initrd.img"
umount "$MNT"

qemu-img create -f qcow2 "$DISK" "${DISK_GIB}G" >/dev/null || die "qemu-img не создал диск"

# Целевой диск — virtio: под TCG это самый быстрый путь, а fstab установленной
# системы монтирует по меткам, поэтому имя устройства роли не играет.
# SC2054 про запятые здесь ложняк: запятые внутри строки — часть синтаксиса
# QEMU (-drive a=b,c=d), а не разделители элементов массива.
# shellcheck disable=SC2054
DISK_OPTS=(-drive "file=$DISK,if=none,id=disk0,format=qcow2" -device virtio-blk-pci,drive=disk0)

# Массив строится на каждый вызов: -serial указывает в лог конкретной фазы, а
# «один общий массив на оба прогона» означал бы, что второй прогон дописывает
# в файл первого и grep не различает, где что было.
common_opts() {
	printf '%s\n' -machine q35,accel=kvm:tcg -cpu max -smp 2 -m "$MEMORY_MB" \
		-display none -no-reboot -no-shutdown -monitor none \
		-serial "file:$1" -netdev user,id=n0 -device virtio-net-pci,netdev=n0
}

run_phase() { # run_phase <лог> <таймаут> <ожидание> <аргументы qemu...>
	local log_file="$1" timeout_s="$2" needle="$3"; shift 3
	local deadline=$(( $(date +%s) + timeout_s ))
	local -a common
	mapfile -t common < <(common_opts "$log_file")
	: > "$log_file"
	timeout --signal=SIGKILL "$timeout_s" qemu-system-x86_64 "${common[@]}" "$@" \
		>"${log_file}.qemu" 2>&1 &
	local qemu_pid=$!
	while [ "$(date +%s)" -lt "$deadline" ]; do
		if grep -qa "$needle" "$log_file" 2>/dev/null; then
			kill "$qemu_pid" 2>/dev/null
			wait "$qemu_pid" 2>/dev/null
			return 0
		fi
		if grep -qaE "BYTEGURU-INSTALL-FAIL|BYTEGURU-SESSION-FAIL" "$log_file" 2>/dev/null; then
			kill "$qemu_pid" 2>/dev/null
			wait "$qemu_pid" 2>/dev/null
			return 1
		fi
		if ! kill -0 "$qemu_pid" 2>/dev/null; then
			wait "$qemu_pid" 2>/dev/null
			return 1
		fi
		sleep 5
	done
	kill "$qemu_pid" 2>/dev/null
	wait "$qemu_pid" 2>/dev/null
	return 1
}

dump_tail() {
	log "хвост консоли ($1):"
	tail -30 "$2" 2>/dev/null | sed 's/^/[install-test]   /'
}

log "фаза 1: установка на виртуальный диск (до ${INSTALL_TIMEOUT} с)"
if ! run_phase "$WORK/install.log" "$INSTALL_TIMEOUT" "BYTEGURU-INSTALL-OK" \
	"${DISK_OPTS[@]}" -cdrom "$ISO" -boot d \
	-kernel "$WORK/vmlinuz" -initrd "$WORK/initrd.img" \
	-append "boot=live components hostname=byteguru username=byteguru console=ttyS0,115200 byteguru.autoinstall byteguru.target_disk=/dev/vda"; then
	grep -a "BYTEGURU-INSTALL-FAIL" "$WORK/install.log" | head -6 | sed 's/^/[install-test]   /'
	dump_tail "установка" "$WORK/install.log"
	die "установка не завершилась успехом"
fi
log "ok: система установлена на виртуальный диск"

log "фаза 2: загрузка с установленного диска (до ${SESSION_TIMEOUT} с)"
if ! run_phase "$WORK/boot.log" "$SESSION_TIMEOUT" "BYTEGURU-SESSION-OK" \
	"${DISK_OPTS[@]}" -boot order=c; then
	dump_tail "загрузка с диска" "$WORK/boot.log"
	die "установленная система не дала рабочий сеанс"
fi
log "ok: установленная система загрузилась и подняла сеанс"

log "пройден полный путь: live → установка → загрузка с диска"
log "не проверено этим тестом: UEFI/NVRAM (efibootmgr, boot-repair) — см. шапку скрипта"
[ -n "${KEEP:-}" ] && log "диск сохранён: $DISK"
exit 0
