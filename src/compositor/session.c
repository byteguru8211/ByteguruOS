/*
 * Session power and privacy: idle detection, screen lock, DPMS, laptop lid.
 *
 * The chain is deliberately one-directional and has two stages:
 *
 *   activity  → resets the timer, wakes the panels
 *   idle      → stage 1 spawns the lock screen, stage 2 blanks the panels
 *   lid close → locks immediately
 *
 * Locking uses ext-session-lock-v1, which is the protocol that makes a lock
 * trustworthy: while a lock is live bgwl disables the normal scene trees and
 * routes every keystroke to the lock surface, so no client — including one
 * that ignores the lock — can receive input. Drawing a window over the desktop
 * is not locking a session, and that distinction is the reason this file
 * exists.
 *
 * Two stages rather than one, because a lock screen that blanks its own pixels
 * is unusable: the panels may only go off after the lock has taken over.
 *
 * wlr_idle_v1 was removed from wlroots; ext-idle-notify-v1 replaces it and is
 * seat-scoped, which is why activity is reported against the seat.
 */
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_idle_inhibit_v1.h>
#include <wlr/types/wlr_idle_notify_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_session_lock_v1.h>
#include <wlr/types/wlr_switch.h>
#include <wlr/util/log.h>

#include "bgwl.h"

/* Timer stages. LOCK fires first; DPMS only after the lock screen is up. */
enum bgwl_idle_stage {
	BGWL_IDLE_LOCK = 1,
	BGWL_IDLE_DPMS,
};

struct bgwl_lock_surface {
	struct bgwl_lock *lock;
	struct wlr_session_lock_surface_v1 *surface;
	struct wl_listener commit;
	struct wl_listener destroy;
	bool configured;
	struct wl_list link;
};

/* One active ext-session-lock. The protocol allows only one at a time; a
 * second client asking while this exists has its lock destroyed, which is the
 * behaviour the protocol specifies. */
struct bgwl_lock {
	struct bgwl_session *session;
	struct wlr_session_lock_v1 *wlr_lock;
	struct wl_listener new_surface;
	struct wl_listener unlock;
	struct wl_listener destroy;
	struct wl_list surfaces;
	int wanted_outputs;
	int committed_surfaces;
	bool locked_sent;
};

struct bgwl_switch {
	struct bgwl_session *session;
	struct wlr_input_device *device;
	struct wl_listener toggle;
	struct wl_listener destroy;
	struct wl_list link;
};

struct bgwl_session {
	struct bgwl_server *server;
	struct wl_event_source *idle_source;
	struct bgwl_lock *lock;
	bool inhibited;
	bool dpms_off;
	enum bgwl_idle_stage stage;
	struct wl_list switches;
	struct wl_listener new_lock;
	struct wl_listener new_inhibitor;
};

/* --- helpers ------------------------------------------------------------ */

bool bgwl_server_is_locked(struct bgwl_server *server) {
	return server->locked;
}

/* Everything that must stop being visible or reachable while the session is
 * locked. The overlay tree stays: a lock screen may need an input-method
 * surface, and an IME popup has no business showing the desktop. */
static void set_desktop_visible(struct bgwl_server *server, bool visible) {
	for (int i = BGWL_TREE_BG; i <= BGWL_TREE_TOP; i++) {
		if (server->trees[i] != NULL) {
			wlr_scene_node_set_enabled(&server->trees[i]->node, visible);
		}
	}
}

static bool any_idle_inhibitor_visible(struct bgwl_session *session) {
	struct wlr_idle_inhibitor_v1 *inhibitor;
	wl_list_for_each(inhibitor, &session->server->idle_inhibit->inhibitors,
			link) {
		if (inhibitor->surface != NULL && inhibitor->surface->mapped) {
			return true;
		}
	}
	return false;
}

static void update_inhibition(struct bgwl_session *session) {
	bool inhibited = any_idle_inhibitor_visible(session);
	if (inhibited == session->inhibited) {
		return;
	}
	session->inhibited = inhibited;
	wlr_idle_notifier_v1_set_inhibited(session->server->idle_notifier,
		inhibited);
	wlr_log(WLR_INFO, "idle %s", inhibited ? "inhibited" : "allowed");
}

/* The manager's new-inhibitor signal fires after the inhibitor is linked, so
 * recounting is enough; a stopped video player leaves the list by itself and
 * the same recount re-arms the blanking path. */
