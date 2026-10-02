import type { Point } from "./types"

/*
 * Draws the canvas's connections as right-angled lines that go round the
 * steps rather than through them. Every connection is routed at once, so
 * lines that would run on top of each other are spread onto tracks of
 * their own, ordered so they cross as little as they can. A loop's body
 * comes back over the top of the body, down into the loop. Labels go on a
 * stretch of their line where they cover no step and no other label.
 */

export type Box = { x: number; y: number; width: number; height: number }

export type RouteRequest = {
  id: string
  source: string
  target: string
  /** Where it leaves: the outer edge of the source's way out, on its right. */
  from: Point
  /** Where it arrives: the outer edge of the target's way in, on its left. */
  to: Point
  /** A loop's body coming back to the loop: drawn over the body, into the loop's top. */
  loop?: string
  /** Lines of one bundle into the same step may share their last stretch. */
  bundle: string
  label?: {
    text: string
    /** Room kept for a leading icon. */
    icon?: boolean
    /** Its measured width; estimated from the words without one. */
    width?: number
    /** On show whenever there's room, not only on long lines. */
    always?: boolean
  }
}

export type EdgeRoute = {
  /** The corners, from the way out to the way in (or the loop's top). */
  points: Point[]
  /** Where its label sits (the centre) and how wide it is, when there was room. */
  label?: Point & { width: number }
  /** Whether the label shows without the line being hovered or selected. */
  labelShown: boolean
  /** A spot on the line for a chip about it (removing it, inserting a step). */
  anchor: Point
}

/** How far lines keep from steps. */
const CLEAR = 14
/** The straight run out of a way out, or into a way in, before a bend. */
const STUB = 20
/** The shortest run a nudged line keeps at either end. */
const MIN_STUB = 10
/** Between side-by-side lines. */
export const TRACK = 10
/** How far above a loop and its body the way back runs. */
export const LANE = 30
/** Lines at least this long label their branch without being hovered. */
const LONG = 260
/** What a bend and a crossing cost a way, in pixels of length. */
const BEND = 24
const CROSSING = 240
/** The most corridors tried for one line. */
const MAX_CORRIDORS = 16
export const LABEL_HEIGHT = 18

type Rect = {
  id: string
  left: number
  top: number
  right: number
  bottom: number
}
type Route = { request: RouteRequest; points: Point[] }

const EPS = 0.5

/** A label's width, from its words: 10px medium text in a chip. */
export const labelWidth = (text: string, icon = false) =>
  Math.round(text.length * 5.9 + 14 + (icon ? 13 : 0))

function rectOf(id: string, box: Box, by: number): Rect {
  return {
    id,
    left: box.x - by,
    top: box.y - by,
    right: box.x + box.width + by,
    bottom: box.y + box.height + by,
  }
}

/** The first obstacle a horizontal stretch runs through, if any, but the `skip` steps. */
function hHit(y: number, xa: number, xb: number, rects: Rect[], ...skip: string[]) {
  const lo = Math.min(xa, xb)
  const hi = Math.max(xa, xb)
  return rects.find(
    (r) => !skip.includes(r.id) && r.top < y && y < r.bottom && lo < r.right && hi > r.left
  )
}

/** The first obstacle a vertical stretch runs through, if any, but the `skip` steps. */
function vHit(x: number, ya: number, yb: number, rects: Rect[], ...skip: string[]) {
  const lo = Math.min(ya, yb)
  const hi = Math.max(ya, yb)
  return rects.find(
    (r) => !skip.includes(r.id) && r.left < x && x < r.right && lo < r.bottom && hi > r.top
  )
}

/** The x-ranges in [lo, hi] a vertical stretch over [ya, yb] can stand in. */
function freeColumns(lo: number, hi: number, ya: number, yb: number, rects: Rect[]) {
  const blocked = rects
    .filter((r) => r.top < yb && r.bottom > ya && r.right > lo && r.left < hi)
    .map((r) => [Math.max(lo, r.left), Math.min(hi, r.right)] as const)
    .sort((a, b) => a[0] - b[0])
  const free: [number, number][] = []
  let at = lo
  for (const [a, b] of blocked) {
    if (a > at) free.push([at, a])
    at = Math.max(at, b)
  }
  if (at < hi) free.push([at, hi])
  return free
}

