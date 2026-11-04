/*
 * Configuration, process spawning, the cairo-backed struct wlr_buffer used for
 * the titlebar and the background image, and the xkb keymap.
 *
 * Configuration layering, applied in this exact order:
 *
 *   built-in defaults  <  ~/.config/byteguru/bgwl.conf  <  BGWL_* environment
 *   <  command line
 *
 * The file exists because the shell writes it: a settings change made in
 * bgsettings must survive a reboot without anyone learning environment
 * variables. The environment still wins, because that is what a session
 * launcher, a test, or `bgwl -c` needs to be able to override.
 *
 * Unknown keys are reported and ignored rather than fatal. A compositor that
 * refuses to start on a typo leaves the user with a black screen and no way to
 * fix the typo, which is worse than a slightly wrong layout.
 */
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <drm_fourcc.h>
#include <wayland-server-core.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/types/wlr_buffer.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

#include "bgwl.h"

/* --- names shared with the config file ---------------------------------- */

/* Modifier tokens accepted in a `bind` line. The values are wlroots'
 * WLR_MODIFIER_* bits, spelled out here so a config file never has to know
 * them. */
static const struct {
	const char *name;
	uint32_t modifier;
} modifier_tokens[] = {
	{ "Super", WLR_MODIFIER_LOGO },
	{ "Mod4", WLR_MODIFIER_LOGO },
	{ "Ctrl", WLR_MODIFIER_CTRL },
	{ "Control", WLR_MODIFIER_CTRL },
	{ "Alt", WLR_MODIFIER_ALT },
	{ "Shift", WLR_MODIFIER_SHIFT },
};

/* Key names accepted in a `bind` line. Values are the evdev KEY_* codes the
 * compositor compares against, because that is what a keyboard event carries.
 * A table instead of keysym resolution: it has no runtime dependency, it is
 * auditable, and it covers every binding bgsettings lets the user edit. */
static const struct {
	const char *name;
	uint32_t keycode;
} key_tokens[] = {
	{ "Escape", 1 }, { "1", 2 }, { "2", 3 }, { "3", 4 }, { "4", 5 },
	{ "5", 6 }, { "6", 7 }, { "7", 8 }, { "8", 9 }, { "9", 10 },
	{ "0", 11 }, { "Minus", 12 }, { "Equal", 13 }, { "BackSpace", 14 },
	{ "Tab", 15 }, { "Q", 16 }, { "W", 17 }, { "E", 18 }, { "R", 19 },
	{ "T", 20 }, { "Y", 21 }, { "U", 22 }, { "I", 23 }, { "O", 24 },
	{ "P", 25 }, { "bracketleft", 26 }, { "bracketright", 27 },
	{ "Return", 28 }, { "A", 30 }, { "S", 31 }, { "D", 32 }, { "F", 33 },
	{ "G", 34 }, { "H", 35 }, { "J", 36 }, { "K", 37 }, { "L", 38 },
	{ "semicolon", 39 }, { "apostrophe", 40 }, { "grave", 41 },
	{ "backslash", 43 }, { "Z", 44 }, { "X", 45 }, { "C", 46 }, { "V", 47 },
	{ "B", 48 }, { "N", 49 }, { "M", 50 }, { "comma", 51 }, { "period", 52 },
	{ "slash", 53 }, { "space", 57 }, { "CapsLock", 58 },
	{ "F1", 59 }, { "F2", 60 }, { "F3", 61 }, { "F4", 62 }, { "F5", 63 },
	{ "F6", 64 }, { "F7", 65 }, { "F8", 66 }, { "F9", 67 }, { "F10", 68 },
	{ "NumLock", 69 }, { "ScrollLock", 70 }, { "F11", 87 }, { "F12", 88 },
	{ "Home", 102 }, { "Up", 103 }, { "PageUp", 104 }, { "Left", 105 },
	{ "Right", 106 }, { "End", 107 }, { "Down", 108 }, { "PageDown", 109 },
	{ "Insert", 110 }, { "Delete", 111 }, { "Menu", 139 },
	{ "AudioMute", 113 }, { "AudioRaiseVolume", 114 },
	{ "AudioLowerVolume", 115 }, { "Print", 210 }, { "KPEnter", 214 },
	{ "MonBrightnessUp", 225 }, { "MonBrightnessDown", 226 },
};

const char *bgwl_env_string(const char *name, const char *fallback) {
	const char *value = getenv(name);
	if (value == NULL || value[0] == '\0') {
		return fallback;
	}
	return value;
}

