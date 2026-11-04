#!/usr/bin/env bash
#
# Проверка, что собранный live-rootfs пригоден для входа.
#
# Зачем отдельный скрипт: лог прогона GitHub доступен только по авторизации, а
# вывод этой проверки уходит в аннотации и в job summary — иначе причина «опять
# просит пароль» разбирается гаданием. Скрипт исполняется после lb build, пока
# build/live/chroot ещё не удалён.

set -uo pipefail

CHROOT="${1:-build/live/chroot}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
rc=0

note() { echo "$@" | tee -a "${SUMMARY}"; }
fail() {
	note "- **$1**"
	echo "::error::$1"
	rc=1
}

note "## Проверка входа в live-образ"

if [ ! -d "${CHROOT}" ]; then
	fail "нет каталога ${CHROOT}: стадия chroot не оставила rootfs для проверки"
	exit 1
fi

PASSWD_LINE="$(grep -E '^user:' "${CHROOT}/etc/passwd" 2>/dev/null || true)"
if [ -z "${PASSWD_LINE}" ]; then
	fail "в /etc/passwd нет учётной записи user"
else
	note "- учётная запись: \`${PASSWD_LINE}\`"
	SHELL_FIELD="$(printf '%s' "${PASSWD_LINE}" | cut -d: -f7)"
	if [ "${SHELL_FIELD}" != "/bin/bash" ]; then
		# pam_succeed_if.so в /etc/pam.d/lightdm-autologin отсекает shell из
		# nologin/false: с таким shell автологин не сработает.
		fail "shell пользователя — ${SHELL_FIELD}, а не /bin/bash"
	fi
fi

SHADOW_LINE="$(grep -E '^user:' "${CHROOT}/etc/shadow" 2>/dev/null || true)"
SHADOW_HASH="$(printf '%s' "${SHADOW_LINE}" | cut -d: -f2)"
case "${SHADOW_HASH}" in
	'' | '*'* | '!'*)
		fail "в /etc/shadow нет рабочего хэша пароля для user"
		;;
	*) note "- пароль user: хэш установлен (${SHADOW_HASH:0:3}...)" ;;
esac

for conf in \
	etc/lightdm/lightdm.conf.d/50-byteguru-autologin.conf \
	etc/systemd/system/getty@tty1.service.d/50-byteguru-autologin.conf; do
	if [ -f "${CHROOT}/${conf}" ]; then
		note "- ${conf}: есть"
	else
		fail "нет ${conf}: автологин не настроен"
	fi
done

SESSIONS="$(ls "${CHROOT}/usr/share/xsessions" "${CHROOT}/usr/share/wayland-sessions" 2>/dev/null | tr '\n' ' ')"
note "- доступные сессии: ${SESSIONS:-**нет ни одной**}"
[ -n "${SESSIONS}" ] || fail "в образе нет ни xsessions, ни wayland-sessions"

DM_TARGET="$(readlink "${CHROOT}/etc/systemd/system/display-manager.service" 2>/dev/null || true)"
note "- display-manager: ${DM_TARGET:-не включён}"

OUR_BINS=""
for bin in "${CHROOT}"/usr/bin/bg* "${CHROOT}"/usr/bin/wcmgr "${CHROOT}"/usr/bin/byteguru*; do
	[ -e "${bin}" ] && OUR_BINS="${OUR_BINS}$(basename "${bin}") "
done
note "- наши исполняемые файлы: ${OUR_BINS:-**отсутствуют**}"

note ""
note "Итог: $( [ "${rc}" -eq 0 ] && echo 'вход настроен' || echo 'Есть проблемы — см. выше' )"
exit "${rc}"