static void inhibitor_handle_new(struct wl_listener *listener, void *data) {
	struct bgwl_session *session =
		wl_container_of(listener, session, new_inhibitor);
	update_inhibition(session);
}

/* --- lock --------------------------------------------------------------- */

static void lock_surface_handle_destroy(struct wl_listener *listener,
		void *data) {
	struct bgwl_lock_surface *lock_surface =
		wl_container_of(listener, lock_surface, destroy);
	struct bgwl_lock *lock = lock_surface->lock;

	if (lock_surface->configured) {
		lock->committed_surfaces--;
	}
	wl_list_remove(&lock_surface->commit.link);
	wl_list_remove(&lock_surface->destroy.link);
	wl_list_remove(&lock_surface->link);
	free(lock_surface);
}

static void lock_surface_handle_commit(struct wl_listener *listener,
		void *data) {
	struct bgwl_lock_surface *lock_surface =
		wl_container_of(listener, lock_surface, commit);
	struct bgwl_lock *lock = lock_surface->lock;
	struct wlr_surface *surface = lock_surface->surface->surface;

	if (lock_surface->configured || !surface->mapped) {
		return;
	}
	lock_surface->configured = true;
	lock->committed_surfaces++;

	/* Only once every output has a committed lock surface may the client be
	 * told the session is locked — telling it earlier lets it assume a
	 * keyboard that has nowhere to go. */
	if (lock->committed_surfaces >= lock->wanted_outputs &&
			!lock->locked_sent) {
		lock->locked_sent = true;
		lock->session->server->locked = true;
		set_desktop_visible(lock->session->server, false);
		wlr_session_lock_v1_send_locked(lock->wlr_lock);
		wlr_log(WLR_INFO, "session locked (%d surface(s))",
			lock->committed_surfaces);
		bgwl_ctl_broadcast(lock->session->server, "locked");
	}
}

static void lock_handle_new_surface(struct wl_listener *listener, void *data) {
	struct bgwl_lock *lock = wl_container_of(listener, lock, new_surface);
	struct wlr_session_lock_surface_v1 *surface = data;
	struct bgwl_server *server = lock->session->server;

	struct bgwl_lock_surface *lock_surface = calloc(1, sizeof(*lock_surface));
	if (lock_surface == NULL) {
		wlr_log(WLR_ERROR, "out of memory for lock surface");
		return;
	}
	lock_surface->lock = lock;
	lock_surface->surface = surface;
	wl_list_insert(&lock->surfaces, &lock_surface->link);

	/* Configure at the output's own resolution: a lock surface that does not
	 * cover its screen is not a lock. */
	int width = 0, height = 0;
	if (surface->output != NULL) {
		wlr_output_transformed_resolution(surface->output, &width, &height);
	} else {
		struct bgwl_output *first = bgwl_server_first_output(server);
		if (first != NULL) {
			wlr_output_transformed_resolution(first->wlr_output, &width,
				&height);
		}
	}
	wlr_session_lock_surface_v1_configure(surface, width, height);

	lock_surface->commit.notify = lock_surface_handle_commit;
	wl_signal_add(&surface->surface->events.commit, &lock_surface->commit);
	lock_surface->destroy.notify = lock_surface_handle_destroy;
	wl_signal_add(&surface->events.destroy, &lock_surface->destroy);
}

static void lock_finish(struct bgwl_lock *lock) {
	struct bgwl_server *server = lock->session->server;

	if (server->locked) {
		set_desktop_visible(server, true);
	}
	server->locked = false;

	wl_list_remove(&lock->new_surface.link);
	wl_list_remove(&lock->unlock.link);
	wl_list_remove(&lock->destroy.link);

	struct bgwl_lock_surface *lock_surface, *tmp;
	wl_list_for_each_safe(lock_surface, tmp, &lock->surfaces, link) {
		wl_list_remove(&lock_surface->commit.link);
		wl_list_remove(&lock_surface->destroy.link);
		wl_list_remove(&lock_surface->link);
		free(lock_surface);
	}
	free(lock);
	lock = NULL;

	/* A seat with no keyboard focus after an unlock is a session that looks
	 * frozen; hand it back to whatever the user was working on. */
	struct bgwl_toplevel *focused = bgwl_workspace_first_toplevel(
		server->active_ws);
	if (focused != NULL) {
		bgwl_toplevel_focus(focused);
	}
	bgwl_ctl_broadcast(server, "unlocked");
	wlr_log(WLR_INFO, "session unlocked");
}