/* "#RRGGBB" or "#RRGGBBAA" into premultiplied RGBA floats. */
bool bgwl_parse_color(const char *text, float out[static 4]) {
	if (text == NULL || text[0] != '#') {
		return false;
	}
	size_t digits = strlen(text + 1);
	if (digits != 6 && digits != 8) {
		return false;
	}
	for (size_t i = 0; i < digits; i++) {
		if (!isxdigit((unsigned char)text[1 + i])) {
			return false;
		}
	}

	unsigned long long value = strtoull(text + 1, NULL, 16);
	float r, g, b, a;
	if (digits == 6) {
		r = ((value >> 16) & 0xff) / 255.0f;
		g = ((value >> 8) & 0xff) / 255.0f;
		b = (value & 0xff) / 255.0f;
		a = 1.0f;
	} else {
		r = ((value >> 24) & 0xff) / 255.0f;
		g = ((value >> 16) & 0xff) / 255.0f;
		b = ((value >> 8) & 0xff) / 255.0f;
		a = (value & 0xff) / 255.0f;
	}
	/* wlr_scene_rect wants premultiplied components. */
	out[0] = r * a;
	out[1] = g * a;
	out[2] = b * a;
	out[3] = a;
	return true;
}

bool bgwl_env_color(const char *name, float out[static 4]) {
	const char *value = getenv(name);
	if (value == NULL || value[0] == '\0') {
		return false;
	}
	if (!bgwl_parse_color(value, out)) {
		wlr_log(WLR_ERROR, "%s: expected #RRGGBB or #RRGGBBAA, got '%s'",
			name, value);
		return false;
	}
	return true;
}

bool bgwl_parse_bool(const char *text, bool *out) {
	if (text == NULL) {
		return false;
	}
	if (strcmp(text, "true") == 0 || strcmp(text, "1") == 0 ||
			strcmp(text, "on") == 0 || strcmp(text, "yes") == 0) {
		*out = true;
		return true;
	}
	if (strcmp(text, "false") == 0 || strcmp(text, "0") == 0 ||
			strcmp(text, "off") == 0 || strcmp(text, "no") == 0) {
		*out = false;
		return true;
	}
	return false;
}

int bgwl_parse_layout(const char *text) {
	if (text == NULL) {
		return -1;
	}
	if (strcmp(text, "float") == 0) {
		return BGWL_LAYOUT_FLOAT;
	}
	if (strcmp(text, "tile") == 0) {
		return BGWL_LAYOUT_TILE;
	}
	if (strcmp(text, "even") == 0) {
		return BGWL_LAYOUT_EVEN;
	}
	return -1;
}

bool bgwl_parse_keycombo(const char *text, uint32_t *mods_out,
		uint32_t *keycode_out) {
	if (text == NULL || *text == '\0') {
		return false;
	}

	char buffer[BGWL_COMMAND_LEN];
	snprintf(buffer, sizeof(buffer), "%s", text);

	/* Split on '+': every token but the last is a modifier, the last is the
	 * key. Done by hand because strtok would consume separators that are
	 * still needed while walking the modifier list. */
	char *parts[8];
	int count = 0;
	char *cursor = buffer;
	while (cursor != NULL && count < 8) {
		parts[count++] = cursor;
		char *plus = strchr(cursor, '+');
		if (plus == NULL) {
			cursor = NULL;
		} else {
			*plus = '\0';
			cursor = plus + 1;
		}
	}
	if (count == 0) {
		return false;
	}

	uint32_t modifiers = 0;
	for (int i = 0; i < count - 1; i++) {
		bool matched = false;
		for (size_t j = 0;
				j < sizeof(modifier_tokens) / sizeof(modifier_tokens[0]); j++) {
			if (strcmp(parts[i], modifier_tokens[j].name) == 0) {
				modifiers |= modifier_tokens[j].modifier;
				matched = true;
				break;
			}
		}
		if (!matched) {
			wlr_log(WLR_ERROR, "unknown modifier '%s' in '%s'", parts[i], text);
			return false;
		}
	}

	const char *key_name = parts[count - 1];
	for (size_t i = 0; i < sizeof(key_tokens) / sizeof(key_tokens[0]); i++) {
		if (strcmp(key_name, key_tokens[i].name) == 0) {
			*mods_out = modifiers;
			*keycode_out = key_tokens[i].keycode;
			return true;
		}
	}

	/* Numeric escape hatch: a raw evdev code, for keys not in the table. */
	char *end = NULL;
	long code = strtol(key_name, &end, 10);
	if (end != key_name && *end == '\0' && code > 0 && code < 512) {
		*mods_out = modifiers;
		*keycode_out = (uint32_t)code;
		return true;
	}

	wlr_log(WLR_ERROR, "unknown key '%s' in '%s'", key_name, text);
	return false;
}

/* --- config file -------------------------------------------------------- */

static char *trim(char *text) {
	while (*text != '\0' && isspace((unsigned char)*text)) {
		text++;
	}
	char *end = text + strlen(text);
	while (end > text && isspace((unsigned char)end[-1])) {
		end--;
	}
	*end = '\0';
	return text;
}

