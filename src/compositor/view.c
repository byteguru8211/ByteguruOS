/*
 * Toplevel views: the window frame (titlebar, buttons, focus border), stacking,
 * focus, hit testing, workspace membership, and the interactive move/resize
 * grab.
 *
 * Decoration choice: bgwl draws its own titlebar (SSD) with cairo into a
 * struct wlr_buffer and shows it as a wlr_scene_buffer. Clients that ask for
 * client-side decorations (GTK4, Qt) get it hidden instead — see
 * bgwl_toplevel_set_decorated() and protocols.c. The height of the frame is
 * therefore a per-window property, decor_height(), and every geometry
 * calculation in this file goes through it; hard-coding BGWL_DECOR_HEIGHT
 * anywhere else would break CSD windows.
 *
 * Geometry convention, stated once because it is the easiest thing to get
 * wrong in this file:
 *
 *   scene_tree origin  == the frame's top-left, in layout coordinates
 *   xdg_tree origin    == frame top-left + (0, decor_height)
 *   get_geometry()     == the FRAME box (titlebar included)
 *   set_geometry()     == takes a FRAME box and configures content size
 *
 * Borders are one wlr_scene_rect created before anything else in the window's
 * tree, so it sits underneath and shows only around the edges. That costs one
 * quad per window instead of four, and it is why the focus ring is free.
 */
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "bgwl.h"

/* How far inside the frame the pointer still counts as "on the resize
 * border". Wider than the drawn border so a 1 px border is still grabbable. */
#define BGWL_RESIZE_GRAB_MARGIN 6

static void bgwl_toplevel_redraw_decor(struct bgwl_toplevel *toplevel);
static void bgwl_toplevel_update_border(struct bgwl_toplevel *toplevel);
static struct bgwl_workspace *active_or_first(struct bgwl_server *server);

static int decor_height(const struct bgwl_toplevel *toplevel) {
	return toplevel->decorated ? BGWL_DECOR_HEIGHT : 0;
}

/* wlroots 0.18 has no public wlr_xdg_surface.geometry field; the box has to be
 * asked for. Kept in one place so a wlroots bump touches one line. */
static struct wlr_box bgwl_xdg_geometry(struct wlr_xdg_surface *base) {
	struct wlr_box box;
	wlr_xdg_surface_get_geometry(base, &box);
	return box;
}

static struct wlr_surface *toplevel_surface(struct bgwl_toplevel *toplevel) {
	if (toplevel == NULL || toplevel->xdg_toplevel == NULL ||
			toplevel->xdg_toplevel->base == NULL) {
		return NULL;
	}
	return toplevel->xdg_toplevel->base->surface;
}

/* The output a window mostly covers; used for maximise, fullscreen and
 * tiling placement. */
static struct wlr_output *toplevel_output(struct bgwl_toplevel *toplevel) {
	struct bgwl_server *server = toplevel->server;
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	struct wlr_output *found = wlr_output_layout_output_at(server->output_layout,
		frame.x + frame.width / 2, frame.y + frame.height / 2);
	if (found != NULL) {
		return found;
	}
	struct bgwl_output *first = bgwl_server_first_output(server);
	return first != NULL ? first->wlr_output : NULL;
}

void bgwl_toplevel_get_geometry(struct bgwl_toplevel *toplevel,
		struct wlr_box *box) {
	struct wlr_scene_node *node = &toplevel->scene_tree->node;
	box->x = node->x;
	box->y = node->y;

	struct wlr_surface *surface = toplevel_surface(toplevel);
	if (surface == NULL || !toplevel->xdg_toplevel->base->initialized) {
		/* Before the first commit the client has not told us anything; report
		 * the size we asked for so callers can still place the window. */
		box->width = toplevel->pending_width > 0 ? toplevel->pending_width : 640;
		box->height = (toplevel->pending_height > 0
			? toplevel->pending_height : 480) + decor_height(toplevel);
		return;
	}

	struct wlr_box geo = bgwl_xdg_geometry(toplevel->xdg_toplevel->base);
	box->width = geo.width;
	box->height = geo.height + decor_height(toplevel);
}