/**
 * Where a bend may stand in each free range [a, b] of [lo, hi]: a line
 * that fans out of its step bends in the first range, halfway across the
 * gap; one joining others at a step bends just before it, so they share
 * as little of the way in as they can.
 */
function columnCandidates(
  free: [number, number][],
  lo: number,
  hi: number,
  prefer: "source" | "target"
) {
  const out: number[] = []
  const ranges = prefer === "source" ? free : [...free].reverse()
  for (const [a, b] of ranges) {
    const inset = Math.min(30, (b - a) / 2)
    const near =
      prefer === "source"
        ? [a === lo ? Math.min((a + b) / 2, a + 40) : a + inset, (a + b) / 2, b - inset]
        : [b === hi ? Math.max(a, b - 4) : b - inset, (a + b) / 2, a + inset]
    for (const x of near) if (!out.some((o) => Math.abs(o - x) < 1)) out.push(x)
  }
  return out
}

/** Drops repeated corners and corners on a straight run. */
function simplify(points: Point[]) {
  const out: Point[] = []
  for (const p of points) {
    const last = out.at(-1)
    if (last && Math.abs(last.x - p.x) < EPS && Math.abs(last.y - p.y) < EPS) continue
    const before = out.at(-2)
    if (
      before &&
      last &&
      ((Math.abs(before.x - last.x) < EPS && Math.abs(last.x - p.x) < EPS) ||
        (Math.abs(before.y - last.y) < EPS && Math.abs(last.y - p.y) < EPS))
    ) {
      out[out.length - 1] = p
      continue
    }
    out.push(p)
  }
  return out
}

/**
 * Ways round a corridor: out of the way out's row at x1, along yc, into
 * the way in's row at x2; one for each corridor, nearest the rows first.
 */
function corridorRoutes(request: RouteRequest, rects: Rect[]): Point[][] {
  const { from: s, to: t, source, target } = request
  const back = t.x < s.x + 2 * STUB
  const left = Math.min(s.x, t.x) - 80
  const right = Math.max(s.x, t.x) + 80
  const near = rects.filter((r) => r.right > left && r.left < right)
  // Bends keep at least a stub from the ways they leave and reach.
  const x1s = [s.x + STUB, ...near.map((r) => r.right + 4)]
    .filter((x) => x >= s.x + STUB)
    .sort((a, b) => a - b)
  const x2s = [t.x - STUB, ...near.map((r) => r.left - 4)]
    .filter((x) => x <= t.x - STUB)
    .sort((a, b) => b - a)
  const ys = new Set([s.y, t.y])
  for (const r of near) {
    ys.add(r.top - 2)
    ys.add(r.bottom + 2)
  }
  const detour = (y: number) => Math.abs(y - s.y) + Math.abs(y - t.y)
  // The nearest corridors, and the ways over and under everything nearby,
  // which cross the fewest lines.
  const sorted = [...ys].sort((a, b) => detour(a) - detour(b))
  const tried = [...sorted.slice(0, MAX_CORRIDORS), Math.min(...ys), Math.max(...ys)]
  const found: Point[][] = []
  for (const yc of new Set(tried)) {
    const x1 = x1s.find((x) => !hHit(s.y, s.x, x, rects, source) && !vHit(x, s.y, yc, rects))
    if (x1 === undefined) continue
    const x2 = x2s.find(
      (x) => (back || x >= x1) && !vHit(x, yc, t.y, rects) && !hHit(t.y, x, t.x, rects, target)
    )
    if (x2 === undefined || hHit(yc, x1, x2, rects)) continue
    found.push(
      simplify([s, { x: x1, y: s.y }, { x: x1, y: yc }, { x: x2, y: yc }, { x: x2, y: t.y }, t])
    )
  }
  return found
}

/** A stretch of a line already drawn, and whose it is. */
type Drawn = { a: Point; b: Point; request: RouteRequest; seen: number }

/** The lines drawn so far, filed by where they run so a new line only meets its neighbours. */
class DrawnLines {
  private cells = new Map<string, Drawn[]>()
  private stamp = 0
  private static SIZE = 160

