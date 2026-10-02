import * as React from "react"

// One clock for every component that shows elapsed time, ticking only while
// one of them listens.
let now = Date.now()
const listeners = new Set<() => void>()
let timer: ReturnType<typeof setInterval> | undefined

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (!timer) {
    now = Date.now()
    timer = setInterval(() => {
      now = Date.now()
      for (const notify of listeners) notify()
    }, 1000)
  }
  return () => {
    listeners.delete(listener)
    if (!listeners.size && timer) {
      clearInterval(timer)
      timer = undefined
    }
  }
}

const idle = () => () => {}
const snapshot = () => now

/**
 * The current time in ms, updated every second while `ticking` (e.g. while
 * a run is active, for its elapsed time). Stays put otherwise.
 */
export function useNow(ticking = true) {
  return React.useSyncExternalStore(ticking ? subscribe : idle, snapshot)
}