/* Applies a frame box: positions the window and asks the client for the
 * content size that fits inside it. */
void bgwl_toplevel_set_geometry(struct bgwl_toplevel *toplevel,
		struct wlr_box box) {
	struct wlr_scene_node *node = &toplevel->scene_tree->node;
	int decor = decor_height(toplevel);

	int content_width = box.width;
	int content_height = box.height - decor;
	if (content_width < 1) {
		content_width = 1;
	}
	if (content_height < 1) {
		content_height = 1;
	}

	/* Respect the client's own size hints: a dialog that says "I am 400x300
	 * and no smaller" must not be stretched by a tiling pass. */
	struct wlr_xdg_toplevel_state *current = &toplevel->xdg_toplevel->current;
	if (current->min_width > 0 && content_width < current->min_width) {
		content_width = current->min_width;
	}
	if (current->min_height > 0 && content_height < current->min_height) {
		content_height = current->min_height;
	}
	if (current->max_width > 0 && content_width > current->max_width) {
		content_width = current->max_width;
	}
	if (current->max_height > 0 && content_height > current->max_height) {
		content_height = current->max_height;
	}

	toplevel->pending_width = content_width;
	toplevel->pending_height = content_height;

	wlr_scene_node_set_position(node, box.x, box.y);
	if (content_width != current->width || content_height != current->height) {
		wlr_xdg_toplevel_set_size(toplevel->xdg_toplevel, content_width,
			content_height);
	}
	bgwl_toplevel_update_border(toplevel);
}

void bgwl_toplevel_set_decorated(struct bgwl_toplevel *toplevel, bool value) {
	if (toplevel == NULL || toplevel->decorated == value) {
		return;
	}
	toplevel->decorated = value;
	wlr_scene_node_set_enabled(&toplevel->decor_node->node, value);
	/* The frame grew or shrank, so the content has to be re-requested. */
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	bgwl_toplevel_set_geometry(toplevel, frame);
	bgwl_toplevel_redraw_decor(toplevel);
}

void bgwl_scene_set_opacity(struct wlr_scene_node *node, float opacity) {
	/* wlr_scene_node_for_each_buffer walks the whole subtree, which is what a
	 * window needs: its tree holds the titlebar buffer and every surface
	 * buffer the client has committed. */
	if (node == NULL) {
		return;
	}
	if (node->type == WLR_SCENE_NODE_BUFFER) {
		wlr_scene_buffer_set_opacity(wlr_scene_buffer_from_node(node), opacity);
		return;
	}
	if (node->type != WLR_SCENE_NODE_TREE) {
		return;
	}
	struct wlr_scene_tree *tree = wlr_scene_tree_from_node(node);
	struct wlr_scene_node *child;
	wl_list_for_each(child, &tree->children, link) {
		bgwl_scene_set_opacity(child, opacity);
	}
}

static void bgwl_toplevel_update_border(struct bgwl_toplevel *toplevel) {
	if (toplevel->border == NULL) {
		return;
	}
	struct bgwl_server *server = toplevel->server;
	int width = server->cfg.border_width;
	if (width <= 0) {
		wlr_scene_rect_set_size(toplevel->border, 1, 1);
		return;
	}

	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	wlr_scene_node_set_position(&toplevel->border->node, -width, -width);
	wlr_scene_rect_set_size(toplevel->border, frame.width + 2 * width,
		frame.height + 2 * width);
}

void bgwl_toplevel_update_appearance(struct bgwl_toplevel *toplevel) {
	struct bgwl_server *server = toplevel->server;
	struct wlr_surface *surface = toplevel_surface(toplevel);
	if (surface == NULL) {
		return;
	}
	bool focused = server->seat->keyboard_state.focused_surface == surface;

	if (toplevel->border != NULL) {
		const float *color = focused
			? (toplevel->ws != NULL ? toplevel->ws->accent
				: server->cfg.accent_color)
			: server->cfg.border_color;
		wlr_scene_rect_set_color(toplevel->border, color);
	}
	if (toplevel->decor_node != NULL) {
		toplevel->decor_dirty = true;
		if (surface->mapped) {
			bgwl_toplevel_redraw_decor(toplevel);
		}
	}
}

