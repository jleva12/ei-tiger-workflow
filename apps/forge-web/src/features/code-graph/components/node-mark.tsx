import { cn } from "cn"

import { lookOf, type NodeFamily, type NodeLook } from "../lib/graph"
import { GLYPH_FONT, GLYPH_SCALE, glyphTextSize, ICON_PATHS } from "./node-look"

// The family outlines in the 24-unit box, inset for their stroke.
function FamilyShape({
  family,
  style,
}: {
  family: NodeFamily
  style: React.CSSProperties
}) {
  switch (family) {
    case "file":
      return (
        <rect x="2" y="3.5" width="20" height="17" rx="4.5" style={style} />
      )
    case "type":
      return <polygon points="12,1.5 22.5,12 12,22.5 1.5,12" style={style} />
    case "callable":
      return <circle cx="12" cy="12" r="10.5" style={style} />
    case "value":
      return (
        <polygon
          points="1.5,12 6.75,2.9 17.25,2.9 22.5,12 17.25,21.1 6.75,21.1"
          style={style}
        />
      )
    case "other":
      return (
        <polygon
          points="7.6,1.5 16.4,1.5 22.5,7.6 22.5,16.4 16.4,22.5 7.6,22.5 1.5,16.4 1.5,7.6"
          style={style}
        />
      )
  }
}

/**
 * A node's mark as the canvas draws it: its family's shape and colour and,
 * when given one, its kind's glyph. Derived and unresolved nodes are
 * hollow, with a dashed or dotted outline.
 */
export function NodeMark({
  family,
  origin = "declared",
  glyph,
  size = 16,
  className,
}: {
  family: NodeFamily
  origin?: NodeLook["origin"]
  glyph?: NodeLook["glyph"]
  size?: number
  className?: string
}) {
  const color = `var(--graph-${family})`
  const hollow = origin !== "declared"
  // Relative to the mark, glyphs are drawn larger than on the canvas: a
  // mark is small, and its glyph has to stay legible.
  const scale = GLYPH_SCALE[family] / GLYPH_SCALE.file
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={cn("shrink-0 overflow-visible", className)}
    >
      <FamilyShape
        family={family}
        style={{
          fill: hollow ? "var(--card)" : color,
          stroke: color,
          strokeWidth: hollow ? 2 : 1.5,
          strokeLinejoin: "round",
          strokeLinecap: "round",
          strokeDasharray:
            origin === "derived"
              ? "3.2 2.4"
              : origin === "unresolved"
                ? "0.1 3"
                : undefined,
        }}
      />
      {glyph && (
        <g
          transform={`translate(12 12) scale(${scale}) translate(-12 -12)`}
          style={{
            color: hollow ? color : `var(--graph-${family}-ink)`,
          }}
        >
          {"icon" in glyph ? (
            <path
              d={ICON_PATHS[glyph.icon]}
              fill="none"
              stroke="currentColor"
              strokeWidth={2.2}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          ) : (
            <text
              x="12"
              y="12.5"
              textAnchor="middle"
              dominantBaseline="central"
              fontSize={glyphTextSize(glyph.text)}
              fontWeight={650}
              fill="currentColor"
              style={{ fontFamily: GLYPH_FONT }}
            >
              {glyph.text}
            </text>
          )}
        </g>
      )}
    </svg>
  )
}

/** A kind's mark, for lists and filters beside the canvas. */
export function KindBadge({
  kind,
  size,
  className,
}: {
  kind: string
  size?: number
  className?: string
}) {
  const look = lookOf(kind)
  return (
    <NodeMark
      family={look.family}
      origin={look.origin}
      glyph={look.glyph}
      size={size}
      className={className}
    />
  )
}
