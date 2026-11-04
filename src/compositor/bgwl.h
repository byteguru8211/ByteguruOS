/*
 * bgwl — ByteguruOS Wayland compositor. Shared declarations.
 *
 * Decisions recorded here once so every translation unit agrees:
 *
 * - Target is the wlroots 0.18 scene-graph API shipped in Debian 13 (trixie),
 *   which is what the ByteguruOS image is built against. No legacy root/damage
 *   listeners exist in this codebase: since wlroots 0.16 the scene graph is the
 *   API the upstream examples (tinywl) are written against.
 * - Every toplevel lives inside its workspace's scene tree. Switching a
 *   workspace therefore enables/disables one node instead of iterating over
 *   windows, and a slide animation moves one node. This is the whole reason
 *   workspaces are cheap enough to run on 2 GiB hardware.
 * - Server-side decoration: bgwl draws its own titlebar with cairo into a
 *   struct wlr_buffer and pushes it into the scene as a wlr_scene_buffer
 *   (see util.c). wlroots has no wlr_scene_text helper, so a cairo buffer is
 *   the only way to draw a label without shipping a widget toolkit. Clients
 *   that ask for client-side decorations (GTK4, Qt) get our titlebar hidden —
 *   see the decoration field on bgwl_toplevel.
 * - Layer surfaces live in five scene trees stacked bottom-up:
 *   bg -> bottom -> normal (workspaces) -> top -> overlay.
 * - The compositor owns no product policy: the panel, launcher and settings
 *   are separate processes that talk to it over the bgwl control socket
 *   (ctl.c). Everything they can ask for is a line of text; everything bgwl
 *   answers is JSON.
 *
 * Public wlroots fields read directly by this code (verified against the
 * installed 0.18.2 headers): wlr_keyboard.keymap/modifiers/keycodes/
 * num_keycodes, wlr_xdg_surface.role/data/surface/initialized,
 * wlr_seat.keyboard_state/pointer_state, wlr_cursor.x/y,
 * wlr_output.name/width/height/scale/current_mode. Note in particular that
 * wlr_xdg_surface has NO public `geometry` field in 0.18 — use
 * wlr_xdg_surface_get_geometry(); view.c funnels every read through one
 * helper so an API bump touches exactly one function.
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
/* The shell's lock screen identifies itself with this namespace; see
 * bgwl_server_set_locked(). */
#define BGWL_LOCK_NAMESPACE_PREFIX "bgwl-lock"
#define BGWL_MAX_WORKSPACES 10
#define BGWL_MAX_OUTPUTS 8
#define BGWL_NAME_LEN 48
#define BGWL_COMMAND_LEN 256
#define BGWL_MAX_RULES 64
#define BGWL_MAX_BINDINGS 64

/* Geometry code reads better with these than with open-coded comparisons, and
 * having them in the header keeps tiling.c and view.c from each defining a
 * private pair. */
static inline int bgwl_max(int a, int b) {
	return a > b ? a : b;
}

static inline int bgwl_min(int a, int b) {
	return a < b ? a : b;
}

/* One titlebar button. Hit testing is done by x-range inside the bar; there
 * is no separate widget, so the drawing in util.c and the hit test in view.c
 * must agree on BGWL_BUTTON_WIDTH. */
#define BGWL_BUTTON_WIDTH 24
#define BGWL_BUTTON_COUNT 3

/* cairo image surface wrapped as a struct wlr_buffer so it can be pushed
 * into the scene graph as a wlr_scene_buffer node. */
