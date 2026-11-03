/*
 * Toplevel views: server-side titlebar, stacking, focus, hit testing and the
 * interactive move/resize grab.
 *
 * Decoration choice: bgwl draws its own titlebar (SSD) with cairo into a
 * struct wlr_buffer and shows it as a wlr_scene_buffer. CSD-only would be less
 * code, but ByteguruOS wants a uniform window frame in v0, and wlroots 0.19
 * has no helper that draws text into the scene (no wlr_scene_text), so a cairo
 * buffer is the only real option — see examples/cairo-buffer.c upstream.
 * No buttons and no resize borders: the bar is a drag handle and a label.
 */
#include <stdlib.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "bgwl.h"

static void bgwl_toplevel_redraw_decor(struct bgwl_toplevel *toplevel);

/* Layout-space rectangle occupied by our titlebar. */
static void bgwl_toplevel_decor_box(struct bgwl_toplevel *toplevel,
		struct wlr_box *box) {
	struct wlr_scene_node *node = &toplevel->scene_tree->node;
	box->x = node->x;
	box->y = node->y;
	box->width = toplevel->decor_width;
	box->height = BGWL_DECOR_HEIGHT;
}

void bgwl_toplevel_reset_cursor_mode(struct bgwl_server *server) {
	server->cursor_mode = BGWL_CURSOR_PASSTHROUGH;
	server->grabbed_toplevel = NULL;
}

void bgwl_toplevel_focus(struct bgwl_toplevel *toplevel) {
	if (toplevel == NULL) {
		return;
	}
	struct bgwl_server *server = toplevel->server;
	struct wlr_seat *seat = server->seat;
	struct wlr_surface *prev_surface = seat->keyboard_state.focused_surface;
	struct wlr_surface *surface = toplevel->xdg_toplevel->base->surface;

	if (prev_surface == surface) {
		return;
	}
	if (prev_surface != NULL) {
		struct wlr_xdg_toplevel *prev =
			wlr_xdg_toplevel_try_from_wlr_surface(prev_surface);
		if (prev != NULL) {
			wlr_xdg_toplevel_set_activated(prev, false);
		}
	}

	struct wlr_keyboard *keyboard = wlr_seat_get_keyboard(seat);

	/* Keep stacking order and focus order in sync: our list is front-most
	 * first, matching the scene order after raise_to_top. */
	wlr_scene_node_raise_to_top(&toplevel->scene_tree->node);
	wl_list_remove(&toplevel->link);
	wl_list_insert(&server->toplevels, &toplevel->link);

	wlr_xdg_toplevel_set_activated(toplevel->xdg_toplevel, true);

	if (keyboard != NULL) {
		wlr_seat_keyboard_notify_enter(seat, surface, keyboard->keycodes,
			keyboard->num_keycodes, &keyboard->modifiers);
	}
}

struct wlr_surface *bgwl_surface_at(struct bgwl_server *server, double lx,
		double ly, double *sx_out, double *sy_out) {
	double dummy_sx, dummy_sy;
	double *sx = sx_out != NULL ? sx_out : &dummy_sx;
	double *sy = sy_out != NULL ? sy_out : &dummy_sy;

	/* Top-most node at these layout coordinates, whatever layer it is in.
	 * Our titlebar buffers live in the scene too but have no
	 * wlr_scene_surface, so clicks landing on them resolve to NULL and the
	 * caller can treat them as compositor-owned. */
	struct wlr_scene_node *node = wlr_scene_node_at(&server->scene->tree.node,
		lx, ly, sx, sy);
	if (node == NULL || node->type != WLR_SCENE_NODE_BUFFER) {
		return NULL;
	}
	struct wlr_scene_buffer *scene_buffer = wlr_scene_buffer_from_node(node);
	struct wlr_scene_surface *scene_surface =
		wlr_scene_surface_try_from_buffer(scene_buffer);
	if (scene_surface == NULL) {
		return NULL;
	}
	return scene_surface->surface;
}

