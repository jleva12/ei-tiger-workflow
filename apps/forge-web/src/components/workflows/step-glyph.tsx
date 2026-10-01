import { KindGlyph } from "@/components/builder/glyph"
import { STEP_KINDS, stepIcon, type StepData, type StepKind } from "@/lib/workflows/model"

/** A workflow step's mark (see the builder kit's KindGlyph). */
export function StepGlyph({
  kind,
  step,
  size = "default",
  className,
}: {
  kind: StepKind
  /** The step itself, when there is one: an entry point's glyph says how it starts. */
  step?: StepData
  size?: "default" | "sm"
  className?: string
}) {
  return (
    <KindGlyph
      info={STEP_KINDS[kind]}
      icon={step ? stepIcon(step) : undefined}
      size={size}
      className={className}
    />
  )
}
