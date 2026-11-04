"""Тема ByteguruOS: один акцент, два режима, три потребителя.

Настройка `accent` живёт в одном месте и расходится по трём:

1. композитор (bgwl `set accent`) — рамка фокуса и полоса заголовка;
2. оболочка (сгенерированный CSS, который подключает каждый наш оконный
   компонент через bggui.load_theme);
3. конфиг композитора (~/.config/byteguru/bgwl.conf), чтобы после перезагрузки
   акцент был тот же, что и перед выключением.

Акцент не «красивый цвет из палитры», а отправная точка: из него вычисляются
тёмный/светлый текст на нём, приглушённый вариант для неактивных окон и цвета
панелей. Поэтому пользователь может выбрать #ff0000 или #00ff88 — и интерфейс
не сломается на читаемости.

Сторонние приложения эту тему пока не наследуют: для им нужен бэкенд настроек
XDG-desktop-portal, которого в image ещё нет. Оболочка и композитор — красятся.
"""

from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, replace
from pathlib import Path

ACCENT_RE = re.compile(r"^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

CONF_DIR = Path.home() / ".config" / "byteguru"
THEME_CSS = CONF_DIR / "theme.css"

# Дефолт совпадает с bgwl: amber на тёмном сланце. Держать два значения в
# двух местах — гарантированное расхождение через полгода.
DEFAULT_ACCENT = "#f2a33d"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def hex_to_rgb(text: str) -> tuple[float, float, float]:
    match = ACCENT_RE.match(text)
    if match is None:
        raise ValueError(f"not a #RRGGBB colour: {text!r}")
    digits = match.group(1)
    return tuple(int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        *(round(_clamp(channel) * 255) for channel in rgb))


def _mix(rgb: tuple[float, float, float], target: tuple[float, float, float],
        amount: float) -> tuple[float, float, float]:
    return tuple(rgb[i] * (1 - amount) + target[i] * amount for i in range(3))  # type: ignore


def _lift(rgb: tuple[float, float, float], amount: float) -> tuple[float, float, float]:
    """Поднимает яркость без обесцвечивания: Hue и Saturation остаются."""
    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)
    return colorsys.hsv_to_rgb(hue, saturation * (1 - amount * 0.4),
        _clamp(value + amount))


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG-яркость — по ней решается, какой текст читать на акценте."""
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else (
            (channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


@dataclass(frozen=True)
class Palette:
    """Все производные цвета темы. Хранятся как #RRGGBB: их читают и CSS, и
    композитор, и вывод диагностики, и формат должен быть один."""

    accent: str = DEFAULT_ACCENT
    dark: bool = True

    @property
    def accent_rgb(self) -> tuple[float, float, float]:
        return hex_to_rgb(self.accent)

    @property
    def accent_fg(self) -> str:
        """Текст на акцентной плашке: белый на тёмном акценте, чёрный на
        светлом. Порог 0.45 — не догма, а место, где начинаются жалобы."""
        return "#ffffff" if relative_luminance(self.accent_rgb) < 0.45 else "#101010"

    @property
    def accent_soft(self) -> str:
        """Приглушённый акцент: неактивная рамка окна, подсветка строк."""
        rgb = self.accent_rgb
        return rgb_to_hex(_mix(rgb, (0.5, 0.5, 0.5), 0.45))

    @property
    def accent_hover(self) -> str:
        return rgb_to_hex(_lift(self.accent_rgb, 0.16))

    @property
    def window_bg(self) -> str:
        return "#12151c" if self.dark else "#f4f5f7"

    @property
    def surface_bg(self) -> str:
        return "#181c25" if self.dark else "#ffffff"

    @property
    def panel_bg(self) -> str:
        return "#0e1116" if self.dark else "#e8ebee"

    @property
    def text(self) -> str:
        return "#e0e2e6" if self.dark else "#1b1d21"

    @property
    def text_muted(self) -> str:
        return "#9aa0ab" if self.dark else "#5c6169"

    @property
    def border(self) -> str:
        return "#2b313c" if self.dark else "#cdd2da"

    def css(self) -> str:
        """CSS, который подключают все окна оболочки.

        Именованные цвета (accent_bg_color и остальные) — те, что использует
        Adwaita, поэтому часть виджетов красится «бесплатно»; классы с
        префиксом .bg — наши собственные.
        """
        return f"""/* порождено bgshell.themes — править не здесь, а в настройках */