struct bgwl_toplevel *bgwl_toplevel_at(struct bgwl_server *server, double lx,
		double ly) {
	/* Our own titlebars sit on top of everything the toplevel owns, so they
	 * are tested first, in stacking order. */
	struct bgwl_toplevel *toplevel;
	wl_list_for_each(toplevel, &server->toplevels, link) {
		struct wlr_box box;
		bgwl_toplevel_decor_box(toplevel, &box);
		if (box.width > 0 && lx >= box.x && lx < box.x + box.width &&
				ly >= box.y && ly < box.y + box.height) {
			return toplevel;
		}
	}

	struct wlr_surface *surface = bgwl_surface_at(server, lx, ly, NULL, NULL);
	if (surface == NULL) {
		return NULL;
	}
	/* ВЕРИФИЦИРОВАТЬ: wlr_xdg_surface_try_from_wlr_surface() dereferences its
	 * argument without a NULL check in 0.19.3 (types/xdg_shell/
	 * wlr_xdg_surface.c reads surface->role), hence the guard above. */
	struct wlr_xdg_surface *xdg_surface =
		wlr_xdg_surface_try_from_wlr_surface(surface);
	if (xdg_surface == NULL ||
			xdg_surface->role != WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
		return NULL;
	}
	/* bgwl_toplevel_create() stores the wrapper tree in xdg_surface.data. */
	struct wlr_scene_tree *tree = xdg_surface->data;
	if (tree == NULL) {
		return NULL;
	}
	return tree->node.data;
}

void bgwl_toplevel_begin_interactive(struct bgwl_toplevel *toplevel,
		enum bgwl_cursor_mode mode, uint32_t edges) {
	struct bgwl_server *server = toplevel->server;
	struct wlr_scene_node *node = &toplevel->scene_tree->node;

	server->grabbed_toplevel = toplevel;
	server->cursor_mode = mode;

	if (mode == BGWL_CURSOR_MOVE) {
		server->grab_x = server->cursor->x - node->x;
		server->grab_y = server->cursor->y - node->y;
		return;
	}

	struct wlr_box *geo = &toplevel->xdg_toplevel->base->geometry;
	double border_x = (node->x + geo->x) +
		((edges & WLR_EDGE_RIGHT) ? geo->width : 0);
	double border_y = (node->y + geo->y - BGWL_DECOR_HEIGHT) +
		((edges & WLR_EDGE_BOTTOM) ? geo->height : 0);
	server->grab_x = server->cursor->x - border_x;
	server->grab_y = server->cursor->y - border_y;

	server->grab_geobox = *geo;
	server->grab_geobox.x += node->x;
	server->grab_geobox.y += node->y - BGWL_DECOR_HEIGHT;
	server->resize_edges = edges;
}

void bgwl_toplevel_process_motion(struct bgwl_server *server) {
	struct bgwl_toplevel *toplevel = server->grabbed_toplevel;
	struct wlr_scene_node *node = &toplevel->scene_tree->node;

	if (server->cursor_mode == BGWL_CURSOR_MOVE) {
		wlr_scene_node_set_position(node,
			server->cursor->x - server->grab_x,
			server->cursor->y - server->grab_y);
		return;
	}

	double border_x = server->cursor->x - server->grab_x;
	double border_y = server->cursor->y - server->grab_y;
	int left = server->grab_geobox.x;
	int right = server->grab_geobox.x + server->grab_geobox.width;
	int top = server->grab_geobox.y;
	int bottom = server->grab_geobox.y + server->grab_geobox.height;

	if (server->resize_edges & WLR_EDGE_TOP) {
		top = border_y;
		if (top >= bottom) {
			top = bottom - 1;
		}
	} else if (server->resize_edges & WLR_EDGE_BOTTOM) {
		bottom = border_y;
		if (bottom <= top) {
			bottom = top + 1;
		}
	}
	if (server->resize_edges & WLR_EDGE_LEFT) {
		left = border_x;
		if (left >= right) {
			left = right - 1;
		}
	} else if (server->resize_edges & WLR_EDGE_RIGHT) {
		right = border_x;
		if (right <= left) {
			right = left + 1;
		}
	}

	struct wlr_box *geo = &toplevel->xdg_toplevel->base->geometry;
	wlr_scene_node_set_position(node, left - geo->x,
		top + BGWL_DECOR_HEIGHT - geo->y);

	int new_width = right - left;
	int new_height = bottom - top - BGWL_DECOR_HEIGHT;
	if (new_width < 1) {
		new_width = 1;
	}
	if (new_height < 1) {
		new_height = 1;
	}
	wlr_xdg_toplevel_set_size(toplevel->xdg_toplevel, new_width, new_height);
}

