/*
 * The tiling engine.
 *
 * Two layouts ship, both deliberately simple, because a layout the user can
 * predict beats a clever one they have to reason about:
 *
 *   tile — a master band plus a stack band, split by a draggable percentage.
 *   even — a balanced grid, no master.
 *
 * float is not an algorithm: it is this file doing nothing, leaving each
 * window exactly where the client or the user put it.
 *
 * Both are built from one primitive, arrange_band(): n windows inside a
 * rectangle, divided along the band's short axis. tile is then just two
 * bands, and even is one band wrapped into rows.
 *
 * Windows are assigned to an output by the centre of their current box, then
 * tiled inside that output's usable area (what layer.c leaves after the
 * panel's exclusive zone). Tiles never overlap and never cover the panel.
 */
#include <stdlib.h>
#include <string.h>

#include <wlr/types/wlr_scene.h>
#include <wlr/util/log.h>

#include "bgwl.h"

#define BGWL_MIN_TILE_WIDTH 200
#define BGWL_MIN_TILE_HEIGHT 140

static bool box_contains_center(const struct wlr_box *box, int x, int y) {
	return x >= box->x && x < box->x + box->width &&
		y >= box->y && y < box->y + box->height;
}

/* Splits [start, start+extent) into n cells that differ by at most one pixel. */
static void split_axis(int start, int extent, int n, int *offset, int *size) {
	int base = extent / n;
	int remainder = extent % n;
	int cursor = start;
	for (int i = 0; i < n; i++) {
		int width = base + (i < remainder ? 1 : 0);
		if (offset != NULL) {
			offset[i] = cursor;
		}
		if (size != NULL) {
			size[i] = width;
		}
		cursor += width;
	}
}

/* A window's tile is inset by half an inner gap on each side that has a
 * neighbour; the outer edge of the area keeps the outer gap only. */
static void inset(int cell_start, int cell_size, bool first, bool last,
		int gap, int *out_start, int *out_size) {
	int lead = first ? 0 : gap / 2;
	int trail = last ? 0 : gap / 2;
	*out_start = cell_start + lead;
	*out_size = bgwl_max(1, cell_size - lead - trail);
}

static void place(struct bgwl_toplevel *toplevel, int x, int y, int width,
		int height) {
	struct wlr_box box = { x, y, bgwl_max(1, width), bgwl_max(1, height) };
	bgwl_toplevel_set_geometry(toplevel, box);
}

/*
 * Lays n windows across `area`.
 *
 * vertical == true divides the area into columns (one window per column, the
 * classic "side by side" band); false divides it into rows.
 */
static void arrange_band(struct bgwl_toplevel **windows, int n,
		struct wlr_box area, bool vertical, int gap) {
	if (n <= 0) {
		return;
	}
	int offset[BGWL_MAX_WORKSPACES * 4];
	int size[BGWL_MAX_WORKSPACES * 4];
	if (n > (int)(sizeof(offset) / sizeof(offset[0]))) {
		n = (int)(sizeof(offset) / sizeof(offset[0]));
	}

	if (vertical) {
		split_axis(area.x, area.width, n, offset, size);
		for (int i = 0; i < n; i++) {
			int x, w;
			inset(offset[i], size[i], i == 0, i == n - 1, gap, &x, &w);
			place(windows[i], x, area.y, w, area.height);
		}
		return;
	}
	split_axis(area.y, area.height, n, offset, size);
	for (int i = 0; i < n; i++) {
		int y, h;
		inset(offset[i], size[i], i == 0, i == n - 1, gap, &y, &h);
		place(windows[i], area.x, y, area.width, h);
	}
}

static void tile_even(struct bgwl_toplevel **windows, int n,
		struct wlr_box area, int gap) {
	int rows = 1;
	while (rows * rows < n) {
		rows++;
	}
	/* A row of cells shorter than a usable window is not a grid: prefer
	 * fewer, taller rows and let the last row be short. */
	while (rows > 1 && area.height / rows < BGWL_MIN_TILE_HEIGHT) {
		rows--;
	}
	int cols = bgwl_max(1, (n + rows - 1) / rows);

	int row_off[BGWL_MAX_WORKSPACES * 4], row_size[BGWL_MAX_WORKSPACES * 4];
	if (rows > (int)(sizeof(row_off) / sizeof(row_off[0]))) {
		rows = (int)(sizeof(row_off) / sizeof(row_off[0]));
		cols = bgwl_max(1, (n + rows - 1) / rows);
	}
	split_axis(area.y, area.height, rows, row_off, row_size);

	for (int r = 0; r < rows; r++) {
		int first = r * cols;
		int count = bgwl_min(cols, n - first);
		if (count <= 0) {
			break;
		}
		int y, h;
		inset(row_off[r], row_size[r], r == 0, r == rows - 1, gap, &y, &h);
		struct wlr_box row_area = { area.x, y, area.width, h };
		arrange_band(&windows[first], count, row_area, true, gap);
	}
}