static void set_string(char **slot, const char *value) {
	free(*slot);
	*slot = strdup(value);
}

static void add_binding(struct bgwl_config *cfg, const char *combo,
		const char *action) {
	if (cfg->binding_count >= BGWL_MAX_BINDINGS) {
		wlr_log(WLR_ERROR, "too many bindings, ignoring '%s'", combo);
		return;
	}
	struct bgwl_binding *binding = &cfg->bindings[cfg->binding_count];
	if (!bgwl_parse_keycombo(combo, &binding->modifiers, &binding->keycode)) {
		return;
	}
	snprintf(binding->action, sizeof(binding->action), "%s", action);
	cfg->binding_count++;
}

/* "workspace = 3:дизайн:#ff7755" — index, name, optional accent. */
static void apply_workspace_line(struct bgwl_config *cfg, char *value) {
	char *index_text = strtok(value, ":");
	char *name = strtok(NULL, ":");
	char *accent = strtok(NULL, ":");

	int index = atoi(index_text);
	if (index < 1 || index > BGWL_MAX_WORKSPACES) {
		wlr_log(WLR_ERROR, "workspace index out of range: %s", index_text);
		return;
	}
	if (index > cfg->workspace_count) {
		cfg->workspace_count = index;
	}
	if (name != NULL && name[0] != '\0') {
		snprintf(cfg->workspace_names[index - 1], BGWL_NAME_LEN, "%s", name);
	}
	if (accent != NULL && accent[0] != '\0') {
		if (!bgwl_parse_color(accent, cfg->workspace_accents[index - 1])) {
			wlr_log(WLR_ERROR, "workspace accent '%s' is not a colour", accent);
		}
	}
}

/* "rule = firefox:workspace=2:layout=tile" */
static void apply_rule_line(struct bgwl_config *cfg, char *value) {
	if (cfg->rule_count >= BGWL_MAX_RULES) {
		wlr_log(WLR_ERROR, "too many rules, ignoring '%s'", value);
		return;
	}
	struct bgwl_rule *rule = &cfg->rules[cfg->rule_count];
	memset(rule, 0, sizeof(*rule));
	rule->layout = -1;

	char *app_id = strtok(value, ":");
	if (app_id == NULL || app_id[0] == '\0') {
		return;
	}
	snprintf(rule->app_id, sizeof(rule->app_id), "%s", app_id);

	char *part;
	while ((part = strtok(NULL, ":")) != NULL) {
		if (strncmp(part, "workspace=", 10) == 0) {
			rule->workspace = atoi(part + 10);
		} else if (strncmp(part, "layout=", 7) == 0) {
			rule->layout = bgwl_parse_layout(part + 7);
		} else if (strcmp(part, "fullscreen") == 0) {
			rule->fullscreen = true;
		} else if (strcmp(part, "sticky") == 0) {
			rule->sticky = true;
		} else {
			wlr_log(WLR_ERROR, "rule '%s': unknown option '%s'", app_id, part);
		}
	}
	cfg->rule_count++;
}