static void bgwl_toplevel_redraw_decor(struct bgwl_toplevel *toplevel) {
	struct wlr_xdg_surface *base = toplevel->xdg_toplevel->base;
	int width = base->geometry.width;
	if (width <= 0) {
		/* Nothing committed yet: hide the bar rather than draw a zero-width
		 * cairo surface, which cairo refuses to create. */
		if (toplevel->decor_buffer != NULL) {
			wlr_scene_buffer_set_buffer(toplevel->decor_node, NULL);
			bgwl_buffer_drop(toplevel->decor_buffer);
			toplevel->decor_buffer = NULL;
			toplevel->decor_width = 0;
		}
		return;
	}

	if (toplevel->decor_buffer != NULL && toplevel->decor_width == width &&
			!toplevel->decor_dirty) {
		return;
	}

	struct bgwl_buffer *buffer = bgwl_draw_titlebar(toplevel->server, width,
		toplevel->xdg_toplevel->title);
	if (buffer == NULL) {
		wlr_log(WLR_ERROR, "failed to draw titlebar for toplevel");
		return;
	}

	/* ВЕРИФИЦИРОВАТЬ: wlr_scene_buffer_set_buffer() ref-counting: it is assumed to
	 * lock the new buffer and drop the one it held before. Check
	 * types/scene/wlr_scene.c:scene_buffer_set_buffer if frames leak. */
	/* The scene buffer locks the new buffer and releases the previous one, so
	 * dropping our own reference to the old buffer afterwards is safe. */
	wlr_scene_buffer_set_buffer(toplevel->decor_node, &buffer->base);
	if (toplevel->decor_buffer != NULL) {
		bgwl_buffer_drop(toplevel->decor_buffer);
	}
	toplevel->decor_buffer = buffer;
	toplevel->decor_width = width;
	toplevel->decor_dirty = false;
}

static void toplevel_handle_commit(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel = wl_container_of(listener, toplevel, commit);
	struct wlr_xdg_toplevel *xdg_toplevel = toplevel->xdg_toplevel;
	struct wlr_xdg_surface *base = xdg_toplevel->base;

	if (base->initial_commit) {
		/* A configure must be sent for the client to map. Size 0,0 lets the
		 * client choose its own initial size; the titlebar follows. */
		wlr_xdg_toplevel_set_size(xdg_toplevel, 0, 0);
		return;
	}

	/* Window geometry is relative to the surface origin; shift it so the
	 * client content begins exactly under the titlebar. */
	wlr_scene_node_set_position(&toplevel->xdg_tree->node, -base->geometry.x,
		BGWL_DECOR_HEIGHT - base->geometry.y);

	bgwl_toplevel_redraw_decor(toplevel);
}

static void toplevel_handle_map(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel = wl_container_of(listener, toplevel, map);

	/* Front of the list is the top-most window. */
	wl_list_insert(&toplevel->server->toplevels, &toplevel->link);

	/* Re-enable the bar: unmap disabled it, and the client may re-map this
	 * surface without recreating our scene nodes. */
	if (toplevel->decor_node != NULL) {
		wlr_scene_node_set_enabled(&toplevel->decor_node->node, true);
	}

	bgwl_toplevel_redraw_decor(toplevel);
	bgwl_toplevel_focus(toplevel);
}

static void toplevel_handle_unmap(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, unmap);

	if (toplevel == toplevel->server->grabbed_toplevel) {
		bgwl_toplevel_reset_cursor_mode(toplevel->server);
	}

	wl_list_remove(&toplevel->link);

	/* Hide the titlebar while unmapped; the scene tree itself stays alive so
	 * popups can still find their parent node. */
	if (toplevel->decor_node != NULL) {
		wlr_scene_node_set_enabled(&toplevel->decor_node->node, false);
	}
}

static void toplevel_handle_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, destroy);
	struct bgwl_server *server = toplevel->server;

	if (toplevel == server->grabbed_toplevel) {
		bgwl_toplevel_reset_cursor_mode(server);
	}

	if (toplevel->decor_buffer != NULL) {
		bgwl_buffer_drop(toplevel->decor_buffer);
		toplevel->decor_buffer = NULL;
	}

	wl_list_remove(&toplevel->map.link);
	wl_list_remove(&toplevel->unmap.link);
	wl_list_remove(&toplevel->commit.link);
	wl_list_remove(&toplevel->destroy.link);
	wl_list_remove(&toplevel->request_move.link);
	wl_list_remove(&toplevel->request_resize.link);
	wl_list_remove(&toplevel->request_maximize.link);
	wl_list_remove(&toplevel->request_fullscreen.link);
	wl_list_remove(&toplevel->set_title.link);

	/* Our wrapper tree (and with it the xdg surface tree and the titlebar
	 * node) is a child of the normal layer, so it must go explicitly: the
	 * scene helper for xdg surfaces does not own our tree. */
	if (toplevel->scene_tree != NULL) {
		wlr_scene_node_destroy(&toplevel->scene_tree->node);
	}

	free(toplevel);
}

