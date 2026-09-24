import * as React from "react"
import { cn } from "cn"

/**
 * The assistant's mark: a chat bubble in the agent-orb gradient with a white
 * spark, drawn on a 24px grid like the rest of the icons. The spark twinkles
 * when a surrounding `group/launcher` is hovered, and turns slowly while
 * `active` (the agent is working).
 */
function AssistantMark({
  active = false,
  className,
  ...props
}: React.ComponentProps<"svg"> & { active?: boolean }) {
  const id = React.useId()
  const fill = `${id}-fill`
  const glow = `${id}-glow`

  return (
    <svg
      data-slot="assistant-mark"
      data-active={active || undefined}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={cn("size-6 shrink-0", className)}
      {...props}
    >
      <defs>
        {/* The orb-agent-0 palette, top-left to bottom-right. */}
        <linearGradient
          id={fill}
          x1="4"
          y1="3"
          x2="20"
          y2="21"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#7ce7e0" />
          <stop offset="0.42" stopColor="#7f8df5" />
          <stop offset="0.74" stopColor="#e1a3e9" />
          <stop offset="1" stopColor="#f0b892" />
        </linearGradient>
        <radialGradient
          id={glow}
          cx="7.5"
          cy="5.5"
          r="9"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#c7ffff" stopOpacity="0.9" />
          <stop offset="1" stopColor="#c7ffff" stopOpacity="0" />
        </radialGradient>
      </defs>
      <g className="transition-[filter] duration-500 group-hover/launcher:hue-rotate-24 motion-reduce:transition-none">
        <path
          d="M12 2.75A8.75 8.75 0 1 1 8.3 19.43L4.6 20.36a.6.6 0 0 1-.73-.76l.55-3.72A8.75 8.75 0 0 1 12 2.75Z"
          fill={`url(#${fill})`}
        />
        <path
          d="M12 2.75A8.75 8.75 0 1 1 8.3 19.43L4.6 20.36a.6.6 0 0 1-.73-.76l.55-3.72A8.75 8.75 0 0 1 12 2.75Z"
          fill={`url(#${glow})`}
          stroke="#fff"
          strokeOpacity="0.35"
          strokeWidth="0.75"
        />
      </g>
      <path
        d="M12 6.9c.42 2.37 2.23 4.18 4.6 4.6-2.37.42-4.18 2.23-4.6 4.6-.42-2.37-2.23-4.18-4.6-4.6 2.37-.42 4.18-2.23 4.6-4.6Z"
        fill="#fff"
        className={cn(
          "origin-center transition-transform duration-500 ease-[cubic-bezier(0.2,0,0,1)] [transform-box:fill-box] group-hover/launcher:scale-110 group-hover/launcher:rotate-90 motion-reduce:transition-none",
          active &&
            "animate-spin [animation-duration:2.4s] motion-reduce:animate-none"
        )}
      />
      <path
        d="M16.9 4.75c.14.77.72 1.35 1.5 1.5-.78.14-1.36.72-1.5 1.5-.14-.78-.72-1.36-1.5-1.5.78-.15 1.36-.73 1.5-1.5Z"
        fill="#fff"
        className="origin-center scale-90 opacity-80 transition-[scale,opacity] delay-75 duration-500 [transform-box:fill-box] group-hover/launcher:scale-125 group-hover/launcher:opacity-100 motion-reduce:transition-none"
      />
    </svg>
  )
}

export { AssistantMark }
