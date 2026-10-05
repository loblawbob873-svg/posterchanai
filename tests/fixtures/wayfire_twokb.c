/* Two virtual keyboards = two input devices, the shape of a keyboard whose N-key-rollover interface
 * carries the letters while another interface carries Super. Usage: twokb <same|split>
 *   same : Super and G both on keyboard A (an ordinary keyboard)
 *   split: Super on keyboard A, G on keyboard B (ROG Strix Scope II 96 in NKRO mode) */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "vk.h"
static struct wl_seat *seat; static struct zwp_virtual_keyboard_manager_v1 *mgr;
static void reg(void *d, struct wl_registry *r, uint32_t n, const char *i, uint32_t v){
  if(!strcmp(i, wl_seat_interface.name)) seat = wl_registry_bind(r, n, &wl_seat_interface, 1);
  else if(!strcmp(i, zwp_virtual_keyboard_manager_v1_interface.name)) mgr = wl_registry_bind(r, n, &zwp_virtual_keyboard_manager_v1_interface, 1);
}
static void unreg(void *d, struct wl_registry *r, uint32_t n){}
static const struct wl_registry_listener rl = { reg, unreg };
static struct zwp_virtual_keyboard_v1 *make(struct wl_display *dpy, const char *km){
  struct zwp_virtual_keyboard_v1 *k = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(mgr, seat);
  size_t n = strlen(km) + 1; int fd = memfd_create("km", 0); if(ftruncate(fd, n)) exit(3);
  char *p = mmap(0, n, PROT_WRITE, MAP_SHARED, fd, 0); memcpy(p, km, n); munmap(p, n);
  zwp_virtual_keyboard_v1_keymap(k, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd, n); close(fd);
  wl_display_roundtrip(dpy); return k;
}
static uint32_t t = 1;
static void key(struct wl_display *dpy, struct zwp_virtual_keyboard_v1 *k, uint32_t code, int down, uint32_t mods){
  zwp_virtual_keyboard_v1_key(k, t++, code, down);
  zwp_virtual_keyboard_v1_modifiers(k, mods, 0, 0, 0);
  wl_display_roundtrip(dpy); usleep(30000);
}
int main(int c, char **v){
  int split = c > 1 && !strcmp(v[1], "split"), taponly = c > 1 && !strcmp(v[1], "tap");
  struct wl_display *dpy = wl_display_connect(NULL); if(!dpy) return 2;
  wl_registry_add_listener(wl_display_get_registry(dpy), &rl, 0); wl_display_roundtrip(dpy);
  if(!seat || !mgr) return 4;
  struct xkb_context *ctx = xkb_context_new(0);
  struct xkb_keymap *map = xkb_keymap_new_from_names(ctx, NULL, 0);
  char *km = xkb_keymap_get_as_string(map, XKB_KEYMAP_FORMAT_TEXT_V1);
  uint32_t logo = 1u << xkb_keymap_mod_get_index(map, XKB_MOD_NAME_LOGO);
  struct zwp_virtual_keyboard_v1 *a = make(dpy, km), *b = split ? make(dpy, km) : a;
  const uint32_t META = 125, G = 34;          /* evdev codes */
  key(dpy, a, META, 1, logo);                 /* Super down on A: A's modifiers say Logo */
  if(!taponly){
    key(dpy, b, G, 1, split ? 0 : logo);      /* G on B: B itself holds no modifier */
    key(dpy, b, G, 0, split ? 0 : logo);
  }
  key(dpy, a, META, 0, 0);
  wl_display_roundtrip(dpy); return 0;
}
