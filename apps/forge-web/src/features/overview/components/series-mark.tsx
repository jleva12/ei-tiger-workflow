import * as React from "react"
import { cn } from "cn"

import { ChartTypeContext } from "@/features/overview/lib/chart-type"

/**
 * A series' mark beside its name, as its chart draws it: a 2px-rounded
 * square for bars and areas, a line key for lines (DESIGN.md: 8px swatch or
 * 12×2px key in a legend, 10px square or 4px-wide line in a tooltip).
 */
export function SeriesMark({
  className,
  color,
  size = "legend",
}: {
  /** Its colour as a background utility, such as `bg-viz-1`. */
  className?: string
  /** Its colour as a CSS value, when there's no utility for it. */
  color?: string
  size?: "legend" | "tooltip"
}) {
  const line = React.useContext(ChartTypeContext) === "line"
  return (
    <span
      aria-hidden="true"
      style={color ? { background: color } : undefined}
      className={cn(
        "shrink-0",
        size === "legend"
          ? line
            ? "h-0.5 w-3 rounded-full"
            : "size-2 rounded-[2px]"
          : line
            ? "h-2.5 w-1 rounded-[2px]"
            : "size-2.5 rounded-[2px]",
        className
      )}
    />
  )
}
