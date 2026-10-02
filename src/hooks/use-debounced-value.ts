import * as React from "react"

/** `value`, once it has stopped changing for `ms` (for search as you type). */
export function useDebouncedValue<T>(value: T, ms = 250) {
  const [debounced, setDebounced] = React.useState(value)
  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return debounced
}