static void apply_config_line(struct bgwl_config *cfg, char *line) {
	char *eq = strchr(line, '=');
	if (eq == NULL) {
		return;
	}
	*eq = '\0';
	char *key = trim(line);
	char *value = trim(eq + 1);
	if (key[0] == '\0' || value[0] == '\0') {
		return;
	}

	if (strcmp(key, "terminal") == 0) {
		set_string(&cfg->terminal, value);
	} else if (strcmp(key, "launcher") == 0) {
		set_string(&cfg->launcher, value);
	} else if (strcmp(key, "file-manager") == 0) {
		set_string(&cfg->file_manager, value);
	} else if (strcmp(key, "screenshot") == 0) {
		set_string(&cfg->screenshot, value);
	} else if (strcmp(key, "lock-command") == 0) {
		set_string(&cfg->lock_command, value);
	} else if (strcmp(key, "lid-command") == 0) {
		set_string(&cfg->lid_command, value);
	} else if (strcmp(key, "cursor-theme") == 0) {
		set_string(&cfg->cursor_theme, value);
	} else if (strcmp(key, "xkb-layout") == 0) {
		set_string(&cfg->xkb_layout, value);
	} else if (strcmp(key, "xkb-variant") == 0) {
		set_string(&cfg->xkb_variant, value);
	} else if (strcmp(key, "xkb-model") == 0) {
		set_string(&cfg->xkb_model, value);
	} else if (strcmp(key, "xkb-options") == 0) {
		set_string(&cfg->xkb_options, value);
	} else if (strcmp(key, "workspace") == 0) {
		apply_workspace_line(cfg, value);
	} else if (strcmp(key, "rule") == 0) {
		apply_rule_line(cfg, value);
	} else if (strcmp(key, "bind") == 0) {
		/* `bind = Super+Return:terminal` — ':' separates the combo from the
		 * action because '=' already split the line. */
		char *colon = strchr(value, ':');
		if (colon == NULL) {
			wlr_log(WLR_ERROR, "bind: expected combo:action, got '%s'", value);
			return;
		}
		*colon = '\0';
		add_binding(cfg, value, colon + 1);
	} else if (strncmp(key, "command-", 8) == 0) {
		/* A shortcut for a command line without learning the combo syntax:
		 * `command-Super+Print = grim ~/shot.png` */
		add_binding(cfg, key + 8, value);
	} else if (strcmp(key, "background-color") == 0) {
		if (!bgwl_parse_color(value, cfg->bg_color)) {
			wlr_log(WLR_ERROR, "background-color: bad colour '%s'", value);
		}
	} else if (strcmp(key, "accent-color") == 0) {
		if (!bgwl_parse_color(value, cfg->accent_color)) {
			wlr_log(WLR_ERROR, "accent-color: bad colour '%s'", value);
		}
	} else if (strcmp(key, "border-color") == 0) {
		if (!bgwl_parse_color(value, cfg->border_color)) {
			wlr_log(WLR_ERROR, "border-color: bad colour '%s'", value);
		}
	} else if (strcmp(key, "decor-color") == 0) {
		if (!bgwl_parse_color(value, cfg->decor_color)) {
			wlr_log(WLR_ERROR, "decor-color: bad colour '%s'", value);
		}
	} else if (strcmp(key, "text-color") == 0) {
		if (!bgwl_parse_color(value, cfg->text_color)) {
			wlr_log(WLR_ERROR, "text-color: bad colour '%s'", value);
		}
	} else if (strcmp(key, "background") == 0) {
		set_string(&cfg->bg_image, value);
	} else if (strcmp(key, "layout") == 0) {
		int layout = bgwl_parse_layout(value);
		if (layout < 0) {
			wlr_log(WLR_ERROR, "layout: expected float|tile|even, got '%s'",
				value);
		} else {
			cfg->layout = (enum bgwl_layout)layout;
		}
	} else if (strcmp(key, "workspaces") == 0) {
		int count = atoi(value);
		cfg->workspace_count = bgwl_min(BGWL_MAX_WORKSPACES, bgwl_max(1, count));
	} else if (strcmp(key, "gap-inner") == 0) {
		cfg->gap_inner = bgwl_max(0, atoi(value));
	} else if (strcmp(key, "gap-outer") == 0) {
		cfg->gap_outer = bgwl_max(0, atoi(value));
	} else if (strcmp(key, "border-width") == 0) {
		cfg->border_width = bgwl_max(0, atoi(value));
	} else if (strcmp(key, "cursor-size") == 0) {
		cfg->cursor_size = bgwl_max(8, atoi(value));
	} else if (strcmp(key, "idle-timeout") == 0) {
		cfg->idle_timeout_seconds = bgwl_max(0, atoi(value));
	} else if (strcmp(key, "output-scale") == 0) {
		cfg->output_scale = (float)atof(value);
	} else if (strcmp(key, "headless-size") == 0) {
		int width = 0, height = 0;
		if (sscanf(value, "%dx%d", &width, &height) == 2 && width > 0 &&
				height > 0) {
			cfg->headless_width = width;
			cfg->headless_height = height;
		}
	} else if (strcmp(key, "animations") == 0) {
		if (!bgwl_parse_bool(value, &cfg->animations)) {
			wlr_log(WLR_ERROR, "animations: expected a boolean");
		}
	} else if (strcmp(key, "anim-duration") == 0) {
		cfg->anim_duration_ms = bgwl_max(0, atoi(value));
	} else if (strcmp(key, "focus-follows-mouse") == 0) {
		if (!bgwl_parse_bool(value, &cfg->focus_follows_mouse)) {
			wlr_log(WLR_ERROR, "focus-follows-mouse: expected a boolean");
		}
	} else if (strcmp(key, "server-side-decoration") == 0) {
		if (!bgwl_parse_bool(value, &cfg->decorate_by_default)) {
			wlr_log(WLR_ERROR, "server-side-decoration: expected a boolean");
		}
	} else {
		wlr_log(WLR_ERROR, "bgwl.conf: unknown key '%s' (ignored)", key);
	}
}

static void load_config_file(struct bgwl_config *cfg) {
	const char *home = getenv("HOME");
	if (home == NULL) {
		return;
	}
	char path[PATH_MAX];
	snprintf(path, sizeof(path), "%s/.config/byteguru/bgwl.conf", home);

	FILE *file = fopen(path, "r");
	if (file == NULL) {
		/* Not an error: the defaults are a complete configuration. */
		return;
	}

	char line[1024];
	int applied = 0;
	while (fgets(line, sizeof(line), file) != NULL) {
		char *text = trim(line);
		if (text[0] == '\0' || text[0] == '#' || text[0] == ';') {
			continue;
		}
		apply_config_line(cfg, text);
		applied++;
	}
	fclose(file);
	wlr_log(WLR_INFO, "applied %d settings from %s", applied, path);
}