  private *keys(a: Point, b: Point) {
    const size = DrawnLines.SIZE
    const [x0, x1] = [Math.floor(Math.min(a.x, b.x) / size), Math.floor(Math.max(a.x, b.x) / size)]
    const [y0, y1] = [Math.floor(Math.min(a.y, b.y) / size), Math.floor(Math.max(a.y, b.y) / size)]
    for (let x = x0; x <= x1; x += 1) for (let y = y0; y <= y1; y += 1) yield `${x}:${y}`
  }

  add(route: Route) {
    route.points.slice(1).forEach((b, i) => {
      const d: Drawn = {
        a: route.points[i],
        b,
        request: route.request,
        seen: 0,
      }
      for (const key of this.keys(d.a, b)) {
        const list = this.cells.get(key)
        if (list) list.push(d)
        else this.cells.set(key, [d])
      }
    })
  }

  /** Each line stretch near the one from `a` to `b`, once. */
  near(a: Point, b: Point) {
    this.stamp += 1
    const out: Drawn[] = []
    for (const key of this.keys(a, b)) {
      for (const d of this.cells.get(key) ?? []) {
        if (d.seen === this.stamp) continue
        d.seen = this.stamp
        out.push(d)
      }
    }
    return out
  }
}

/**
 * How many lines already drawn a way would cross. Lines into the same step
 * in the same colour, or out of the same way, join rather than cross.
 * (Lines that would run along each other are spread onto tracks of their
 * own afterwards; see `spread`.)
 */
function crossingsWith(points: Point[], request: RouteRequest, drawn: DrawnLines) {
  let count = 0
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1]
    const b = points[i]
    const vertical = Math.abs(a.x - b.x) < EPS
    for (const d of drawn.near(a, b)) {
      const other = d.request
      if (
        (other.target === request.target && other.bundle === request.bundle) ||
        (other.source === request.source && other.from.y === request.from.y)
      ) {
        continue
      }
      if (vertical === Math.abs(d.a.x - d.b.x) < EPS) continue
      const [v, h] = vertical
        ? [
            [a, b],
            [d.a, d.b],
          ]
        : [
            [d.a, d.b],
            [a, b],
          ]
      const x = v[0].x
      const y = h[0].y
      if (
        x > Math.min(h[0].x, h[1].x) + EPS &&
        x < Math.max(h[0].x, h[1].x) - EPS &&
        y > Math.min(v[0].y, v[1].y) + EPS &&
        y < Math.max(v[0].y, v[1].y) - EPS
      ) {
        count += 1
      }
    }
  }
  return count
}

const lengthOf = (points: Point[]) =>
  points.reduce(
    (sum, p, i) =>
      i ? sum + Math.abs(p.x - points[i - 1].x) + Math.abs(p.y - points[i - 1].y) : 0,
    0
  )

/**
 * A connection's way, from those that go round every step: the shortest
 * with the fewest bends and crossings. A line that fans out of its step
 * bends near it; one joining others at a step bends near that step.
 */
function routeOf(
  request: RouteRequest,
  rects: Rect[],
  drawn: DrawnLines,
  prefer: "source" | "target"
): Point[] {
  const { from: s, to: t, source, target } = request
  const options: { points: Point[]; lean: number }[] = []
  const lo = s.x + STUB
  const hi = t.x - STUB
  if (Math.abs(s.y - t.y) < 1 && t.x > s.x && !hHit(s.y, s.x, t.x, rects, source, target)) {
    options.push({ points: [s, t], lean: 0 })
  }
  if (hi >= lo) {
    const free = freeColumns(lo, hi, Math.min(s.y, t.y), Math.max(s.y, t.y), rects)
    columnCandidates(free, lo, hi, prefer).forEach((x, order) => {
      if (hHit(s.y, s.x, x, rects, source) || hHit(t.y, x, t.x, rects, target)) return
      options.push({
        points: [s, { x, y: s.y }, { x, y: t.y }, t],
        lean: order,
      })
    })
  }
  // Cheapest first; crossings are counted only while an option could still
  // win. Ways round a corridor are only looked for when the simpler ways
  // are blocked or cross something.
  type Option = { points: Point[]; lean: number }
  type Best = { points: Point[]; cost: number; crossed: number }
  const pick = (list: Option[], best?: Best) => {
    const ranked = list
      .map((o) => ({
        ...o,
        base: lengthOf(o.points) + BEND * (o.points.length - 2) + o.lean,
      }))
      .sort((a, b) => a.base - b.base)
    for (const option of ranked) {
      if (best && option.base >= best.cost) break
      const crossed = crossingsWith(option.points, request, drawn)
      const cost = option.base + CROSSING * crossed
      if (!best || cost < best.cost) best = { points: option.points, cost, crossed }
    }
    return best
  }
  let best = pick(options)
  if (!best || best.crossed > 0) {
    const round = corridorRoutes(request, rects).map((points) => ({
      points,
      lean: 0,
    }))
    best = pick(round, best)
  }
  if (best) return best.points
  // Nowhere clear: bend halfway, as a plain step line would.
  const x = hi >= lo ? (s.x + t.x) / 2 : s.x + STUB
  const y = (s.y + t.y) / 2
  return hi >= lo
    ? [s, { x, y: s.y }, { x, y: t.y }, t]
    : [s, { x, y: s.y }, { x, y }, { x: t.x - STUB, y }, { x: t.x - STUB, y: t.y }, t]
}

