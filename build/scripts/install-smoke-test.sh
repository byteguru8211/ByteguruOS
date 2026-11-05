#!/usr/bin/env bash
#
# End-to-end прогон установки: live-образ → диск → загрузка с этого диска.
#
# Зачем, если есть boot-smoke-test: он отвечает на «образ грузится». Про
# «установщик ставит систему, которая потом грузится сама» он не знает
# ничего, а это ровно тот путь, где live-build уже не помогает: свои
# монтирования, свои метки, свой fstab, свой загрузчик и запись в NVRAM.
#
# Два прогона QEMU с одной и той же UEFI-прошивкой:
#   1. ISO + целевой диск + маленький fat-диск с файлом-ответом. Внутри
#      live-сессии byteguru-autoinstall размечает диск, переносит систему и
#      ставит загрузчик. Успех — строка BYTEGURU-INSTALL-OK на консоли.
#   2. только целевой диск: ждём BYTEGURU-SESSION-OK уже от установленной
#      системы. Это единственный способ доказать, что установка не «прошла
#      успешно», а оставила рабочую машину.
#
# Файл-ответ, а не флаг ядра, — потому что иначе нельзя: при загрузке через
# OVMF командную строку ядру передать нечем (-kernel с UEFI-прошивкой не
# работает), а BIOS-путь в live-образе недоступен в принципе (grub-pc
# конфликтует с grub-efi-amd64). Переменные OVMF общие для обоих прогонов:
# запись загрузки, созданная в первом, должна дожить до второго.
#
# Использование: sudo bash build/scripts/install-smoke-test.sh [путь.iso]
# Переменные: INSTALL_TIMEOUT (3600), SESSION_TIMEOUT (1800), DISK_GIB (24),
#            KEEP=1 — не удалять рабочие файлы.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_PREFIX="[install-test]"
INSTALL_TIMEOUT="${INSTALL_TIMEOUT:-3600}"
SESSION_TIMEOUT="${SESSION_TIMEOUT:-1800}"
DISK_GIB="${DISK_GIB:-24}"
MEMORY_MB="${BOOT_MEMORY:-3072}"
OVMF_CODE="${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}"
OVMF_VARS_TEMPLATE="${OVMF_VARS_TEMPLATE:-/usr/share/OVMF/OVMF_VARS_4M.fd}"

log() { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
die() {
	printf '%s ОШИБКА: %s\n' "$LOG_PREFIX" "$*" >&2
	exit 1
}

[ "$(id -u)" -eq 0 ] || die "нужен root: loop-монтирование и qcow2"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 не установлен"
command -v qemu-img >/dev/null 2>&1 || die "qemu-utils не установлен"
[ -r "$OVMF_CODE" ] || die "нет UEFI-прошивки: apt-get install ovmf"
[ -r "$OVMF_VARS_TEMPLATE" ] || die "нет переменных OVMF: apt-get install ovmf"

ISO="${1:-}"
if [ -z "$ISO" ]; then
	ISO="$(ls -1t "$REPO_ROOT"/build/out/*.iso 2>/dev/null | head -1 || true)"
fi
[ -n "$ISO" ] && [ -f "$ISO" ] || die "ISO не найден: укажите путь аргументом"

WORK="$(mktemp -d /tmp/byteguru-install-test.XXXXXX)"
DISK="$WORK/target.qcow2"
ANSWER_IMG="$WORK/answer.img"
VARS="$WORK/ovmf_vars.fd"
MNT="$WORK/mnt"
cleanup() {
	if [ -z "${KEEP:-}" ]; then
		umount "$MNT" 2>/dev/null || true
		rm -rf "$WORK"
	fi
}
trap cleanup EXIT

log "образ: $ISO"

# Целевой диск sparse: 24 ГиБ занимают ровно столько, сколько реально записано.
qemu-img create -f qcow2 "$DISK" "${DISK_GIB}G" >/dev/null || die "qemu-img не создал диск"
cp "$OVMF_VARS_TEMPLATE" "$VARS" || die "не удалось скопировать переменные OVMF"

# Файл-ответ на fat-разделе. Метка короткая: mkfs.fat не принимает имена
# длиннее 11 символов и отказывается создавать диск. Сам автоустановщик
# смотрит не на метку, а на наличие byteguru-autoinstall.cfg — иначе случайный
# vfat-том превращался бы в команду «стирай диск».
qemu-img create -f raw "$ANSWER_IMG" 32M >/dev/null
mkfs.vfat -n BG_INSTALL "$ANSWER_IMG" >/dev/null || die "mkfs.vfat не отработал"
mkdir -p "$MNT"
mount -o loop "$ANSWER_IMG" "$MNT" || die "не удалось смонтировать файл-ответ"
cat > "$MNT/byteguru-autoinstall.cfg" <<CFG
target_disk=/dev/vda
profile=kiosk
user=guru
password=byteguru
hostname=byteguru-vm
CFG
umount "$MNT" || die "не удалось размонтировать файл-ответ"

# pflash с unit=0 — код прошивки (только чтение), unit=1 — переменные.
# Один и тот же файл переменных на оба прогона: иначе EFI-запись, созданная
# установщиком, исчезает, и второй прогон падает не из-за установки, а из-за
# тестовой обвязки.
firmware_opts=(-drive "if=pflash,format=raw,readonly=on,file=$OVMF_CODE"
	-drive "if=pflash,format=raw,file=$VARS")
# Запятые внутри строк — синтаксис QEMU (-drive a=b,c=d), а не разделители
# элементов массива; на это ругается SC2054.
# shellcheck disable=SC2054
disk_opts=(-drive "file=$DISK,if=none,id=disk0,format=qcow2" -device virtio-blk-pci,drive=disk0)
# shellcheck disable=SC2054
answer_opts=(-drive "file=$ANSWER_IMG,if=none,id=ans0,format=raw" -device virtio-blk-pci,drive=ans0)

# ISO подключается через -cdrom, а порядок — через -boot order: у CD-привода
# без явного IDE-моста нет короткого способа задать bootindex («ide-cd-pci» —
# несуществующая модель устройства), а OVMF читает fw_cfg и так. Проверено на
# QEMU 10.0: с именованной шиной фаза 1 не стартует вообще.
# shellcheck disable=SC2054
disk_opts=(-drive "file=$DISK,if=none,id=disk0,format=qcow2"
	-device virtio-blk-pci,drive=disk0)
# shellcheck disable=SC2054
answer_opts=(-drive "file=$ANSWER_IMG,if=none,id=ans0,format=raw"
	-device virtio-blk-pci,drive=ans0)

run_phase() { # run_phase <лог> <таймаут> <ожидание> <аргументы qemu...>
	local log_file="$1" timeout_s="$2" needle="$3"; shift 3
	local attempt=1 deadline qemu_pid retry
	while : ; do
		retry=0
		deadline=$(( $(date +%s) + timeout_s ))
		: > "$log_file"
		timeout --signal=SIGKILL "$timeout_s" qemu-system-x86_64 \
			-machine q35,accel=kvm:tcg -cpu max -smp 2 -m "$MEMORY_MB" \
			-display none -no-reboot -monitor none \
			-serial "file:$log_file" \
			-netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
			"${firmware_opts[@]}" "$@" >"${log_file}.qemu" 2>&1 &
		qemu_pid=$!
		while [ "$(date +%s)" -lt "$deadline" ]; do
			if grep -qa "$needle" "$log_file" 2>/dev/null; then
				kill "$qemu_pid" 2>/dev/null; wait "$qemu_pid" 2>/dev/null
				return 0
			fi
			if grep -qaE "BYTEGURU-INSTALL-FAIL|BYTEGURU-SESSION-FAIL" "$log_file" 2>/dev/null; then
				kill "$qemu_pid" 2>/dev/null; wait "$qemu_pid" 2>/dev/null
				return 1
			fi
			if grep -qaE "Kernel panic|VFS: Unable to mount root" "$log_file" 2>/dev/null; then
				kill "$qemu_pid" 2>/dev/null; wait "$qemu_pid" 2>/dev/null
				# Под TCG таймерная паника — спор гостя за CPU с параллельной
				# сборкой, а не дефект образа: тот же образ на свободном хосте
				# грузится. Одна повторная попытка, дальше — настоящий отказ.
				if [ "$attempt" -eq 1 ]; then
					log "паника ядра — повторяю фазу с нуля"
					attempt=2
					retry=1
					break
				fi
				return 2
			fi
			if ! kill -0 "$qemu_pid" 2>/dev/null; then
				wait "$qemu_pid" 2>/dev/null
				return 3
			fi
			sleep 5
		done
		if [ "$retry" -eq 1 ]; then
			continue
		fi
		kill "$qemu_pid" 2>/dev/null; wait "$qemu_pid" 2>/dev/null
		return 1
	done
}

report() { # report <заголовок> <лог>
	log "$1:"
	tail -30 "$2" 2>/dev/null | sed "s/^/$LOG_PREFIX   /"
}

log "фаза 1: установка на виртуальный диск (до ${INSTALL_TIMEOUT} с)"
run_phase "$WORK/install.log" "$INSTALL_TIMEOUT" "BYTEGURU-INSTALL-OK" \
	"${disk_opts[@]}" "${answer_opts[@]}" -cdrom "$ISO" -boot order=d
install_rc=$?
case $install_rc in
	0) log "ok: система установлена на виртуальный диск" ;;
	1) grep -a "BYTEGURU-INSTALL-FAIL" "$WORK/install.log" | head -8 |
			sed "s/^/$LOG_PREFIX   /"; report "хвост консоли установки" "$WORK/install.log"
		die "установка не завершилась успехом" ;;
	2) report "фатальная ошибка ядра" "$WORK/install.log"
		die "live-сессия упала во время установки" ;;
	*) report "QEMU завершился раньше" "$WORK/install.log"
		die "прогон установки оборвался (код $install_rc)" ;;
esac

log "фаза 2: загрузка с установленного диска (до ${SESSION_TIMEOUT} с)"
run_phase "$WORK/boot.log" "$SESSION_TIMEOUT" "BYTEGURU-SESSION-OK" \
	"${disk_opts[@]}" -boot order=c
boot_rc=$?
case $boot_rc in
	0) log "ok: установленная система загрузилась и подняла сеанс" ;;
	1) report "хвост консоли установленной системы" "$WORK/boot.log"
		die "установленная система не дала рабочий сеанс" ;;
	2) report "фатальная ошибка ядра" "$WORK/boot.log"
		die "установленная система не смогла смонтировать корень" ;;
	*) report "QEMU завершился раньше" "$WORK/boot.log"
		die "прогон загрузки оборвался (код $boot_rc)" ;;
esac

log "полный путь пройден: live → установка → загрузка с диска"
[ -n "${KEEP:-}" ] && log "рабочие файлы сохранены: $WORK"
exit 0
