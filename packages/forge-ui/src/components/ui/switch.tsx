import { Switch as SwitchPrimitive } from "@base-ui/react/switch"
import { cn } from "cn"

/**
 * An on/off setting that applies at once: a 32 × 18px track, the dark
 * primary when on, with a thumb that slides across.
 */
function Switch({ className, ...props }: SwitchPrimitive.Root.Props) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        "peer group/switch relative inline-flex h-[1.125rem] w-8 shrink-0 cursor-pointer items-center rounded-full p-0.5 transition-colors duration-150 outline-none after:absolute after:-inset-x-2 after:-inset-y-2 data-checked:bg-primary data-unchecked:bg-muted-foreground/35 data-disabled:cursor-not-allowed data-disabled:opacity-50",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className="pointer-events-none block size-3.5 rounded-full bg-background shadow-sm transition-transform duration-150 ease-out data-checked:translate-x-3.5 dark:data-checked:bg-primary-foreground data-unchecked:translate-x-0 dark:data-unchecked:bg-foreground"
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