/**
 * The ways back of one loop: each rises from its step to a lane over the
 * loop and its body, and the lane comes down into the loop's top.
 */
function loopRoutes(
  loop: Box & { id: string },
  body: Box[],
  requests: RouteRequest[],
  rects: Rect[]
): Map<string, Point[]> {
  const end = { x: loop.x + loop.width / 2, y: loop.y }
  let lane = Math.min(loop.y, ...body.map((b) => b.y)) - LANE
  let risers = new Map<string, number>()
  for (let attempt = 0; attempt < 12; attempt += 1) {
    risers = new Map()
    for (const r of requests) {
      const candidates = [
        r.from.x + STUB,
        ...rects.filter((o) => o.left > r.from.x - 1).map((o) => o.right + 4),
      ].sort((a, b) => a - b)
      const x =
        candidates.find(
          (c) => !hHit(r.from.y, r.from.x, c, rects, r.source) && !vHit(c, r.from.y, lane, rects)
        ) ?? r.from.x + STUB
      risers.set(r.id, x)
    }
    const xs = [end.x, ...risers.values()]
    // A step in the lane's way: over it. (One between the lane and the
    // loop's top can't be gone round; the line crosses it.)
    const blocker = hHit(lane, Math.min(...xs), Math.max(...xs), rects)
    if (!blocker) break
    lane = blocker.top - 2
  }
  return new Map(
    requests.map((r) => {
      const x = risers.get(r.id)!
      return [
        r.id,
        simplify([r.from, { x, y: r.from.y }, { x, y: lane }, { x: end.x, y: lane }, end]),
      ]
    })
  )
}

/* -------------------------------------------------------------------------- */
/* Spreading lines that share a stretch                                       */
/* -------------------------------------------------------------------------- */

type Axis = "v" | "h"
/** Along the axis a segment moves on (x for a vertical one), and along its run. */
const pos = (p: Point, axis: Axis) => (axis === "v" ? p.x : p.y)
const run = (p: Point, axis: Axis) => (axis === "v" ? p.y : p.x)

type Segment = {
  route: Route
  /** Its first corner's index: it runs from points[i] to points[i + 1]. */
  i: number
  key: string
  at: number
  lo: number
  hi: number
  /** Where lines meet it, and on which side they leave: -1 before `at`, 1 after. */
  joins: { at: number; side: -1 | 1 }[]
  /** How far it may move. */
  min: number
  max: number
}

type Item = {
  segments: Segment[]
  at: number
  lo: number
  hi: number
  joins: Segment["joins"]
  min: number
  max: number
}

