import type * as React from "react"

/** Label / value rows for a stats tooltip. */
export function StatsList({
  rows,
}: {
  rows: [label: string, value: React.ReactNode][]
}) {
  return (
    <dl className="grid grid-cols-[auto_auto] gap-x-4 gap-y-0.5 py-0.5 text-2xs">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="opacity-60">{label}</dt>
          <dd className="text-end font-mono tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}
