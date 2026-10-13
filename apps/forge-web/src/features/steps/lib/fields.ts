import type { FieldCheck, FieldMode } from "./expressions"
import type { StepData } from "./model"
import { withItem, type ItemBinding, type Scope } from "./scope"

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
  /**
   * Read once per item of a list (a Filter's condition): the list's
   * expression, what the item is called, and the step's name.
   */
  each?: ItemBinding
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
        ...step.config.query.flatMap((q) => [
          template(`query.${q.id}.name`, `Query parameter ${q.name || "without a name"}`, q.name),
          template(`query.${q.id}`, `Query parameter ${q.name || "without a name"}'s value`, q.value),
        ]),
        ...step.config.headers.map((h) =>
          template(`headers.${h.id}`, `Header ${h.name || "without a name"}`, h.value)
        ),
        { key: "body", label: "Body", mode: "expression", text: step.config.body, check: {} },
      ]
    case "bash":
      // Its command is run as it's written: it reads these instead.
      return [
        template("cwd", "Folder", step.config.cwd),
        ...step.config.env.map((v) =>
          template(`env.${v.id}`, `Variable ${v.name || "without a name"}`, v.value)
        ),
      ]
    case "git_clone":
      return [
        template("repository", "Repository", step.config.repository),
        template("ref", "Branch or tag", step.config.ref),
        template("folder", "Folder", step.config.folder),
      ]
    case "git_branch":
      return [
        template("folder", "Folder", step.config.folder),
        template("branch", "Branch", step.config.branch),
        template("start", "Starts from", step.config.start),
      ]
    case "git_push":
      return [
        template("folder", "Folder", step.config.folder),
        template("message", "Commit message", step.config.message),
        template("author_name", "Author", step.config.author_name),
        template("author_email", "Author's email", step.config.author_email),
      ]
    case "github_pull_request":
      return [
        template("repository", "Repository", step.config.repository),
        template("head", "Branch", step.config.head),
        template("base", "Into", step.config.base),
        template("title", "Title", step.config.title),
        template("body", "Description", step.config.body),
      ]
    case "transform":
      return [
        { key: "expression", label: "Expression", mode: "expression", text: step.config.expression, check: {} },
      ]
    case "if":
      return [
        { key: "condition", label: "Condition", mode: "expression", text: step.config.condition, check: { expect: "condition" } },
      ]
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
        check: { expect: "condition" as const },
      }))
    case "loop":
    case "for_each":
      return [
        { key: "items", label: "Go through", mode: "expression", text: step.config.items, check: { expect: "list" } },
      ]
    case "filter":
      return [
        { key: "items", label: "List", mode: "expression", text: step.config.items, check: { expect: "list" } },
        {
          key: "condition",
          label: "Keep when",
          mode: "expression",
          text: step.config.condition,
          check: { expect: "condition" },
          each: { items: step.config.items, name: step.config.item_name || "item", of: step.name },
        },
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

/** What a setting reads: the step's scope, and the item it's read for, if it's read per item. */
export const settingScope = (setting: ExpressionSetting | undefined, scope: Scope) =>
  setting?.each ? withItem(scope, setting.each) : scope