void bgwl_toplevel_raise(struct bgwl_toplevel *toplevel) {
	struct bgwl_server *server = toplevel->server;

	/* "above" moves the window out of the workspace tree into the top layer,
	 * which is how a pinned terminal stays visible across streams. */
	struct wlr_scene_tree *parent = server->trees[BGWL_TREE_TOP];
	if (!toplevel->above) {
		if (toplevel->ws == NULL) {
			return;
		}
		parent = toplevel->ws->tree;
	}
	if (toplevel->scene_tree->node.parent != parent) {
		wlr_scene_node_reparent(&toplevel->scene_tree->node, parent);
	}
	wlr_scene_node_raise_to_top(&toplevel->scene_tree->node);
	wl_list_remove(&toplevel->link);
	wl_list_insert(&server->toplevels, &toplevel->link);
}

void bgwl_toplevel_move_to_workspace(struct bgwl_toplevel *toplevel,
		struct bgwl_workspace *ws) {
	if (toplevel == NULL || ws == NULL || toplevel->ws == ws) {
		return;
	}
	struct bgwl_workspace *from = toplevel->ws;

	if (from != NULL) {
		wl_list_remove(&toplevel->ws_link);
		from->mapped_count--;
	}
	wl_list_insert(from == NULL ? &ws->toplevels : ws->toplevels.prev,
		&toplevel->ws_link);
	if (!ws->toplevels.prev || from == NULL) {
		/* First window: put it at the head so a lone master is also the
		 * focused window after a move. */
		wl_list_remove(&toplevel->ws_link);
		wl_list_insert(&ws->toplevels, &toplevel->ws_link);
	}
	ws->mapped_count++;
	toplevel->ws = ws;

	if (!toplevel->above) {
		wlr_scene_node_reparent(&toplevel->scene_tree->node, ws->tree);
	}
	/* The window was placed for the old workspace's layout; the new one has
	 * different geometry, so untile until reflow puts it back. */
	bgwl_toplevel_set_tiled(toplevel, ws->layout != BGWL_LAYOUT_FLOAT);
}

void bgwl_toplevel_focus(struct bgwl_toplevel *toplevel) {
	if (toplevel == NULL) {
		return;
	}
	struct bgwl_server *server = toplevel->server;
	struct wlr_seat *seat = server->seat;
	struct wlr_surface *surface = toplevel_surface(toplevel);
	struct wlr_surface *prev_surface = seat->keyboard_state.focused_surface;

	if (surface == NULL || (prev_surface == surface &&
			server->focus_view == toplevel)) {
		return;
	}
	/* Look the previous window up before touching focus, while the seat still
	 * points at it — its border has to lose the accent colour. */
	struct bgwl_toplevel *prev_view = bgwl_server_focused_toplevel(server);

	if (prev_surface != NULL) {
		struct wlr_xdg_toplevel *prev =
			wlr_xdg_toplevel_try_from_wlr_surface(prev_surface);
		if (prev != NULL) {
			wlr_xdg_toplevel_set_activated(prev, false);
		}
		if (prev_view != NULL && prev_view != toplevel) {
			bgwl_toplevel_update_appearance(prev_view);
		}
	}

	bgwl_toplevel_raise(toplevel);
	wlr_xdg_toplevel_set_activated(toplevel->xdg_toplevel, true);

	struct wlr_keyboard *keyboard = wlr_seat_get_keyboard(seat);
	if (keyboard != NULL) {
		wlr_seat_keyboard_notify_enter(seat, surface, keyboard->keycodes,
			keyboard->num_keycodes, &keyboard->modifiers);
	}
	/* Recorded even when the seat refused the enter: without a keyboard there
	 * is no client focus, but the compositor still knows which window the user
	 * is working in, and that is what the panel and every window action use. */
	server->focus_view = toplevel;
	bgwl_toplevel_update_appearance(toplevel);
	bgwl_ctl_broadcast(server, "focus");
}

