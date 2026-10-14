/*
 * The statuses an HTTP request's status way names, as the runner reads them
 * (forge_task_adk_workflows.support.http.statuses_of): each a status
 * (`404`), a class (`4xx`) or a range (`500-599`), apart by commas or spaces.
 */

const CODE = /^(?:([1-5])xx|([1-5]\d\d)(?:-([1-5]\d\d))?)$/i

/** The statuses codes name, as ranges; or why they don't name any. */
export type ReadStatuses = { ranges: [number, number][] } | { problem: string }

export function readStatuses(codes: string): ReadStatuses {
  const ranges: [number, number][] = []
  for (const token of codes.trim().split(/[\s,]+/)) {
    if (!token) continue
    const found = CODE.exec(token)
    if (!found)
      return {
        problem: `"${token}" isn't a status (404), a class (4xx) or a range (500-599).`,
      }
    const [, klass, low, high] = found
    if (klass) ranges.push([Number(klass) * 100, Number(klass) * 100 + 99])
    else if (high && Number(high) < Number(low))
      return { problem: `"${token}" ends before it starts.` }
    else ranges.push([Number(low), Number(high ?? low)])
  }
  return ranges.length ? { ranges } : { problem: "It names no status." }
}

/** Whether a status is one of the ranges. */
export const holds = (ranges: [number, number][], status: number) =>
  ranges.some(([low, high]) => low <= status && status <= high)

/**
 * The statuses a way names that an earlier way names too: the earlier one
 * takes them, so this one never does for those.
 */
export function shadowed(
  earlier: [number, number][][],
  ranges: [number, number][]
): number[] {
  const found: number[] = []
  for (const [low, high] of ranges)
    for (let status = low; status <= high; status++)
      if (earlier.some((taken) => holds(taken, status))) found.push(status)
  return found
}
