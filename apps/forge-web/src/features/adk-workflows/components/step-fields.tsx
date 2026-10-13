import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  bare,
  ChoiceField,
  CODE,
  ListEditor,
  NumberField,
  Row,
  SwitchField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { SchemaField } from "@/features/builder/components/fields/schema-field"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  DELAY_UNITS,
  HTTP_METHODS,
  uid,
  type HeaderRow,
  type StepConfigs,
  type StepData,
  type StepKind,
} from "@/features/steps/lib/model"
import { resolveExpression } from "@/features/steps/lib/expressions"
import { expressionSetting, settingScope } from "@/features/steps/lib/fields"
import { oneOf, valuesOf } from "@/features/steps/lib/types"
import { useAgentBuilder as useBuilder } from "@/features/adk-workflows/components/agent-store"
import type { AgentStep } from "@/features/adk-workflows/lib/model"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { ExpressionField } from "@/features/builder/components/fields/expression-field"
import { CredentialField } from "@/features/credentials/components/credential-field"
import { FolderPlace } from "@/features/adk-workflows/components/folder-place"

/*
 * The settings of each kind of step a workflow shares with Forge's
 * steps, as its settings dialog shows them. Every change goes to the
 * builder's store as it's typed; typing in one field is one step of undo.
 */

type Setter<K extends StepKind> = <F extends keyof StepConfigs[K]>(
  field: F,
  value: StepConfigs[K][F]
) => void

function useSetter<K extends StepKind>(id: string): Setter<K> {
  const updateStep = useBuilder((s) => s.updateStep)
  return React.useCallback(
    (field, value) =>
      updateStep(
        id,
        (step) =>
          ({
            ...step,
            config: { ...step.config, [field]: value },
          }) as AgentStep,
        String(field)
      ),
    [id, updateStep]
  )
}

type Props<K extends StepKind> = { id: string; config: StepConfigs[K] }

/**
 * A setting that reads the run's data, completed and checked against what
 * the step can read. How it's written (an expression, a template, code,
 * arguments) and what it must come to come from the step's kind
 * (steps/lib/fields.ts), the same as the workflow's issues.
 */
function Setting({
  id,
  setting,
  ...field
}: Omit<
  React.ComponentProps<typeof ExpressionField>,
  "mode" | "scope" | "check"
> & {
  id: string
  /** Its key in steps/lib/fields.ts. */
  setting: string
}) {
  const scopeOf = useBuilder((s) => s.scopeOf)
  const step = useBuilder((s) => s.nodes.find((n) => n.id === id)?.data)
  const spec = React.useMemo(
    () => (step ? expressionSetting(step as StepData, setting) : undefined),
    [step, setting]
  )
  // A setting read per item (a Filter's condition) reads the item too.
  const scope = React.useMemo(
    () => settingScope(spec, scopeOf(id)),
    [spec, scopeOf, id]
  )
  // Its issues, but for its expression's: it checks those itself.
  const { issues, key } = useFieldIssues(setting, { whole: true })
  if (!spec) return null
  return (
    <ExpressionField
      {...field}
      mode={spec.mode}
      scope={scope}
      check={spec.check}
      issues={issues.filter((i) => !i.id.includes(":field:"))}
      field={key}
    />
  )
}

/**
 * Named values: an HTTP request's headers, a Bash step's environment
 * variables. A row each, its name and its value side by side and a bin to
 * remove it. The value is a template, completed and checked like the URL.
 */
