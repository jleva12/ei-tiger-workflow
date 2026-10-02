import { useCallback, useEffect, useRef, type RefObject } from "react"

/** Set on the scrolling ancestor while a disclosure animates in it. */
export const DISCLOSURE_HOLD_ATTRIBUTE = "data-disclosure-hold"
// How long held height the thread no longer fills takes to go.
const SETTLE_MS = 180

const scrollableAncestor = (element: HTMLElement) => {
  let current = element.parentElement
  while (current) {
    const { overflowY } = getComputedStyle(current)
    if (overflowY === "scroll" || overflowY === "auto") return current
    current = current.parentElement
  }
  return null
}

type Hold = {
  /** Disclosures animating; nested ones share the hold. */
  count: number
  content: HTMLElement | null
  /** The inline styles to put back. */
  minHeight: string
  overflowAnchor: string
  /** Stops the settling after the last one ended, if it's running. */
  stopSettling?: () => void
}
const holds = new WeakMap<HTMLElement, Hold>()

/**
 * Keeps the scroller from shrinking while a disclosure animates: its content
 * keeps at least its current height, and the browser's scroll anchoring is
 * off. Otherwise a panel closing near the bottom shortens the thread each
 * frame, the browser pulls the scroll position down, and the thread's top
 * anchor (whose spacer catches up a frame later) pushes it back up: the
 * conversation shakes.
 *
 * @returns Releases the hold.
 */
function holdScroller(scroller: HTMLElement) {
  let hold = holds.get(scroller)
  if (hold?.stopSettling) {
    // Still settling from the last one: this one takes the hold over.
    hold.stopSettling()
    hold.stopSettling = undefined
  }
  if (!hold) {
    const content = scroller.firstElementChild as HTMLElement | null
    hold = {
      count: 0,
      content,
      minHeight: content?.style.minHeight ?? "",
      overflowAnchor: scroller.style.overflowAnchor,
    }
    holds.set(scroller, hold)
    scroller.style.overflowAnchor = "none"
    scroller.setAttribute(DISCLOSURE_HOLD_ATTRIBUTE, "")
  }
  if (hold.count === 0 && hold.content) {
    hold.content.style.minHeight = `${hold.content.getBoundingClientRect().height}px`
  }
  hold.count++
  const current = hold
  let released = false
  return () => {
    if (released) return
    released = true
    if (--current.count > 0) return
    const done = () => {
      if (holds.get(scroller) !== current || current.count > 0) return
      holds.delete(scroller)
      if (current.content) current.content.style.minHeight = current.minHeight
      scroller.style.overflowAnchor = current.overflowAnchor
      scroller.removeAttribute(DISCLOSURE_HOLD_ATTRIBUTE)
    }
    const excess = current.content && excessHeight(scroller, current)
    if (excess) current.stopSettling = easeAway(current.content!, excess, done)
    else done()
  }
}

/**
 * How much of the held height the thread no longer fills, when giving it up
 * at once would pull the scroll position down (a panel closed near the
 * bottom of a reply taller than the thread); null when releasing moves
 * nothing, as when the thread's top-anchor spacer has taken the room back.
 * Measured by dropping the hold and putting it back before anything paints.
 */
function excessHeight(scroller: HTMLElement, hold: Hold) {
  const content = hold.content!
  const held = content.style.minHeight
  const top = scroller.scrollTop
  content.style.minHeight = hold.minHeight
  const natural = scroller.scrollHeight
  content.style.minHeight = held
  if (scroller.scrollTop !== top) {
    scroller.scrollTo({ top, behavior: "instant" })
  }
  const bottom = natural - scroller.clientHeight
  const heldHeight = Number.parseFloat(held)
  return top > bottom + 0.5 && Number.isFinite(heldHeight)
    ? { from: heldHeight, to: heldHeight - (top - bottom) }
    : null
}

/**
 * Gives up held height over a moment instead of at once, so what's above
 * the closed panel settles down smoothly rather than jumping.
 *
 * @returns Stops it where it is, without finishing.
 */
function easeAway(
  content: HTMLElement,
  { from, to }: { from: number; to: number },
  done: () => void
) {
  const start = performance.now()
  let frame = 0
  const step = () => {
    const progress = Math.min(1, (performance.now() - start) / SETTLE_MS)
    const eased = 1 - (1 - progress) ** 3
    content.style.minHeight = `${from + (to - from) * eased}px`
    if (progress < 1) frame = requestAnimationFrame(step)
    else finish()
  }
  // Frames don't run in a hidden tab; it still settles.
  const timeout = setTimeout(() => finish(), SETTLE_MS + 200)
  const stop = () => {
    cancelAnimationFrame(frame)
    clearTimeout(timeout)
  }
  const finish = () => {
    stop()
    done()
  }
  frame = requestAnimationFrame(step)
  return stop
}

/**
 * Keeps a disclosure (reasoning, a tool call, a group of them) where it is
 * on screen while it opens or closes, so what was clicked stays under the
 * pointer and nothing around it jumps:
 *
 * - The thread can't get shorter during the animation (`holdScroller`);
 *   room it no longer fills afterwards goes smoothly.
 * - Each frame, it scrolls by however far the disclosure moved, instantly.
 * - The thread's reply following (`useFollowReply`) waits until it's done
 *   (`data-disclosure-hold` on the scroller).
 *
 * It replaces assistant-ui's `useScrollLock`, which hides the scrollbar for
 * the animation: the thread reserves the scrollbar's gutter on both edges
 * (`scrollbar-gutter: stable both-edges`), so hiding it moved the whole
 * conversation sideways and back on every toggle.
 *
 * @param ref The disclosure.
 * @param duration How long its animation runs, in milliseconds.
 * @returns Call it right before the disclosure opens or closes.
 */
export function useDisclosureAnchor(
  ref: RefObject<HTMLElement | null>,
  duration: number
) {
  const stop = useRef<(() => void) | null>(null)
  useEffect(() => () => stop.current?.(), [])

  return useCallback(() => {
    stop.current?.()
    const element = ref.current
    const scroller = element && scrollableAncestor(element)
    if (!element || !scroller) {
      stop.current = null
      return
    }
    const release = holdScroller(scroller)
    const start = element.getBoundingClientRect().top
    // One frame past the animation, for its last step.
    const end = performance.now() + duration + 20
    let frame = 0
    const finish = () => {
      cancelAnimationFrame(frame)
      clearTimeout(timeout)
      release()
      stop.current = null
    }
    // Frames don't run in a hidden tab; the hold still ends.
    const timeout = setTimeout(finish, duration + 200)
    const hold = () => {
      const drift = element.getBoundingClientRect().top - start
      if (Math.abs(drift) >= 0.5) {
        scroller.scrollBy({ top: drift, behavior: "instant" })
      }
      if (performance.now() < end) frame = requestAnimationFrame(hold)
      else finish()
    }
    frame = requestAnimationFrame(hold)
    stop.current = finish
  }, [ref, duration])
}
