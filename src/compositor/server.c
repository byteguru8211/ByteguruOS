/*
 * bgwl server lifecycle: CLI, backend auto-detection with a headless
 * fallback, global registration, signal handling, shutdown.
 *
 * Structure follows wlroots' bundled tinywl example (examples/tinywl in
 * 0.19.x) — same split of responsibilities, but with the compositor-specific
 * state moved to the other translation units. See README.md.
 */
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/backend/drm.h>
#include <wlr/backend/headless.h>
#include <wlr/backend/multi.h>
#include <wlr/render/allocator.h>
#include <wlr/render/pixman.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_subcompositor.h>
#include <wlr/util/log.h>
#include <wlr/version.h>

#include "bgwl.h"
#include "bgwl_config.h"

/* The layer-shell global is bound at version 4: enough for the ByteguruOS
 * panel (exclusive zones, anchors, keyboard interactivity). wlroots 0.18
 * allows up to ZWLR_LAYER_SHELL_V1_VERSION (5); 4 is what gtk4-layer-shell
 * actually drives, and this is the version the panel and the lock screen run
 * against in a live session. */
#define BGWL_LAYER_SHELL_VERSION 4
/* xdg_wm_base version 3, matching tinywl: everything newer is optional for
 * the clients bgwl is expected to run in v0. */
#define BGWL_XDG_SHELL_VERSION 3

static void usage(const char *argv0) {
	printf("Usage: %s [options]\n"
		"  -c, --command CMD     run CMD once the compositor is up\n"
		"  -b, --backend NAME    auto (default) or headless\n"
		"  -v, --version         print version and exit\n"
		"  -h, --help            show this help and exit\n"
		"\n"
		"\n"
		"Configuration: ~/.config/byteguru/bgwl.conf, then BGWL_* variables,\n"
		"then these options. See src/compositor/README.md for every key.\n"
		"\n"
		"Control socket: $XDG_RUNTIME_DIR/byteguru/compositor.sock (bgctl).\n",
		argv0);
}

/* Hand-rolled parser: only exact long options and single-character short
 * options, no getopt, no dependencies. */
static bool parse_args(int argc, char **argv, struct bgwl_config *cfg) {
	for (int i = 1; i < argc; i++) {
		const char *arg = argv[i];

		if (strcmp(arg, "-c") == 0 || strcmp(arg, "--command") == 0) {
			if (i + 1 >= argc) {
				fprintf(stderr, "%s: %s requires an argument\n",
					argv[0], arg);
				return false;
			}
			free(cfg->startup_cmd);
			cfg->startup_cmd = strdup(argv[++i]);
			if (cfg->startup_cmd == NULL) {
				return false;
			}
		} else if (strcmp(arg, "-b") == 0 || strcmp(arg, "--backend") == 0) {
			if (i + 1 >= argc) {
				fprintf(stderr, "%s: %s requires an argument\n",
					argv[0], arg);
				return false;
			}
			free(cfg->backend_hint);
			cfg->backend_hint = strdup(argv[++i]);
			if (cfg->backend_hint == NULL) {
				return false;
			}
		} else if (arg[0] == '-' && arg[1] != '\0') {
			fprintf(stderr, "%s: unknown option '%s'\n", argv[0], arg);
			usage(argv[0]);
			return false;
		} else {
			fprintf(stderr, "%s: unexpected positional argument '%s'\n",
				argv[0], arg);
			return false;
		}
	}

	if (cfg->backend_hint != NULL &&
			strcmp(cfg->backend_hint, "headless") != 0 &&
			strcmp(cfg->backend_hint, "auto") != 0) {
		fprintf(stderr, "%s: --backend must be 'auto' or 'headless'\n",
			argv[0]);
		return false;
	}
	return true;
}

/* -h and -v are answered before a single wlroots object exists, so querying
 * the version works on a machine with no DRM device, no input, no session. */