function segmentsOf(routes: Route[], axis: Axis, rects: Rect[]): Segment[] {
  const out: Segment[] = []
  for (const route of routes) {
    const p = route.points
    const { request } = route
    // Only middle stretches move: the first and last are fixed to their ways.
    for (let i = 1; i < p.length - 2; i += 1) {
      const a = p[i]
      const b = p[i + 1]
      if (
        Math.abs(pos(a, axis) - pos(b, axis)) > EPS ||
        Math.abs(run(a, axis) - run(b, axis)) < 1
      ) {
        continue
      }
      const at = pos(a, axis)
      const lo = Math.min(run(a, axis), run(b, axis))
      const hi = Math.max(run(a, axis), run(b, axis))
      const last = i === p.length - 3
      const key = request.loop
        ? `loop:${request.loop}:${axis}:${i}`
        : last && axis === "v"
          ? `in:${request.target}:${request.bundle}`
          : i === 1 && axis === "v"
            ? `out:${request.source}:${Math.round(request.from.y)}`
            : `${request.id}:${i}`
      const before = pos(p[i - 1], axis)
      const after = pos(p[i + 2], axis)
      let min = before < at ? before + (i === 1 ? MIN_STUB : 0) : -Infinity
      let max = before > at ? before - (i === 1 ? MIN_STUB : 0) : Infinity
      if (after > at) max = Math.min(max, after - (i + 2 === p.length - 1 ? MIN_STUB + 4 : 0))
      else min = Math.max(min, after + (i + 2 === p.length - 1 ? MIN_STUB + 4 : 0))
      // It keeps clear of the steps beside it.
      for (const r of rects) {
        const [rLo, rHi, rMin, rMax] =
          axis === "v" ? [r.top, r.bottom, r.left, r.right] : [r.left, r.right, r.top, r.bottom]
        if (rLo >= hi || rHi <= lo) continue
        if (rMax <= at + EPS) min = Math.max(min, rMax)
        else if (rMin >= at - EPS) max = Math.min(max, rMin)
      }
      out.push({
        route,
        i,
        key,
        at,
        lo,
        hi,
        joins: [
          { at: run(a, axis), side: before < at ? -1 : 1 },
          { at: run(b, axis), side: after > at ? 1 : -1 },
        ],
        min,
        max,
      })
    }
  }
  return out
}

/** How many times two side-by-side lines cross, with `a` placed before `b`. */
function crossings(a: Item, b: Item) {
  let count = 0
  for (const j of a.joins) if (j.side === 1 && j.at > b.lo + EPS && j.at < b.hi - EPS) count += 1
  for (const j of b.joins) if (j.side === -1 && j.at > a.lo + EPS && j.at < a.hi - EPS) count += 1
  return count
}

function orderItems(items: Item[]): Item[] {
  if (items.length <= 6) {
    let best = items
    let bestScore = Infinity
    const permute = (list: Item[], rest: Item[]) => {
      if (!rest.length) {
        let score = 0
        for (let i = 0; i < list.length; i += 1) {
          for (let j = i + 1; j < list.length; j += 1) score += crossings(list[i], list[j])
        }
        // Ties keep lines near where they were.
        score += list.reduce((sum, item, i) => sum + Math.abs(i - items.indexOf(item)) * 1e-3, 0)
        if (score < bestScore) {
          bestScore = score
          best = list
        }
        return
      }
      rest.forEach((item, i) =>
        permute([...list, item], [...rest.slice(0, i), ...rest.slice(i + 1)])
      )
    }
    permute([], items)
    return best
  }
  return [...items].sort((a, b) => crossings(a, b) - crossings(b, a) || a.at - b.at)
}

/** Spreads lines that would run on top of each other onto tracks of their own. */
function spread(routes: Route[], axis: Axis, rects: Rect[]) {
  // Spreading a group can bring it beside another line: again until settled.
  for (let pass = 0; pass < 4; pass += 1) {
    if (!spreadOnce(routes, axis, rects)) return
  }
}