void bgwl_config_load(struct bgwl_config *cfg) {
	memset(cfg, 0, sizeof(*cfg));

	/* ByteguruOS brand defaults: dark slate canvas, amber accent. The accent
	 * is the amber the GTK themes use, so the focus ring and the panel agree
	 * without either of them asking. */
	static const float default_bg[4] = { 0.078f, 0.094f, 0.121f, 1.0f };
	static const float default_decor[4] = { 0.149f, 0.165f, 0.200f, 1.0f };
	static const float default_text[4] = { 0.878f, 0.886f, 0.902f, 1.0f };
	static const float default_accent[4] = { 0.949f, 0.639f, 0.239f, 1.0f };
	static const float default_border[4] = { 0.243f, 0.267f, 0.314f, 1.0f };
	memcpy(cfg->bg_color, default_bg, sizeof(default_bg));
	memcpy(cfg->decor_color, default_decor, sizeof(default_decor));
	memcpy(cfg->text_color, default_text, sizeof(default_text));
	memcpy(cfg->accent_color, default_accent, sizeof(default_accent));
	memcpy(cfg->border_color, default_border, sizeof(default_border));

	cfg->terminal = strdup(bgwl_env_string("BGWL_TERMINAL", "foot"));
	cfg->launcher = strdup(bgwl_env_string("BGWL_LAUNCHER", "bglauncher"));
	cfg->file_manager = strdup(bgwl_env_string("BGWL_FILE_MANAGER", "thunar"));
	cfg->screenshot = strdup(bgwl_env_string("BGWL_SCREENSHOT",
		"grim -g \"$(slurp)\" ~/Pictures/byteguru-$(date +%%Y%%m%%d-%%H%%M%%S).png"));
	cfg->lock_command = strdup(bgwl_env_string("BGWL_LOCK", "bglock"));
	cfg->lid_command = strdup(bgwl_env_string("BGWL_LID", "bglock"));
	cfg->cursor_theme = strdup(bgwl_env_string("BGWL_CURSOR_THEME", "default"));
	cfg->cursor_size = 24;
	cfg->layout = BGWL_LAYOUT_FLOAT;
	cfg->workspace_count = 6;
	cfg->gap_inner = 6;
	cfg->gap_outer = 8;
	cfg->border_width = 2;
	cfg->focus_follows_mouse = false;
	cfg->decorate_by_default = true;
	cfg->animations = true;
	cfg->anim_duration_ms = 140;
	cfg->idle_timeout_seconds = 300;
	cfg->output_scale = 0.0f;
	cfg->headless_width = BGWL_DEFAULT_HEADLESS_WIDTH;
	cfg->headless_height = BGWL_DEFAULT_HEADLESS_HEIGHT;

	/* Both Russian and English layouts are compiled into one keymap; the xkb
	 * option below gives group cycling, and bgwl adds Super+Space on top. */
	cfg->xkb_layout = strdup(bgwl_env_string("BGWL_XKB_LAYOUT", "ru,us"));
	cfg->xkb_variant = strdup(bgwl_env_string("BGWL_XKB_VARIANT", ""));
	cfg->xkb_model = strdup(bgwl_env_string("BGWL_XKB_MODEL", "pc105"));
	cfg->xkb_options = strdup(bgwl_env_string("BGWL_XKB_OPTIONS",
		"grp:alt_shift_toggle"));

	/* alpha 0 means "this stream has no accent of its own", which is how one
	 * accent= line styles the whole desktop while per-stream colours stay
	 * opt-in. */
	for (int i = 0; i < BGWL_MAX_WORKSPACES; i++) {
		cfg->workspace_accents[i][3] = 0.0f;
	}

	load_config_file(cfg);

	/* Environment after the file, so a session launcher can still override. */
	const char *background = getenv("BGWL_BACKGROUND");
	if (background != NULL && background[0] != '\0') {
		set_string(&cfg->bg_image, background);
	}
	if (bgwl_env_color("BGWL_BACKGROUND_COLOR", cfg->bg_color) &&
			cfg->bg_image != NULL) {
		/* An explicit solid colour wins over an image: exactly one of the two
		 * background modes is ever active. */
		free(cfg->bg_image);
		cfg->bg_image = NULL;
	}
	bgwl_env_color("BGWL_DECOR_COLOR", cfg->decor_color);
	bgwl_env_color("BGWL_TEXT_COLOR", cfg->text_color);
	bgwl_env_color("BGWL_ACCENT_COLOR", cfg->accent_color);
	if (getenv("BGWL_HEADLESS_SIZE") != NULL) {
		int w = 0, h = 0;
		if (sscanf(getenv("BGWL_HEADLESS_SIZE"), "%dx%d", &w, &h) == 2 &&
				w > 0 && h > 0) {
			cfg->headless_width = w;
			cfg->headless_height = h;
		} else {
			wlr_log(WLR_ERROR, "BGWL_HEADLESS_SIZE: expected WxH, ignored");
		}
	}
}

