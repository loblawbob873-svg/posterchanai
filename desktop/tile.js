'use strict';
/* WINDOW LAYOUTS FOR "ARRANGE" -- pure, so the arithmetic is tested without a compositor.
 *
 * Asked for: "window tiling support so we can do 4 windows in a grid", "an easy way to grid
 * everything on a desktop or split". Given a work area (the monitor minus the taskbar) and how many
 * windows are on it, answer one rectangle per window, most recently used first:
 *
 *   grid          1 → the whole area · 2 → side by side · 3 → one tall on the left, two stacked on
 *                 the right · 4 → 2×2 · 5+ → an even grid, the last row stretched to fill
 *   side-by-side  n equal columns
 *   stacked       n equal rows
 *
 * Integer rectangles that tile the area exactly: every pixel belongs to one window and none to two,
 * which is what makes adjacent windows meet instead of overlapping or leaving a seam.
 */
const LAYOUTS = ['grid', 'side-by-side', 'stacked'];

function split(start, total, parts){
  const out = [];
  for(let i = 0; i < parts; i++){
    const a = start + Math.round(total * i / parts), b = start + Math.round(total * (i + 1) / parts);
    out.push([a, b - a]);
  }
  return out;
}

function tileRects(layout, count, work){
  const n = Math.max(0, Math.min(64, Math.floor(Number(count) || 0)));
  const x = Math.round(Number(work && work.x) || 0), y = Math.round(Number(work && work.y) || 0);
  const w = Math.max(1, Math.round(Number(work && (work.width ?? work.w)) || 1));
  const h = Math.max(1, Math.round(Number(work && (work.height ?? work.h)) || 1));
  if(!n || !LAYOUTS.includes(layout)) return [];
  if(layout === 'side-by-side') return split(x, w, n).map(([cx, cw]) => ({ x: cx, y, w: cw, h }));
  if(layout === 'stacked') return split(y, h, n).map(([cy, ch]) => ({ x, y: cy, w, h: ch }));
  if(n === 1) return [{ x, y, w, h }];
  if(n === 2) return split(x, w, 2).map(([cx, cw]) => ({ x: cx, y, w: cw, h }));
  if(n === 3){
    const [[lx, lw], [rx, rw]] = split(x, w, 2), rows = split(y, h, 2);
    return [{ x: lx, y, w: lw, h }, ...rows.map(([ry, rh]) => ({ x: rx, y: ry, w: rw, h: rh }))];
  }
  const cols = Math.ceil(Math.sqrt(n)), rowsN = Math.ceil(n / cols), rows = split(y, h, rowsN), out = [];
  for(let r = 0; r < rowsN; r++){
    const inRow = r === rowsN - 1 ? n - cols * (rowsN - 1) : cols;
    for(const [cx, cw] of split(x, w, inRow)) out.push({ x: cx, y: rows[r][0], w: cw, h: rows[r][1] });
  }
  return out;
}

module.exports = { tileRects, LAYOUTS };