static void handle_info_args(int argc, char **argv, const char *argv0) {
	for (int i = 1; i < argc; i++) {
		const char *arg = argv[i];
		if (strcmp(arg, "-h") == 0 || strcmp(arg, "--help") == 0) {
			usage(argv0);
			exit(0);
		}
		if (strcmp(arg, "-v") == 0 || strcmp(arg, "--version") == 0) {
			printf("bgwl %s (wlroots %s)\n", BGWL_VERSION, WLR_VERSION_STR);
			exit(0);
		}
	}
}

/* --- backend selection -------------------------------------------------- */

struct bgwl_backend_probe {
	bool has_drm;
	bool has_headless;
};

static void probe_backend(struct wlr_backend *backend, void *data) {
	struct bgwl_backend_probe *probe = data;
	if (wlr_backend_is_drm(backend)) {
		probe->has_drm = true;
	}
	if (wlr_backend_is_headless(backend)) {
		probe->has_headless = true;
	}
}

static struct bgwl_backend_probe probe_backends(struct wlr_backend *backend) {
	struct bgwl_backend_probe probe = { .has_drm = false, .has_headless = false };
	if (wlr_backend_is_drm(backend)) {
		probe.has_drm = true;
		return probe;
	}
	if (wlr_backend_is_headless(backend)) {
		probe.has_headless = true;
		return probe;
	}
	if (wlr_backend_is_multi(backend)) {
		wlr_multi_for_each_backend(backend, probe_backend, &probe);
	}
	return probe;
}

/* Headless backend wrapped in a multi-backend, with one output so that a
 * compositor run in CI or over SSH still has something to render to. */
static struct wlr_backend *create_headless_fallback(struct bgwl_server *server,
		struct wl_event_loop *loop) {
	struct wlr_backend *multi = wlr_multi_backend_create(loop);
	if (multi == NULL) {
		wlr_log(WLR_ERROR, "wlr_multi_backend_create failed");
		return NULL;
	}

	struct wlr_backend *headless = wlr_headless_backend_create(loop);
	if (headless == NULL) {
		wlr_log(WLR_ERROR, "wlr_headless_backend_create failed");
		wlr_backend_destroy(multi);
		return NULL;
	}

	if (!wlr_multi_backend_add(multi, headless)) {
		wlr_log(WLR_ERROR, "failed to add headless backend to multi-backend");
		wlr_backend_destroy(headless);
		wlr_backend_destroy(multi);
		return NULL;
	}

	if (wlr_headless_add_output(headless, (unsigned int)server->cfg.headless_width,
			(unsigned int)server->cfg.headless_height) == NULL) {
		wlr_log(WLR_ERROR, "wlr_headless_add_output failed");
		wlr_backend_destroy(multi);
		return NULL;
	}

	wlr_log(WLR_INFO, "headless fallback output %dx%d",
		server->cfg.headless_width, server->cfg.headless_height);
	return multi;
}

static struct wlr_backend *select_backend(struct bgwl_server *server,
		struct wl_event_loop *loop) {
	if (server->cfg.backend_hint != NULL &&
			strcmp(server->cfg.backend_hint, "headless") == 0) {
		wlr_log(WLR_INFO, "--backend headless requested");
		return create_headless_fallback(server, loop);
	}

	/* Autocreate picks DRM+libinput on real hardware, and a nested Wayland or
	 * X11 window when one is available. It returns NULL when nothing works —
	 * that is the case we cover with the headless fallback. */
	struct wlr_backend *backend = wlr_backend_autocreate(loop, NULL);
	if (backend == NULL) {
		wlr_log(WLR_INFO, "no hardware backend available, falling back to headless");
		return create_headless_fallback(server, loop);
	}

	/* WLR_BACKENDS=headless уже дал нам headless-бэкенд с выводом. Второй
	 * вывод отсюда — это «рабочий стол на два экрана» в CI и на скриншотах,
	 * поэтому добавляем запасной вывод только когда нет ни DRM, ни headless. */
	struct bgwl_backend_probe found = probe_backends(backend);
	if (!found.has_drm && !found.has_headless) {
		wlr_log(WLR_INFO,
			"no DRM or headless backend available; adding a headless "
			"output so the session is still usable");
		struct wlr_backend *headless = wlr_headless_backend_create(loop);
		if (headless != NULL) {
			if (wlr_multi_backend_add(backend, headless)) {
				if (wlr_headless_add_output(headless,
						(unsigned int)server->cfg.headless_width,
						(unsigned int)server->cfg.headless_height) == NULL) {
					wlr_log(WLR_ERROR, "wlr_headless_add_output failed");
				}
			} else {
				wlr_log(WLR_ERROR, "wlr_multi_backend_add failed");
				wlr_backend_destroy(headless);
			}
		} else {
			wlr_log(WLR_ERROR, "wlr_headless_backend_create failed");
		}
	}

	return backend;
}

