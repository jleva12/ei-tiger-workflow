import { useEffect, useLayoutEffect, useRef, type RefObject } from "react"
import { useAuiState } from "@assistant-ui/react"

import {
  DISCLOSURE_HOLD_ATTRIBUTE,
  THREAD_FLOOR_ATTRIBUTE,
} from "./use-disclosure-anchor"
import { WRITING_IN_ATTRIBUTE } from "./whole-replies"

// Pixels from the bottom that still count as at the bottom.
const AT_BOTTOM = 2
// Pixels below the viewport's top that still count as pinned there.
const PINNED = 24
// How long assistant-ui gets to scroll a new question to the top.
const PIN_MS = 1500
// How long after a run ends its last text and the reply's action bar are
// still followed: they arrive with the end of the run.
const SETTLE_MS = 1500
// How long after a wheel, touch or key a scroll counts as the person's.
const INPUT_MS = 500
const SCROLL_KEYS = new Set([
  "ArrowUp",
  "ArrowDown",
  "PageUp",
  "PageDown",
  "Home",
  "End",
  " ",
])
const TEXT_ENTRY = "textarea, input, select, [contenteditable]"

/**
 * Keeps a streaming reply's newest text in view once the reply fills the
 * viewport, and the thread still until then.
 *
 * With `turnAnchor="top"`, sending a message scrolls it to the top and
 * reserves the rest of the viewport for the reply (an empty spacer after it,
 * `data-aui-top-anchor-reserve`), but assistant-ui never scrolls while the
 * reply streams, so a long one runs on below the fold. Once the question is
 * pinned and the reply has used up the spacer, this follows the bottom as it
 * grows, until shortly after the run ends (or after a reply that arrived
 * whole is written in, `WRITING_IN_ATTRIBUTE`); should the reply shrink again
 * (its reasoning folding away), following the bottom returns to the pinned
 * question. Scrolling up to read stops it, and so does opening or closing
 * the reasoning or a tool call (they're reading it, and it holds itself in
 * place: `useDisclosureAnchor`); getting back to the bottom (scrolling, or
 * the scroll-to-bottom button) starts it again.
 *
 * Meanwhile it keeps the thread where it is when the page changes under it,
 * which WebKit (Safari, the desktop app) doesn't do on its own — it has no
 * scroll anchoring:
 *
 * - The thread can't get shorter than the bottom of the viewport, not even
 *   for a moment (`THREAD_FLOOR_ATTRIBUTE`). assistant-ui replaces the
 *   reply's element when its first event arrives, and a part that loads
 *   swaps its placeholder; WebKit lays the page out between React removing
 *   the old element and inserting the new one, pulled the scroll position up
 *   to the shorter end, and the question dropped down the screen. A change
 *   that lasts is let through once it has settled.
 * - The pinned question stays put when something above it changes size (an
 *   earlier reply showing its author once another agent answers).
 *
 * @param viewportRef The thread's `ThreadPrimitive.Viewport`.
 */
