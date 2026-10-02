import { useEffect, useRef, type RefObject } from "react"
import { useAuiState } from "@assistant-ui/react"

import { DISCLOSURE_HOLD_ATTRIBUTE } from "./use-disclosure-anchor"

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
 * viewport.
 *
 * With `turnAnchor="top"`, sending a message scrolls it to the top and
 * reserves the rest of the viewport for the reply (an empty spacer after it,
 * `data-aui-top-anchor-reserve`), but assistant-ui never scrolls while the
 * reply streams, so a long one runs on below the fold. Once the question is
 * pinned and the reply has used up the spacer, this follows the bottom as it
 * grows, until shortly after the run ends; should the reply shrink again
 * (its reasoning folding away), following the bottom returns to the pinned
 * question. Scrolling up to read stops it, and so does opening or closing
 * the reasoning or a tool call (they're reading it, and it holds itself in
 * place: `useDisclosureAnchor`); getting back to the bottom (scrolling, or
 * the scroll-to-bottom button) starts it again.
 *
 * @param viewportRef The thread's `ThreadPrimitive.Viewport`.
 */
export function useFollowReply(viewportRef: RefObject<HTMLElement | null>) {
  const isRunning = useAuiState((s) => s.thread.isRunning)
  // Shared with the listeners below, which live as long as the viewport.
  const follow = useRef({
    following: false,
    started: 0,
    until: 0,
    followed: false,
  })

  // Each run arms it, until a moment after the run ends.
  useEffect(() => {
    if (isRunning) {
      follow.current = {
        following: true,
        started: performance.now(),
        until: Number.POSITIVE_INFINITY,
        followed: false,
      }
    } else {
      follow.current.until = performance.now() + SETTLE_MS
    }
  }, [isRunning])

  useEffect(() => {
    const viewport = viewportRef.current
    const content = viewport?.firstElementChild
    if (!viewport || !content) return

    const belowFold = () =>
      viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop
    // Whether assistant-ui is still scrolling the new question to the top.
    // Until it's there, the bottom is below it; once it is, the bottom is
    // the pinned position for as long as the spacer holds room for the reply.
    const pinning = () => {
      if (performance.now() - follow.current.started > PIN_MS) return false
      const questions = viewport.querySelectorAll('[data-role="user"]')
      const question = questions[questions.length - 1]
      if (!question) return false
      const top = question.getBoundingClientRect().top
      return top - viewport.getBoundingClientRect().top > PINNED
    }

    const onResize = () => {
      const state = follow.current
      if (!state.following || performance.now() > state.until) return
      if (belowFold() <= AT_BOTTOM || (!state.followed && pinning())) return
      state.followed = true
      viewport.scrollTo({ top: viewport.scrollHeight, behavior: "instant" })
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
      if (viewport.hasAttribute(DISCLOSURE_HOLD_ATTRIBUTE)) {
        lastTop = top
        return
      }
      const byPerson = dragging || performance.now() - inputAt < INPUT_MS
      if (byPerson && top < lastTop - 1) follow.current.following = false
      else if (belowFold() <= AT_BOTTOM) follow.current.following = true
      lastTop = top
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

    const resize = new ResizeObserver(onResize)
    resize.observe(content)
    // Opening or closing the reasoning or a tool call stops following.
    const disclosure = new MutationObserver(() => {
      if (viewport.hasAttribute(DISCLOSURE_HOLD_ATTRIBUTE)) {
        follow.current.following = false
      }
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
    }
  }, [viewportRef])
}
