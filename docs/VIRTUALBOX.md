# ByteguruOS в VirtualBox: пошаговая установка

Документ написан под реальные ограничения проекта: образ собирается в GitHub
Actions (на Windows собрать ISO нельзя), а тестировать его вы будете в VirtualBox
на своём Legion Y9000K.

## 0. Где взять ISO

Образ существует только как артефакт прогона CI — локально он не строится.

1. Откройте репозиторий `byteguru8211/ByteguruOS` → вкладка **Actions**.
2. Выберите последний прогон **iso** (ветка `main`, завершённый зелёным).
3. В разделе **Artifacts** скачайте `byteguruos-amd64-iso-run<номер>`.
4. Распакуйте: внутри два файла — `byteguruos-*.iso` и `byteguruos-*.iso.sha256`.
5. Проверьте целостность (в Git Bash или PowerShell):

```sh
sha256sum -c byteguruos-*.iso.sha256
# или в PowerShell:
Get-FileHash .\byteguruos-*.iso -Algorithm SHA256
```

Хэш обязан совпасть с тем, что напечатан в **Job summary** прогона.

Если прогон красный — ISO нет, и это нормально на текущей стадии: значит первая
реальная сборка вскрыла расхождение (обычно live-build/debootstrap или имя
пакета). Логи прогона содержат точный шаг.

## 1. Параметры виртуальной машины

Требование важное: **UEFI, а не Legacy BIOS** — загрузчик ByteguruOS рассчитан на
systemd-boot/GRUB в EFI, и в BIOS-режиме образ загрузится не так, как вы будете
устанавливать на реальную машину.

| Параметр | Значение |
|---|---|
| Тип | Linux 2.x 64-bit (или «Debian (64-bit)») |
| Video Memory | 128 МБ |
| Graphics Controller | **VMSVGA** (не VBoxSVGA: с wlroots/Mesa надёжнее VMSVGA) |
| 3D Acceleration | включить, если есть проблемы с композитором — выключить |
| RAM | 8192 МБ (для WinCompat-тестов — 12288 МБ и больше) |
| CPU | 4 ядра |
| Disk | VDI, динамический, **60 ГБ минимум** |
| Disk bus | SATA (NVMe тоже годится) |
| Enable EFI (special OSes only) | **включить** |
| Secure Boot | **выключить** (см. пункт 5) |
| Network | NAT (для установки пакетов) |
| Audio | ICH6 |
| USB | USB 3.0 (xHCI), если проверяете проброс ключей |

Два диска вместо одного — отдельный сценарий: если хотите проверить установку
«рядом с Windows», создайте вторую VDI и поставьте на неё Windows (см. пункт 6).

## 2. Подключение образа и загрузка

1. **Настройки → Носители → IDE/SATA** → добавить оптический диск → выбрать скачанный `.iso`.
2. **Система → Порядок загрузки**: оптический диск выше жёсткого.
3. Запуск ВМ. В меню GRUB на live-образе выбрать **ByteguruOS live session**.
4. Если загрузка зависла на логотипе — в настройках ВМ отключите 3D-ускорение и
   повторите: это типичный конфликт видеодрйвера, а не поломка образа.

Проверьте в терминале live-сессии, что система узнаёт VirtualBox:

```sh
systemd-detect-virt          # должен сказать oracle
lsmod | grep -E 'vboxguest|vboxsf|vboxvideo'
```

## 3. Общая папка с хостом (нужна для переноса файлов и логов)

В VirtualBox: **Устройства → Общие папки → Настройки общих папок** → путь на
хосте, имя `share`, галочка «Автомонтирование» **выключена** (мы смонтируем сами,
чтобы получить права).

В гостевой системе:

```sh
sudo modprobe vboxsf
sudo byteguru-vboxsf-mount share        # helper из образа, точка /mnt/share
ls /mnt/share
```

Helper монтирует с `fmask/dmask`, чтобы файлы от root не ломали права в вашей
домашней директории. Если команда не найдена — значит overlay из `iso/` не попал в
образ: это дефект сборки, его надо чинить в `build/live/config/includes.chroot/`.

Доп. вариант, если helper отсутствует:

```sh
sudo mkdir -p /mnt/share
sudo mount -t vboxsf -o uid=$(id -u),gid=$(id -g),rw share /mnt/share
```

## 4. Установка на диск

Первый образ не содержит graphical-инсталлятора (calamares намеренно не включён),
поэтому установка — из консоли live-сессии, через планировщик разметки из этого
репозитория. Это безопаснее «мастера», который угадывает: команды печатаются до
исполнения.