struct bgwl_buffer {
	struct wlr_buffer base;
	cairo_surface_t *surface;
	bool opaque;
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

enum bgwl_button {
	BGWL_BTN_NONE = 0,
	BGWL_BTN_CLOSE,
	BGWL_BTN_MAXIMIZE,
	BGWL_BTN_MENU,
};

/* How a workspace arranges its windows.
 * FLOAT keeps every window where the client put it; TILE is master+stack;
 * EVEN is a balanced grid with no master. */
enum bgwl_layout {
	BGWL_LAYOUT_FLOAT = 0,
	BGWL_LAYOUT_TILE,
	BGWL_LAYOUT_EVEN,
	BGWL_LAYOUT_COUNT,
};

/* Which side the stack grows on in BGWL_LAYOUT_TILE. */
enum bgwl_split {
	BGWL_SPLIT_RIGHT = 0,	/* master left, stack right */
	BGWL_SPLIT_BOTTOM,	/* master top, stack bottom */
};

struct bgwl_button_box {
	int x, width;
	enum bgwl_button button;
};

/* A rule from the config file: match an app-id, force a workspace and/or a
 * layout. Applied when a toplevel maps, which is what makes "the browser
 * always opens in Поток 2" work without the user touching anything. */
struct bgwl_rule {
	char app_id[BGWL_NAME_LEN];
	int workspace;		/* 1-based, 0 = unset */
	int layout;		/* enum bgwl_layout, -1 = unset */
	bool fullscreen;
	bool sticky;
};

/* A configured shortcut: modifier mask + XKB evdev keycode + action string.
 * Keycodes are resolved from names at load time (xkb_keysym_from_name →
 * evdev), so the config says "Return" and not "36". */
struct bgwl_binding {
	uint32_t modifiers;	/* WLR_MODIFIER_* */
	uint32_t keycode;	/* KEY_* from linux/input-event-codes.h */
	char action[BGWL_COMMAND_LEN];
};

struct bgwl_config {
	char *terminal;       /* command line run by the default terminal binding */
	char *launcher;       /* Guru Line */
	char *file_manager;
	char *screenshot;     /* full command; area selection is slurp+grim */
	char *bg_image;       /* path to a PNG; NULL = solid */
	float bg_color[4];    /* solid background, premultiplied RGBA */
	float decor_color[4]; /* titlebar fill, premultiplied RGBA */
	float text_color[4];  /* titlebar text, non-premultiplied RGBA for cairo */
	float accent_color[4];/* focus border + default workspace accent */
	float border_color[4];/* unfocused border */
	char *startup_cmd;    /* -c/--command */
	char *backend_hint;   /* --backend: "auto", "headless" or "drm" */
	int headless_width;
	int headless_height;

	/* Window management. */
	enum bgwl_layout layout;
	int workspace_count;
	char workspace_names[BGWL_MAX_WORKSPACES][BGWL_NAME_LEN];
	float workspace_accents[BGWL_MAX_WORKSPACES][4];
	int gap_inner;
	int gap_outer;
	int border_width;
	bool focus_follows_mouse;
	bool decorate_by_default;	/* SSD unless the client insists on CSD */

	/* Motion. */
	bool animations;
	int anim_duration_ms;

	/* Idle + lock: 0 disables. */
	int idle_timeout_seconds;
	char *lock_command;

	/* Power: command run when the lid closes. */
	char *lid_command;

	char *cursor_theme;
	int cursor_size;

	/* Output: 0 means "ask the backend". */
	float output_scale;

	/* xkb: both Russian and English must be reachable from the keymap. */
	char *xkb_layout;
	char *xkb_variant;
	char *xkb_model;
	char *xkb_options;

	struct bgwl_rule rules[BGWL_MAX_RULES];
	int rule_count;
	struct bgwl_binding bindings[BGWL_MAX_BINDINGS];
	int binding_count;
};

/* Forward declarations for the subsystems that keep state on the server. */
struct bgwl_workspace;
struct bgwl_animation;
struct bgwl_ctl;
struct bgwl_ctl_client;
struct bgwl_session;
struct bgwl_xwm;

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
	struct wl_list layer_surfaces; /* bgwl_layer_surface.link */
	int next_window_id;	    /* handed out to each new toplevel */

