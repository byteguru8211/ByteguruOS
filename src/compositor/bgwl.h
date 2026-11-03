/*
 * bgwl — ByteguruOS v0 Wayland compositor. Shared declarations.
 *
 * Decisions recorded here once so every translation unit agrees:
 *
 * - wlroots 0.19 scene-graph API (struct wlr_scene) is used for all rendering.
 *   No legacy root/damage listeners exist in this codebase: since wlroots 0.16
 *   the scene graph is the API the upstream examples (tinywl) are written
 *   against, and 0.19 has no non-scene example left.
 * - Server-side decoration is a single cairo-drawn titlebar per xdg toplevel,
 *   pushed into the scene as a struct wlr_buffer owned by us (see util.c).
 *   There are no min/max/close buttons and no resize borders in v0; the
 *   titlebar is a drag handle only.
 * - Layer surfaces live in five scene trees stacked bottom-up:
 *   bg -> bottom -> normal (xdg toplevels) -> top -> overlay.
 *
/* ВЕРИФИЦИРОВАТЬ: Several structs below read public fields of wlroots types directly
 * (wlr_keyboard.keymap/modifiers, wlr_xdg_surface.geometry/initial_commit/
 * initialized/data, wlr_seat.keyboard_state/pointer_state,
 * wlr_cursor.x/y, wlr_output.name/width/height/scale/current_mode). All of
 * them exist in wlroots 0.19.3; after any wlroots bump, grep this marker and
 * re-check each field against the installed headers.
 *
 * Include guard style: BGWL_* only. Nothing in this header may rely on
 * wlroots' own internal guard macros.
 */
#ifndef BGWL_H
#define BGWL_H

#include <stdbool.h>
#include <stdint.h>

#include <cairo.h>
#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/render/allocator.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_layer_shell_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/box.h>
#include <wlr/util/edges.h>
#include <xkbcommon/xkbcommon.h>

#define BGWL_DECOR_HEIGHT 28
#define BGWL_DEFAULT_HEADLESS_WIDTH 1280
#define BGWL_DEFAULT_HEADLESS_HEIGHT 800
#define BGWL_PANEL_NAMESPACE_PREFIX "bgwl-panel"

/* cairo image surface wrapped as a struct wlr_buffer so it can be pushed
 * into the scene graph as a wlr_scene_buffer node. */
struct bgwl_buffer {
	struct wlr_buffer base;
	cairo_surface_t *surface;
};

enum bgwl_cursor_mode {
	BGWL_CURSOR_PASSTHROUGH,
	BGWL_CURSOR_MOVE,
	BGWL_CURSOR_RESIZE,
};

/* Scene trees the compositor maintains, indexed by
 * zwlr_layer_shell_v1_layer for the four layer values plus the background
 * tree at index BGWL_TREE_BG. */
enum bgwl_tree {
	BGWL_TREE_BG = 0,
	BGWL_TREE_BACKGROUND,
	BGWL_TREE_BOTTOM,
	BGWL_TREE_NORMAL,
	BGWL_TREE_TOP,
	BGWL_TREE_OVERLAY,
	BGWL_TREE_COUNT,
};

struct bgwl_config {
	char *terminal;       /* BGWL_TERMINAL, command line run by Super+Enter */
	char *bg_image;       /* BGWL_BACKGROUND, path to a PNG; NULL = solid */
	float bg_color[4];    /* solid background, premultiplied RGBA */
	float decor_color[4]; /* titlebar fill, premultiplied RGBA */
	float text_color[4];  /* titlebar text, non-premultiplied RGBA for cairo */
	char *startup_cmd;    /* -c/--command */
	char *backend_hint;   /* --backend: "auto" or "headless" */
	int headless_width;
	int headless_height;
	/* xkb: both Russian and English must be reachable from the keymap. */
	char *xkb_layout;     /* BGWL_XKB_LAYOUT, default "ru,us" */
	char *xkb_variant;    /* BGWL_XKB_VARIANT, default "" */
	char *xkb_model;      /* BGWL_XKB_MODEL, default "pc105" */
	char *xkb_options;    /* BGWL_XKB_OPTIONS, default "grp:alt_shift_toggle" */
};

