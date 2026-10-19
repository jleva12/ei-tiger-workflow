import * as React from "react"
import { cn } from "cn"
import { DayPicker, getDefaultClassNames } from "react-day-picker"

import { Icon } from "@/components/forge/icon"
import { buttonVariants } from "@/components/ui/button"

/**
 * A month grid to pick a day or a range of days: react-day-picker painted
 * from Forge's tokens (its own stylesheet isn't loaded). Weeks start on
 * Monday, as the overview's weekly buckets do. Pass any DayPicker props:
 * `mode="range"`, `selected`, `onSelect`, `numberOfMonths`, `disabled`…
 */
function Calendar({
  className,
  classNames,
  showOutsideDays = true,
  ...props
}: React.ComponentProps<typeof DayPicker>) {
  const defaults = getDefaultClassNames()
  return (
    <DayPicker
      data-slot="calendar"
      showOutsideDays={showOutsideDays}
      weekStartsOn={1}
      className={cn("p-1 text-xs", className)}
      classNames={{
        root: cn("w-fit", defaults.root),
        months: "relative flex gap-6 max-[600px]:flex-col",
        month: "flex flex-col gap-3",
        nav: "absolute inset-x-0 top-0 flex items-center justify-between",
        button_previous: cn(
          buttonVariants({ variant: "ghost", size: "icon-sm" }),
          "text-muted-foreground aria-disabled:opacity-40"
        ),
        button_next: cn(
          buttonVariants({ variant: "ghost", size: "icon-sm" }),
          "text-muted-foreground aria-disabled:opacity-40"
        ),
        month_caption: "flex h-7 items-center justify-center",
        caption_label: "text-xs font-medium",
        month_grid: "border-collapse",
        weekdays: "flex",
        weekday:
          "w-8 pb-1 text-center text-2xs font-normal text-muted-foreground",
        week: "mt-0.5 flex",
        day: "group/day relative size-8 p-0 text-center",
        day_button:
          "inline-flex size-8 items-center justify-center rounded-(--radius-soft) text-xs tabular-nums outline-none hover:bg-accent disabled:pointer-events-none",
        // A range is a band: its ends dark, its middle the band's tint.
        range_start:
          "rounded-l-(--radius-soft) bg-accent [&>button]:bg-primary [&>button]:text-primary-foreground [&>button]:hover:bg-primary",
        range_middle:
          "bg-accent [&>button]:rounded-none [&>button]:hover:bg-transparent",
        range_end:
          "rounded-r-(--radius-soft) bg-accent [&>button]:bg-primary [&>button]:text-primary-foreground [&>button]:hover:bg-primary",
        selected: "[&>button]:font-medium",
        today:
          "[&>button]:font-semibold [&>button]:underline [&>button]:underline-offset-4",
        outside: "text-subtle",
        disabled: "text-subtle opacity-50",
        hidden: "invisible",
        ...classNames,
      }}
      components={{
        Chevron: ({ orientation }) => (
          <Icon icon={orientation === "left" ? "left" : "right"} size={15} />
        ),
      }}
      {...props}
    />
  )
}

export { Calendar }
