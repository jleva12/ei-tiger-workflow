import {
  ChartAreaIcon,
  ChartColumnStackedIcon,
  ChartSplineIcon,
} from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { CHART_TYPES, type ChartType } from "@/features/overview/lib/chart-type"

const CHOICES = {
  bars: { label: "Bars", icon: ChartColumnStackedIcon },
  area: { label: "Area", icon: ChartAreaIcon },
  line: { label: "Lines", icon: ChartSplineIcon },
} satisfies Record<ChartType, { label: string; icon: unknown }>

/** A chart widget's type, picked in its bar or the widget builder. */
export function ChartTypeSwitch({
  value,
  onChange,
  disabled,
}: {
  value: ChartType
  onChange: (type: ChartType) => void
  disabled?: boolean
}) {
  return (
    <ToggleGroup
      aria-label="Chart type"
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onChange(next[0] as ChartType)
      }}
      spacing={0}
      disabled={disabled}
      className="shrink-0 rounded-(--radius-control) p-0.5"
    >
      {CHART_TYPES.map((type) => (
        <Tooltip key={type}>
          <TooltipTrigger
            render={
              <ToggleGroupItem
                value={type}
                aria-label={CHOICES[type].label}
                className="size-6 min-w-0 rounded-(--radius-soft)! p-0 text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
              />
            }
          >
            <Icon icon={CHOICES[type].icon} size={14} />
          </TooltipTrigger>
          <TooltipContent side="top">{CHOICES[type].label}</TooltipContent>
        </Tooltip>
      ))}
    </ToggleGroup>
  )
}