@define-color accent_color {self.accent};
@define-color accent_bg_color {self.accent};
@define-color accent_fg_color {self.accent_fg};
@define-color window_bg_color {self.window_bg};
@define-color window_fg_color {self.text};
@define-color card_bg_color {self.surface_bg};
@define-color card_fg_color {self.text};
@define-color bg_muted {self.text_muted};
@define-color bg_border {self.border};
@define-color bg_soft {self.accent_soft};

.background {{ background-color: {self.window_bg}; color: {self.text}; }}
.bg-panel {{ background-color: {self.panel_bg}; }}
.bg-card {{ background-color: {self.surface_bg}; border: 1px solid {self.border};
            border-radius: 12px; }}
.bg-pill {{ background-image: none; background-color: {self.surface_bg};
             border-radius: 999px; color: {self.text}; }}
.bg-pill:hover {{ background-color: {self.accent_soft}; }}
.bg-pill-active {{ background-color: {self.accent}; color: {self.accent_fg};
                    border-radius: 999px; }}
.bg-pill-active:hover {{ background-color: {self.accent_hover}; }}
.bg-muted {{ color: {self.text_muted}; }}
.bg-stream-dot {{ border-radius: 999px; min-width: 8px; min-height: 8px; }}
.bg-entry {{ background-color: {self.surface_bg}; border-radius: 14px; }}
.bg-row:selected {{ background-color: {self.accent_soft}; }}
.bg-warn {{ color: #ff6b6b; }}
"""


def default_conf_dir() -> Path:
    return CONF_DIR


def write_css(palette: Palette, path: Path | None = None) -> Path:
    target = path or THEME_CSS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(palette.css(), encoding="utf-8")
    return target


def write_compositor_conf(palette: Palette, streams: list[tuple[int, str, str]]
        | None = None, path: Path | None = None) -> Path:
    """Перечисляет настройки композитора в bgwl.conf.

    Файл переписывается целиком из модели темы, а не правится построчно:
    частичная правка — это расхождение между тем, что показала панель, и тем,
    что прочитает bgwl после перезагрузки.
    """
    target = path or CONF_DIR / "bgwl.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# порождено оболочкой ByteguruOS; порядок строк не значим",
        f"accent-color = {palette.accent}",
        f"border-color = {palette.accent_soft}",
        f"decor-color = {palette.surface_bg}",
        f"background-color = {palette.window_bg}",
        f"text-color = {palette.text}",
    ]
    for index, name, accent in streams or []:
        lines.append(f"workspace = {index + 1}:{name}:{accent}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def apply(palette: Palette, compositor=None, streams: list[tuple[int, str, str]]
        | None = None) -> list[str]:
    """Раскладывает тему по потребителям и возвращает список того, что
    подействовало — чтобы настройки могли показать настоящую причину, а не
    «сохранено»."""
    applied: list[str] = []
    write_css(palette)
    applied.append("css")

    write_compositor_conf(palette, streams)
    applied.append("conf")

    if compositor is not None:
        try:
            compositor.set("accent", palette.accent)
            applied.append("compositor")
        except Exception as error:  # noqa: BLE001 — показываем причину пользователю
            applied.append(f"compositor: {error}")
    return applied


def from_settings(settings: dict) -> Palette:
    """Собирает палитру из словаря настроек оболочки (bgshell.settings)."""
    accent = str(settings.get("theme.accent") or DEFAULT_ACCENT)
    if not ACCENT_RE.match(accent):
        accent = DEFAULT_ACCENT
    mode = str(settings.get("theme.mode") or "dark")
    return Palette(accent=accent, dark=mode != "light")


def with_accent(palette: Palette, accent: str) -> Palette:
    """Новая палитра с проверенным акцентом; ValueError для мусора — лучше
    явная ошибка в настройках, чем серая тема из-за опечатки."""
    if not ACCENT_RE.match(accent):
        raise ValueError(f"bad accent colour: {accent!r}")
    return replace(palette, accent=accent.lower())