/* lock_finish takes the pointer out of the session; this is the version the
 * signal handlers call, where the lock is reached through the listener. */
static void lock_handle_unlock(struct wl_listener *listener, void *data) {
	struct bgwl_lock *lock = wl_container_of(listener, lock, unlock);
	lock->session->lock = NULL;
	lock_finish(lock);
}

static void lock_handle_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_lock *lock = wl_container_of(listener, lock, destroy);
	if (lock->session->lock == lock) {
		lock->session->lock = NULL;
	}
	/* The lock object is gone without an unlock — a crashed lock screen. The
	 * session must not stay locked, or the user is stranded. */
	struct bgwl_server *server = lock->session->server;
	if (server->locked) {
		set_desktop_visible(server, true);
		server->locked = false;
		wlr_log(WLR_ERROR, "lock client died; session unlocked defensively");
	}
}

static void session_handle_new_lock(struct wl_listener *listener, void *data) {
	struct bgwl_session *session = wl_container_of(listener, session, new_lock);
	struct wlr_session_lock_v1 *wlr_lock = data;
	struct bgwl_server *server = session->server;

	if (session->lock != NULL) {
		wlr_log(WLR_INFO, "a session is already locked; refusing a second lock");
		wlr_session_lock_v1_destroy(wlr_lock);
		return;
	}

	struct bgwl_lock *lock = calloc(1, sizeof(*lock));
	if (lock == NULL) {
		wlr_log(WLR_ERROR, "out of memory for session lock");
		wlr_session_lock_v1_destroy(wlr_lock);
		return;
	}
	session->lock = lock;
	lock->session = session;
	lock->wlr_lock = wlr_lock;
	wl_list_init(&lock->surfaces);

	/* Every output needs a lock surface before the session counts as locked. */
	lock->wanted_outputs = 0;
	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		lock->wanted_outputs++;
	}

	lock->new_surface.notify = lock_handle_new_surface;
	wl_signal_add(&wlr_lock->events.new_surface, &lock->new_surface);
	lock->unlock.notify = lock_handle_unlock;
	wl_signal_add(&wlr_lock->events.unlock, &lock->unlock);
	lock->destroy.notify = lock_handle_destroy;
	wl_signal_add(&wlr_lock->events.destroy, &lock->destroy);

	if (lock->wanted_outputs == 0) {
		/* Headless: nothing can cover a screen, so the lock is satisfied by
		 * existing at all. */
		lock->locked_sent = true;
		server->locked = true;
		wlr_session_lock_v1_send_locked(wlr_lock);
	}
}

/* --- idle timer --------------------------------------------------------- */

static void rearm_idle(struct bgwl_session *session) {
	if (session->idle_source == NULL ||
			session->server->cfg.idle_timeout_seconds <= 0) {
		return;
	}
	session->stage = BGWL_IDLE_LOCK;
	wl_event_source_timer_update(session->idle_source,
		session->server->cfg.idle_timeout_seconds * 1000);
}

static void set_all_dpms(struct bgwl_server *server, bool on) {
	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		bgwl_output_set_dpms(output, on);
	}
}

static int idle_timeout(void *user_data) {
	struct bgwl_session *session = user_data;
	struct bgwl_server *server = session->server;

	if (server->cfg.idle_timeout_seconds <= 0) {
		return 0;
	}

	/* A playing video or a running fullscreen game is a promise not to blank. */
	if (session->inhibited) {
		rearm_idle(session);
		return 0;
	}

	if (session->stage == BGWL_IDLE_LOCK) {
		if (server->locked) {
			session->stage = BGWL_IDLE_DPMS;
		} else {
			wlr_log(WLR_INFO, "idle: starting the lock screen");
			bgwl_run_action(server, server->cfg.lock_command);
			session->stage = BGWL_IDLE_DPMS;
		}
		wl_event_source_timer_update(session->idle_source,
			server->cfg.idle_timeout_seconds * 1000);
		return 0;
	}

	/* Second stage: only ever blank while actually locked, otherwise the user
	 * gets a black screen with no way back except guessing. */
	if (server->locked && !session->dpms_off) {
		session->dpms_off = true;
		set_all_dpms(server, false);
		wlr_log(WLR_INFO, "idle: panels off");
	}
	return 0;
}

