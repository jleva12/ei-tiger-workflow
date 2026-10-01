import * as React from "react"

import {
  extendedThinkingLevels,
  nearestThinkingLevel,
  type AssistantModel,
} from "@/components/forge/assistant"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import {
  Field,
  FieldDescription,
  FieldLabel,
} from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { ThinkingLevel } from "@/lib/workflows/model"
import { findModel, modelChoiceOf, modelIdOf, useAgentStepModels } from "@/lib/workflows/models"
import { Row } from "./basic-fields"


/** Models by their provider, in the order the providers first appear. */
function byProvider(models: AssistantModel[]) {
  const groups = new Map<string, AssistantModel[]>()
  for (const model of models) {
    const name = model.group ?? ""
    groups.set(name, [...(groups.get(name) ?? []), model])
  }
  return [...groups].map(([name, members]) => ({ name, models: members }))
}

/**
 * A select's key: its options. When they change (the models load, another
 * model's levels), Base UI clears a value it can't find among them, reading
 * the value from before the change, so a new set of options is a new select.
 */
const keyOf = (items: { value: string | null }[]) => items.map((item) => item.value ?? "").join("\n")

/** The thinking levels a model offers, from least to most; all of them for a model Forge doesn't list. */
const levelsOf = (model: AssistantModel | undefined) =>
  model?.thinkingLevels
    ? extendedThinkingLevels.filter((level) => model.thinkingLevels?.includes(level.id))
    : extendedThinkingLevels

/** An option as the assistant's model section lists it: its name, and a line about it. */
function OptionText({ label, description }: { label: React.ReactNode; description?: string | undefined }) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span>{label}</span>
      {description && <span className="text-2xs text-muted-foreground">{description}</span>}
    </span>
  )
}

/** A model in the Model field: its maker's mark and name, and a detail. */
function ModelName({ icon, name, detail }: { icon?: IconProp | undefined; name: string; detail?: string | undefined }) {
  return (
    <>
      {icon && <Icon icon={icon} />}
      <span className="truncate">{name}</span>
      {detail && <span className="truncate text-muted-foreground">{detail}</span>}
    </>
  )
}

/** A model as a step names it: its provider's key and its ID; both empty for the default. */
export type ModelChoice = { provider: string; name: string }

/** A change to the model or the thinking level, and its undo key. */
export type ModelChange = (
  patch: { model?: ModelChoice; thinking_level?: ThinkingLevel | "" },
  key: "model" | "thinking_level"
) => void

/**
 * The model an agent runs on and how long it thinks: the models Forge
 * offers and their thinking levels, as the assistant's model section lists
 * them. A model the step names that Forge doesn't offer (a document from
 * elsewhere) stays, marked, until another is picked; the step fails on it.
 */