	/* Protocol globals that other files need a handle on. */
	struct wlr_xdg_decoration_manager_v1 *xdg_decoration;
	struct wl_listener new_decoration;
	struct wlr_output_manager_v1 *output_manager;
	struct wl_listener output_manager_apply;
	struct wl_listener output_manager_test;
	struct wlr_session_lock_manager_v1 *lock_manager;
	struct wl_listener new_lock;
	struct wlr_idle_notifier_v1 *idle_notifier;
	struct wlr_idle_inhibit_manager_v1 *idle_inhibit;
	struct wl_listener new_inhibit;
	struct wlr_input_method_manager_v2 *input_method;
	struct wlr_text_input_manager_v3 *text_input;
	struct wlr_virtual_keyboard_manager_v1 *virtual_keyboard;
	struct wlr_virtual_pointer_manager_v1 *virtual_pointer;
	struct wlr_screencopy_manager_v1 *screencopy;
	struct wlr_foreign_toplevel_manager_v1 *foreign_toplevel_manager;

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
	/* Button-press offset used by Alt+drag resizing; true while grabbing. */
	bool resize_active;

	struct wlr_output_layout *output_layout;
	struct wl_list outputs; /* bgwl_output.link */
	struct wl_listener new_output;

	/* Workspaces ("Потоки"): one scene tree each, exactly one active. */
	struct wl_list workspaces;   /* bgwl_workspace.link, index order */
	struct bgwl_workspace *active_ws;
	/* bgwl's own record of the focused window. The seat's keyboard focus is the
	 * client-visible half, but it can be empty when no keyboard device exists
	 * yet — a headless session, or a laptop with the lid shut — and the panel
	 * still has to know what is focused. */
	struct bgwl_toplevel *focus_view;
	int ws_transition;	    /* +1 forward, -1 backward, 0 idle */

	/* Subsystems. */
	struct bgwl_ctl *ctl;
	struct bgwl_session *session;
	struct bgwl_xwm *xwayland;
	struct wl_list animations;    /* bgwl_animation.link */
	struct wl_event_source *anim_source;

	/* The panel is owned by another ByteguruOS component; bgwl only tracks
	 * the one layer surface whose namespace is prefixed with
	 * BGWL_PANEL_NAMESPACE_PREFIX and enables/disables its scene node. */
	struct bgwl_layer_surface *panel;
	bool panel_visible;
	/* Lock surface: a layer client whose namespace starts with
	 * BGWL_LOCK_NAMESPACE_PREFIX. It is the shell's way to lock without
	 * ext-session-lock, and bgwl un-locks automatically when it disappears —
	 * a crashed lock screen must never strand a user in a black session. */
	struct bgwl_layer_surface *lock_surface;

	struct wl_event_source *sigterm_source;
	struct wl_event_source *sigint_source;

	/* Set while a session lock owns the screen: keybindings are dead and
	 * only the lock surface receives input. */
	bool locked;
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
	/* Area left after panel exclusive zones, in layout coordinates. Tiling
	 * reads this; it is recomputed by layer.c whenever a layer surface
	 * changes its exclusive zone. */
	struct wlr_box usable;
	bool dpms_on;
};

struct bgwl_workspace {
	struct wl_list link;
	struct bgwl_server *server;
	int index;			/* 0-based */
	char name[BGWL_NAME_LEN];
	float accent[4];
	enum bgwl_layout layout;
	enum bgwl_split split;
	int master_count;
	int master_pct;			/* 10..90 */
	bool fullscreen_visible;	/* a window in this workspace is fullscreen */
	struct wlr_scene_tree *tree;	/* child of trees[BGWL_TREE_NORMAL] */
	struct wl_list toplevels;	/* bgwl_toplevel.ws_link */
	int mapped_count;
};

struct bgwl_toplevel {
	struct wl_list link;      /* bgwl_server.toplevels, stacking order */
	struct wl_list ws_link;     /* bgwl_workspace.toplevels, tiling order */
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
	/* Focus border: one rect behind the window, revealed by the width. */
	struct wlr_scene_rect *border;