/* --- signals ------------------------------------------------------------ */

static int handle_terminate(int signal_number, void *data) {
	struct bgwl_server *server = data;
	wlr_log(WLR_INFO, "received signal %d, shutting down", signal_number);
	wl_display_terminate(server->wl_display);
	return 0;
}

static bool add_signal_handler(struct bgwl_server *server, int signal_number,
		struct wl_event_source **source_out) {
	/* wl_event_loop_add_signal() gives back a NULL only when the loop is
	 * gone; a session without signal handlers would outlive Ctrl+C silently,
	 * so this is treated as fatal. */
	struct wl_event_source *source = wl_event_loop_add_signal(server->event_loop,
		signal_number, handle_terminate, server);
	if (source == NULL) {
		wlr_log(WLR_ERROR, "failed to watch signal %d", signal_number);
		return false;
	}
	*source_out = source;
	return true;
}

/* --- assembly ----------------------------------------------------------- */

static void create_scene_trees(struct bgwl_server *server) {
	/* Creation order is stacking order, so build bottom-up. */
	server->trees[BGWL_TREE_BG] = wlr_scene_tree_create(&server->scene->tree);
	server->trees[BGWL_TREE_BACKGROUND] =
		wlr_scene_tree_create(&server->scene->tree);
	server->trees[BGWL_TREE_BOTTOM] =
		wlr_scene_tree_create(&server->scene->tree);
	server->trees[BGWL_TREE_NORMAL] =
		wlr_scene_tree_create(&server->scene->tree);
	server->trees[BGWL_TREE_TOP] = wlr_scene_tree_create(&server->scene->tree);
	server->trees[BGWL_TREE_OVERLAY] =
		wlr_scene_tree_create(&server->scene->tree);

	for (int i = 0; i < BGWL_TREE_COUNT; i++) {
		if (server->trees[i] == NULL) {
			wlr_log(WLR_ERROR, "failed to create scene tree %d", i);
			abort();
		}
	}
}

static void init_server_listeners(struct bgwl_server *server) {
	/* Every listener is self-linked before use so that the cleanup path in
	 * bgwl_server_finish() can call wl_list_remove() unconditionally: a
	 * zero-initialised wl_list would crash on removal, and a partially built
	 * server is exactly the state we shut down after a failed start. */
	struct wl_listener *listeners[] = {
		&server->new_xdg_toplevel, &server->new_xdg_popup,
		&server->new_layer_surface,
		&server->cursor_motion, &server->cursor_motion_absolute,
		&server->cursor_button, &server->cursor_axis, &server->cursor_frame,
		&server->new_input, &server->new_output,
		&server->request_cursor, &server->request_set_selection,
		&server->new_decoration, &server->output_manager_apply,
		&server->output_manager_test,
	};
	for (size_t i = 0; i < sizeof(listeners) / sizeof(listeners[0]); i++) {
		wl_list_init(&listeners[i]->link);
	}
}

