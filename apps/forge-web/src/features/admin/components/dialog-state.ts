import * as React from "react"

/**
 * Counts the times `open` turns true, so a dialog's form can be keyed on it
 * and start fresh each time while its content stays mounted to animate out.
 */
export function useOpenCount(open: boolean) {
  const [count, setCount] = React.useState(open ? 1 : 0)
  const [wasOpen, setWasOpen] = React.useState(open)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) setCount(count + 1)
  }
  return count
}

/** The last node given, so a dialog keeps showing it while it closes. */
export function useLastNode<T>(node: T | undefined) {
  const [last, setLast] = React.useState(node)
  if (node !== undefined && node !== last) setLast(node)
  return node ?? last
}