export function useFollowReply(viewportRef: RefObject<HTMLElement | null>) {
  const isRunning = useAuiState((s) => s.thread.isRunning)
  const thread = useAuiState((s) => s.threadListItem.id)
  // Shared with the listeners below, which live as long as the viewport.
  const follow = useRef({
    following: false,
    started: 0,
    until: 0,
    followed: false,
  })
  // Lets the floor go (the listeners below set it).
  const releaseFloor = useRef(() => {})

  // Each run arms it, until a moment after the run ends.
  useEffect(() => {
    if (isRunning) {
      follow.current = {
        following: true,
        started: performance.now(),
        until: Number.POSITIVE_INFINITY,
        followed: false,
      }
      return
    }
    follow.current.until = performance.now() + SETTLE_MS
    // Nothing need move once it stops following.
    const timeout = setTimeout(() => releaseFloor.current(), SETTLE_MS)
    return () => clearTimeout(timeout)
  }, [isRunning])

  // The floor was the last conversation's, not this one's.
  useLayoutEffect(() => releaseFloor.current(), [thread])

  useEffect(() => {
    const viewport = viewportRef.current
    const content = viewport?.firstElementChild
    if (!viewport || !(content instanceof HTMLElement)) return

    const belowFold = () =>
      viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop
    const lastQuestion = () => {
      const questions = viewport.querySelectorAll('[data-role="user"]')
      return questions[questions.length - 1] ?? null
    }
    const topOf = (element: Element) =>
      element.getBoundingClientRect().top - viewport.getBoundingClientRect().top
    // Whether assistant-ui is still scrolling the new question to the top.
    // Until it's there, the bottom is below it; once it is, the bottom is
    // the pinned position for as long as the spacer holds room for the reply.
    const pinning = () => {
      if (performance.now() - follow.current.started > PIN_MS) return false
      const question = lastQuestion()
      return question !== null && topOf(question) > PINNED
    }
    const held = () => viewport.hasAttribute(DISCLOSURE_HOLD_ATTRIBUTE)
    // A reply that arrived whole is still being written in, maybe after the
    // run has ended: it's followed until it's all there.
    const writingIn = () =>
      viewport.querySelector(`[${WRITING_IN_ATTRIBUTE}]`) !== null
    const following = () =>
      performance.now() <= follow.current.until || writingIn()
    // Whether it's keeping the thread where it is: the pinned question or
    // the reply's end in view.
    const keeping = () => follow.current.following && following()

    // The floor: the content kept at least as tall as the bottom of the
    // viewport, so nothing that's gone for a moment pulls the thread up.
    const release = () => {
      if (held() || !content.hasAttribute(THREAD_FLOOR_ATTRIBUTE)) return
      content.removeAttribute(THREAD_FLOOR_ATTRIBUTE)
      content.style.minHeight = ""
    }
    const floor = () => {
      if (held()) return
      if (!keeping()) return release()
      content.setAttribute(THREAD_FLOOR_ATTRIBUTE, "")
      content.style.minHeight = `${Math.ceil(viewport.scrollTop + viewport.clientHeight)}px`
    }
    releaseFloor.current = release

    // The pinned question as last seen: where it was on screen, and how far
    // the thread was scrolled then.
    let pinned: { question: Element; top: number; scrollTop: number } | null =
      null
    const notePinned = () => {
      const question = lastQuestion()
      pinned =
        question && keeping() && !follow.current.followed && !pinning()
          ? { question, top: topOf(question), scrollTop: viewport.scrollTop }
          : null
    }
    // Something above the pinned question changed size: scroll by as much,
    // unless the browser's scroll anchoring has already.
    const holdQuestion = () => {
      const was = pinned
      if (!was || was.question !== lastQuestion() || !keeping()) return
      const scrolled = viewport.scrollTop - was.scrollTop
      const moved = topOf(was.question) - was.top + scrolled
      if (Math.abs(moved) >= 0.5 && Math.abs(moved - scrolled) >= 0.5) {
        viewport.scrollBy({ top: moved, behavior: "instant" })
      }
    }

    const followBottom = () => {
      const state = follow.current
      if (!state.following || !following()) return
      if (belowFold() <= AT_BOTTOM || (!state.followed && pinning())) return
      state.followed = true
      viewport.scrollTo({ top: viewport.scrollHeight, behavior: "instant" })
    }
    const onResize = () => {
      // A change that lasts (the reply got shorter) goes through now.
      release()
      holdQuestion()
      followBottom()
      notePinned()
      floor()
    }
    // Following stops only when the thread itself moves up because of the
    // person: a wheel over a panel that scrolls on its own (the reasoning, a
    // code block) doesn't move it, and a reply that shrinks moves it without
    // them.
    let lastTop = viewport.scrollTop
    let inputAt = Number.NEGATIVE_INFINITY
    let dragging = false
    const onInput = () => {
      inputAt = performance.now()
    }
    const onScroll = () => {
      const top = viewport.scrollTop
      // A disclosure holding itself in place scrolls; that isn't a return to
      // the bottom.
      if (held()) {
        lastTop = top
        return
      }
      const byPerson = dragging || performance.now() - inputAt < INPUT_MS
      if (byPerson && top < lastTop - 1) follow.current.following = false
      else if (belowFold() <= AT_BOTTOM) follow.current.following = true
      lastTop = top
      notePinned()
      floor()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as Element | null
      if (SCROLL_KEYS.has(event.key) && !target?.closest(TEXT_ENTRY)) onInput()
    }
    // A press on the viewport itself, not its content, is on the scrollbar.
    const onPointerDown = (event: PointerEvent) => {
      if (event.target === viewport) dragging = true
    }
    const onPointerUp = () => {
      dragging = false
    }

    // The content, and what's in it: with the floor holding the content's
    // height, a reply getting shorter shows only inside.
    const resize = new ResizeObserver(onResize)
    resize.observe(content)
    for (const child of content.children) resize.observe(child)
    // Opening or closing the reasoning or a tool call stops following.
    const disclosure = new MutationObserver(() => {
      if (held()) follow.current.following = false
    })
    disclosure.observe(viewport, {
      attributes: true,
      attributeFilter: [DISCLOSURE_HOLD_ATTRIBUTE],
    })
    viewport.addEventListener("scroll", onScroll, { passive: true })
    viewport.addEventListener("wheel", onInput, { passive: true })
    viewport.addEventListener("touchmove", onInput, { passive: true })
    viewport.addEventListener("keydown", onKeyDown)
    viewport.addEventListener("pointerdown", onPointerDown)
    window.addEventListener("pointerup", onPointerUp)
    return () => {
      resize.disconnect()
      disclosure.disconnect()
      viewport.removeEventListener("scroll", onScroll)
      viewport.removeEventListener("wheel", onInput)
      viewport.removeEventListener("touchmove", onInput)
      viewport.removeEventListener("keydown", onKeyDown)
      viewport.removeEventListener("pointerdown", onPointerDown)
      window.removeEventListener("pointerup", onPointerUp)
      release()
      releaseFloor.current = () => {}
    }
  }, [viewportRef])
}