void bgwl_server_init(struct bgwl_server *server) {
	memset(server, 0, sizeof(*server));
	server->wl_display = wl_display_create();
	if (server->wl_display == NULL) {
		wlr_log(WLR_ERROR, "wl_display_create failed");
		exit(1);
	}
	server->event_loop = wl_display_get_event_loop(server->wl_display);
	server->cursor_mode = BGWL_CURSOR_PASSTHROUGH;
	server->panel_visible = true;

	wl_list_init(&server->toplevels);
	wl_list_init(&server->outputs);
	wl_list_init(&server->keyboards);
	wl_list_init(&server->layer_surfaces);
	wl_list_init(&server->workspaces);
	server->next_window_id = 1;

	init_server_listeners(server);
}

/* Renderer, in the order of "what this machine can actually give us".
 *
 * A KMS device with no Mesa driver of its own (VirtualBox, Hyper-V, a GPU the
 * kernel drives but Mesa does not) fails before wlroots picks a renderer: GBM
 * cannot create a screen, EGL returns EGL_NOT_INITIALIZED, and there is
 * nothing to draw with. Mesa can be told to use its software KMS driver by
 * name; where even that is unavailable, pixman needs no GL at all.
 *
 * Each step costs frames per second, never the desktop — and "no desktop" is
 * exactly what a black text console looked like.
 */
static struct wlr_renderer *create_renderer(struct bgwl_server *server) {
	struct wlr_renderer *renderer = wlr_renderer_autocreate(server->backend);
	if (renderer != NULL) {
		return renderer;
	}

	if (setenv("MESA_LOADER_DRIVER_OVERRIDE", "kms_swrast", 1) == 0) {
		wlr_log(WLR_INFO, "no GL renderer: retrying with Mesa's software "
			"KMS driver (kms_swrast)");
		renderer = wlr_renderer_autocreate(server->backend);
		if (renderer != NULL) {
			return renderer;
		}
	}

	wlr_log(WLR_INFO, "no Mesa renderer: falling back to pixman (CPU rendering)");
	return wlr_pixman_renderer_create();
}