struct bgwl_toplevel *bgwl_server_focused_toplevel(struct bgwl_server *server) {
	/* Own record first: it survives a seat with no keyboard attached. */
	if (server->focus_view != NULL) {
		return server->focus_view;
	}
	struct wlr_surface *surface = server->seat->keyboard_state.focused_surface;
	if (surface == NULL) {
		return NULL;
	}
	struct wlr_xdg_surface *xdg_surface =
		wlr_xdg_surface_try_from_wlr_surface(surface);
	if (xdg_surface == NULL ||
			xdg_surface->role != WLR_XDG_SURFACE_ROLE_TOPLEVEL ||
			xdg_surface->data == NULL) {
		return NULL;
	}
	return ((struct wlr_scene_tree *)xdg_surface->data)->node.data;
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
	struct bgwl_toplevel *toplevel;
	wl_list_for_each(toplevel, &server->toplevels, link) {
		if (!toplevel->scene_tree->node.enabled) {
			continue;
		}
		struct wlr_box frame;
		bgwl_toplevel_get_geometry(toplevel, &frame);
		if (lx >= frame.x && lx < frame.x + frame.width &&
				ly >= frame.y && ly < frame.y + frame.height) {
			return toplevel;
		}
	}
	return NULL;
}

enum bgwl_button bgwl_toplevel_button_at(struct bgwl_toplevel *toplevel,
		double lx, double ly) {
	if (!toplevel->decorated) {
		return BGWL_BTN_NONE;
	}
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	if (ly < frame.y || ly >= frame.y + BGWL_DECOR_HEIGHT) {
		return BGWL_BTN_NONE;
	}
	/* Buttons are right-aligned, close nearest the edge; util.c draws exactly
	 * this order, and the two must not drift apart. */
	double from_right = frame.x + frame.width - lx;
	if (from_right <= 4) {
		return BGWL_BTN_NONE;
	}
	from_right -= 4;
	int slot = (int)(from_right / BGWL_BUTTON_WIDTH);
	switch (slot) {
	case 0: return BGWL_BTN_CLOSE;
	case 1: return BGWL_BTN_MAXIMIZE;
	case 2: return BGWL_BTN_MENU;
	default: return BGWL_BTN_NONE;
	}
}

/* Edges of the frame within grab distance of the pointer, 0 when the pointer
 * is inside the window. Floating windows only: a tile has no free edge to
 * drag, and resizing one means moving the split, not the window. */
static uint32_t resize_edges_at(struct bgwl_toplevel *toplevel, double lx,
		double ly) {
	struct bgwl_server *server = toplevel->server;
	if (toplevel->tiled || toplevel->fullscreen) {
		return 0;
	}
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);

	int margin = bgwl_max(BGWL_RESIZE_GRAB_MARGIN, server->cfg.border_width);
	uint32_t edges = 0;
	if (lx <= frame.x + margin) {
		edges |= WLR_EDGE_LEFT;
	}
	if (lx >= frame.x + frame.width - margin) {
		edges |= WLR_EDGE_RIGHT;
	}
	if (ly <= frame.y + margin) {
		edges |= WLR_EDGE_TOP;
	}
	if (ly >= frame.y + frame.height - margin) {
		edges |= WLR_EDGE_BOTTOM;
	}
	return edges;
}

uint32_t bgwl_toplevel_resize_edges_at(struct bgwl_toplevel *toplevel,
		double lx, double ly) {
	return resize_edges_at(toplevel, lx, ly);
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

	struct wlr_box geo = bgwl_xdg_geometry(toplevel->xdg_toplevel->base);
	/* Content corners in layout space: the frame origin plus the geometry
	 * offset, minus the titlebar because geo.y is relative to the content. */
	double border_x = (node->x + geo.x) +
		((edges & WLR_EDGE_RIGHT) ? geo.width : 0);
	double border_y = (node->y + geo.y - decor_height(toplevel)) +
		((edges & WLR_EDGE_BOTTOM) ? geo.height : 0);
	server->grab_x = server->cursor->x - border_x;
	server->grab_y = server->cursor->y - border_y;

	server->grab_geobox = geo;
	server->grab_geobox.x += node->x;
	server->grab_geobox.y += node->y - decor_height(toplevel);
	server->resize_edges = edges;
	wlr_xdg_toplevel_set_resizing(toplevel->xdg_toplevel, true);
}