/** One pass of `spread`; whether it moved anything. */
function spreadOnce(routes: Route[], axis: Axis, rects: Rect[]) {
  let moved = false
  const segments = segmentsOf(routes, axis, rects)
  // Stretches of one key close together share a track.
  const items: Item[] = []
  for (const segment of [...segments].sort((a, b) => a.at - b.at)) {
    const same = items.find(
      (item) => item.segments[0].key === segment.key && Math.abs(item.at - segment.at) < TRACK
    )
    if (same) {
      same.segments.push(segment)
      same.lo = Math.min(same.lo, segment.lo)
      same.hi = Math.max(same.hi, segment.hi)
      same.joins.push(...segment.joins)
      same.min = Math.max(same.min, segment.min)
      same.max = Math.min(same.max, segment.max)
    } else {
      items.push({
        segments: [segment],
        at: segment.at,
        lo: segment.lo,
        hi: segment.hi,
        joins: [...segment.joins],
        min: segment.min,
        max: segment.max,
      })
    }
  }
  // Items too close that overlap along their run form a group to spread.
  const parent = items.map((_, i) => i)
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])))
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      const a = items[i]
      const b = items[j]
      if (Math.abs(a.at - b.at) >= TRACK - EPS) continue
      if (a.lo < b.hi + 2 && b.lo < a.hi + 2) parent[find(i)] = find(j)
    }
  }
  const groups = new Map<number, Item[]>()
  items.forEach((item, i) => {
    const root = find(i)
    const list = groups.get(root)
    if (list) list.push(item)
    else groups.set(root, [item])
  })
  for (const group of groups.values()) {
    if (group.length < 2) continue
    const ordered = orderItems(group)
    const k = ordered.length
    const centre = ordered.reduce((sum, item) => sum + item.at, 0) / k
    const min = Math.max(...ordered.map((item) => item.min))
    const max = Math.min(...ordered.map((item) => item.max))
    const room = max - min
    const gap =
      Number.isFinite(room) && room < (k - 1) * TRACK ? Math.max(3, room / (k - 1)) : TRACK
    let start = centre - ((k - 1) * gap) / 2
    if (Number.isFinite(min)) start = Math.max(start, min)
    if (Number.isFinite(max)) start = Math.min(start, max - (k - 1) * gap)
    const at = ordered.map((_, i) => start + i * gap)
    // Each keeps within its own reach, in order.
    for (let i = 0; i < k; i += 1) {
      at[i] = Math.max(at[i], ordered[i].min, i > 0 ? at[i - 1] + 3 : -Infinity)
    }
    for (let i = k - 1; i >= 0; i -= 1) {
      at[i] = Math.min(at[i], ordered[i].max, i < k - 1 ? at[i + 1] - 3 : Infinity)
    }
    ordered.forEach((item, i) => {
      for (const { route, i: index } of item.segments) {
        const p = route.points
        if (Math.abs(pos(p[index], axis) - at[i]) > EPS) moved = true
        if (axis === "v") {
          p[index] = { ...p[index], x: at[i] }
          p[index + 1] = { ...p[index + 1], x: at[i] }
        } else {
          p[index] = { ...p[index], y: at[i] }
          p[index + 1] = { ...p[index + 1], y: at[i] }
        }
      }
    })
  }
  return moved
}

/* -------------------------------------------------------------------------- */
/* Labels                                                                     */
/* -------------------------------------------------------------------------- */

type Spot = Point
type Area = { left: number; top: number; right: number; bottom: number }

const overlaps = (a: Area, b: Area) =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom

/**
 * Where on its line a label could sit, best first: a loop's way back on
 * its lane, a loop's way in on its way out of the loop; any other on its
 * way into the step it reaches, then along the way. Never just out of its
 * step, where the branch's own row already names it.
 */
function spotsFor(route: Route, width: number): Spot[] {
  const p = route.points
  const last = p.length - 2
  const spots: Spot[] = []
  const horizontal = (i: number) => Math.abs(p[i].y - p[i + 1].y) < EPS
  const lengthAt = (i: number) => Math.abs(p[i].x - p[i + 1].x) + Math.abs(p[i].y - p[i + 1].y)
  const along = (i: number, startPad: number, endPad: number, where: "middle" | "end") => {
    const forwards = p[i].x < p[i + 1].x
    const a = Math.min(p[i].x, p[i + 1].x) + (forwards ? startPad : endPad)
    const b = Math.max(p[i].x, p[i + 1].x) - (forwards ? endPad : startPad)
    if (b - a < width) return
    const y = p[i].y
    if (where === "end") spots.push({ x: forwards ? b - width / 2 : a + width / 2, y })
    spots.push({ x: (a + b) / 2, y })
  }
  const middles = [...Array(Math.max(0, last - 1)).keys()]
    .map((k) => k + 1)
    .sort((a, b) => lengthAt(b) - lengthAt(a))
  if (route.request.loop) {
    for (const i of middles) if (horizontal(i)) along(i, 8, 8, "middle")
    return spots
  }
  if (route.request.label?.always && horizontal(0)) {
    along(0, 10, last === 0 ? 16 : 8, "middle")
  }
  if (horizontal(last)) along(last, 8, 16, "end")
  for (const i of middles) if (horizontal(i)) along(i, 8, 8, "middle")
  // On a long upright stretch, the line running behind the label.
  for (const i of middles) {
    if (horizontal(i) || lengthAt(i) < LABEL_HEIGHT + 32) continue
    spots.push({ x: p[i].x, y: (p[i].y + p[i + 1].y) / 2 })
  }
  return spots
}

