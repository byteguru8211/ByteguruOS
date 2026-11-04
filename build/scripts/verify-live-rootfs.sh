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

# /etc/shadow имеет режим 0640 root:shadow. Без root grep молча вернул бы пустоту,
# и проверка доложила бы «пароля нет» там, где просто нет доступа — ровно так и
# вышло в первом прогоне. Эти два случая теперь различаются явно.
if [ ! -r "${CHROOT}/etc/shadow" ]; then
	note "- /etc/shadow: **не читается без root** — шаг CI обязан идти под sudo"
	echo "::warning::нет прав на ${CHROOT}/etc/shadow: вывод про пароль неинформативен"
else
	SHADOW_LINE="$(grep -E '^user:' "${CHROOT}/etc/shadow" 2>/dev/null || true)"
	SHADOW_HASH="$(printf '%s' "${SHADOW_LINE}" | cut -d: -f2)"
	case "${SHADOW_HASH}" in
		'' | '*'* | '!'*)
			# Автологин это не ломает: /etc/pam.d/lightdm-autologin оканчивается
			# pam_permit.so и входит без пароля. Без хэsha не работают ручной ввод
			# в greeter'е и sudo с паролем.
			fail "в /etc/shadow нет рабочего хэша пароля для user (ручной вход в greeter не сработает)"
			;;
		*) note "- пароль user: хэш установлен (${SHADOW_HASH:0:3}...)" ;;
	esac
fi

for conf in \
	etc/lightdm/lightdm.conf.d/50-byteguru-autologin.conf \
	etc/systemd/system/getty@tty1.service.d/50-byteguru-autologin.conf; do
	if [ -f "${CHROOT}/${conf}" ]; then
		note "- ${conf}: есть"
	else
		fail "нет ${conf}: автологин не настроен"
	fi
done

# Локаль и раскладка: печатаем содержимое, а не только вердикт. По одному
# «не найдено» невозможно отличить не отработавший хук от файла, перезаписанного
# более поздней стадией live-build.
if [ -f "${CHROOT}/etc/default/locale" ]; then
	note "- \`/etc/default/locale\`: \`$(head -n 2 "${CHROOT}/etc/default/locale" | tr '\n' ' ')\`"
	grep -q 'ru_RU' "${CHROOT}/etc/default/locale" ||
		note "- локаль: **ru_RU отсутствует** — хук 0100 не применил LANG"
else
	note "- локаль: **/etc/default/locale отсутствует** — хук 0100 не отработал"
fi

if [ -f "${CHROOT}/etc/default/keyboard" ]; then
	note "- \`/etc/default/keyboard\`: \`$(tr '\n' ' ' < "${CHROOT}/etc/default/keyboard")\`"
fi

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