static void toplevel_handle_set_title(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, set_title);
	toplevel->decor_dirty = true;
	if (toplevel->xdg_toplevel->base->surface->mapped) {
		bgwl_toplevel_redraw_decor(toplevel);
	}
}

static void toplevel_handle_request_move(struct wl_listener *listener,
		void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, request_move);
	bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_MOVE, 0);
}

static void toplevel_handle_request_resize(struct wl_listener *listener,
		void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, request_resize);
	struct wlr_xdg_toplevel_resize_event *event = data;
	bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_RESIZE, event->edges);
}

/* Maximise and fullscreen are deliberately not implemented in v0 (see
 * "Что НЕ реализовано" in README.md); xdg-shell still requires a configure in
 * reply to the request, so send an empty one. */
static void toplevel_handle_request_maximize(struct wl_listener *listener,
		void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, request_maximize);
	if (toplevel->xdg_toplevel->base->initialized) {
		wlr_xdg_surface_schedule_configure(toplevel->xdg_toplevel->base);
	}
}

static void toplevel_handle_request_fullscreen(struct wl_listener *listener,
		void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, request_fullscreen);
	if (toplevel->xdg_toplevel->base->initialized) {
		wlr_xdg_surface_schedule_configure(toplevel->xdg_toplevel->base);
	}
}

struct bgwl_toplevel *bgwl_toplevel_create(struct bgwl_server *server,
		struct wlr_xdg_toplevel *xdg_toplevel) {
	struct bgwl_toplevel *toplevel = calloc(1, sizeof(*toplevel));
	if (toplevel == NULL) {
		wlr_log(WLR_ERROR, "out of memory for toplevel");
		return NULL;
	}

	toplevel->server = server;
	toplevel->xdg_toplevel = xdg_toplevel;
	toplevel->decor_dirty = true;

	toplevel->scene_tree = wlr_scene_tree_create(server->trees[BGWL_TREE_NORMAL]);
	if (toplevel->scene_tree == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_tree_create failed");
		free(toplevel);
		return NULL;
	}
	toplevel->scene_tree->node.data = toplevel;
	/* Popups look up their parent's scene node through xdg_surface.data. */
	xdg_toplevel->base->data = toplevel->scene_tree;

	toplevel->decor_node = wlr_scene_buffer_create(toplevel->scene_tree, NULL);
	if (toplevel->decor_node == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_buffer_create failed");
		wlr_scene_node_destroy(&toplevel->scene_tree->node);
		free(toplevel);
		return NULL;
	}

	toplevel->xdg_tree = wlr_scene_xdg_surface_create(toplevel->scene_tree,
		xdg_toplevel->base);
	if (toplevel->xdg_tree == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_xdg_surface_create failed");
		wlr_scene_node_destroy(&toplevel->scene_tree->node);
		free(toplevel);
		return NULL;
	}
	toplevel->xdg_tree->node.data = toplevel;

	struct wlr_surface *surface = xdg_toplevel->base->surface;
	toplevel->map.notify = toplevel_handle_map;
	wl_signal_add(&surface->events.map, &toplevel->map);
	toplevel->unmap.notify = toplevel_handle_unmap;
	wl_signal_add(&surface->events.unmap, &toplevel->unmap);
	toplevel->commit.notify = toplevel_handle_commit;
	wl_signal_add(&surface->events.commit, &toplevel->commit);

	toplevel->destroy.notify = toplevel_handle_destroy;
	wl_signal_add(&xdg_toplevel->events.destroy, &toplevel->destroy);
	toplevel->set_title.notify = toplevel_handle_set_title;
	wl_signal_add(&xdg_toplevel->events.set_title, &toplevel->set_title);

	toplevel->request_move.notify = toplevel_handle_request_move;
	wl_signal_add(&xdg_toplevel->events.request_move, &toplevel->request_move);
	toplevel->request_resize.notify = toplevel_handle_request_resize;
	wl_signal_add(&xdg_toplevel->events.request_resize,
		&toplevel->request_resize);
	toplevel->request_maximize.notify = toplevel_handle_request_maximize;
	wl_signal_add(&xdg_toplevel->events.request_maximize,
		&toplevel->request_maximize);
	toplevel->request_fullscreen.notify = toplevel_handle_request_fullscreen;
	wl_signal_add(&xdg_toplevel->events.request_fullscreen,
		&toplevel->request_fullscreen);

	return toplevel;
}
