import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { FieldDescription } from "@/components/ui/field"
import { formatBytes } from "@/lib/format"
import {
  acceptOf,
  describeTypes,
  takesFile,
  type FilesRule,
} from "@/features/adk-workflows/lib/files"

/*
 * The files a run starts with, when its start takes files: chosen or
 * dropped here, each of a type the start takes (others are refused here,
 * and by the API). The API saves each as an artifact of the run's session,
 * for its steps to read.
 */

export function RunFilesField({
  rule,
  files,
  onChange,
  disabled,
}: {
  rule: FilesRule
  files: File[]
  onChange: (files: File[]) => void
  disabled?: boolean
}) {
  const labelId = React.useId()
  const picker = React.useRef<HTMLInputElement>(null)
  const [refused, setRefused] = React.useState<string[]>([])
  const [dragging, setDragging] = React.useState(false)
  const types = describeTypes(rule.types)

  const add = (picked: File[]) => {
    if (!picked.length) return
    setRefused(
      picked.filter((f) => !takesFile(rule, f.name)).map((f) => f.name)
    )
    const taken = picked.filter((f) => takesFile(rule, f.name))
    if (taken.length) onChange([...files, ...taken])
  }
  const remove = (index: number) =>
    onChange(files.filter((_, at) => at !== index))

  return (
    <div role="group" aria-labelledby={labelId} className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span id={labelId} className="text-xs font-medium text-foreground">
          Files
        </span>
        <Button
          type="button"
          variant="outline"
          size="xs"
          disabled={disabled}
          onClick={() => picker.current?.click()}
        >
          <Icon icon="file" data-icon="inline-start" />
          Choose files
        </Button>
      </div>
      <div
        onDragOver={(event) => {
          if (disabled) return
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          if (!disabled) add([...event.dataTransfer.files])
        }}
        className={cn(
          "rounded-(--radius-item) border border-dashed border-border p-2 transition-colors",
          dragging && "border-ring bg-accent"
        )}
      >
        {files.length ? (
          <ul className="flex flex-col gap-0.5">
            {files.map((file, index) => (
              <li
                key={`${file.name}-${index}`}
                className="flex min-w-0 items-center gap-2 rounded-(--radius-chip) px-1.5 py-1 text-xs"
              >
                <Icon
                  icon="file"
                  className="size-4 shrink-0 text-muted-foreground"
                />
                <span className="min-w-0 flex-1 truncate text-foreground">
                  {file.name}
                </span>
                <span className="shrink-0 text-2xs text-muted-foreground tabular-nums">
                  {formatBytes(file.size)}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  disabled={disabled}
                  aria-label={`Remove ${file.name}`}
                  onClick={() => remove(index)}
                >
                  <Icon icon="close" />
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-1.5 py-3 text-center text-xs text-muted-foreground">
            Drop files here, or choose them: {types}.
          </p>
        )}
      </div>
      {refused.length > 0 ? (
        <p role="alert" className="text-xs text-destructive">
          {refused.join(", ")}: the workflow takes {types} files only.
        </p>
      ) : (
        <FieldDescription>
          Optional. Each is saved in the run&apos;s artifact store, where its
          steps read it.
        </FieldDescription>
      )}
      <input
        ref={picker}
        type="file"
        multiple
        accept={acceptOf(rule.types) || undefined}
        className="hidden"
        onChange={(event) => {
          const picked = [...(event.target.files ?? [])]
          event.target.value = ""
          add(picked)
        }}
      />
    </div>
  )
}