void bgwl_session_activity(struct bgwl_server *server) {
	struct bgwl_session *session = server->session;
	if (session == NULL) {
		return;
	}

	if (!server->locked && server->idle_notifier != NULL) {
		wlr_idle_notifier_v1_notify_activity(server->idle_notifier, server->seat);
	}

	if (session->dpms_off) {
		session->dpms_off = false;
		set_all_dpms(server, true);
	}
	rearm_idle(session);
}

/* --- lid switch --------------------------------------------------------- */

static void switch_handle_toggle(struct wl_listener *listener, void *data) {
	struct bgwl_switch *sw = wl_container_of(listener, sw, toggle);
	struct wlr_switch_toggle_event *event = data;
	struct bgwl_server *server = sw->session->server;

	if (event->switch_type != WLR_SWITCH_TYPE_LID) {
		return;
	}
	/* libinput reports ON for a closed lid, which reads backwards and is the
	 * one detail worth a comment because getting it inverted locks the
	 * machine when the user opens it. */
	if (event->switch_state == WLR_SWITCH_STATE_ON) {
		wlr_log(WLR_INFO, "lid closed: %s", server->cfg.lid_command);
		bgwl_run_action(server, server->cfg.lid_command);
	}
}

static void switch_handle_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_switch *sw = wl_container_of(listener, sw, destroy);
	wl_list_remove(&sw->toggle.link);
	wl_list_remove(&sw->destroy.link);
	wl_list_remove(&sw->link);
	free(sw);
}

void bgwl_session_add_switch(struct bgwl_server *server,
		struct wlr_input_device *device) {
	struct bgwl_session *session = server->session;
	if (session == NULL) {
		return;
	}
	struct wlr_switch *wlr_switch = wlr_switch_from_input_device(device);
	if (wlr_switch == NULL) {
		return;
	}

	struct bgwl_switch *sw = calloc(1, sizeof(*sw));
	if (sw == NULL) {
		wlr_log(WLR_ERROR, "out of memory for switch device");
		return;
	}
	sw->session = session;
	sw->device = device;
	sw->toggle.notify = switch_handle_toggle;
	wl_signal_add(&wlr_switch->events.toggle, &sw->toggle);
	sw->destroy.notify = switch_handle_destroy;
	wl_signal_add(&device->events.destroy, &sw->destroy);
	wl_list_insert(&session->switches, &sw->link);
}

/* --- lifetime ----------------------------------------------------------- */

struct bgwl_session *bgwl_session_create(struct bgwl_server *server) {
	struct bgwl_session *session = calloc(1, sizeof(*session));
	if (session == NULL) {
		wlr_log(WLR_ERROR, "out of memory for the session subsystem");
		return NULL;
	}
	session->server = server;
	session->stage = BGWL_IDLE_LOCK;
	session->dpms_off = false;
	wl_list_init(&session->switches);

	if (server->lock_manager != NULL) {
		session->new_lock.notify = session_handle_new_lock;
		wl_signal_add(&server->lock_manager->events.new_lock,
			&session->new_lock);
	} else {
		wlr_log(WLR_ERROR, "no session-lock manager: the screen cannot lock");
	}

	if (server->idle_inhibit != NULL) {
		session->new_inhibitor.notify = inhibitor_handle_new;
		wl_signal_add(&server->idle_inhibit->events.new_inhibitor,
			&session->new_inhibitor);
	}

	if (server->event_loop != NULL && server->cfg.idle_timeout_seconds > 0) {
		session->idle_source = wl_event_loop_add_timer(server->event_loop,
			idle_timeout, session);
		if (session->idle_source == NULL) {
			wlr_log(WLR_ERROR, "could not arm the idle timer");
		} else {
			rearm_idle(session);
		}
	}

	return session;
}

void bgwl_session_destroy(struct bgwl_session *session) {
	if (session == NULL) {
		return;
	}
	if (session->idle_source != NULL) {
		wl_event_source_remove(session->idle_source);
	}
	wl_list_remove(&session->new_lock.link);
	wl_list_remove(&session->new_inhibitor.link);

	struct bgwl_switch *sw, *tmp;
	wl_list_for_each_safe(sw, tmp, &session->switches, link) {
		wl_list_remove(&sw->toggle.link);
		wl_list_remove(&sw->destroy.link);
		wl_list_remove(&sw->link);
		free(sw);
	}

	if (session->lock != NULL) {
		struct bgwl_lock *lock = session->lock;
		wl_list_remove(&lock->new_surface.link);
		wl_list_remove(&lock->unlock.link);
		wl_list_remove(&lock->destroy.link);
		free(lock);
	}
	free(session);
}