void bgwl_toplevel_process_motion(struct bgwl_server *server) {
	struct bgwl_toplevel *toplevel = server->grabbed_toplevel;
	if (toplevel == NULL) {
		return;
	}
	struct wlr_scene_node *node = &toplevel->scene_tree->node;
	int decor = decor_height(toplevel);

	if (server->cursor_mode == BGWL_CURSOR_MOVE) {
		int nx = (int)(server->cursor->x - server->grab_x);
		int ny = (int)(server->cursor->y - server->grab_y);
		wlr_scene_node_set_position(node, nx, ny);
		/* A dragged window leaves the grid: keeping it tiled while the user
		 * is clearly placing it by hand would fight the pointer on the next
		 * reflow. */
		if (toplevel->tiled) {
			struct wlr_box frame;
			bgwl_toplevel_get_geometry(toplevel, &frame);
			toplevel->float_box = frame;
			bgwl_toplevel_set_tiled(toplevel, false);
		}
		bgwl_toplevel_update_border(toplevel);
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

	struct wlr_box geo = bgwl_xdg_geometry(toplevel->xdg_toplevel->base);
	wlr_scene_node_set_position(node, left - geo.x,
		top + decor - geo.y);

	int new_width = right - left;
	int new_height = bottom - top - decor;
	if (new_width < 1) {
		new_width = 1;
	}
	if (new_height < 1) {
		new_height = 1;
	}
	toplevel->pending_width = new_width;
	toplevel->pending_height = new_height;
	wlr_xdg_toplevel_set_size(toplevel->xdg_toplevel, new_width, new_height);
	bgwl_toplevel_update_border(toplevel);
}

void bgwl_toplevel_reset_cursor_mode(struct bgwl_server *server) {
	if (server->cursor_mode == BGWL_CURSOR_RESIZE &&
			server->grabbed_toplevel != NULL) {
		wlr_xdg_toplevel_set_resizing(server->grabbed_toplevel->xdg_toplevel,
			false);
	}
	server->cursor_mode = BGWL_CURSOR_PASSTHROUGH;
	server->grabbed_toplevel = NULL;
	server->resize_edges = 0;
}

static void bgwl_toplevel_redraw_decor(struct bgwl_toplevel *toplevel) {
	struct wlr_xdg_surface *base = toplevel->xdg_toplevel->base;
	struct wlr_box geo = bgwl_xdg_geometry(base);
	int width = geo.width;
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

	struct bgwl_server *server = toplevel->server;
	bool focused = server->seat->keyboard_state.focused_surface == base->surface;
	const float *accent = toplevel->ws != NULL ? toplevel->ws->accent
		: server->cfg.accent_color;
	struct bgwl_buffer *buffer = bgwl_draw_titlebar(server, width,
		toplevel->xdg_toplevel->title, focused, accent);
	if (buffer == NULL) {
		wlr_log(WLR_ERROR, "failed to draw titlebar for toplevel");
		return;
	}

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

void bgwl_toplevel_set_tiled(struct bgwl_toplevel *toplevel, bool value) {
	if (toplevel == NULL || toplevel->tiled == value) {
		return;
	}
	toplevel->tiled = value;

	uint32_t edges = value
		? (WLR_EDGE_TOP | WLR_EDGE_BOTTOM | WLR_EDGE_LEFT | WLR_EDGE_RIGHT)
		: 0;
	wlr_xdg_toplevel_set_tiled(toplevel->xdg_toplevel, edges);
	/* The client is allowed to keep its own borders when tiled; tell it which
	 * edges are covered so it stops drawing a resize handle we do not use. */
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	if (value) {
		toplevel->float_box = frame;
	} else if (frame.width > 0) {
		bgwl_toplevel_set_geometry(toplevel, toplevel->float_box);
	}
}

/* Takes the window out of the grid and remembers where it was, the first time
 * either maximise or fullscreen claims it. */
static void claim_window_geometry(struct bgwl_toplevel *toplevel) {
	if (toplevel->has_restore) {
		return;
	}
	bgwl_toplevel_get_geometry(toplevel, &toplevel->restore_box);
	toplevel->has_restore = true;
	bgwl_toplevel_set_tiled(toplevel, false);
}

static void release_window_geometry(struct bgwl_toplevel *toplevel) {
	if (!toplevel->has_restore) {
		return;
	}
	struct wlr_box box = toplevel->restore_box;
	toplevel->has_restore = false;
	memset(&toplevel->restore_box, 0, sizeof(toplevel->restore_box));

	if (toplevel->ws != NULL && toplevel->ws->layout != BGWL_LAYOUT_FLOAT) {
		/* Back into the grid: the saved box only matters as the user's
		 * floating preference, so keep it for a later switch to float. */
		toplevel->float_box = box;
		bgwl_toplevel_set_tiled(toplevel, true);
		return;
	}
	if (box.width > 0) {
		bgwl_toplevel_set_geometry(toplevel, box);
	}
}

void bgwl_toplevel_set_maximized(struct bgwl_toplevel *toplevel, bool value) {
	if (toplevel == NULL || toplevel->maximized == value) {
		return;
	}
	struct bgwl_server *server = toplevel->server;
	toplevel->maximized = value;
	wlr_xdg_toplevel_set_maximized(toplevel->xdg_toplevel, value);

	if (value) {
		claim_window_geometry(toplevel);
		struct wlr_output *output = toplevel_output(toplevel);
		struct bgwl_output *bg_output;
		wl_list_for_each(bg_output, &server->outputs, link) {
			if (bg_output->wlr_output == output) {
				bgwl_toplevel_set_geometry(toplevel, bg_output->usable);
				break;
			}
		}
	} else if (!toplevel->fullscreen) {
		release_window_geometry(toplevel);
	}
	bgwl_toplevel_redraw_decor(toplevel);
	bgwl_ctl_broadcast(server, "windows");
}

void bgwl_toplevel_set_fullscreen(struct bgwl_toplevel *toplevel, bool value) {
	if (toplevel == NULL || toplevel->fullscreen == value) {
		return;
	}
	struct bgwl_server *server = toplevel->server;
	toplevel->fullscreen = value;
	wlr_xdg_toplevel_set_fullscreen(toplevel->xdg_toplevel, value);

	if (value) {
		claim_window_geometry(toplevel);
		/* No decoration, no panel, no gaps: the whole output box. */
		struct wlr_output *output = toplevel_output(toplevel);
		if (output != NULL) {
			struct wlr_box box;
			wlr_output_layout_get_box(server->output_layout, output, &box);
			if (box.width > 0 && box.height > 0) {
				bgwl_toplevel_set_geometry(toplevel, box);
			}
		}
	} else if (!toplevel->maximized) {
		release_window_geometry(toplevel);
	}
	if (toplevel->border != NULL) {
		wlr_scene_node_set_enabled(&toplevel->border->node, !value);
	}
	bgwl_ctl_broadcast(server, "windows");
}

void bgwl_toplevel_close(struct bgwl_toplevel *toplevel) {
	if (toplevel == NULL || toplevel->xdg_toplevel == NULL) {
		return;
	}
	wlr_xdg_toplevel_send_close(toplevel->xdg_toplevel);
}

/* Cascading offset for the next floating window, so four terminals opened in
 * a row do not become one terminal with four titles. */
static int cascade_offset(struct bgwl_server *server) {
	int n = 0;
	struct bgwl_toplevel *toplevel;
	wl_list_for_each(toplevel, &server->toplevels, link) {
		n++;
	}
	return (n % 8) * 28;
}

static void place_floating(struct bgwl_toplevel *toplevel, struct wlr_box box) {
	struct bgwl_server *server = toplevel->server;
	struct wlr_box area = bgwl_server_usable_box(server);

	if (box.width <= 0 || box.height <= 0) {
		box.width = bgwl_min(900, bgwl_max(320, area.width * 3 / 4));
		box.height = bgwl_min(620, bgwl_max(240, area.height * 3 / 4));
	}
	int offset = cascade_offset(server);
	box.x = area.x + (area.width - box.width) / 2 + offset;
	box.y = area.y + (area.height - box.height) / 3 + offset;

	/* Keep the titlebar on screen: a window whose bar is off the edge cannot
	 * be moved back by hand. */
	if (box.x + box.width > area.x + area.width) {
		box.x = bgwl_max(area.x, area.x + area.width - box.width);
	}
	if (box.y + box.height > area.y + area.height) {
		box.y = bgwl_max(area.y, area.y + area.height - box.height);
	}
	bgwl_toplevel_set_geometry(toplevel, box);
}

static void apply_rules(struct bgwl_toplevel *toplevel) {
	struct bgwl_server *server = toplevel->server;
	const char *app_id = toplevel->xdg_toplevel->app_id;
	if (app_id == NULL) {
		return;
	}

	for (int i = 0; i < server->cfg.rule_count; i++) {
		struct bgwl_rule *rule = &server->cfg.rules[i];
		if (rule->app_id[0] == '\0' || strcmp(rule->app_id, app_id) != 0) {
			continue;
		}
		if (rule->workspace > 0) {
			struct bgwl_workspace *ws = bgwl_workspace_by_index(server,
				rule->workspace - 1);
			if (ws != NULL && ws != toplevel->ws) {
				bgwl_toplevel_move_to_workspace(toplevel, ws);
			}
		}
		if (rule->layout >= 0 && toplevel->ws != NULL) {
			toplevel->ws->layout = (enum bgwl_layout)rule->layout;
		}
		if (rule->sticky) {
			toplevel->sticky = true;
		}
		if (rule->fullscreen) {
			bgwl_toplevel_set_fullscreen(toplevel, true);
		}
		return;
	}
}

static void toplevel_handle_commit(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel = wl_container_of(listener, toplevel, commit);
	struct wlr_xdg_toplevel *xdg_toplevel = toplevel->xdg_toplevel;
	struct wlr_xdg_surface *base = xdg_toplevel->base;
	int decor = decor_height(toplevel);

	if (base->initial_commit) {
		/* A configure must be sent for the client to map. Size 0,0 lets the
		 * client choose its own initial size; the titlebar follows. */
		wlr_xdg_toplevel_set_size(xdg_toplevel, 0, 0);
		return;
	}

	/* Window geometry is relative to the surface origin; shift it so the
	 * client content begins exactly under the titlebar. */
	struct wlr_box geo = bgwl_xdg_geometry(base);
	wlr_scene_node_set_position(&toplevel->xdg_tree->node, -geo.x,
		decor - geo.y);

	bgwl_toplevel_redraw_decor(toplevel);
	bgwl_toplevel_update_border(toplevel);
}

static void toplevel_handle_map(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel = wl_container_of(listener, toplevel, map);
	struct bgwl_server *server = toplevel->server;

	/* Front of the list is the top-most window. */
	wl_list_insert(&server->toplevels, &toplevel->link);

	/* Re-enable the bar: unmap disabled it, and the client may re-map this
	 * surface without recreating our scene nodes. */
	if (toplevel->decor_node != NULL) {
		wlr_scene_node_set_enabled(&toplevel->decor_node->node,
			toplevel->decorated);
	}

	apply_rules(toplevel);

	bool tile = toplevel->ws != NULL &&
		toplevel->ws->layout != BGWL_LAYOUT_FLOAT;
	if (tile) {
		bgwl_toplevel_set_tiled(toplevel, true);
		bgwl_reflow_workspace(toplevel->ws);
	} else if (toplevel->float_box.width <= 0) {
		place_floating(toplevel, (struct wlr_box){0});
	}

	bgwl_toplevel_redraw_decor(toplevel);
	bgwl_toplevel_focus(toplevel);
	if (server->cfg.animations) {
		bgwl_anim_window_open(toplevel);
	}
	bgwl_ctl_broadcast(server, "windows");
}

static void toplevel_handle_unmap(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, unmap);
	struct bgwl_server *server = toplevel->server;

	if (toplevel == server->grabbed_toplevel) {
		bgwl_toplevel_reset_cursor_mode(server);
	}

	wl_list_remove(&toplevel->link);
	if (toplevel->server->focus_view == toplevel) {
		toplevel->server->focus_view = NULL;
	}

	/* Hide the frame while unmapped; the scene tree itself stays alive so
	 * popups can still find their parent node. */
	if (toplevel->decor_node != NULL) {
		wlr_scene_node_set_enabled(&toplevel->decor_node->node, false);
	}
	if (toplevel->border != NULL) {
		wlr_scene_node_set_enabled(&toplevel->border->node, false);
	}

	/* Whatever was focused is gone: hand the keyboard to a neighbour rather
	 * than leaving the seat with no focus until the next click. */
	if (bgwl_server_focused_toplevel(server) == NULL) {
		struct bgwl_toplevel *next = bgwl_workspace_first_toplevel(
			server->active_ws);
		if (next != NULL) {
			bgwl_toplevel_focus(next);
		}
	}
	bgwl_reflow(server);
	bgwl_ctl_broadcast(server, "windows");
}

static void toplevel_handle_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, destroy);
	struct bgwl_server *server = toplevel->server;

	if (toplevel == server->grabbed_toplevel) {
		bgwl_toplevel_reset_cursor_mode(server);
	}
	if (toplevel == server->focus_view) {
		server->focus_view = NULL;
	}
	if (toplevel->ws != NULL) {
		wl_list_remove(&toplevel->ws_link);
		wl_list_init(&toplevel->ws_link);
		toplevel->ws->mapped_count--;
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

	if (toplevel->scene_tree != NULL) {
		wlr_scene_node_destroy(&toplevel->scene_tree->node);
	}
	free(toplevel);
}

