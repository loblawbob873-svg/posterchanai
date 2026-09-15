/* A real Wayland surface receiving relative-pointer events in the private test
 * compositor. Honor fullscreen sizes so edge coordinates focus this client, and
 * optionally request a native lock. */
#define _GNU_SOURCE
#include "pointer-constraints-client-protocol.h"
#include "relative-pointer-client-protocol.h"
#include "xdg-shell-client-protocol.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct wl_seat *seat;
static struct xdg_wm_base *wm;
static struct zwp_relative_pointer_manager_v1 *manager;
static struct wl_surface *surface;
static struct zwp_pointer_constraints_v1 *constraints;
static struct wl_buffer *buffer;
static int width = 400, height = 300;
static void top_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h,
                          struct wl_array *a) {
  if (w > 0)
    width = w;
  if (h > 0)
    height = h;
}
static void top_close(void *d, struct xdg_toplevel *t) {}
static const struct xdg_toplevel_listener topl = {top_configure, top_close};
static void ping(void *d, struct xdg_wm_base *w, uint32_t s) {
  xdg_wm_base_pong(w, s);
}
static const struct xdg_wm_base_listener wml = {ping};
static void global(void *d, struct wl_registry *r, uint32_t n, const char *i,
                   uint32_t v) {
  if (!strcmp(i, "zwp_pointer_constraints_v1"))
    constraints =
        wl_registry_bind(r, n, &zwp_pointer_constraints_v1_interface, 1);
  if (!strcmp(i, "wl_compositor"))
    compositor = wl_registry_bind(r, n, &wl_compositor_interface, 4);
  if (!strcmp(i, "wl_shm"))
    shm = wl_registry_bind(r, n, &wl_shm_interface, 1);
  if (!strcmp(i, "wl_seat"))
    seat = wl_registry_bind(r, n, &wl_seat_interface, 1);
  if (!strcmp(i, "xdg_wm_base"))
    wm = wl_registry_bind(r, n, &xdg_wm_base_interface, 1);
  if (!strcmp(i, "zwp_relative_pointer_manager_v1"))
    manager =
        wl_registry_bind(r, n, &zwp_relative_pointer_manager_v1_interface, 1);
}
static void removed(void *d, struct wl_registry *r, uint32_t n) {}
static const struct wl_registry_listener registry = {global, removed};
static void configure(void *d, struct xdg_surface *s, uint32_t serial) {
  xdg_surface_ack_configure(s, serial);
  int fd = memfd_create("relative-test", 0);
  if (fd < 0 || ftruncate(fd, width * height * 4) < 0)
    exit(5);
  void *pixels =
      mmap(NULL, width * height * 4, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (pixels == MAP_FAILED)
    exit(6);
  memset(pixels, 255, width * height * 4);
  struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, width * height * 4);
  buffer = wl_shm_pool_create_buffer(pool, 0, width, height, width * 4,
                                     WL_SHM_FORMAT_XRGB8888);
  wl_shm_pool_destroy(pool);
  munmap(pixels, width * height * 4);
  close(fd);
  wl_surface_attach(surface, buffer, 0, 0);
  wl_surface_damage(surface, 0, 0, width, height);
  wl_surface_commit(surface);
}
static const struct xdg_surface_listener xdgl = {configure};
static void motion(void *d, struct zwp_relative_pointer_v1 *p, uint32_t hi,
                   uint32_t lo, wl_fixed_t dx, wl_fixed_t dy, wl_fixed_t ux,
                   wl_fixed_t uy) {
  printf("%.6f %.6f %.6f %.6f\n", wl_fixed_to_double(dx),
         wl_fixed_to_double(dy), wl_fixed_to_double(ux),
         wl_fixed_to_double(uy));
  fflush(stdout);
}
static const struct zwp_relative_pointer_v1_listener rel = {motion};
int main() {
  struct wl_display *display = wl_display_connect(NULL);
  if (!display)
    return 2;
  wl_registry_add_listener(wl_display_get_registry(display), &registry, NULL);
  wl_display_roundtrip(display);
  if (!compositor || !shm || !seat || !wm || !manager)
    return 3;
  xdg_wm_base_add_listener(wm, &wml, NULL);
  surface = wl_compositor_create_surface(compositor);
  struct xdg_surface *xs = xdg_wm_base_get_xdg_surface(wm, surface);
  xdg_surface_add_listener(xs, &xdgl, NULL);
  struct xdg_toplevel *top = xdg_surface_get_toplevel(xs);
  xdg_toplevel_add_listener(top, &topl, NULL);
  xdg_toplevel_set_title(top, "relative-test");
  struct wl_pointer *pointer = wl_seat_get_pointer(seat);
  if (getenv("TEST_NATIVE_LOCK")) {
    if (!constraints)
      return 4;
    zwp_pointer_constraints_v1_lock_pointer(
        constraints, surface, pointer, NULL,
        ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT);
  }
  struct zwp_relative_pointer_v1 *relative =
      zwp_relative_pointer_manager_v1_get_relative_pointer(manager, pointer);
  zwp_relative_pointer_v1_add_listener(relative, &rel, NULL);
  wl_surface_commit(surface);
  while (wl_display_dispatch(display) >= 0) {
  }
  return 0;
}
