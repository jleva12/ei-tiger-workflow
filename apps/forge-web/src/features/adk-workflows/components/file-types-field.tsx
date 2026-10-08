import * as React from "react"
import { UnfoldMoreIcon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import {
  FILE_TYPE_GROUPS,
  FILE_TYPE_IDS,
  FILE_TYPES,
  describeTypes,
  type FileTypeId,
} from "@/features/adk-workflows/lib/files"

/**
 * The types of files a start takes: a dropdown of the types, grouped
 * (documents, data, images…), each ticked on or off; none ticked takes
 * any type.
 */
export function FileTypesField({
  value,
  onChange,
  description,
}: {
  value: FileTypeId[]
  onChange: (types: FileTypeId[]) => void
  description?: React.ReactNode
}) {
  const id = React.useId()
  const { issues, invalid, key } = useFieldIssues("file_types")
  // Kept in the list's order, whatever order they were ticked in.
  const toggle = (type: FileTypeId, on: boolean) =>
    onChange(FILE_TYPE_IDS.filter((t) => (t === type ? on : value.includes(t))))
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={id}>File types</FieldLabel>
      <DropdownMenu>
        <DropdownMenuTrigger
          id={id}
          render={
            <Button
              variant="outline"
              aria-invalid={invalid || undefined}
              className="w-full justify-between font-normal"
            />
          }
        >
          <span
            className={cn("truncate", !value.length && "text-muted-foreground")}
          >
            {value.length ? describeTypes(value) : "Any type"}
          </span>
          <Icon
            icon={UnfoldMoreIcon}
            data-icon="inline-end"
            className="text-muted-foreground"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent className="max-h-80 w-(--anchor-width)">
          {FILE_TYPE_GROUPS.map((group) => (
            <DropdownMenuGroup key={group.id}>
              <DropdownMenuLabel>{group.label}</DropdownMenuLabel>
              {FILE_TYPE_IDS.filter(
                (type) => FILE_TYPES[type].group === group.id
              ).map((type) => (
                <DropdownMenuCheckboxItem
                  key={type}
                  checked={value.includes(type)}
                  closeOnClick={false}
                  onCheckedChange={(on) => toggle(type, on)}
                >
                  <span className="truncate">{FILE_TYPES[type].label}</span>
                  <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">
                    {FILE_TYPES[type].extensions.join(" ")}
                  </span>
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuGroup>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuGroup>
            <DropdownMenuItem
              disabled={!value.length}
              onClick={() => onChange([])}
            >
              Any type
            </DropdownMenuItem>
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      {issues.length ? (
        <IssueMessages issues={issues} />
      ) : (
        description && <FieldDescription>{description}</FieldDescription>
      )}
    </Field>
  )
}
