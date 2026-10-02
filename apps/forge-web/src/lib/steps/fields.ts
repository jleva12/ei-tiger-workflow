import type { FieldCheck, FieldMode } from "./expressions"
import type { StepData } from "./model"

/*
 * The settings of each kind of step that read the run's data, how each is
 * written, and what it must come to. The settings dialog's fields and the
 * workflow's checks both come from here, so a field is completed and
 * checked the same way it's reported.
 */

export type ExpressionSetting = {
  /** Which setting: a config key, or `<list>.<item id>` for a list's item. */
  key: string
  label: string
  mode: FieldMode
  text: string
  check: FieldCheck
}

const template = (key: string, label: string, text: string): ExpressionSetting => ({
  key,
  label,
  mode: "template",
  text,
  check: { textual: true },
})

/** The step's settings that hold expressions, templates or code. */
export function expressionSettings(step: StepData): ExpressionSetting[] {
  switch (step.kind) {
    case "approval":
      return [template("message", "What they're deciding", step.config.message)]
    case "http":
      return [
        template("url", "URL", step.config.url),
        ...step.config.headers.map((h) =>
          template(`headers.${h.id}`, `Header ${h.name || "without a name"}`, h.value)
        ),
        { key: "body", label: "Body", mode: "expression", text: step.config.body, check: {} },
      ]
    case "transform":
      return [
        { key: "expression", label: "Expression", mode: "expression", text: step.config.expression, check: {} },
      ]
    case "if":
      return [{ key: "condition", label: "Condition", mode: "expression", text: step.config.condition, check: {} }]
    case "switch":
      return [
        { key: "value", label: "Switch on", mode: "expression", text: step.config.value, check: { expect: "value" } },
      ]
    case "match":
      return step.config.arms.map((arm, index) => ({
        key: `arms.${arm.id}`,
        label: arm.label.trim() || `Rule ${index + 1}`,
        mode: "expression" as const,
        text: arm.condition,
        check: {},
      }))
    case "loop":
      return [
        { key: "items", label: "Go through", mode: "expression", text: step.config.items, check: { expect: "list" } },
      ]
    case "end":
      return [{ key: "result", label: "Result", mode: "expression", text: step.config.result, check: {} }]
    default:
      return []
  }
}

/** One setting's field spec, by key. */
export const expressionSetting = (step: StepData, key: string) =>
  expressionSettings(step).find((s) => s.key === key)