	/* Window management state owned by bgwl, not the client. */
	int id;				/* stable handle used by the control socket */
	struct bgwl_workspace *ws;
	bool sticky;			/* follows every workspace */
	bool tiled;
	bool maximized;
	bool fullscreen;
	bool wants_server_side;		/* xdg-decoration said "server_side" */
	bool decorated;			/* draw the titlebar for this window */
	bool above;			/* pinned to the top layer tree */
	bool was_tiled;		/* tiled state remembered across fullscreen */
	struct wlr_box float_box;	/* where the user left it, restored on untile */
	/* The geometry to come back to when maximise or fullscreen releases the
	 * window. One box for both states, recorded the first time either is
	 * entered: maximise and fullscreen can overlap, and two separate boxes
	 * would have to agree about which one wins. */
	struct wlr_box restore_box;
	bool has_restore;
	/* Last size we configured. Before the client commits its own geometry this
	 * is the only truth about the window's size, and centre-of-window
	 * hit testing depends on it. */
	int pending_width, pending_height;
	float opacity;
};

struct bgwl_popup {
	struct wlr_xdg_popup *xdg_popup;
	struct wl_listener commit;
	struct wl_listener destroy;
};

struct bgwl_layer_surface {
	struct wl_list link;	/* bgwl_server.layer_surfaces, reconfigured in order */
	struct bgwl_server *server;
	struct wlr_layer_surface_v1 *layer_surface;
	struct wlr_scene_layer_surface_v1 *scene_layer_surface;
	bool is_panel;
	bool is_lock;
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
/* Union of every output's usable area, in layout coordinates. */
struct wlr_box bgwl_server_usable_box(struct bgwl_server *server);
/* True while a session lock owns the screen (input.c asks before bindings). */
bool bgwl_server_is_locked(struct bgwl_server *server);
/* Locks or unlocks the session. Both ext-session-lock (session.c) and the
 * shell's own lock surface (layer.c, ctl.c) go through this one function, so
 * there is exactly one definition of "locked". */
void bgwl_server_set_locked(struct bgwl_server *server, bool locked);
/* True while an ext-session-lock client owns the screen: the shell's own lock
 * surface must not be able to unlock it. */
bool bgwl_session_ext_lock_active(struct bgwl_server *server);

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
/* Parses "Super+Shift+Return" into (WLR_MODIFIER_*, KEY_*). False on unknown
 * names so a typo in the config is reported instead of silently ignored. */
bool bgwl_parse_keycombo(const char *text, uint32_t *mods_out,
	uint32_t *keycode_out);
/* "float|tile|even" -> enum bgwl_layout; -1 when unrecognised. */
int bgwl_parse_layout(const char *text);
bool bgwl_parse_bool(const char *text, bool *out);
/* Premultiplied RGBA from "#RRGGBB[AA]"; false if malformed. */
bool bgwl_parse_color(const char *text, float out[static 4]);
/* Spawn a command line through /bin/sh; returns false if it could not even
 * fork. The child is reaped automatically (SIGCHLD is ignored). */
bool bgwl_spawn(struct bgwl_server *server, const char *command);
/* Executes a binding action: a known keyword (terminal, launcher, …) or a
 * raw command line. */
void bgwl_run_action(struct bgwl_server *server, const char *action);
/* cairo image surface wrapped as a struct wlr_buffer so it can be pushed
 * into the scene graph as a wlr_scene_buffer node. */
/* opaque=true отдаёт буфер как XRGB8888: для залитого цветом заголовка это не
 * оптимизация, а корректность — как ARGB композитор читал бы альфу из
 * G-канала cairo-пикселя. Для изображений с прозрачностью — false. */
struct bgwl_buffer *bgwl_cairo_buffer_create(int width, int height,
	bool opaque);
void bgwl_buffer_drop(struct bgwl_buffer *buffer);
/* Draws the solid+text titlebar with the window buttons for a toplevel. */
struct bgwl_buffer *bgwl_draw_titlebar(struct bgwl_server *server,
	int width, const char *title, bool focused, const float accent[static 4]);
/* Loads a PNG as a cairo buffer; NULL if the file is missing or not PNG. */
struct bgwl_buffer *bgwl_load_png(const char *path);
/* Builds the configured keymap. Returns NULL on failure. */
struct xkb_keymap *bgwl_xkb_keymap_new(const struct bgwl_config *cfg);
/* $XDG_RUNTIME_DIR/byteguru, created 0700; NULL if it could not be made. */
const char *bgwl_runtime_dir(void);

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
/* Which titlebar button, if any, is at these layout coordinates. */
enum bgwl_button bgwl_toplevel_button_at(struct bgwl_toplevel *toplevel,
	double lx, double ly);
/* Frame edges within grab distance of these layout coordinates; 0 if none. */
uint32_t bgwl_toplevel_resize_edges_at(struct bgwl_toplevel *toplevel,
	double lx, double ly);
/* Show or hide our titlebar for this window (xdg-decoration answer). */
void bgwl_toplevel_set_decorated(struct bgwl_toplevel *toplevel, bool value);
void bgwl_toplevel_begin_interactive(struct bgwl_toplevel *toplevel,
	enum bgwl_cursor_mode mode, uint32_t edges);
void bgwl_toplevel_process_motion(struct bgwl_server *server);
void bgwl_toplevel_reset_cursor_mode(struct bgwl_server *server);
/* Geometry helpers used by the layout engine and by interactive moves. */
void bgwl_toplevel_set_geometry(struct bgwl_toplevel *toplevel,
	struct wlr_box box);
void bgwl_toplevel_get_geometry(struct bgwl_toplevel *toplevel,
	struct wlr_box *box);
/* State transitions that keep bgwl's model and the client's xdg state in sync. */
void bgwl_toplevel_set_maximized(struct bgwl_toplevel *toplevel, bool value);
void bgwl_toplevel_set_fullscreen(struct bgwl_toplevel *toplevel, bool value);
void bgwl_toplevel_set_tiled(struct bgwl_toplevel *toplevel, bool value);
void bgwl_toplevel_close(struct bgwl_toplevel *toplevel);
void bgwl_toplevel_raise(struct bgwl_toplevel *toplevel);
/* Reparents into a workspace's tree and updates visibility. */
void bgwl_toplevel_move_to_workspace(struct bgwl_toplevel *toplevel,
	struct bgwl_workspace *ws);
/* Focused toplevel across all workspaces (NULL if none). */
struct bgwl_toplevel *bgwl_server_focused_toplevel(struct bgwl_server *server);
/* Redraw the border/opacity for the focus ring; cheap, called on focus. */
void bgwl_toplevel_update_appearance(struct bgwl_toplevel *toplevel);
/* Set opacity of every buffer under a node (fade animations). */
void bgwl_scene_set_opacity(struct wlr_scene_node *node, float opacity);

/* --- workspace.c --- */
void bgwl_workspaces_init(struct bgwl_server *server);
void bgwl_workspaces_finish(struct bgwl_server *server);
struct bgwl_workspace *bgwl_workspace_by_index(struct bgwl_server *server,
	int index);
const char *bgwl_layout_name(enum bgwl_layout layout);
int bgwl_layout_from_name(const char *name);
/* Switching: hides the old tree, shows the new, reflows tiling, animates. */
void bgwl_workspace_switch(struct bgwl_server *server,
	struct bgwl_workspace *target, bool animate);
void bgwl_workspace_send_window(struct bgwl_toplevel *toplevel,
	struct bgwl_workspace *target);
/* Cycle focus within the active workspace, front-most first. */
void bgwl_workspace_cycle_focus(struct bgwl_workspace *ws, int direction);
/* Sets a workspace's layout and re-tiles its windows in one step, so a layout
 * switch and a new mapping cannot drift apart. */
void bgwl_workspace_set_layout(struct bgwl_workspace *ws,
	enum bgwl_layout layout);
/* The workspace whose tree contains a toplevel, or the active one. */
struct bgwl_toplevel *bgwl_workspace_first_toplevel(struct bgwl_workspace *ws);

/* --- tiling.c --- */
/* Recompute every tiled window's geometry for its workspace. Called after
 * focus/size/mapping changes; it does nothing for a floating workspace. */
void bgwl_reflow(struct bgwl_server *server);
void bgwl_reflow_workspace(struct bgwl_workspace *ws);
/* Grows/shrinks the master area; direction is +1 or -1, axis picks the edge. */
void bgwl_tiling_adjust_master(struct bgwl_workspace *ws, int direction);
void bgwl_tiling_rotate_stack(struct bgwl_workspace *ws, int direction);

/* --- input.c --- */
void bgwl_keyboard_switch_layout(struct bgwl_keyboard *keyboard);
/* The keybinding dispatch table; returns true when the press was consumed. */
bool bgwl_handle_binding(struct bgwl_server *server, uint32_t modifiers,
	uint32_t keycode, bool pressed);

/* --- xdg.c --- */
struct bgwl_popup *bgwl_popup_create(struct wlr_xdg_popup *xdg_popup,
	struct wlr_scene_tree *parent_tree);

/* --- layer.c --- */
void bgwl_panel_toggle(struct bgwl_server *server);
/* Recomputes output->usable from every layer surface's exclusive zone and
 * reflows tiling. Called on map, unmap and configure commits. */
void bgwl_layer_update_usable_areas(struct bgwl_server *server);

/* --- protocols.c --- */
bool bgwl_protocols_create(struct bgwl_server *server);

/* --- ctl.c --- */
struct bgwl_ctl *bgwl_ctl_create(struct bgwl_server *server);
void bgwl_ctl_destroy(struct bgwl_ctl *ctl);
/* Called by every subsystem whose state the panel shows. */
void bgwl_ctl_broadcast(struct bgwl_server *server, const char *event);
/* One command line from a connected client; defined in ctl.c, whose client
 * struct stays private to that file. */
void bgwl_ctl_handle_command(struct bgwl_server *server,
	struct bgwl_ctl_client *client, const char *line);

/* --- session.c --- */
struct bgwl_session *bgwl_session_create(struct bgwl_server *server);
void bgwl_session_destroy(struct bgwl_session *session);
/* Called on every seat activity so idle-notify can count from a real event. */
void bgwl_session_activity(struct bgwl_server *server);
/* Laptop lid and other switch devices; input.c forwards them here. */
void bgwl_session_add_switch(struct bgwl_server *server,
	struct wlr_input_device *device);

/* --- anim.c --- */
void bgwl_anim_init(struct bgwl_server *server);
void bgwl_anim_finish(struct bgwl_server *server);
/* Workspace slide: moves the two workspace trees in opposite directions. */
void bgwl_anim_workspace_slide(struct bgwl_server *server,
	struct bgwl_workspace *from, struct bgwl_workspace *to, int direction);

/* --- xwayland.c --- */
struct bgwl_xwm *bgwl_xwayland_create(struct bgwl_server *server);
void bgwl_xwayland_destroy(struct bgwl_xwm *xwm);

/* --- output.c --- */
void bgwl_output_update_background(struct bgwl_output *output);
void bgwl_output_apply_configured(struct bgwl_output *output);
/* Commits a mode, or the configured defaults when requested is NULL. */
bool bgwl_output_apply_mode(struct bgwl_output *output,
	const struct wlr_output_state *requested);
void bgwl_output_set_dpms(struct bgwl_output *output, bool on);
struct bgwl_output *bgwl_output_neighbour(struct bgwl_server *server,
	struct bgwl_output *current, int direction);
/* Re-publishes the layout to output-management clients; call after any change. */
void bgwl_output_publish_configuration(struct bgwl_server *server);
void bgwl_output_management_install(struct bgwl_server *server);

#endif /* BGWL_H */
