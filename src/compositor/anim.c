/*
 * Motion.
 *
 * bgwl animates two things and nothing else: a workspace switch slides the two
 * stream trees, and a new window fades in. Both are driven by one timer, not
 * by per-frame work, and both stop on their own after a fixed number of
 * milliseconds.
 *
 * What is deliberately not here:
 *
 * - Window scale/zoom animations. wlroots 0.18 cannot scale a surface in the
 *   scene graph without a viewporter round-trip per client, so a zoom would
 *   either be fake (scaling our own buffers only) or cost every client a
 *   reconfigure.
 * - Close animations. The client destroys its surface when it closes; keeping
 *   the last buffer alive to fade it out means copying it, which is a full
 *   frame of memory per window for an effect the user sees for 90 ms.
 * - Blur, shadows, drop-in glow. They need extra render passes, and ByteguruOS
 *   targets hardware where that is the difference between 60 fps and 40.
 *
 * The slide moves exactly two scene nodes regardless of how many windows are
 * open, which is the practical payoff of giving every workspace its own tree.
 */
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/util/log.h>

#include "bgwl.h"

/* Timer resolution for animations. 8 ms is well under one frame at 60 Hz, so
 * no tick is ever skipped, while still leaving the loop idle most of the time
 * when nothing moves. */
#define BGWL_ANIM_TICK_MS 8

enum bgwl_anim_kind {
	BGWL_ANIM_WORKSPACE_SLIDE,
	BGWL_ANIM_WINDOW_FADE,
};

struct bgwl_animation {
	struct bgwl_server *server;
	struct wl_list link;
	enum bgwl_anim_kind kind;
	int64_t started_us;
	int64_t duration_us;
	int direction;			/* slide: +1 forward, -1 backward */
	struct bgwl_workspace *from, *to;
	struct bgwl_toplevel *toplevel;
};

static int64_t now_us(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* cubic-out: fast start, gentle stop, which is what reads as "the interface is
 * quick" without overshooting into bounce. */
static double ease_out(double t) {
	double inverse = 1.0 - t;
	return 1.0 - inverse * inverse * inverse;
}

static void animation_finish(struct bgwl_animation *animation) {
	struct bgwl_server *server = animation->server;

	switch (animation->kind) {
	case BGWL_ANIM_WORKSPACE_SLIDE:
		if (animation->to != NULL) {
			wlr_scene_node_set_position(&animation->to->tree->node, 0, 0);
		}
		if (animation->from != NULL) {
			/* Parked at the origin but disabled, so the next slide in the
			 * same direction starts from the right place. */
			wlr_scene_node_set_position(&animation->from->tree->node, 0, 0);
		}
		break;
	case BGWL_ANIM_WINDOW_FADE:
		if (animation->toplevel != NULL) {
			animation->toplevel->opacity = 1.0f;
			bgwl_scene_set_opacity(&animation->toplevel->scene_tree->node,
				1.0f);
		}
		break;
	}

	wl_list_remove(&animation->link);
	free(animation);

	/* The last animation to end stops the timer; nothing runs while the
	 * desktop is still. */
	if (wl_list_empty(&server->animations) && server->anim_source != NULL) {
		wl_event_source_remove(server->anim_source);
		server->anim_source = NULL;
	}
}

static int animation_tick(void *user_data) {
	struct bgwl_server *server = user_data;
	struct bgwl_animation *animation, *tmp;
	int64_t now = now_us();

	wl_list_for_each_safe(animation, tmp, &server->animations, link) {
		double t = (double)(now - animation->started_us) /
			(double)animation->duration_us;
		if (t >= 1.0) {
			animation_finish(animation);
			continue;
		}
		if (t < 0.0) {
			t = 0.0;
		}
		double eased = ease_out(t);

		switch (animation->kind) {
		case BGWL_ANIM_WORKSPACE_SLIDE: {
			struct wlr_box area = bgwl_server_usable_box(server);
			int distance = area.width > 0 ? area.width : 1;
			if (animation->to != NULL) {
				wlr_scene_node_set_position(&animation->to->tree->node,
					animation->direction * distance *
						(1.0 - eased), 0);
			}
			if (animation->from != NULL) {
				wlr_scene_node_set_position(&animation->from->tree->node,
					-animation->direction * distance * eased, 0);
			}
			break;
		}
		case BGWL_ANIM_WINDOW_FADE:
			if (animation->toplevel != NULL) {
				float opacity = (float)(0.0 + eased);
				animation->toplevel->opacity = opacity;
				bgwl_scene_set_opacity(&animation->toplevel->scene_tree->node,
					opacity);
			}
			break;
		}
	}

	return 1;
}

static void start_animation(struct bgwl_server *server,
		struct bgwl_animation *animation) {
	animation->server = server;
	animation->started_us = now_us();
	animation->duration_us = (int64_t)server->cfg.anim_duration_ms * 1000;
	if (animation->duration_us <= 0) {
		free(animation);
		return;
	}
	wl_list_insert(&server->animations, &animation->link);

	if (server->anim_source == NULL) {
		struct wl_event_loop *loop = wl_display_get_event_loop(
			server->wl_display);
		server->anim_source = wl_event_loop_add_timer(loop, animation_tick,
			server);
		if (server->anim_source == NULL) {
			wlr_log(WLR_ERROR, "could not start the animation timer");
			wl_list_remove(&animation->link);
			free(animation);
			return;
		}
	}
	wl_event_source_timer_update(server->anim_source, BGWL_ANIM_TICK_MS);
}

void bgwl_anim_init(struct bgwl_server *server) {
	wl_list_init(&server->animations);
}

void bgwl_anim_finish(struct bgwl_server *server) {
	struct bgwl_animation *animation, *tmp;
	wl_list_for_each_safe(animation, tmp, &server->animations, link) {
		wl_list_remove(&animation->link);
		free(animation);
	}
	if (server->anim_source != NULL) {
		wl_event_source_remove(server->anim_source);
		server->anim_source = NULL;
	}
}

void bgwl_anim_workspace_slide(struct bgwl_server *server,
		struct bgwl_workspace *from, struct bgwl_workspace *to,
		int direction) {
	if (!server->cfg.animations || server->cfg.anim_duration_ms <= 0) {
		return;
	}
	struct bgwl_animation *animation = calloc(1, sizeof(*animation));
	if (animation == NULL) {
		return;
	}
	animation->kind = BGWL_ANIM_WORKSPACE_SLIDE;
	animation->from = from;
	animation->to = to;
	animation->direction = direction;
	start_animation(server, animation);
}

void bgwl_anim_window_open(struct bgwl_toplevel *toplevel) {
	struct bgwl_server *server = toplevel->server;
	if (!server->cfg.animations || server->cfg.anim_duration_ms <= 0) {
		return;
	}
	struct bgwl_animation *animation = calloc(1, sizeof(*animation));
	if (animation == NULL) {
		return;
	}
	animation->kind = BGWL_ANIM_WINDOW_FADE;
	animation->toplevel = toplevel;
	/* Start invisible; the first tick brings it up. Skipping the initial set
	 * would show one fully opaque frame and look like a glitch instead. */
	bgwl_scene_set_opacity(&toplevel->scene_tree->node, 0.0f);
	start_animation(server, animation);
}
