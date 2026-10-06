import { ChoiceField } from "@/features/builder/components/fields/basic-fields"

/** Where a versioned thing (an agent, a workflow) is, as a version picker offers it. */
export type VersionChoice = {
  /** Its latest published version; null before the first. */
  published: number | null
  hasDraft: boolean
  /** The version its draft will be published as; null without a draft. */
  draftVersion: number | null
}

/** A pinned version, as a document holds it: a number, "draft", or null for the latest published. */
export type PinnedVersion = number | "draft" | null

/**
 * Which version of an agent or workflow runs: the latest published as each
 * run starts, one that never changes, or its draft as it was when the run
 * started.
 */
export function VersionField({
  issue = "version",
  value,
  choice,
  noun,
  description,
  onChange,
}: {
  issue?: string
  value: PinnedVersion
  choice: VersionChoice
  /** "agent", "workflow". */
  noun: string
  description?: string
  onChange: (value: PinnedVersion) => void
}) {
  const published = choice.published ?? 0
  const options = [
    {
      value: "latest",
      label: published
        ? `Latest published (v${published} now)`
        : `Latest published (none yet: its current ${noun})`,
    },
    ...Array.from({ length: published }, (_, i) => published - i).map((n) => ({
      value: String(n),
      label: `Version ${n}`,
    })),
    ...(choice.hasDraft || value === "draft"
      ? [
          {
            value: "draft",
            label: choice.draftVersion
              ? `Its draft (v${choice.draftVersion}), as last saved`
              : "Its draft, as last saved",
          },
        ]
      : []),
  ]
  return (
    <ChoiceField
      issue={issue}
      label="Version"
      value={value === null ? "latest" : String(value)}
      options={options}
      onChange={(next) =>
        onChange(
          next === "latest" ? null : next === "draft" ? "draft" : Number(next)
        )
      }
      description={
        description ??
        `Which of its versions runs: the latest published as each run starts, one that never changes, or its draft as it was when the run started. A published ${noun === "workflow" ? "workflow" : "version"} can only run published versions.`
      }
    />
  )
}