bool bgwl_server_start(struct bgwl_server *server) {
	server->backend = select_backend(server, server->event_loop);
	if (server->backend == NULL) {
		wlr_log(WLR_ERROR, "failed to create a backend");
		return false;
	}

	server->renderer = create_renderer(server);
	if (server->renderer == NULL) {
		wlr_log(WLR_ERROR, "failed to create wlr_renderer (set WLR_RENDERER?)");
		return false;
	}
	/* This is what advertises wl_shm: wlr_renderer_init_wl_display() calls
	 * wlr_shm_create_with_renderer() internally, so wl_shm must NOT be
	 * created again by us or clients would see two globals. */
	if (!wlr_renderer_init_wl_display(server->renderer, server->wl_display)) {
		wlr_log(WLR_ERROR, "wlr_renderer_init_wl_display failed");
		return false;
	}

	server->allocator = wlr_allocator_autocreate(server->backend,
		server->renderer);
	if (server->allocator == NULL) {
		wlr_log(WLR_ERROR, "failed to create wlr_allocator");
		return false;
	}

	/* wlr_renderer_init_wl_display() is what advertises wl_shm (through
	 * wlr_shm_create_with_renderer()), so there is deliberately no second
	 * shm global here — two globals for one interface split clients' buffer
	 * allocation across them. */
	wlr_compositor_create(server->wl_display, 5, server->renderer);
	wlr_subcompositor_create(server->wl_display);
	wlr_data_device_manager_create(server->wl_display);

	server->output_layout = wlr_output_layout_create(server->wl_display);
	if (server->output_layout == NULL) {
		wlr_log(WLR_ERROR, "wlr_output_layout_create failed");
		return false;
	}

	server->scene = wlr_scene_create();
	if (server->scene == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_create failed");
		return false;
	}
	create_scene_trees(server);
	server->scene_layout = wlr_scene_attach_output_layout(server->scene,
		server->output_layout);
	if (server->scene_layout == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_attach_output_layout failed");
		return false;
	}

	server->xdg_shell = wlr_xdg_shell_create(server->wl_display,
		BGWL_XDG_SHELL_VERSION);
	if (server->xdg_shell == NULL) {
		wlr_log(WLR_ERROR, "wlr_xdg_shell_create failed");
		return false;
	}
	server->new_xdg_toplevel.notify = bgwl_new_xdg_toplevel;
	wl_signal_add(&server->xdg_shell->events.new_toplevel,
		&server->new_xdg_toplevel);
	server->new_xdg_popup.notify = bgwl_new_xdg_popup;
	wl_signal_add(&server->xdg_shell->events.new_popup, &server->new_xdg_popup);

	server->layer_shell = wlr_layer_shell_v1_create(server->wl_display,
		BGWL_LAYER_SHELL_VERSION);
	if (server->layer_shell == NULL) {
		wlr_log(WLR_ERROR, "wlr_layer_shell_v1_create failed");
		return false;
	}
	server->new_layer_surface.notify = bgwl_new_layer_surface;
	wl_signal_add(&server->layer_shell->events.new_surface,
		&server->new_layer_surface);

	server->cursor = wlr_cursor_create();
	if (server->cursor == NULL) {
		wlr_log(WLR_ERROR, "wlr_cursor_create failed");
		return false;
	}
	wlr_cursor_attach_output_layout(server->cursor, server->output_layout);

	/* Theme name NULL means "whatever the XDG default cursor is"; passing
	 * the configured name keeps bgwl and the GTK shell on the same cursor. */
	server->cursor_mgr = wlr_xcursor_manager_create(
		server->cfg.cursor_theme, (uint32_t)server->cfg.cursor_size);
	if (server->cursor_mgr == NULL) {
		wlr_log(WLR_ERROR, "wlr_xcursor_manager_create failed for theme '%s' "
			"(is it installed?)", server->cfg.cursor_theme);
		return false;
	}

	server->cursor_motion.notify = bgwl_cursor_motion;
	wl_signal_add(&server->cursor->events.motion, &server->cursor_motion);
	server->cursor_motion_absolute.notify = bgwl_cursor_motion_absolute;
	wl_signal_add(&server->cursor->events.motion_absolute,
		&server->cursor_motion_absolute);
	server->cursor_button.notify = bgwl_cursor_button;
	wl_signal_add(&server->cursor->events.button, &server->cursor_button);
	server->cursor_axis.notify = bgwl_cursor_axis;
	wl_signal_add(&server->cursor->events.axis, &server->cursor_axis);
	server->cursor_frame.notify = bgwl_cursor_frame;
	wl_signal_add(&server->cursor->events.frame, &server->cursor_frame);

	server->new_input.notify = bgwl_new_input;
	wl_signal_add(&server->backend->events.new_input, &server->new_input);
	server->new_output.notify = bgwl_new_output;
	wl_signal_add(&server->backend->events.new_output, &server->new_output);

	server->seat = wlr_seat_create(server->wl_display, "seat0");
	if (server->seat == NULL) {
		wlr_log(WLR_ERROR, "wlr_seat_create failed");
		return false;
	}
	server->request_cursor.notify = bgwl_seat_request_cursor;
	wl_signal_add(&server->seat->events.request_set_cursor,
		&server->request_cursor);
	server->request_set_selection.notify = bgwl_seat_request_set_selection;
	wl_signal_add(&server->seat->events.request_set_selection,
		&server->request_set_selection);

	/* Protocol globals: everything the portal, the clipboard manager and a
	 * sandboxed Flatpak need in order to start at all. */
	if (!bgwl_protocols_create(server)) {
		wlr_log(WLR_ERROR, "protocol globals could not be registered");
		return false;
	}

	/* Streams are the last structural piece: a toplevel needs a workspace
	 * tree to live in, so this must run before any client can connect. */
	bgwl_workspaces_init(server);
	bgwl_anim_init(server);

	/* Output-management is advertised by protocols.c; the apply/test handling
	 * and the first configuration publish happen here, once the layout has
	 * outputs in it. */
	bgwl_output_management_install(server);

	server->ctl = bgwl_ctl_create(server);
	if (server->ctl == NULL) {
		/* Not fatal: the desktop still works, the shell simply cannot see
		 * window state. Say so loudly, because it looks like a bug from the
		 * user's side. */
		wlr_log(WLR_ERROR, "control socket unavailable: bgpanel will not "
			"show windows (check XDG_RUNTIME_DIR)");
	}

	server->session = bgwl_session_create(server);
	if (server->session == NULL) {
		wlr_log(WLR_ERROR, "session power/lock subsystem could not start");
		return false;
	}

	if (!add_signal_handler(server, SIGTERM, &server->sigterm_source) ||
			!add_signal_handler(server, SIGINT, &server->sigint_source)) {
		return false;
	}
	/* Spawned clients are fire-and-forget; ignoring SIGCHLD keeps the compositor
	 * from accumulating zombies without a waitpid loop. */
	signal(SIGCHLD, SIG_IGN);

	const char *socket = wl_display_add_socket_auto(server->wl_display);
	if (socket == NULL) {
		wlr_log(WLR_ERROR, "wl_display_add_socket_auto failed");
		return false;
	}

	if (!wlr_backend_start(server->backend)) {
		wlr_log(WLR_ERROR, "wlr_backend_start failed");
		return false;
	}

	setenv("WAYLAND_DISPLAY", socket, true);
	/* The shell finds the compositor's control socket through this, so a
	 * non-default XDG_RUNTIME_DIR does not break the panel. */
	const char *runtime = bgwl_runtime_dir();
	if (runtime != NULL) {
		char ctl_path[512];
		snprintf(ctl_path, sizeof(ctl_path), "%s/compositor.sock", runtime);
		setenv("BGWL_CONTROL_SOCKET", ctl_path, true);
	}
	setenv("BGWL_SESSION_TYPE", "wayland", true);
	setenv("XDG_SESSION_TYPE", "wayland", true);
	setenv("XDG_CURRENT_DESKTOP", "ByteguruOS", true);

	if (server->cfg.startup_cmd != NULL) {
		bgwl_spawn(server, server->cfg.startup_cmd);
	}

	wlr_log(WLR_INFO, "bgwl %s running on WAYLAND_DISPLAY=%s", BGWL_VERSION,
		socket);
	return true;
}