static void tile_master(struct bgwl_workspace *ws, struct bgwl_toplevel **win,
		int n, struct wlr_box area, int gap) {
	int master_count = bgwl_min(bgwl_max(1, ws->master_count), n);
	int stack_count = n - master_count;
	bool vertical = (ws->split == BGWL_SPLIT_RIGHT);

	if (stack_count == 0) {
		arrange_band(win, master_count, area, vertical, gap);
		return;
	}

	/* How the master band shares the area with the stack is along the axis
	 * the stack itself does NOT divide: split-right means master on the left,
	 * divided from the stack vertically. */
	int span = vertical ? area.width : area.height;
	int master_size = span * ws->master_pct / 100;
	master_size = bgwl_max(master_size, BGWL_MIN_TILE_WIDTH);
	int stack_size = span - master_size - gap;
	if (stack_size < BGWL_MIN_TILE_WIDTH) {
		/* The screen is too narrow for two bands: a grid serves the user
		 * better than a 60-pixel stack column. */
		tile_even(win, n, area, gap);
		return;
	}

	struct wlr_box master_area = area;
	struct wlr_box stack_area = area;
	if (vertical) {
		master_area.width = master_size;
		stack_area.x = area.x + master_size + gap;
		stack_area.width = stack_size;
	} else {
		master_area.height = master_size;
		stack_area.y = area.y + master_size + gap;
		stack_area.height = stack_size;
	}

	/* Masters stack along the band's long axis; so does the stack. */
	arrange_band(win, master_count, master_area, !vertical, gap);
	arrange_band(&win[master_count], stack_count, stack_area, !vertical, gap);
}

static void toplevel_center(struct bgwl_toplevel *toplevel, int *cx, int *cy) {
	struct wlr_box box;
	bgwl_toplevel_get_geometry(toplevel, &box);
	*cx = box.x + box.width / 2;
	*cy = box.y + box.height / 2;
}

static bool is_layout_candidate(struct bgwl_toplevel *toplevel) {
	return toplevel->tiled && !toplevel->fullscreen &&
		toplevel->xdg_toplevel != NULL &&
		toplevel->xdg_toplevel->base != NULL &&
		toplevel->xdg_toplevel->base->surface != NULL &&
		toplevel->xdg_toplevel->base->surface->mapped;
}

/* Windows already sitting on this output, in tiling order. */
static int collect_output_windows(struct bgwl_workspace *ws,
		const struct wlr_box *area, struct bgwl_toplevel **out, int max) {
	struct bgwl_toplevel *toplevel;
	int n = 0;

	wl_list_for_each(toplevel, &ws->toplevels, ws_link) {
		if (!is_layout_candidate(toplevel)) {
			continue;
		}
		int cx, cy;
		toplevel_center(toplevel, &cx, &cy);
		if (!box_contains_center(area, cx, cy)) {
			continue;
		}
		if (n < max) {
			out[n++] = toplevel;
		}
	}
	return n;
}

void bgwl_reflow_workspace(struct bgwl_workspace *ws) {
	if (ws == NULL || ws->layout == BGWL_LAYOUT_FLOAT) {
		return;
	}
	struct bgwl_server *server = ws->server;
	struct bgwl_output *output;

	wl_list_for_each(output, &server->outputs, link) {
		struct wlr_box area = output->usable;
		if (area.width <= 0 || area.height <= 0) {
			continue;
		}

		int outer = server->cfg.gap_outer;
		int inner = server->cfg.gap_inner;
		if (outer > 0) {
			area.x += outer;
			area.y += outer;
			area.width -= 2 * outer;
			area.height -= 2 * outer;
		}

		struct bgwl_toplevel *windows[BGWL_MAX_WORKSPACES * 4];
		int n = collect_output_windows(ws, &area, windows,
			(int)(sizeof(windows) / sizeof(windows[0])));
		if (n <= 0) {
			continue;
		}
		if (n == 1) {
			place(windows[0], area.x, area.y, area.width, area.height);
		} else if (ws->layout == BGWL_LAYOUT_EVEN) {
			tile_even(windows, n, area, inner);
		} else {
			tile_master(ws, windows, n, area, inner);
		}
	}
}

void bgwl_reflow(struct bgwl_server *server) {
	struct bgwl_workspace *ws;
	wl_list_for_each(ws, &server->workspaces, link) {
		bgwl_reflow_workspace(ws);
	}
}

void bgwl_tiling_adjust_master(struct bgwl_workspace *ws, int direction) {
	if (ws == NULL) {
		return;
	}
	int next = bgwl_min(80, bgwl_max(20, ws->master_pct + direction * 5));
	if (next == ws->master_pct) {
		return;
	}
	ws->master_pct = next;
	bgwl_reflow_workspace(ws);
}

void bgwl_tiling_rotate_stack(struct bgwl_workspace *ws, int direction) {
	if (ws == NULL) {
		return;
	}
	const int max_master = 4;
	if (direction > 0) {
		ws->master_count = ws->master_count >= max_master ? 1 : ws->master_count + 1;
	} else {
		ws->master_count = ws->master_count <= 1 ? max_master : ws->master_count - 1;
	}
	bgwl_reflow_workspace(ws);
}
