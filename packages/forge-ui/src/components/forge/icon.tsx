import { HugeiconsIcon, type HugeiconsIconProps } from "@hugeicons/react"

import { resolveIcon, type IconProp } from "./icons"

type IconProps = Omit<HugeiconsIconProps, "icon" | "size"> & {
  /** A Forge icon name (`"home"`) or a Hugeicons glyph object. */
  icon: IconProp
  /** Pixel size. 17px is the workspace default; controls resize icons via CSS. */
  size?: number
}

/**
 * Decorative icon. Inside shadcn controls (Button, menu items, tabs) the
 * control sizes its icons, so `size` only matters in free-standing markup.
 */
function Icon({ icon, size = 17, strokeWidth = 1.6, ...props }: IconProps) {
  return (
    <HugeiconsIcon
      data-slot="icon"
      icon={resolveIcon(icon)}
      size={size}
      strokeWidth={strokeWidth}
      aria-hidden="true"
      {...props}
    />
  )
}

export { Icon }