export function ModelFields({
  model,
  thinkingLevel,
  onChange,
}: {
  model: ModelChoice
  thinkingLevel: ThinkingLevel | ""
  onChange: ModelChange
}) {
  const { models, defaultModel, isLoading } = useAgentStepModels()
  const modelField = React.useId()
  const levelField = React.useId()
  const named = modelIdOf(model)
  const picked = findModel(models, model)
  const fallback = models.find((model) => model.id === defaultModel)
  // Listed, and it isn't there: the runner won't run it.
  const gone = Boolean(named) && !picked && models.length > 0
  const runsOn = named ? picked : fallback
  const levels = levelsOf(runsOn)
  // A model that doesn't reason offers only off: there's nothing to choose.
  const reasons = levels.length > 1
  const level = thinkingLevel
  const known = extendedThinkingLevels.find((each) => each.id === level)
  const nearest = level ? nearestThinkingLevel(levels, extendedThinkingLevels, level) : undefined
  const clamped = known && nearest !== level ? extendedThinkingLevels.find((each) => each.id === nearest) : undefined

  const modelItems = [
    {
      value: null,
      label: <ModelName icon={fallback?.icon} name="Default" detail={fallback?.name} />,
    },
    ...models.map((model) => ({ value: model.id, label: <ModelName icon={model.icon} name={model.name} /> })),
    ...(named && !picked
      ? [{ value: named, label: <ModelName icon={gone ? "warning" : undefined} name={named} /> }]
      : []),
  ]
  const levelItems = [
    { value: null, label: "Default" },
    ...levels.map((each) => ({ value: each.id, label: each.label })),
    ...(known && clamped ? [{ value: known.id, label: known.label }] : []),
  ]
  const withIcons = models.some((model) => model.icon)

  // A new model keeps the level asked for, or the nearest it offers; none when it doesn't reason.
  const chooseModel = (value: string | null) => {
    const chosen = value ? modelChoiceOf(value) : { provider: "", name: "" }
    const target = value ? models.find((each) => each.id === value) : fallback
    const offered = levelsOf(target)
    const asked = thinkingLevel
    const thinking = asked && offered.length > 1 ? nearestThinkingLevel(offered, extendedThinkingLevels, asked) : ""
    onChange({ model: chosen, thinking_level: (thinking ?? "") as ThinkingLevel | "" }, "model")
  }

  return (
    <Row>
      <Field>
        <FieldLabel htmlFor={modelField}>Model</FieldLabel>
        <Select
          key={keyOf(modelItems)}
          items={modelItems}
          value={named ? (picked?.id ?? named) : null}
          onValueChange={chooseModel}
        >
          <SelectTrigger id={modelField} className="w-full min-w-0" aria-invalid={gone || undefined}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} className="min-w-64">
            <SelectGroup>
              <SelectItem value={null}>
                {fallback?.icon ? <Icon icon={fallback.icon} /> : withIcons && <span aria-hidden className="size-4" />}
                <OptionText
                  label="Default"
                  description={fallback ? `${fallback.name}, Forge's default` : "Forge's default model"}
                />
              </SelectItem>
            </SelectGroup>
            {byProvider(models).map((group) => (
              <React.Fragment key={group.name}>
                <SelectSeparator />
                <SelectGroup>
                  {group.name && (
                    <SelectLabel className="font-medium text-foreground">{group.name}</SelectLabel>
                  )}
                  {group.models.map((model) => (
                    <SelectItem key={model.id} value={model.id}>
                      {model.icon ? <Icon icon={model.icon} /> : withIcons && <span aria-hidden className="size-4" />}
                      <OptionText label={model.name} description={model.description} />
                    </SelectItem>
                  ))}
                </SelectGroup>
              </React.Fragment>
            ))}
            {named && !picked && (
              <>
                <SelectSeparator />
                <SelectGroup>
                  <SelectItem value={named}>
                    {gone ? <Icon icon="warning" /> : withIcons && <span aria-hidden className="size-4" />}
                    <OptionText
                      label={named}
                      description={gone ? "Not one Forge offers: the step fails on it" : undefined}
                    />
                  </SelectItem>
                </SelectGroup>
              </>
            )}
          </SelectContent>
        </Select>
        {gone ? (
          <FieldDescription className="text-destructive">
            Forge doesn&apos;t offer {named}; pick one of its models.
          </FieldDescription>
        ) : (
          models.length === 0 &&
          !isLoading && <FieldDescription>Forge&apos;s models couldn&apos;t be listed.</FieldDescription>
        )}
      </Field>
      <Field data-disabled={!reasons || undefined}>
        <FieldLabel htmlFor={levelField}>Thinking</FieldLabel>
        <Select
          key={keyOf(levelItems)}
          items={levelItems}
          value={level || null}
          disabled={!reasons}
          onValueChange={(value) => onChange({ thinking_level: (value ?? "") as ThinkingLevel | "" }, "thinking_level")}
        >
          <SelectTrigger id={levelField} className="w-full min-w-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} className="min-w-64">
            <SelectGroup>
              <SelectItem value={null}>
                <OptionText label="Default" description="The model's own setting" />
              </SelectItem>
              {levels.map((each) => (
                <SelectItem key={each.id} value={each.id}>
                  <OptionText label={each.label} description={each.description} />
                </SelectItem>
              ))}
              {known && clamped && (
                <SelectItem value={known.id}>
                  <OptionText
                    label={known.label}
                    description={`Not offered: it runs at ${clamped.label.toLowerCase()}`}
                  />
                </SelectItem>
              )}
            </SelectGroup>
          </SelectContent>
        </Select>
        {!reasons ? (
          <FieldDescription>{runsOn?.name ?? "The model"} answers without thinking first.</FieldDescription>
        ) : (
          clamped && (
            <FieldDescription>
              {runsOn?.name} doesn&apos;t offer {known?.label.toLowerCase()}: it runs at{" "}
              {clamped.label.toLowerCase()}.
            </FieldDescription>
          )
        )}
      </Field>
    </Row>
  )
}