function placeLabels(routes: Route[], boxes: Map<string, Box>) {
  const placed: Area[] = []
  const result = new Map<string, { label?: EdgeRoute["label"]; shown: boolean }>()
  const nodes: Area[] = [...boxes.values()].map((b) => ({
    left: b.x - 4,
    top: b.y - 4,
    right: b.x + b.width + 4,
    bottom: b.y + b.height + 4,
  }))
  const wanted = routes
    .filter((r) => r.request.label)
    .map((r) => {
      const length = lengthOf(r.points)
      return {
        route: r,
        shown: Boolean(r.request.label!.always) || length >= LONG,
        length,
      }
    })
    .sort(
      (a, b) =>
        Number(b.shown) - Number(a.shown) ||
        Number(Boolean(b.route.request.label!.always)) -
          Number(Boolean(a.route.request.label!.always)) ||
        b.length - a.length
    )
  const lineAreas = (except: RouteRequest) =>
    routes
      .filter(
        (r) =>
          r.request.id !== except.id &&
          !(r.request.target === except.target && r.request.bundle === except.bundle)
      )
      .flatMap((r) =>
        r.points.slice(1).map((p, i) => {
          const q = r.points[i]
          return {
            left: Math.min(p.x, q.x) - 1,
            right: Math.max(p.x, q.x) + 1,
            top: Math.min(p.y, q.y) - 1,
            bottom: Math.max(p.y, q.y) + 1,
          }
        })
      )
  // One label says it for lines that share their way into a step.
  const said = new Set<string>()
  for (const { route, shown } of wanted) {
    const { text, icon } = route.request.label!
    const width = route.request.label!.width ?? labelWidth(text, icon)
    const saying = `${route.request.target}|${route.request.bundle}|${text}`
    const spots = spotsFor(route, width + 8)
    const areaOf = (s: Spot): Area => ({
      left: s.x - width / 2 - 3,
      right: s.x + width / 2 + 3,
      top: s.y - LABEL_HEIGHT / 2 - 3,
      bottom: s.y + LABEL_HEIGHT / 2 + 3,
    })
    const lines = lineAreas(route.request)
    const free = (s: Spot, strict: boolean) => {
      const area = areaOf(s)
      return (
        !nodes.some((n) => overlaps(area, n)) &&
        !placed.some((p) => overlaps(area, p)) &&
        (!strict || !lines.some((l) => overlaps(area, l)))
      )
    }
    // A branch's label is on show only where it covers no other line;
    // elsewhere it waits for its line to be hovered. A loop's always shows.
    const always = Boolean(route.request.label!.always)
    const clear = spots.find((s) => free(s, true))
    const spot = clear ?? spots.find((s) => free(s, false))
    if (spot) {
      const onShow = shown && (always || Boolean(clear)) && !said.has(saying)
      if (onShow) {
        placed.push(areaOf(spot))
        said.add(saying)
      }
      result.set(route.request.id, { label: { ...spot, width }, shown: onShow })
    } else {
      result.set(route.request.id, { shown: false })
    }
  }
  return result
}

/** The middle of a line's longest stretch. */
function anchorOf(points: Point[]): Point {
  let best = { length: -1, at: points[0] }
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1]
    const b = points[i]
    const length = Math.abs(a.x - b.x) + Math.abs(a.y - b.y)
    if (length > best.length) best = { length, at: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } }
  }
  return best.at
}

/* -------------------------------------------------------------------------- */

/**
 * Routes every connection on the canvas. `boxes` are the steps where they
 * stand; `bodies` are each loop's body, by the loop's ID.
 */