struct bgwl_server {
	struct wl_display *wl_display;
	struct wl_event_loop *event_loop;
	struct wlr_backend *backend;
	struct wlr_renderer *renderer;
	struct wlr_allocator *allocator;
	struct wlr_scene *scene;
	struct wlr_scene_output_layout *scene_layout;
	struct wlr_scene_tree *trees[BGWL_TREE_COUNT];

	struct bgwl_config cfg;

	struct wlr_xdg_shell *xdg_shell;
	struct wlr_layer_shell_v1 *layer_shell;
	struct wl_listener new_xdg_toplevel;
	struct wl_listener new_xdg_popup;
	struct wl_listener new_layer_surface;
	struct wl_list toplevels; /* bgwl_toplevel.link, front is top-most */

	struct wlr_cursor *cursor;
	struct wlr_xcursor_manager *cursor_mgr;
	struct wl_listener cursor_motion;
	struct wl_listener cursor_motion_absolute;
	struct wl_listener cursor_button;
	struct wl_listener cursor_axis;
	struct wl_listener cursor_frame;

	struct wlr_seat *seat;
	struct wl_listener new_input;
	struct wl_listener request_cursor;
	struct wl_listener request_set_selection;
	struct wl_list keyboards; /* bgwl_keyboard.link */
	enum bgwl_cursor_mode cursor_mode;
	struct bgwl_toplevel *grabbed_toplevel;
	double grab_x, grab_y;
	struct wlr_box grab_geobox;
	uint32_t resize_edges;

	struct wlr_output_layout *output_layout;
	struct wl_list outputs; /* bgwl_output.link */
	struct wl_listener new_output;

	/* The panel is owned by another ByteguruOS component; bgwl only tracks
	 * the one layer surface whose namespace is prefixed with
	 * BGWL_PANEL_NAMESPACE_PREFIX and enables/disables its scene node. */
	struct bgwl_layer_surface *panel;
	bool panel_visible;

	struct wl_event_source *sigterm_source;
	struct wl_event_source *sigint_source;

};

struct bgwl_output {
	struct wl_list link;
	struct bgwl_server *server;
	struct wlr_output *wlr_output;
	struct wlr_scene_output *scene_output;
	struct wl_listener frame;
	struct wl_listener request_state;
	struct wl_listener destroy;
	/* Per-output background, child of server->trees[BGWL_TREE_BG]. */
	struct wlr_scene_tree *bg_tree;
	struct wlr_scene_rect *bg_rect;
	struct bgwl_buffer *bg_image;
	struct wlr_scene_buffer *bg_image_node;
};

struct bgwl_toplevel {
	struct wl_list link;
	struct bgwl_server *server;
	struct wlr_xdg_toplevel *xdg_toplevel;
	/* scene_tree is our wrapper; the xdg surface hangs below it, offset by
	 * the titlebar height so the window geometry starts right under it. */
	struct wlr_scene_tree *scene_tree;
	struct wlr_scene_tree *xdg_tree;
	struct wl_listener map;
	struct wl_listener unmap;
	struct wl_listener commit;
	struct wl_listener destroy;
	struct wl_listener request_move;
	struct wl_listener request_resize;
	struct wl_listener request_maximize;
	struct wl_listener request_fullscreen;
	struct wl_listener set_title;
	/* Titlebar decoration. */
	struct bgwl_buffer *decor_buffer;
	struct wlr_scene_buffer *decor_node;
	int decor_width;  /* width the current buffer was drawn for */
	bool decor_dirty;
};

struct bgwl_popup {
	struct wlr_xdg_popup *xdg_popup;
	struct wl_listener commit;
	struct wl_listener destroy;
};

struct bgwl_layer_surface {
	struct bgwl_server *server;
	struct wlr_layer_surface_v1 *layer_surface;
	struct wlr_scene_layer_surface_v1 *scene_layer_surface;
	bool is_panel;
	struct wl_listener map;
	struct wl_listener unmap;
	struct wl_listener commit;
	struct wl_listener new_popup;
	struct wl_listener destroy;
};

struct bgwl_keyboard {
	struct wl_list link;
	struct bgwl_server *server;
	struct wlr_keyboard *wlr_keyboard;
	struct wl_listener modifiers;
	struct wl_listener key;
	struct wl_listener destroy;
};

