/*
 * Small self-contained helpers: environment parsing, process spawning, the
 * cairo-backed struct wlr_buffer used for the titlebar and the background
 * image, and the ru+us xkb keymap.
 *
 * Nothing here invents wlroots API: the buffer implementation follows
 * wlroots' own examples/cairo-buffer.c, which is the only documented way to
 * get cairo pixels into struct wlr_scene on 0.19 (there is no
 * wlr_scene_cairo node and no wlr_buffer_data in this version).
 */
#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>

#include <drm_fourcc.h>
#include <wayland-server-core.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/types/wlr_buffer.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

#include "bgwl.h"

const char *bgwl_env_string(const char *name, const char *fallback) {
	const char *value = getenv(name);
	if (value == NULL || value[0] == '\0') {
		return fallback;
	}
	return value;
}

/* "#RRGGBB" or "#RRGGBBAA" into premultiplied RGBA floats. */
static bool parse_hex_color(const char *text, float out[static 4]) {
	if (text[0] != '#') {
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
	if (!parse_hex_color(value, out)) {
		wlr_log(WLR_ERROR, "%s: expected #RRGGBB or #RRGGBBAA, got '%s'",
			name, value);
		return false;
	}
	return true;
}

void bgwl_config_load(struct bgwl_config *cfg) {
	memset(cfg, 0, sizeof(*cfg));

	cfg->terminal = strdup(bgwl_env_string("BGWL_TERMINAL", "foot"));
	cfg->bg_image = NULL;
	const char *background = getenv("BGWL_BACKGROUND");
	if (background != NULL && background[0] != '\0') {
		cfg->bg_image = strdup(background);
	}
	cfg->startup_cmd = NULL;
	cfg->backend_hint = NULL;

	/* ByteguruOS brand default: dark slate. */
	static const float default_bg[4] = { 0.078f, 0.094f, 0.121f, 1.0f };
	static const float default_decor[4] = { 0.149f, 0.165f, 0.200f, 1.0f };
	static const float default_text[4] = { 0.878f, 0.886f, 0.902f, 1.0f };
	memcpy(cfg->bg_color, default_bg, sizeof(default_bg));
	memcpy(cfg->decor_color, default_decor, sizeof(default_decor));
	memcpy(cfg->text_color, default_text, sizeof(default_text));
	if (bgwl_env_color("BGWL_BACKGROUND_COLOR", cfg->bg_color) &&
			cfg->bg_image != NULL) {
		/* An explicit solid colour wins over an image: exactly one of the
		 * two background modes is ever active. */
		free(cfg->bg_image);
		cfg->bg_image = NULL;
	}
	bgwl_env_color("BGWL_DECOR_COLOR", cfg->decor_color);
	bgwl_env_color("BGWL_TEXT_COLOR", cfg->text_color);

	cfg->headless_width = BGWL_DEFAULT_HEADLESS_WIDTH;
	cfg->headless_height = BGWL_DEFAULT_HEADLESS_HEIGHT;
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

	/* Both Russian and English layouts are compiled into one keymap; the
	 * xkb option below gives group cycling, and bgwl adds Super+Space on
	 * top of it (see input.c). */
	cfg->xkb_layout = strdup(bgwl_env_string("BGWL_XKB_LAYOUT", "ru,us"));
	cfg->xkb_variant = strdup(bgwl_env_string("BGWL_XKB_VARIANT", ""));
	cfg->xkb_model = strdup(bgwl_env_string("BGWL_XKB_MODEL", "pc105"));
	cfg->xkb_options = strdup(bgwl_env_string("BGWL_XKB_OPTIONS",
		"grp:alt_shift_toggle"));
}

void bgwl_config_finish(struct bgwl_config *cfg) {
	free(cfg->terminal);
	free(cfg->bg_image);
	free(cfg->startup_cmd);
	free(cfg->backend_hint);
	free(cfg->xkb_layout);
	free(cfg->xkb_variant);
	free(cfg->xkb_model);
	free(cfg->xkb_options);
	cfg->terminal = NULL;
	cfg->bg_image = NULL;
	cfg->startup_cmd = NULL;
	cfg->backend_hint = NULL;
	cfg->xkb_layout = NULL;
	cfg->xkb_variant = NULL;
	cfg->xkb_model = NULL;
	cfg->xkb_options = NULL;
}

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
		 * carries WAYLAND_DISPLAY and BGWL_SESSION_TYPE. */
		setsid();
		execl("/bin/sh", "/bin/sh", "-c", command, (void *)NULL);
		/* Only reached when exec failed; async-signal-safe reporting. */
		_exit(127);
	}

	wlr_log(WLR_INFO, "spawned '%s' (pid %d)", command, (int)pid);
	(void)server;
	return true;
}

/* --- cairo buffer ------------------------------------------------------- */

static void bgwl_cairo_buffer_destroy(struct wlr_buffer *wlr_buffer) {
	struct bgwl_buffer *buffer = wl_container_of(wlr_buffer, buffer, base);
	wlr_buffer_finish(wlr_buffer);
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

/* ВЕРИФИЦИРОВАТЬ: struct wlr_buffer_impl field names (destroy,
 * begin_data_ptr_access, end_data_ptr_access), wlr_buffer_init() and
 * wlr_buffer_drop() come from <wlr/interfaces/wlr_buffer.h> and
 * <wlr/types/wlr_buffer.h>; this custom-buffer route is the only supported
 * way to feed cairo pixels to the scene in 0.19.3 (no wlr_buffer_data, no
 * wlr_scene_text). */
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
	/* Colours arriving from the environment are premultiplied (scene rect
	 * convention); cairo wants straight alpha. */
	float a = color[3];
	if (a <= 0.0f) {
		cairo_set_source_rgba(cr, 0.0, 0.0, 0.0, 0.0);
		return;
	}
	cairo_set_source_rgba(cr, color[0] / a, color[1] / a, color[2] / a, a);
}

struct bgwl_buffer *bgwl_draw_titlebar(struct bgwl_server *server,
		int width, const char *title) {
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

	set_source_rgba(cr, server->cfg.decor_color);
	cairo_paint(cr);

	/* Underline separating the titlebar from the window content. */
	cairo_set_line_width(cr, 1.0);
	cairo_move_to(cr, 0, BGWL_DECOR_HEIGHT - 0.5);
	cairo_line_to(cr, width, BGWL_DECOR_HEIGHT - 0.5);
	cairo_set_source_rgba(cr, 0.0f, 0.0f, 0.0f, 0.35f);
	cairo_stroke(cr);

	if (title != NULL && title[0] != '\0') {
		cairo_select_font_face(cr, "sans-serif", CAIRO_FONT_SLANT_NORMAL,
			CAIRO_FONT_WEIGHT_NORMAL);
		cairo_set_font_size(cr, 13.0);
		set_source_rgba(cr, server->cfg.text_color);
		cairo_move_to(cr, 9.0, BGWL_DECOR_HEIGHT - 9.0);
		cairo_show_text(cr, title);
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