void bgwl_server_terminate(struct bgwl_server *server) {
	wl_display_terminate(server->wl_display);
}

struct bgwl_output *bgwl_server_first_output(struct bgwl_server *server) {
	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		return output;
	}
	return NULL;
}

/* Union of every output's usable area. Used by the slide animation for its
 * travel distance and by floating placement for centring. */
struct wlr_box bgwl_server_usable_box(struct bgwl_server *server) {
	struct wlr_box union_box = { 0, 0, 0, 0 };
	bool any = false;
	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		struct wlr_box box = output->usable;
		if (box.width <= 0 || box.height <= 0) {
			continue;
		}
		if (!any) {
			union_box = box;
			any = true;
			continue;
		}
		int right = bgwl_max(union_box.x + union_box.width, box.x + box.width);
		int bottom = bgwl_max(union_box.y + union_box.height, box.y + box.height);
		union_box.x = bgwl_min(union_box.x, box.x);
		union_box.y = bgwl_min(union_box.y, box.y);
		union_box.width = right - union_box.x;
		union_box.height = bottom - union_box.y;
	}
	if (any) {
		return union_box;
	}

	/* No outputs yet: report the headless geometry so a window mapped before
	 * the first mode-set still lands somewhere visible. */
	struct wlr_box fallback = { 0, 0, server->cfg.headless_width,
		server->cfg.headless_height };
	return fallback;
}

/* Warps the cursor to the centre of the layout once outputs exist; called
 * from output.c on the first hotplug. */
void bgwl_server_warp_cursor_to_center(struct bgwl_server *server) {
	struct wlr_box box;
	wlr_output_layout_get_box(server->output_layout, NULL, &box);
	if (box.width <= 0 || box.height <= 0) {
		return;
	}
	if (!wlr_cursor_warp(server->cursor, NULL, box.x + box.width / 2.0,
			box.y + box.height / 2.0)) {
		wlr_log(WLR_DEBUG, "cursor warp to layout centre failed");
	}
}