/* --- server.c --- */
void bgwl_server_init(struct bgwl_server *server);
bool bgwl_server_start(struct bgwl_server *server);
void bgwl_server_finish(struct bgwl_server *server);
void bgwl_server_terminate(struct bgwl_server *server);
struct bgwl_output *bgwl_server_first_output(struct bgwl_server *server);
void bgwl_server_warp_cursor_to_center(struct bgwl_server *server);

/* Cross-file wl_listener callbacks. They are intentionally not static: each
 * one is installed in server.c and defined in the file that owns the object
 * it belongs to. */
void bgwl_new_output(struct wl_listener *listener, void *data);       /* output.c */
void bgwl_new_input(struct wl_listener *listener, void *data);        /* input.c */
void bgwl_cursor_motion(struct wl_listener *listener, void *data);    /* input.c */
void bgwl_cursor_motion_absolute(struct wl_listener *listener, void *data);
void bgwl_cursor_button(struct wl_listener *listener, void *data);
void bgwl_cursor_axis(struct wl_listener *listener, void *data);
void bgwl_cursor_frame(struct wl_listener *listener, void *data);
void bgwl_seat_request_cursor(struct wl_listener *listener, void *data);
void bgwl_seat_request_set_selection(struct wl_listener *listener, void *data);
void bgwl_new_xdg_toplevel(struct wl_listener *listener, void *data); /* xdg.c */
void bgwl_new_xdg_popup(struct wl_listener *listener, void *data);    /* xdg.c */
void bgwl_new_layer_surface(struct wl_listener *listener, void *data);/* layer.c */

/* --- util.c --- */
const char *bgwl_env_string(const char *name, const char *fallback);
bool bgwl_env_color(const char *name, float out[static 4]);
void bgwl_config_load(struct bgwl_config *cfg);
void bgwl_config_finish(struct bgwl_config *cfg);
/* Spawn a command line through /bin/sh; returns false if it could not even
 * fork. The child is reaped automatically (SIGCHLD is ignored). */
bool bgwl_spawn(struct bgwl_server *server, const char *command);
/* cairo-backed struct wlr_buffer, modelled on wlroots' own cairo-buffer
 * example. Caller owns the returned buffer and must bgwl_buffer_drop() it
 * once the scene node no longer needs it. */
struct bgwl_buffer *bgwl_cairo_buffer_create(int width, int height);
void bgwl_buffer_drop(struct bgwl_buffer *buffer);
/* Draws the solid+text titlebar for a toplevel into a fresh buffer. */
struct bgwl_buffer *bgwl_draw_titlebar(struct bgwl_server *server,
	int width, const char *title);
/* Loads a PNG as a cairo buffer; NULL if the file is missing or not PNG. */
struct bgwl_buffer *bgwl_load_png(const char *path);
/* Builds the ru+us keymap. Returns NULL on failure. */
struct xkb_keymap *bgwl_xkb_keymap_new(const struct bgwl_config *cfg);

/* --- view.c --- */
struct bgwl_toplevel *bgwl_toplevel_create(struct bgwl_server *server,
	struct wlr_xdg_toplevel *xdg_toplevel);
void bgwl_toplevel_focus(struct bgwl_toplevel *toplevel);
/* Top-most client surface at layout coords (NULL for compositor-owned
 * pixels such as our titlebar or the background). */
struct wlr_surface *bgwl_surface_at(struct bgwl_server *server, double lx,
	double ly, double *sx_out, double *sy_out);
/* Toplevel under the cursor, including its server-side titlebar. */
struct bgwl_toplevel *bgwl_toplevel_at(struct bgwl_server *server,
	double lx, double ly);
void bgwl_toplevel_begin_interactive(struct bgwl_toplevel *toplevel,
	enum bgwl_cursor_mode mode, uint32_t edges);
void bgwl_toplevel_process_motion(struct bgwl_server *server);
void bgwl_toplevel_reset_cursor_mode(struct bgwl_server *server);

/* --- input.c --- */
void bgwl_keyboard_switch_layout(struct bgwl_keyboard *keyboard);

/* --- xdg.c --- */
struct bgwl_popup *bgwl_popup_create(struct wlr_xdg_popup *xdg_popup,
	struct wlr_scene_tree *parent_tree);

/* --- layer.c --- */
void bgwl_panel_toggle(struct bgwl_server *server);

#endif /* BGWL_H */