void bgwl_config_finish(struct bgwl_config *cfg) {
	free(cfg->terminal);
	free(cfg->launcher);
	free(cfg->file_manager);
	free(cfg->screenshot);
	free(cfg->lock_command);
	free(cfg->lid_command);
	free(cfg->cursor_theme);
	free(cfg->bg_image);
	free(cfg->startup_cmd);
	free(cfg->backend_hint);
	free(cfg->xkb_layout);
	free(cfg->xkb_variant);
	free(cfg->xkb_model);
	free(cfg->xkb_options);
	cfg->terminal = NULL;
	cfg->launcher = NULL;
	cfg->file_manager = NULL;
	cfg->screenshot = NULL;
	cfg->lock_command = NULL;
	cfg->lid_command = NULL;
	cfg->cursor_theme = NULL;
	cfg->bg_image = NULL;
	cfg->startup_cmd = NULL;
	cfg->backend_hint = NULL;
	cfg->xkb_layout = NULL;
	cfg->xkb_variant = NULL;
	cfg->xkb_model = NULL;
	cfg->xkb_options = NULL;
}

const char *bgwl_runtime_dir(void) {
	static char path[PATH_MAX];
	const char *runtime = getenv("XDG_RUNTIME_DIR");
	if (runtime == NULL || runtime[0] == '\0') {
		runtime = "/tmp";
	}
	snprintf(path, sizeof(path), "%s/byteguru", runtime);
	if (mkdir(path, 0700) != 0 && errno != EEXIST) {
		wlr_log(WLR_ERROR, "could not create %s: %s", path, strerror(errno));
		return NULL;
	}
	return path;
}

/* --- spawning ----------------------------------------------------------- */

bool bgwl_spawn(struct bgwl_server *server, const char *command) {
	if (command == NULL || command[0] == '\0') {
		return false;
	}

	pid_t pid = fork();
	if (pid < 0) {
		wlr_log(WLR_ERROR, "fork failed: %s", strerror(errno));
		return false;
	}
	if (pid == 0) {
		/* The child inherits the compositor's environment, which already
		 * carries WAYLAND_DISPLAY and BGWL_SESSION_TYPE. A new session keeps
		 * Ctrl+C on the compositor's terminal from reaching the client. */
		setsid();
		execl("/bin/sh", "/bin/sh", "-c", command, (void *)NULL);
		/* Only reached when exec failed; async-signal-safe reporting. */
		_exit(127);
	}

	wlr_log(WLR_INFO, "spawned '%s' (pid %d)", command, (int)pid);
	(void)server;
	return true;
}

void bgwl_run_action(struct bgwl_server *server, const char *action) {
	if (action == NULL || *action == '\0') {
		return;
	}
	struct bgwl_config *cfg = &server->cfg;

	/* Keywords first: they let the shell rebind a shortcut without knowing
	 * which program implements it, and let one command serve two bindings. */
	if (strcmp(action, "terminal") == 0) {
		bgwl_spawn(server, cfg->terminal);
	} else if (strcmp(action, "launcher") == 0) {
		bgwl_spawn(server, cfg->launcher);
	} else if (strcmp(action, "file-manager") == 0) {
		bgwl_spawn(server, cfg->file_manager);
	} else if (strcmp(action, "screenshot") == 0) {
		bgwl_spawn(server, cfg->screenshot);
	} else if (strcmp(action, "lock") == 0) {
		bgwl_spawn(server, cfg->lock_command);
	} else {
		bgwl_spawn(server, action);
	}
}

/* --- xkb ---------------------------------------------------------------- */

struct xkb_keymap *bgwl_xkb_keymap_new(const struct bgwl_config *cfg) {
	struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	if (context == NULL) {
		wlr_log(WLR_ERROR, "xkb_context_new failed");
		return NULL;
	}

	struct xkb_rule_names rules = {
		.rules = "evdev",
		.model = cfg->xkb_model,
		.layout = cfg->xkb_layout,
		.variant = cfg->xkb_variant,
		.options = cfg->xkb_options,
	};
	struct xkb_keymap *keymap = xkb_keymap_new_from_names(context, &rules,
		XKB_KEYMAP_COMPILE_NO_FLAGS);
	if (keymap == NULL) {
		wlr_log(WLR_ERROR,
			"failed to compile xkb keymap (layout='%s' variant='%s' "
			"model='%s' options='%s'); is xkb-data installed?",
			rules.layout, rules.variant, rules.model, rules.options);
	}
	xkb_context_unref(context);
	return keymap;
}