void bgwl_server_finish(struct bgwl_server *server) {
	if (server->wl_display == NULL) {
		return;
	}

	wl_display_destroy_clients(server->wl_display);

	if (server->sigterm_source != NULL) {
		wl_event_source_remove(server->sigterm_source);
		server->sigterm_source = NULL;
	}
	if (server->sigint_source != NULL) {
		wl_event_source_remove(server->sigint_source);
		server->sigint_source = NULL;
	}

	wl_list_remove(&server->new_xdg_toplevel.link);
	wl_list_remove(&server->new_xdg_popup.link);
	wl_list_remove(&server->new_layer_surface.link);
	wl_list_remove(&server->cursor_motion.link);
	wl_list_remove(&server->cursor_motion_absolute.link);
	wl_list_remove(&server->cursor_button.link);
	wl_list_remove(&server->cursor_axis.link);
	wl_list_remove(&server->cursor_frame.link);
	wl_list_remove(&server->new_input.link);
	wl_list_remove(&server->new_output.link);
	wl_list_remove(&server->request_cursor.link);
	wl_list_remove(&server->request_set_selection.link);
	wl_list_remove(&server->new_decoration.link);
	if (server->output_manager != NULL) {
		wl_list_remove(&server->output_manager_apply.link);
		wl_list_remove(&server->output_manager_test.link);
	}

	if (server->ctl != NULL) {
		bgwl_ctl_destroy(server->ctl);
		server->ctl = NULL;
	}
	if (server->session != NULL) {
		bgwl_session_destroy(server->session);
		server->session = NULL;
	}
	bgwl_anim_finish(server);

	/* Windows hold references into the workspace trees, so the toplevel list
	 * must be emptied before the trees go away. */
	bgwl_workspaces_finish(server);

	/* Backend first: destroying it fires the output and input destroy events
	 * our handlers use to drop scene nodes and free per-device state, and at
	 * that point the scene graph is still alive. */
	if (server->backend != NULL) {
		wlr_backend_destroy(server->backend);
		server->backend = NULL;
	}

	if (server->scene != NULL) {
		wlr_scene_node_destroy(&server->scene->tree.node);
		server->scene = NULL;
	}
	if (server->cursor_mgr != NULL) {
		wlr_xcursor_manager_destroy(server->cursor_mgr);
		server->cursor_mgr = NULL;
	}
	if (server->cursor != NULL) {
		wlr_cursor_destroy(server->cursor);
		server->cursor = NULL;
	}
	if (server->allocator != NULL) {
		wlr_allocator_destroy(server->allocator);
		server->allocator = NULL;
	}
	if (server->renderer != NULL) {
		wlr_renderer_destroy(server->renderer);
		server->renderer = NULL;
	}
	if (server->output_layout != NULL) {
		wlr_output_layout_destroy(server->output_layout);
		server->output_layout = NULL;
	}

	wl_display_destroy(server->wl_display);
	server->wl_display = NULL;

	bgwl_config_finish(&server->cfg);
}

int main(int argc, char **argv) {
	wlr_log_init(WLR_DEBUG, NULL);

	handle_info_args(argc, argv, argv[0]);

	struct bgwl_server server = {0};
	/* bgwl_server_init() zeroes the struct, so the configuration is loaded
	 * into it afterwards. */
	bgwl_server_init(&server);
	bgwl_config_load(&server.cfg);

	if (!parse_args(argc, argv, &server.cfg)) {
		bgwl_server_finish(&server);
		return EXIT_FAILURE;
	}

	if (!bgwl_server_start(&server)) {
		bgwl_server_finish(&server);
		return EXIT_FAILURE;
	}

	wl_display_run(server.wl_display);
	bgwl_server_finish(&server);
	return EXIT_SUCCESS;
}
