import * as React from "react"

import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Sheet } from "@/components/ui/sheet"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import {
  FormColumns,
  FormFooter,
  TaskForm,
  TaskSheetContent,
  TaskSheetHeader,
} from "@/components/forge/task-sheet"

const plans = [
  { value: "default", label: "Default coding workflow · 3 agents" },
  { value: "review", label: "Review-heavy workflow · 4 agents" },
]

/** The "Create a task" side sheet, wired with local demo state. */
export function CreateTaskSheet({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState("")
  const submit: React.ComponentProps<"form">["onSubmit"] = (event) => {
    event.preventDefault()
    const description = String(
      new FormData(event.currentTarget).get("description") ?? ""
    ).trim()
    if (description.length < 8) {
      setError("Describe the task in at least 8 characters.")
      return
    }
    setError("")
    setBusy(true)
    setTimeout(() => {
      setBusy(false)
      onOpenChange(false)
      toast.add({
        title: "Task created and added to the queue.",
        type: "success",
      })
    }, 1200)
  }
  return (
    <Sheet open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <TaskSheetContent showCloseButton={!busy}>
        <TaskSheetHeader
          eyebrow="Coding workspace"
          eyebrowIcon="task"
          title="Create a task"
          description="Describe the change. Your coding agents will take it from here."
        />
        <TaskForm onSubmit={submit} noValidate>
          <FieldGroup>
            <Field data-invalid={error ? true : undefined}>
              <FieldLabel htmlFor="create-description">
                What needs to be done?
              </FieldLabel>
              <Textarea
                id="create-description"
                name="description"
                placeholder="Implement a feature, fix a bug, or improve your code…"
                rows={6}
                aria-invalid={error ? true : undefined}
                disabled={busy}
                autoFocus
              />
              <FieldDescription>
                Include the expected behavior and any useful context.
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="create-repository">Repository</FieldLabel>
              <Input
                id="create-repository"
                placeholder="https://github.com/your-team/repository.git"
                disabled={busy}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="create-base">
                Base branch or reference
              </FieldLabel>
              <Input id="create-base" defaultValue="main" disabled={busy} />
            </Field>
            <Field>
              <FieldLabel htmlFor="create-plan">Agent plan</FieldLabel>
              <Select items={plans} defaultValue="default" disabled={busy}>
                <SelectTrigger id="create-plan">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  <SelectGroup>
                    {plans.map((plan) => (
                      <SelectItem key={plan.value} value={plan.value}>
                        {plan.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              <FieldDescription>
                An implementer, reviewers, and a fixer work together on your
                task.
              </FieldDescription>
            </Field>
            <FormColumns>
              <Field>
                <FieldLabel htmlFor="create-iterations">
                  Maximum fix attempts
                </FieldLabel>
                <Input
                  id="create-iterations"
                  type="number"
                  min={1}
                  max={6}
                  defaultValue={3}
                  disabled={busy}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="create-limit">
                  Time limit{" "}
                  <span className="font-normal text-muted-foreground">
                    (minutes)
                  </span>
                </FieldLabel>
                <Input
                  id="create-limit"
                  type="number"
                  min={1}
                  placeholder="No limit"
                  disabled={busy}
                />
              </Field>
            </FormColumns>
          </FieldGroup>
          {error && <ErrorCallout>{error}</ErrorCallout>}
          <FormFooter hint="Runs on a dedicated task branch">
            <Button type="submit" disabled={busy}>
              {busy ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Icon icon="plus" data-icon="inline-start" />
              )}
              {busy ? "Creating…" : "Create task"}
            </Button>
          </FormFooter>
        </TaskForm>
      </TaskSheetContent>
    </Sheet>
  )
}