/* --- cairo buffer ------------------------------------------------------- */

static void bgwl_cairo_buffer_destroy(struct wlr_buffer *wlr_buffer) {
	struct bgwl_buffer *buffer = wl_container_of(wlr_buffer, buffer, base);
	cairo_surface_destroy(buffer->surface);
	free(buffer);
}

static bool bgwl_cairo_buffer_begin_data_ptr_access(struct wlr_buffer *wlr_buffer,
		uint32_t flags, void **data, uint32_t *format, size_t *stride) {
	struct bgwl_buffer *buffer = wl_container_of(wlr_buffer, buffer, base);

	if (flags & WLR_BUFFER_DATA_PTR_ACCESS_WRITE) {
		return false;
	}
	*format = DRM_FORMAT_ARGB8888;
	*data = cairo_image_surface_get_data(buffer->surface);
	*stride = cairo_image_surface_get_stride(buffer->surface);
	return true;
}

static void bgwl_cairo_buffer_end_data_ptr_access(struct wlr_buffer *wlr_buffer) {
}

/* Custom-buffer route (verified against wlroots 0.18.2): a struct wlr_buffer
 * with our own impl is the only supported way to feed cairo pixels into the
 * scene — wlroots has no wlr_buffer_data helper and no wlr_scene_text. */
static const struct wlr_buffer_impl bgwl_cairo_buffer_impl = {
	.destroy = bgwl_cairo_buffer_destroy,
	.begin_data_ptr_access = bgwl_cairo_buffer_begin_data_ptr_access,
	.end_data_ptr_access = bgwl_cairo_buffer_end_data_ptr_access,
};

struct bgwl_buffer *bgwl_cairo_buffer_create(int width, int height) {
	if (width <= 0 || height <= 0) {
		return NULL;
	}

	struct bgwl_buffer *buffer = calloc(1, sizeof(*buffer));
	if (buffer == NULL) {
		wlr_log(WLR_ERROR, "out of memory allocating cairo buffer");
		return NULL;
	}

	buffer->surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32,
		width, height);
	if (cairo_surface_status(buffer->surface) != CAIRO_STATUS_SUCCESS) {
		wlr_log(WLR_ERROR, "cairo_image_surface_create failed");
		cairo_surface_destroy(buffer->surface);
		free(buffer);
		return NULL;
	}

	wlr_buffer_init(&buffer->base, &bgwl_cairo_buffer_impl, width, height);
	return buffer;
}

void bgwl_buffer_drop(struct bgwl_buffer *buffer) {
	if (buffer != NULL) {
		wlr_buffer_drop(&buffer->base);
	}
}

static void set_source_rgba(cairo_t *cr, const float color[static 4]) {
	/* Colours arriving from the configuration are premultiplied (scene rect
	 * convention); cairo wants straight alpha. */
	float a = color[3];
	if (a <= 0.0f) {
		cairo_set_source_rgba(cr, 0.0, 0.0, 0.0, 0.0);
		return;
	}
	cairo_set_source_rgba(cr, color[0] / a, color[1] / a, color[2] / a, a);
}

/* Buttons are drawn, not typeset: a glyph depends on a font being installed,
 * and a window frame that shows tofu is worse than a plain square. */
static void draw_button(cairo_t *cr, double x, double y, enum bgwl_button kind,
		const float color[static 4]) {
	cairo_save(cr);
	set_source_rgba(cr, color);
	cairo_set_line_width(cr, 1.4);
	cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND);

	double cx = x + BGWL_BUTTON_WIDTH / 2.0;
	double cy = y + BGWL_DECOR_HEIGHT / 2.0;

	switch (kind) {
	case BGWL_BTN_CLOSE:
		cairo_move_to(cr, cx - 4, cy - 4);
		cairo_line_to(cr, cx + 4, cy + 4);
		cairo_move_to(cr, cx + 4, cy - 4);
		cairo_line_to(cr, cx - 4, cy + 4);
		cairo_stroke(cr);
		break;
	case BGWL_BTN_MAXIMIZE:
		cairo_rectangle(cr, cx - 4.5, cy - 4.5, 9, 9);
		cairo_stroke(cr);
		break;
	case BGWL_BTN_MENU:
		for (int row = -1; row <= 1; row++) {
			cairo_new_sub_path(cr);
			cairo_arc(cr, cx, cy + row * 4.0, 1.2, 0, 2 * M_PI);
		}
		cairo_fill(cr);
		break;
	default:
		break;
	}
	cairo_restore(cr);
}