static void toplevel_handle_set_title(struct wl_listener *listener, void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, set_title);
	toplevel->decor_dirty = true;
	struct wlr_surface *surface = toplevel_surface(toplevel);
	if (surface != NULL && surface->mapped) {
		bgwl_toplevel_redraw_decor(toplevel);
	}
	bgwl_ctl_broadcast(toplevel->server, "windows");
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

static void toplevel_handle_request_maximize(struct wl_listener *listener,
		void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, request_maximize);
	if (toplevel->xdg_toplevel->base->initialized) {
		/* Honour the request: a maximise that is answered with an empty
		 * configure is how a window ends up full-size but unmaximised. */
		bgwl_toplevel_set_maximized(toplevel, true);
	}
}

static void toplevel_handle_request_fullscreen(struct wl_listener *listener,
		void *data) {
	struct bgwl_toplevel *toplevel =
		wl_container_of(listener, toplevel, request_fullscreen);
	if (toplevel->xdg_toplevel->base->initialized) {
		bgwl_toplevel_set_fullscreen(toplevel, true);
	}
}

static struct bgwl_workspace *active_or_first(struct bgwl_server *server) {
	if (server->active_ws != NULL) {
		return server->active_ws;
	}
	return bgwl_workspace_by_index(server, 0);
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
	toplevel->id = server->next_window_id++;
	toplevel->decor_dirty = true;
	toplevel->decorated = server->cfg.decorate_by_default;
	toplevel->opacity = 1.0f;
	wl_list_init(&toplevel->link);
	wl_list_init(&toplevel->ws_link);

	toplevel->ws = active_or_first(server);
	struct wlr_scene_tree *parent = toplevel->ws != NULL ? toplevel->ws->tree
		: server->trees[BGWL_TREE_NORMAL];

	toplevel->scene_tree = wlr_scene_tree_create(parent);
	if (toplevel->scene_tree == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_tree_create failed");
		free(toplevel);
		return NULL;
	}
	toplevel->scene_tree->node.data = toplevel;
	/* Popups look up their parent's scene node through xdg_surface.data. */
	xdg_toplevel->base->data = toplevel->scene_tree;

	/* Created first so it renders underneath the window and only shows as a
	 * ring around it. */
	int border_width = server->cfg.border_width;
	toplevel->border = wlr_scene_rect_create(toplevel->scene_tree,
		bgwl_max(1, border_width), bgwl_max(1, border_width),
		server->cfg.border_color);

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

	if (toplevel->ws != NULL) {
		wl_list_insert(toplevel->ws->toplevels.prev, &toplevel->ws_link);
		toplevel->ws->mapped_count++;
	}

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