function NamedRows({
  id,
  field,
  title,
  noun,
  namePlaceholder = "Key",
  headers,
  onChange,
}: {
  id: string
  /** The setting the rows are: `headers`, `env`. */
  field: "headers" | "env"
  title: string
  /** What one row is: "header". */
  noun: string
  namePlaceholder?: string
  headers: HeaderRow[]
  onChange: (headers: HeaderRow[]) => void
}) {
  // A row just added: its name takes focus.
  const [added, setAdded] = React.useState<string>()
  // The name and the bin stand as tall as the value's editor.
  const tall = "h-[2.125rem]"
  const Noun = noun.charAt(0).toUpperCase() + noun.slice(1)
  const change = (header: HeaderRow, patch: Partial<HeaderRow>) =>
    onChange(headers.map((x) => (x.id === header.id ? { ...x, ...patch } : x)))
  // "Every header needs a name": the ones without turn red.
  const { issues, invalid, key } = useFieldIssues(field)
  return (
    <div
      role="group"
      aria-label={title}
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">{title}</span>
      {headers.map((header) => (
        <div
          key={header.id}
          className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-start gap-1.5"
        >
          <Input
            aria-label={`${Noun} name`}
            value={header.name}
            placeholder={namePlaceholder}
            spellCheck={false}
            autoComplete="off"
            autoFocus={header.id === added}
            aria-invalid={(invalid && !header.name.trim()) || undefined}
            onChange={(event) => change(header, { name: event.target.value })}
            className={cn(CODE, tall)}
          />
          <Setting
            id={id}
            setting={`${field}.${header.id}`}
            label={`${header.name.trim() || Noun} value`}
            labelHidden
            value={header.value}
            placeholder="Value"
            onChange={(value) => change(header, { value })}
            // No space under it until it has something to say.
            className="gap-0 has-[[aria-live]>*]:gap-1.5"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${header.name.trim() || noun}`}
            onClick={() => onChange(headers.filter((x) => x.id !== header.id))}
            className={cn(tall, "text-muted-foreground hover:text-destructive")}
          >
            <Icon icon={Delete02Icon} />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          const header = {
            id: uid(field === "env" ? "env" : "header"),
            name: "",
            value: "",
          }
          setAdded(header.id)
          onChange([...headers, header])
        }}
        className="w-full border-dashed text-muted-foreground"
      >
        <Icon icon="plus" data-icon="inline-start" />
        Add {noun}
      </Button>
      <IssueMessages issues={issues} />
    </div>
  )
}

/**
 * An HTTP request's query parameters: a row each, its name and its value
 * side by side and a bin to remove it. Both are templates, completed and
 * checked like the URL; filled in, each is percent-encoded as it's sent.
 */
function QueryList({
  id,
  query,
  onChange,
}: {
  id: string
  query: HeaderRow[]
  onChange: (query: HeaderRow[]) => void
}) {
  // A parameter just added: its name takes focus.
  const [added, setAdded] = React.useState<string>()
  const change = (row: HeaderRow, patch: Partial<HeaderRow>) =>
    onChange(query.map((x) => (x.id === row.id ? { ...x, ...patch } : x)))
  // "Every query parameter needs a name".
  const { issues, key } = useFieldIssues("query")
  // No space under a template until it has something to say.
  const quiet = "gap-0 has-[[aria-live]>*]:gap-1.5"
  return (
    <div
      role="group"
      aria-label="Query parameters"
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">
        Query parameters
      </span>
      {query.map((row) => (
        <div
          key={row.id}
          className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-start gap-1.5"
        >
          <Setting
            id={id}
            setting={`query.${row.id}.name`}
            label="Query parameter name"
            labelHidden
            value={row.name}
            placeholder="Key"
            autoFocus={row.id === added}
            onChange={(name) => change(row, { name })}
            className={quiet}
          />
          <Setting
            id={id}
            setting={`query.${row.id}`}
            label={`${row.name.trim() || "Query parameter"} value`}
            labelHidden
            value={row.value}
            placeholder="Value"
            onChange={(value) => change(row, { value })}
            className={quiet}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${row.name.trim() || "query parameter"}`}
            onClick={() => onChange(query.filter((x) => x.id !== row.id))}
            className="h-[2.125rem] text-muted-foreground hover:text-destructive"
          >
            <Icon icon={Delete02Icon} />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          const row = { id: uid("query"), name: "", value: "" }
          setAdded(row.id)
          onChange([...query, row])
        }}
        className="w-full border-dashed text-muted-foreground"
      >
        <Icon icon="plus" data-icon="inline-start" />
        Add query parameter
      </Button>
      <IssueMessages issues={issues} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Kinds                                                                      */
/* -------------------------------------------------------------------------- */

function ApprovalFields({ id, config }: Props<"approval">) {
  const set = useSetter<"approval">(id)
  return (
    <>
      <Setting
        id={id}
        setting="message"
        label="What they're deciding"
        value={config.message}
        multiline
        rows={3}
        placeholder="Approve {{ input.request.item }} for {{ input.request.amount }}?"
        onChange={(value) => set("message", value)}
      />
      <ChoiceField
        issue="approvers"
        label="Who decides"
        value={config.approvers}
        options={[
          { value: "org:admin", label: "An organization admin" },
          { value: "org:member", label: "Any member of the organization" },
        ]}
        onChange={(value) => set("approvers", value)}
      />
      <NumberField
        issue="timeout_hours"
        label="Wait up to (hours)"
        value={config.timeout_hours}
        onChange={(value) => set("timeout_hours", value)}
        description="0 waits until someone decides. When it runs out, the step takes Rejected."
      />
    </>
  )
}

function HttpFields({ id, config }: Props<"http">) {
  const set = useSetter<"http">(id)
  const organizationId = useBuilder((s) => s.meta.organization_id)
  const credentials = useBuilder((s) => s.lookups.credentials)
  return (
    <>
      <div className="grid grid-cols-[5.5rem_1fr] gap-2.5">
        <ChoiceField
          issue="method"
          label="Method"
          value={config.method}
          options={HTTP_METHODS.map((m) => ({ value: m, label: m }))}
          onChange={(value) => set("method", value)}
        />
        <Setting
          id={id}
          setting="url"
          label="URL"
          value={config.url}
          placeholder="https://"
          onChange={(value) => set("url", value)}
        />
      </div>
      <QueryList
        id={id}
        query={config.query}
        onChange={(value) => set("query", value)}
      />
      <CredentialField
        organizationId={organizationId}
        value={config.credential}
        onChange={(value) => set("credential", value)}
        credentials={credentials}
        url={config.url}
      />
      <NamedRows
        id={id}
        field="headers"
        title="Headers"
        noun="header"
        headers={config.headers}
        onChange={(value) => set("headers", value)}
      />
      {config.method !== "GET" && (
        <Setting
          id={id}
          setting="body"
          label="Body"
          value={config.body}
          multiline
          rows={4}
          placeholder={'{ "key": input.issue.key }'}
          onChange={(value) => set("body", value)}
          description="JSONata. An object or list is sent as JSON; text as it is."
        />
      )}
      <Row>
        <NumberField
          issue="timeout_seconds"
          label="Timeout (s)"
          value={config.timeout_seconds}
          min={1}
          onChange={(value) => set("timeout_seconds", value)}
        />
        <NumberField
          issue="retries"
          label="Retries"
          value={config.retries}
          onChange={(value) => set("retries", value)}
        />
      </Row>
      <SchemaField
        issue="output_schema"
        label="What it returns"
        title="The response's body"
        description={
          <>
            Its fields and their types. Later steps read them as{" "}
            <code className="font-mono text-[0.9em]">
              steps.{id}.output.body
            </code>
            , completed and checked, and a response that doesn&apos;t fit takes
            Error.
          </>
        }
        schema={config.output_schema}
        onChange={(schema) => set("output_schema", schema)}
        empty="Not declared: later steps can't be checked against the response's body."
        subject={{ one: "response", many: "responses" }}
      />
      <p className="text-xs/[1.6] text-muted-foreground">
        {Object.keys(config.output_schema).length
          ? "A 2xx response that fits what it returns takes Success; anything else, or no answer, takes Error."
          : "A 2xx response takes Success; anything else, or no answer, takes Error."}
      </p>
    </>
  )
}

function BashFields({ id, config }: Props<"bash">) {
  const set = useSetter<"bash">(id)
  const mono = "font-mono text-[0.9em]"
  return (
    <>
      <TextField
        issue="command"
        label="Command"
        value={config.command}
        code
        multiline
        rows={5}
        placeholder={'gh repo clone "$REPO" backend'}
        onChange={(value) => set("command", value)}
        description={
          <>
            bash, run as it&apos;s written: it stops at the first command that
            fails. It isn&apos;t filled in with{" "}
            <code className={mono}>{"{{ }}"}</code>, so the run&apos;s data
            can&apos;t become code: give it a variable below and read it as{" "}
            <code className={mono}>&quot;$REPO&quot;</code>.{" "}
            <code className={mono}>$WORKSPACE</code> is the run&apos;s folder.
          </>
        }
      />
      <Setting
        id={id}
        setting="cwd"
        label="Folder"
        value={config.cwd}
        placeholder="The run's folder"
        onChange={(value) => set("cwd", value)}
        description={
          <>
            Where it runs: a path from the run&apos;s folder, such as{" "}
            <code className={mono}>backend</code> or{" "}
            <code className={mono}>{"repos/{{ input.name }}"}</code>, or an
            earlier step&apos;s folder; made when it isn&apos;t there.
          </>
        }
      />
      <FolderPlace
        id={id}
        value={config.cwd}
        verb="Runs in"
        onChange={(value) => set("cwd", value)}
      />
      <NamedRows
        id={id}
        field="env"
        title="Environment variables"
        noun="variable"
        namePlaceholder="NAME"
        headers={config.env}
        onChange={(value) => set("env", value)}
      />
      <NumberField
        issue="timeout_seconds"
        label="Timeout (s)"
        value={config.timeout_seconds}
        min={1}
        onChange={(value) => set("timeout_seconds", value)}
      />
      <p className="text-xs/[1.6] text-muted-foreground">
        Exit code 0 takes Success, handing on its{" "}
        <code className={mono}>stdout</code>,{" "}
        <code className={mono}>stderr</code> and{" "}
        <code className={mono}>exit_code</code>; any other, or running past its
        timeout, takes Error. A run tried again runs it again, so write commands
        that can run twice.
      </p>
    </>
  )
}

// Code in a GitHub step's descriptions, as the text around it.
const INLINE_CODE = "font-mono text-[0.9em]"

/** Where a GitHub step's credential is sent: a URL's origin, or GitHub's. */
function gitOrigin(repository: string, api = false) {
  const written = repository.trim()
  const match = /^https:\/\/[^/\s{]+/.exec(written)
  if (match && !written.startsWith("https://github.com"))
    return api ? `${match[0]}/api/v3` : match[0]
  return api ? "https://api.github.com" : "https://github.com"
}

/** What a GitHub step tells about its credential and its ways out. */
function GitNote({ children }: { children: React.ReactNode }) {
  return <p className="text-xs/[1.6] text-muted-foreground">{children}</p>
}

function GitCloneFields({ id, config }: Props<"git_clone">) {
  const set = useSetter<"git_clone">(id)
  const organizationId = useBuilder((s) => s.meta.organization_id)
  const credentials = useBuilder((s) => s.lookups.credentials)
  return (
    <>
      <Setting
        id={id}
        setting="repository"
        label="Repository"
        value={config.repository}
        placeholder="acme/backend"
        onChange={(value) => set("repository", value)}
        description={
          <>
            <code className={INLINE_CODE}>owner/name</code> on GitHub, or the
            repository&apos;s <code className={INLINE_CODE}>https://</code> URL
            (GitHub Enterprise too).
          </>
        }
      />
      <Setting
        id={id}
        setting="ref"
        label="Branch or tag"
        value={config.ref}
        placeholder="Its default branch"
        onChange={(value) => set("ref", value)}
      />
      <Setting
        id={id}
        setting="folder"
        label="Folder"
        value={config.folder}
        placeholder="Named after the repository"
        onChange={(value) => set("folder", value)}
        description={
          <>
            A path from the run&apos;s folder, such as{" "}
            <code className={INLINE_CODE}>backend</code>; empty for a folder
            named after the repository. A clone of the same repository already
            there is kept, so a run tried again goes on.
          </>
        }
      />
      <FolderPlace id={id} value={config.folder} verb="Clones into" />
      <NumberField
        issue="depth"
        label="History (commits)"
        value={config.depth}
        min={0}
        onChange={(value) => set("depth", value)}
        description="0 clones the whole history; 1 only the latest commit, which is fastest."
      />
      <CredentialField
        organizationId={organizationId}
        value={config.credential}
        onChange={(value) => set("credential", value)}
        credentials={credentials}
        url={gitOrigin(config.repository)}
      />
      <NumberField
        issue="timeout_seconds"
        label="Timeout (s)"
        value={config.timeout_seconds}
        min={1}
        onChange={(value) => set("timeout_seconds", value)}
      />
      <GitNote>
        A private repository needs a credential with a token for{" "}
        <code className={INLINE_CODE}>https://github.com</code>. Success hands
        on its <code className={INLINE_CODE}>folder</code>,{" "}
        <code className={INLINE_CODE}>branch</code> and{" "}
        <code className={INLINE_CODE}>commit</code>; the next git steps work in
        that folder. The start must allow a file system.
      </GitNote>
    </>
  )
}

function GitBranchFields({ id, config }: Props<"git_branch">) {
  const set = useSetter<"git_branch">(id)
  return (
    <>
      <Setting
        id={id}
        setting="folder"
        label="Repository's folder"
        value={config.folder}
        placeholder="The run's folder"
        onChange={(value) => set("folder", value)}
        description="The cloned repository's folder: pick a clone's below, or give a path from the run's folder."
      />
      <FolderPlace
        id={id}
        value={config.folder}
        verb="Works in"
        onChange={(value) => set("folder", value)}
      />
      <Setting
        id={id}
        setting="branch"
        label="Branch"
        value={config.branch}
        placeholder="forge/{{ input.ticket }}"
        onChange={(value) => set("branch", value)}
        description="Made and checked out; one already there is checked out as it is."
      />
      <Setting
        id={id}
        setting="start"
        label="Starts from"
        value={config.start}
        placeholder="What's checked out"
        onChange={(value) => set("start", value)}
        description="A branch, tag or commit; empty for what's checked out."
      />
      <NumberField
        issue="timeout_seconds"
        label="Timeout (s)"
        value={config.timeout_seconds}
        min={1}
        onChange={(value) => set("timeout_seconds", value)}
      />
    </>
  )
}

function GitPushFields({ id, config }: Props<"git_push">) {
  const set = useSetter<"git_push">(id)
  const organizationId = useBuilder((s) => s.meta.organization_id)
  const credentials = useBuilder((s) => s.lookups.credentials)
  return (
    <>
      <Setting
        id={id}
        setting="folder"
        label="Repository's folder"
        value={config.folder}
        placeholder="The run's folder"
        onChange={(value) => set("folder", value)}
        description="The cloned repository's folder: pick a clone's below, or give a path from the run's folder."
      />
      <FolderPlace
        id={id}
        value={config.folder}
        verb="Works in"
        onChange={(value) => set("folder", value)}
      />
      <Setting
        id={id}
        setting="message"
        label="Commit message"
        value={config.message}
        multiline
        rows={3}
        placeholder="Update the notes for {{ input.ticket }}"
        onChange={(value) => set("message", value)}
        description="Everything that changed in the folder is committed with it; nothing changed pushes the branch as it is."
      />
      <div className="grid grid-cols-2 gap-2.5">
        <Setting
          id={id}
          setting="author_name"
          label="Author"
          value={config.author_name}
          placeholder="Forge"
          onChange={(value) => set("author_name", value)}
        />
        <Setting
          id={id}
          setting="author_email"
          label="Author's email"
          value={config.author_email}
          placeholder="forge@localhost"
          onChange={(value) => set("author_email", value)}
        />
      </div>
      <CredentialField
        organizationId={organizationId}
        value={config.credential}
        onChange={(value) => set("credential", value)}
        credentials={credentials}
        url="https://github.com"
      />
      <NumberField
        issue="timeout_seconds"
        label="Timeout (s)"
        value={config.timeout_seconds}
        min={1}
        onChange={(value) => set("timeout_seconds", value)}
      />
      <GitNote>
        It pushes the branch checked out (make one with Create branch) to the
        repository it was cloned from, signed in by a credential with a token
        for <code className={INLINE_CODE}>https://github.com</code>. Success
        hands on the <code className={INLINE_CODE}>branch</code> and{" "}
        <code className={INLINE_CODE}>commit</code> pushed.
      </GitNote>
    </>
  )
}

function PullRequestFields({ id, config }: Props<"github_pull_request">) {
  const set = useSetter<"github_pull_request">(id)
  const organizationId = useBuilder((s) => s.meta.organization_id)
  const credentials = useBuilder((s) => s.lookups.credentials)
  return (
    <>
      <Setting
        id={id}
        setting="repository"
        label="Repository"
        value={config.repository}
        placeholder="{{ steps.push.output.repository }}"
        onChange={(value) => set("repository", value)}
        description={
          <>
            <code className={INLINE_CODE}>owner/name</code> on GitHub, or the
            repository&apos;s <code className={INLINE_CODE}>https://</code> URL.
          </>
        }
      />
      <div className="grid grid-cols-2 gap-2.5">
        <Setting
          id={id}
          setting="head"
          label="Branch"
          value={config.head}
          placeholder="{{ steps.push.output.branch }}"
          onChange={(value) => set("head", value)}
        />
        <Setting
          id={id}
          setting="base"
          label="Into"
          value={config.base}
          placeholder="The default branch"
          onChange={(value) => set("base", value)}
        />
      </div>
      <Setting
        id={id}
        setting="title"
        label="Title"
        value={config.title}
        placeholder="Update the notes for {{ input.ticket }}"
        onChange={(value) => set("title", value)}
      />
      <Setting
        id={id}
        setting="body"
        label="Description"
        value={config.body}
        multiline
        rows={5}
        placeholder="What changed, and why."
        onChange={(value) => set("body", value)}
      />
      <SwitchField
        label="Draft"
        checked={config.draft}
        onChange={(value) => set("draft", value)}
        description="Opened as a draft: not ready for review yet."
      />
      <CredentialField
        organizationId={organizationId}
        value={config.credential}
        onChange={(value) => set("credential", value)}
        credentials={credentials}
        url={gitOrigin(config.repository, true)}
      />
      <NumberField
        issue="timeout_seconds"
        label="Timeout (s)"
        value={config.timeout_seconds}
        min={1}
        onChange={(value) => set("timeout_seconds", value)}
      />
      <GitNote>
        Needs a credential with a token for{" "}
        <code className={INLINE_CODE}>https://api.github.com</code> that may
        open pull requests on the repository. Success hands on its{" "}
        <code className={INLINE_CODE}>number</code> and{" "}
        <code className={INLINE_CODE}>url</code>; one already open for the
        branch is the one handed on.
      </GitNote>
    </>
  )
}

function TransformFields({ id, config }: Props<"transform">) {
  const set = useSetter<"transform">(id)
  return (
    <>
      <Setting
        id={id}
        setting="expression"
        label="Expression"
        value={config.expression}
        multiline
        rows={8}
        placeholder={
          '{\n  "key": input.issue.key,\n  "labels": previous.labels.name\n}'
        }
        onChange={(value) => set("expression", value)}
        description={
          <>
            JSONata. It reads{" "}
            <code className="font-mono text-[0.9em]">input</code>,{" "}
            <code className="font-mono text-[0.9em]">previous</code> and{" "}
            <code className="font-mono text-[0.9em]">steps</code>; what it makes
            is what the next step gets.
          </>
        }
      />
      <SchemaField
        issue="output_schema"
        label="What it makes"
        title="What the transform makes"
        description={
          <>
            Its fields and their types. Later steps read them as{" "}
            <code className="font-mono text-[0.9em]">steps.{id}.output</code>,
            completed and checked, and a run is held to them. Undeclared,
            they&apos;re worked out from the expression where they can be.
          </>
        }
        schema={config.output_schema}
        onChange={(schema) => set("output_schema", schema)}
        empty="Not declared: worked out from the expression where it can be."
        subject={{ one: "result", many: "results" }}
      />
    </>
  )
}

function FilterFields({ id, config }: Props<"filter">) {
  const set = useSetter<"filter">(id)
  const item = config.item_name || "item"
  return (
    <>
      <Setting
        id={id}
        setting="items"
        label="List"
        value={config.items}
        placeholder="previous.results"
        onChange={(value) => set("items", value)}
        description="JSONata giving a list: a loop's results, an HTTP response's items…"
      />
      <TextField
        issue="item_name"
        label="Call each"
        value={config.item_name}
        code
        onChange={(value) => set("item_name", value)}
        description={
          <>
            The condition reads the current one as{" "}
            <code className="font-mono text-[0.9em]">{item}</code>, and its
            place in the list as{" "}
            <code className="font-mono text-[0.9em]">index</code>.
          </>
        }
      />
      <Setting
        id={id}
        setting="condition"
        label="Keep when"
        value={config.condition}
        multiline
        placeholder={`${item}.status = "open"`}
        onChange={(value) => set("condition", value)}
        description={
          <>
            JSONata, read for each item; the items it&apos;s true for are kept,
            in order. Later steps read them as{" "}
            <code className="font-mono text-[0.9em]">
              steps.{id}.output.results
            </code>
            , typed as the list&apos;s items.
          </>
        }
      />
    </>
  )
}

function DelayFields({ id, config }: Props<"delay">) {
  const set = useSetter<"delay">(id)
  return (
    <Row>
      <NumberField
        issue="amount"
        label="Wait"
        value={config.amount}
        min={0}
        onChange={(value) => set("amount", value)}
      />
      <ChoiceField
        issue="unit"
        label="Unit"
        value={config.unit}
        options={DELAY_UNITS}
        onChange={(value) => set("unit", value)}
      />
    </Row>
  )
}

function IfFields({ id, config }: Props<"if">) {
  const set = useSetter<"if">(id)
  return (
    <Setting
      id={id}
      setting="condition"
      label="Condition"
      value={config.condition}
      multiline
      placeholder={'{{ previous.priority }} = "high"'}
      onChange={(value) => set("condition", value)}
      description="Type {{ to pick a field. Compare with =, !=, > or <; combine conditions with and / or. True takes the True way; anything else, False."
    />
  )
}

/**
 * A case's value, picked from the only values the Switch's value can
 * have. One another case has is taken; one it can't have (the field's
 * values changed) shows as such, and the checks flag it.
 */
function CaseChoice({
  label,
  value,
  values,
  taken,
  onChange,
}: {
  label: string
  value: string
  values: string[]
  taken: string[]
  onChange: (value: string) => void
}) {
  const stray = value && !values.includes(value)
  const options = [
    ...values.map((v) => ({ value: v, label: v })),
    ...(stray ? [{ value, label: `${value} (it can't be this)` }] : []),
  ]
  return (
    <Select
      items={options}
      value={value || null}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next)
      }}
    >
      <SelectTrigger
        aria-label={label}
        size="sm"
        className={cn(bare, CODE, "w-full", stray && "text-destructive")}
      >
        <SelectValue placeholder="Pick a value" />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {options.map((option) => (
          <SelectItem
            key={option.value}
            value={option.value}
            disabled={option.value !== value && taken.includes(option.value)}
            className={cn(
              CODE,
              option.value === value && stray && "text-destructive"
            )}
          >
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function SwitchFields({ id, config }: Props<"switch">) {
  const set = useSetter<"switch">(id)
  const scopeOf = useBuilder((s) => s.scopeOf)
  // The only values it can switch on, when what it's on says so: then a
  // case is picked from them, and can't be one it could never be.
  const values = React.useMemo(
    () =>
      config.value.trim()
        ? valuesOf(resolveExpression(config.value, scopeOf(id)))
        : undefined,
    [config.value, scopeOf, id]
  )
  const used = config.cases.map((c) => c.value)
  const open = values?.filter((v) => !used.includes(v)) ?? []
  return (
    <>
      <Setting
        id={id}
        setting="value"
        label="Switch on"
        value={config.value}
        placeholder="previous.kind"
        onChange={(value) => set("value", value)}
        description="JSONata. The case it equals, as text, is taken."
      />
      <ListEditor
        issue="cases"
        label="Cases"
        items={config.cases}
        onChange={(value) => set("cases", value)}
        make={() => ({ id: uid("case"), value: open[0] ?? "" })}
        addLabel="Add case"
        addDisabled={values !== undefined && open.length === 0}
        renderItem={(item, change, index) => ({
          head: values ? (
            <CaseChoice
              label={`Case ${index + 1}`}
              value={item.value}
              values={values}
              taken={config.cases
                .filter((c) => c.id !== item.id)
                .map((c) => c.value)}
              onChange={(value) => change({ value })}
            />
          ) : (
            <Input
              aria-label={`Case ${index + 1}`}
              value={item.value}
              placeholder="bug"
              onChange={(event) => change({ value: event.target.value })}
              className={cn(bare, CODE)}
            />
          ),
        })}
        description={
          values ? (
            open.length ? (
              <>
                It&apos;s {oneOf(values)}: each case is one of them. A value no
                case has ({oneOf(open)}) takes Default.
              </>
            ) : (
              <>
                It&apos;s {oneOf(values)}, and every one has a case: Default is
                never taken.
              </>
            )
          ) : (
            <>
              A value no case has takes Default. Declare the values it can have
              where it&apos;s made (a field with a fixed list of values), and
              each case is picked from them.
            </>
          )
        }
      />
    </>
  )
}

function MatchFields({ id, config }: Props<"match">) {
  const set = useSetter<"match">(id)
  const every = config.mode === "all"
  return (
    <>
      <ChoiceField
        issue="mode"
        label="Takes"
        value={config.mode}
        options={[
          { value: "first", label: "The first rule that holds, then stops" },
          { value: "all", label: "Every rule that holds, at once" },
        ]}
        onChange={(value) => set("mode", value)}
      />
      <ListEditor
        issue="arms"
        label={every ? "Rules" : "Rules, in order"}
        items={config.arms}
        onChange={(value) => set("arms", value)}
        make={() => ({ id: uid("rule"), label: "", condition: "" })}
        addLabel="Add rule"
        ordered={!every}
        renderItem={(arm, change, index) => ({
          head: (
            <Input
              aria-label={`Rule ${index + 1} name`}
              value={arm.label}
              placeholder={`Rule ${index + 1}`}
              onChange={(event) => change({ label: event.target.value })}
              className={cn(bare, "text-xs font-medium md:text-xs")}
            />
          ),
          body: (
            <Setting
              id={id}
              setting={`arms.${arm.id}`}
              label={`${arm.label.trim() || `Rule ${index + 1}`} condition`}
              labelHidden
              value={arm.condition}
              multiline
              placeholder={'previous.severity = "high"'}
              onChange={(value) => change({ condition: value })}
            />
          ),
        })}
        description={
          every
            ? "Each condition is JSONata. Every one that's true is taken, side by side; none takes Otherwise."
            : "Each condition is JSONata. The first that's true is taken; none takes Otherwise."
        }
      />
    </>
  )
}

/** A loop's settings, and a for-each's: the list and how it's gone through. */
function EachFields({
  id,
  kind,
  config,
}: Props<"loop" | "for_each"> & { kind: "loop" | "for_each" }) {
  const set = useSetter<"loop">(id)
  return (
    <>
      <Setting
        id={id}
        setting="items"
        label="Go through"
        value={config.items}
        placeholder="previous.items"
        onChange={(value) => set("items", value)}
        description="JSONata giving a list. Each item's fields complete in the body."
      />
      <TextField
        issue="item_name"
        label="Call each"
        value={config.item_name}
        code
        onChange={(value) => set("item_name", value)}
        description={
          <>
            The body reads the current one as{" "}
            <code className="font-mono text-[0.9em]">
              {config.item_name || "item"}
            </code>
            .
          </>
        }
      />
      <Row>
        <NumberField
          issue="max_iterations"
          label="Most passes"
          value={config.max_iterations}
          min={1}
          onChange={(value) => set("max_iterations", value)}
        />
        <NumberField
          issue="concurrency"
          label="At once"
          value={config.concurrency}
          min={1}
          onChange={(value) => set("concurrency", value)}
        />
      </Row>
      <p className="text-xs/[1.6] text-muted-foreground">
        {kind === "for_each"
          ? "Connect Each item to the steps to run for each item; they never lead back here, and nothing is collected. Done goes on, with the count, when every item's way has ended."
          : "Connect Each item to the steps to repeat, and the last of them back to the loop. Done goes on when every item has run."}
      </p>
    </>
  )
}

function MergeFields({ id, config }: Props<"merge">) {
  const set = useSetter<"merge">(id)
  return (
    <ChoiceField
      issue="mode"
      label="Goes on"
      value={config.mode}
      options={[
        { value: "all", label: "When every way has arrived" },
        { value: "any", label: "At the first way to arrive" },
      ]}
      onChange={(value) => set("mode", value)}
    />
  )
}

function EndFields({ id, config }: Props<"end">) {
  const set = useSetter<"end">(id)
  return (
    <>
      <ChoiceField
        issue="outcome"
        label="The run"
        value={config.outcome}
        options={[
          { value: "succeeded", label: "Succeeds" },
          { value: "failed", label: "Fails" },
        ]}
        onChange={(value) => set("outcome", value)}
      />
      <Setting
        id={id}
        setting="result"
        label="Result"
        value={config.result}
        placeholder="previous"
        onChange={(value) => set("result", value)}
        description="JSONata: what the run hands back."
      />
    </>
  )
}

/** The settings of a step, for its kind. */
export function StepFields({ id, step }: { id: string; step: StepData }) {
  switch (step.kind) {
    case "approval":
      return <ApprovalFields id={id} config={step.config} />
    case "http":
      return <HttpFields id={id} config={step.config} />
    case "bash":
      return <BashFields id={id} config={step.config} />
    case "git_clone":
      return <GitCloneFields id={id} config={step.config} />
    case "git_branch":
      return <GitBranchFields id={id} config={step.config} />
    case "git_push":
      return <GitPushFields id={id} config={step.config} />
    case "github_pull_request":
      return <PullRequestFields id={id} config={step.config} />
    case "transform":
      return <TransformFields id={id} config={step.config} />
    case "filter":
      return <FilterFields id={id} config={step.config} />
    case "delay":
      return <DelayFields id={id} config={step.config} />
    case "if":
      return <IfFields id={id} config={step.config} />
    case "switch":
      return <SwitchFields id={id} config={step.config} />
    case "match":
      return <MatchFields id={id} config={step.config} />
    case "loop":
    case "for_each":
      return <EachFields id={id} kind={step.kind} config={step.config} />
    case "merge":
      return <MergeFields id={id} config={step.config} />
    case "end":
      return <EndFields id={id} config={step.config} />
  }
}