struct bgwl_buffer *bgwl_draw_titlebar(struct bgwl_server *server,
		int width, const char *title, bool focused,
		const float accent[static 4]) {
	struct bgwl_config *cfg = &server->cfg;
	struct bgwl_buffer *buffer = bgwl_cairo_buffer_create(width,
		BGWL_DECOR_HEIGHT);
	if (buffer == NULL) {
		return NULL;
	}

	cairo_t *cr = cairo_create(buffer->surface);
	if (cr == NULL) {
		bgwl_buffer_drop(buffer);
		return NULL;
	}

	set_source_rgba(cr, cfg->decor_color);
	cairo_paint(cr);

	cairo_select_font_face(cr, "sans-serif", CAIRO_FONT_SLANT_NORMAL,
		focused ? CAIRO_FONT_WEIGHT_BOLD : CAIRO_FONT_WEIGHT_NORMAL);
	cairo_set_font_size(cr, 12.0);

	const char *label = title != NULL && title[0] != '\0' ? title
		: "без названия";
	float text[4] = { cfg->text_color[0], cfg->text_color[1],
		cfg->text_color[2],
		focused ? cfg->text_color[3] : cfg->text_color[3] * 0.62f };
	set_source_rgba(cr, text);

	cairo_text_extents_t extents;
	cairo_text_extents(cr, label, &extents);
	double available = width - 20.0 - BGWL_BUTTON_COUNT * BGWL_BUTTON_WIDTH - 8;
	cairo_move_to(cr, 10, BGWL_DECOR_HEIGHT / 2.0 - extents.y_bearing - 1);

	if (available > 0 && extents.x_advance > available) {
		/* Elide instead of overflowing: a title running under the buttons
		 * reads as a bug, and clipping mid-glyph is worse. */
		char clipped[BGWL_COMMAND_LEN];
		snprintf(clipped, sizeof(clipped), "%s", label);
		size_t len = strlen(clipped);
		while (len > 1) {
			clipped[len - 1] = '\0';
			cairo_text_extents(cr, clipped, &extents);
			if (extents.x_advance + 10 <= available) {
				break;
			}
			len--;
		}
		cairo_show_text(cr, clipped);
		cairo_move_to(cr, 10 + extents.x_advance,
			BGWL_DECOR_HEIGHT / 2.0 - extents.y_bearing - 1);
		cairo_show_text(cr, "…");
	} else if (available > 0) {
		cairo_show_text(cr, label);
	}

	/* Hairline under the bar: accent when focused, so the frame reads as one
	 * object with the window instead of a separate strip. */
	float hairline[4];
	if (focused) {
		hairline[0] = accent[0];
		hairline[1] = accent[1];
		hairline[2] = accent[2];
		hairline[3] = accent[3];
	} else {
		hairline[0] = 0.0f;
		hairline[1] = 0.0f;
		hairline[2] = 0.0f;
		hairline[3] = 0.35f;
	}
	set_source_rgba(cr, hairline);
	cairo_rectangle(cr, 0, BGWL_DECOR_HEIGHT - 1, width, 1);
	cairo_fill(cr);

	float button_color[4] = { cfg->text_color[0], cfg->text_color[1],
		cfg->text_color[2],
		focused ? cfg->text_color[3] : cfg->text_color[3] * 0.6f };
	/* Right-aligned, close nearest the edge — the order view.c hit-tests. */
	for (int i = 0; i < BGWL_BUTTON_COUNT; i++) {
		enum bgwl_button kind;
		switch (i) {
		case 0: kind = BGWL_BTN_MENU; break;
		case 1: kind = BGWL_BTN_MAXIMIZE; break;
		default: kind = BGWL_BTN_CLOSE; break;
		}
		double x = width - BGWL_BUTTON_WIDTH * (BGWL_BUTTON_COUNT - i) - 4;
		draw_button(cr, x, 0, kind, button_color);
	}

	cairo_destroy(cr);
	cairo_surface_flush(buffer->surface);
	return buffer;
}

struct bgwl_buffer *bgwl_load_png(const char *path) {
	cairo_surface_t *surface = cairo_image_surface_create_from_png(path);
	cairo_status_t status = cairo_surface_status(surface);
	if (status != CAIRO_STATUS_SUCCESS) {
		wlr_log(WLR_ERROR, "failed to load background '%s': %s", path,
			cairo_status_to_string(status));
		cairo_surface_destroy(surface);
		return NULL;
	}

	int width = cairo_image_surface_get_width(surface);
	int height = cairo_image_surface_get_height(surface);
	struct bgwl_buffer *buffer = bgwl_cairo_buffer_create(width, height);
	if (buffer == NULL) {
		cairo_surface_destroy(surface);
		return NULL;
	}

	cairo_t *cr = cairo_create(buffer->surface);
	if (cr == NULL) {
		cairo_surface_destroy(surface);
		bgwl_buffer_drop(buffer);
		return NULL;
	}
	cairo_set_source_surface(cr, surface, 0, 0);
	cairo_paint(cr);
	cairo_destroy(cr);
	cairo_surface_flush(buffer->surface);
	cairo_surface_destroy(surface);
	return buffer;
}