export function routeEdges(
  boxes: Map<string, Box>,
  requests: RouteRequest[],
  bodies: Map<string, Set<string>>
): Map<string, EdgeRoute> {
  const rects = [...boxes].map(([id, box]) => rectOf(id, box, CLEAR))
  const routes: Route[] = []

  // Ways back to a loop, a loop at a time.
  const returns = new Map<string, RouteRequest[]>()
  for (const r of requests) {
    if (!r.loop || !boxes.has(r.loop)) continue
    const list = returns.get(r.loop)
    if (list) list.push(r)
    else returns.set(r.loop, [r])
  }
  for (const [loop, list] of returns) {
    const body = [...(bodies.get(loop) ?? [])].flatMap((id) => {
      const box = boxes.get(id)
      return box ? [box] : []
    })
    const paths = loopRoutes({ id: loop, ...boxes.get(loop)! }, body, list, rects)
    for (const r of list) routes.push({ request: r, points: paths.get(r.id)! })
  }

  // Everything else, shortest first, each keeping clear of the lines drawn
  // before it where it can. Lines that meet at a step bend just before
  // it; the rest bend on their way out.
  const drawn = new DrawnLines()
  const draw = (route: Route) => {
    routes.push(route)
    drawn.add(route)
  }
  for (const route of routes.splice(0)) draw(route)
  const incoming = new Map<string, number>()
  for (const r of requests) {
    if (!r.loop) incoming.set(r.target, (incoming.get(r.target) ?? 0) + 1)
  }
  const span = (r: RouteRequest) => Math.abs(r.to.x - r.from.x) + Math.abs(r.to.y - r.from.y)
  const rest = requests
    .filter((r) => !(r.loop && boxes.has(r.loop)))
    .sort((a, b) => span(a) - span(b))
  for (const r of rest) {
    const prefer = (incoming.get(r.target) ?? 0) > 1 ? "target" : "source"
    draw({ request: r, points: routeOf(r, rects, drawn, prefer) })
  }

  spread(routes, "v", rects)
  spread(routes, "h", rects)
  for (const route of routes) route.points = simplify(route.points)

  const labels = placeLabels(routes, boxes)
  return new Map(
    routes.map((route) => {
      const placed = labels.get(route.request.id)
      return [
        route.request.id,
        {
          points: route.points,
          label: placed?.label,
          labelShown: Boolean(placed?.shown && placed.label),
          anchor: placed?.label ?? anchorOf(route.points),
        },
      ]
    })
  )
}

/**
 * How long the drawn line is: its stretches, less what rounding each
 * corner cuts off (a right-angled corner of radius r is drawn as a curve
 * about 1.62r long in place of 2r).
 */
export function drawnLength(points: Point[], radius = 8) {
  let length = 0
  for (let i = 1; i < points.length; i += 1) {
    length += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
  }
  for (let i = 1; i < points.length - 1; i += 1) {
    const inLength = Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
    const outLength = Math.hypot(points[i + 1].x - points[i].x, points[i + 1].y - points[i].y)
    const r = Math.min(radius, inLength / 2, outLength / 2)
    if (r >= 0.5) length -= 0.3768 * r
  }
  return length
}

/** An SVG path through the corners, rounding each by up to `radius`. */
export function pathOf(points: Point[], radius = 8): string {
  if (points.length < 2) return ""
  let d = `M ${points[0].x} ${points[0].y}`
  for (let i = 1; i < points.length - 1; i += 1) {
    const prev = points[i - 1]
    const at = points[i]
    const next = points[i + 1]
    const inLength = Math.hypot(at.x - prev.x, at.y - prev.y)
    const outLength = Math.hypot(next.x - at.x, next.y - at.y)
    const r = Math.min(radius, inLength / 2, outLength / 2)
    if (r < 0.5) {
      d += ` L ${at.x} ${at.y}`
      continue
    }
    const a = {
      x: at.x + ((prev.x - at.x) / inLength) * r,
      y: at.y + ((prev.y - at.y) / inLength) * r,
    }
    const b = {
      x: at.x + ((next.x - at.x) / outLength) * r,
      y: at.y + ((next.y - at.y) / outLength) * r,
    }
    d += ` L ${a.x} ${a.y} Q ${at.x} ${at.y} ${b.x} ${b.y}`
  }
  const last = points.at(-1)!
  return `${d} L ${last.x} ${last.y}`
}