```sh
# 1. Посмотреть, что система видит на дисках
lsblk --json --bytes --output NAME,SIZE,FSTYPE,LABEL,TYPE,PARTTYPENAME,PARTUUID > /tmp/lsblk.json
bginstaller probe --lsblk /tmp/lsblk.json

# 2. Проверить готовность диска (запреты: гибернация, BitLocker, грязный NTFS)
bginstaller check --disk-file /tmp/disk-description.json --profile workstation

# 3. Посмотреть план разметки НИКОГО не трогая
bginstaller plan --disk-file /tmp/disk-description.json --profile workstation

# 4. Проба исполнения (команды печатаются, ничего не делается)
bginstaller apply --disk-file /tmp/disk-description.json --profile workstation

# 5. Реальное исполнение — только после того, как прочитали план
bginstaller apply --disk-file /tmp/disk-description.json --profile workstation \
  --apply --confirm-token izmenit-razmetku
```

Описание диска в JSON — результат `probe`, дополненный состоянием Windows (см.
`tests/fixtures/` как пример формата). Ручной путь установки с `debootstrap` и
`rsync` описан в `INSTALL.txt` внутри live-образа, если планировщик ещё не
установлен в live-среду.

После установки: **Machine → Сброс → очистить носители**, затем вынуть ISO из
порядка загрузки и перезагрузиться.

## 5. Secure Boot и EFI

В VirtualBox UEFI без ключей Microsoft не даёт загрузить подписанный Microsoft
shim/Winload. Поэтому:

- **ByteguruOS грузится и с включённым Secure Boot, если в EFI импортирован наш PKC**
  (файл `byteguru-pkc.esl` из комплекта поставки; в VirtualBox это
  **Machine Settings → System → Enable Secure Boot → Import keys from an example template**,
  далее вручную — не вариант; для тестовой ВМ проще держать Secure Boot выключенным).
- Для реальной машины: либо выключить Secure Boot, либо enroll нашего ключа через
  `mokutil --import` в установленной системе.
- Смешанная конфигурация «Secure Boot включён + Windows стоит» работает, но
  требует, чтобы оба загрузчика имели подписи; порядок и риски описаны в RFC по
  двухзагрузочному режиму.

## 6. Проверка двухзагрузочного режима

Самый полезный тест в VirtualBox — вторая VDI с Windows:

1. Создать **второй** диск (SATA порт 1), поставить на него Windows.
2. Подключить ISO ByteguruOS, загрузиться в live.
3. `bginstaller probe` теперь должен показать два диска; на диске с Windows —
   ESP, MSR, NTFS и Recovery.
4. `bginstaller plan --disk-file <описание> --profile office-dualboot` — проверить,
   что в плане **нет** `delete-partition` для NTFS и что сжатие идёт с
   `--dry-run` и требует `--confirm-token`.
5. Установить, перезагрузиться: в меню должны быть обе системы.
6. Проверить ремонт загрузки после «затирания» Windows-обновлением записей:

```sh
efibootmgr -v > /tmp/efi.txt
bginstaller boot-repair --efibootmgr /tmp/efi.txt --disk /dev/sda --esp-partition 1
byteguru-boot-status
```

Команда восстановления **не удаляет** чужие записи — убедитесь, что Windows Boot
Manager осталась в списке.

## 7. Быстрые проверки после установки

```sh
wcmgr doctor                        # что есть в системе для WinCompat
wcmgr check /usr/share/byteguru/wincompat
bgd apps --json | head -50          # единая модель приложений
bgd status --json                   # покрытие и возможности окружения
bgd plan notepad-plus               # что реально исполнится при запуске
byteguru-boot-status                # состояние EFI и соседства
```

Интерфейс: панель (`bgpanel`), лаунчер (`bglauncher`), настройки (`bgsettings`).
Мастер первого запуска запускается автоматически при первом входе; вручную —
`bgfirstboot`.

## 8. Частые проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| «FATAL: No bootable medium found» | ISO не подключен или не гибридный | проверить носитель; в CI артефакт должен быть `.iso` из `xorriso`, а не `.tar` |
| Загрузчик не появляется, сразу Windows | Windows перезаписала BootOrder | `bginstaller boot-repair` / `sudo byteguru-boot-repair` |
| После установки грузится только BIOS-меню | ВМ в Legacy-режиме | включить EFI в настройках ВМ |
| Часы расходятся при переключении ОС | Windows хранит local time, Linux — UTC | `bginstaller clock --windows`, затем выполнить предложенную `reg add` в Windows |
| NTFS не открывается на запись | политика read-only по умолчанию | так и задумано: см. RFC по dual boot; включение — осознанное решение |
| Чёрный экран в live | видеодрйвер/3D | отключить 3D-ускорение, контроллер VMSVGA |
| `vboxsf` не монтируется | нет модуля guest в образе | дефект сборки, чинится в `build/live/config/package-lists/` |
| WinCompat не запускается | нет wine/aa-exec в live-образе | в первой сборке так и есть: WinCompat приедет отдельным пакетом |

## 9. Что в VirtualBox проверить нельзя

Честно: гибридная графика Optimus, проброс реальных USB-ключей Рутокен/Jacarta,
ГОСТ-криптопровайдеры, питание от батареи и гибернация реального железа — всё это
тестируется только на физическими машинах. Для этих пунктов VirtualBox даёт
ложноположительные или ложноотрицательные результаты, и полагаться на него нельзя.
