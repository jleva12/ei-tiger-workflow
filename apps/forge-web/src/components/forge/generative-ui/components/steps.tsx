import { isText } from "../value"

export function RichSteps({ steps }: { steps?: unknown }) {
  const items = (Array.isArray(steps) ? steps : []).filter(
    (step): step is { title: string | number; detail?: unknown } =>
      isText(step?.title)
  )
  if (!items.length) return null
  return (
    <ol data-slot="rich-steps" className="flex flex-col gap-2.5">
      {items.map((step, index) => (
        <li key={index} className="flex gap-2.5">
          <span
            aria-hidden="true"
            className="mt-px flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-3xs font-medium text-muted-foreground tabular-nums"
          >
            {index + 1}
          </span>
          <div className="flex min-w-0 flex-col gap-0.5 pt-0.5">
            <p className="font-medium text-foreground">{step.title}</p>
            {isText(step.detail) && (
              <p className="text-muted-foreground">{step.detail}</p>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}
