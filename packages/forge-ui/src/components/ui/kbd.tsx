import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

const kbdVariants = cva(
  "pointer-events-none inline-flex w-fit items-center justify-center gap-1 font-sans select-none [&_svg:not([class*='size-'])]:size-3",
  {
    variants: {
      variant: {
        default:
          "h-5 min-w-5 rounded-lg bg-muted px-1 text-xs font-medium text-muted-foreground in-data-[slot=input-group]:bg-input in-data-[slot=tooltip-content]:bg-background/20 in-data-[slot=tooltip-content]:text-background dark:in-data-[slot=tooltip-content]:bg-background/10",
        ghost: "text-3xs text-subtle",
        outline:
          "rounded-[3px] border border-border px-[3px] text-4xs leading-normal",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Kbd({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<"kbd"> & VariantProps<typeof kbdVariants>) {
  return (
    <kbd
      data-slot="kbd"
      data-variant={variant}
      className={cn(kbdVariants({ variant }), className)}
      {...props}
    />
  )
}

function KbdGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <kbd
      data-slot="kbd-group"
      className={cn("inline-flex items-center gap-1", className)}
      {...props}
    />
  )
}

export { Kbd, KbdGroup }
