import * as React from "react"

import { Copyable } from "@/components/forge/copyable"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { toast } from "@/components/ui/toast"
import { useMyOrganizations } from "@/lib/access"
import { toApiError } from "@/lib/api/index"
import { useScopeAccess } from "@/lib/hierarchy"
import { usePageContext } from "@/features/assistant/lib/page-context"
import {
  AccentField,
  ChoiceField,
  ConfigSection,
  SuggestionsField,
  SwitchField,
  TextField,
} from "@/features/agents/components/chat-page-config-fields"
import { ChatPageOptions } from "@/features/agents/components/chat-page-options"
import { ChatPagePreview } from "@/features/agents/components/chat-page-preview"
import {
  useChatAgent,
  useChatPage,
  useChatPageSetting,
  type ChatAgentRecord,
} from "@/features/agents/lib/api"
import { chatPageLink } from "@/features/agents/lib/chat-page"
import { CHAT_AGENTS_ICON } from "@/features/agents/lib/model"
import {
  changedSettings,
  pageSettingsOf,
  pageTitleOf,
  PAGE_TEXT_LIMITS,
  type ChatPageBubbles,
  type ChatPageCorners,
  type ChatPageFont,
  type ChatPageSettings,
  type ChatPageTheme,
  type ChatPageWidth,
} from "@/features/agents/lib/page-settings"

/**
 * An agent's chat page, set up beside a live preview of it: its sharing on
 * the left with its words, what it shows and its look; on the right, the
 * page as the people it's shared with will see it, as each setting changes.
 * Changes are kept until saved (the preview shows them already).
 */
export function ChatPageConfigPage({
  organizationId,
  agentId,
}: {
  organizationId: string
  agentId: string
}) {
  const myOrganizations = useMyOrganizations()
  const organization = myOrganizations.data?.find(
    (t) => t.id === organizationId
  )
  const agent = useChatAgent(organizationId, organization ? agentId : undefined)
  const can = useScopeAccess(organization ? `org:${organizationId}` : undefined)
  const canManage = can("agents:manage")
  const name = agent.data?.document.name ?? "Agent"

  useShellPage({
    header: {
      title: "Chat page",
      icon: "message",
      breadcrumbs: organization
        ? [
            {
              label: organization.name,
              href: `/organizations/${organizationId}`,
            },
            {
              label: "Agents",
              href: `/organizations/${organizationId}?view=chat-agents`,
            },
            {
              label: name,
              icon: CHAT_AGENTS_ICON,
              href: `/organizations/${organizationId}/chat-agents/${agentId}`,
            },
          ]
        : [],
    },
    // The settings and the preview take the whole width.
    sidebar: { hidden: true },
  })
  usePageContext({
    entities: [
      { kind: "organization", id: organizationId, label: organization?.name },
    ],
  })

  if (myOrganizations.isPending || (organization && agent.isPending)) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-[60vh] w-full rounded-(--radius-card)" />
      </div>
    )
  }
  if (!organization || agent.error || !agent.data) {
    return (
      <ErrorCallout
        title="Couldn't open the chat page's settings"
        action={
          <Button variant="outline" size="sm" onClick={() => agent.refetch()}>
            Retry
          </Button>
        }
      >
        {agent.error
          ? toApiError(agent.error).message
          : "There's no such agent in this organization."}
      </ErrorCallout>
    )
  }
  return <ChatPageConfig record={agent.data} canManage={canManage} />
}

const THEMES = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  {
    value: "system",
    label: "Visitor's",
    hint: "Light or dark, as each visitor's system is.",
  },
] as const satisfies readonly {
  value: ChatPageTheme
  label: string
  hint?: string
}[]

const BUBBLES = [
  { value: "neutral", label: "Grey" },
  { value: "tint", label: "Tinted" },
  { value: "accent", label: "Accent" },
] as const satisfies readonly { value: ChatPageBubbles; label: string }[]

const CORNERS = [
  { value: "square", label: "Square" },
  { value: "default", label: "Soft" },
  { value: "rounded", label: "Round" },
] as const satisfies readonly { value: ChatPageCorners; label: string }[]

const FONTS = [
  { value: "default", label: "Forge" },
  { value: "system", label: "System" },
  { value: "serif", label: "Serif" },
  { value: "mono", label: "Mono" },
] as const satisfies readonly { value: ChatPageFont; label: string }[]

const WIDTHS = [
  { value: "narrow", label: "Narrow" },
  { value: "default", label: "Medium" },
  { value: "wide", label: "Wide" },
] as const satisfies readonly { value: ChatPageWidth; label: string }[]

function ChatPageConfig({
  record,
  canManage,
}: {
  record: ChatAgentRecord
  canManage: boolean
}) {
  const setting = useChatPageSetting(record.organization_id, record.id)
  const page = useChatPage(record)
  const saved = React.useMemo(() => pageSettingsOf(page), [page])
  const [draft, setDraft] = React.useState(saved)
  // Bumped when the draft is set from outside (discarded, saved), so the
  // fields that keep rows of their own start again from it.
  const [generation, setGeneration] = React.useState(0)
  const changes = changedSettings(saved, draft)
  const dirty = Object.keys(changes).length > 0
  const published = record.published_version !== null
  const enabled = Boolean(page?.enabled)
  const disabled = !canManage || setting.isPending
  const set = <K extends keyof ChatPageSettings>(
    key: K,
    value: ChatPageSettings[K]
  ) => setDraft((current) => ({ ...current, [key]: value }))

  // Leaving with changes unsaved asks first.
  React.useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])

  const save = () =>
    setting.mutate(
      { enabled, ...changes },
      {
        onSuccess: (record) => {
          // As the page keeps them (trimmed), so nothing looks unsaved.
          setDraft(pageSettingsOf(record.chat_page))
          setGeneration((n) => n + 1)
          toast.add({ title: "Saved the chat page", type: "success" })
        },
        onError: (caught) =>
          toast.add({
            title: "Couldn't save the chat page",
            description: toApiError(caught).message,
            type: "error",
          }),
      }
    )
  const discard = () => {
    setDraft(saved)
    setGeneration((n) => n + 1)
  }
  const turn = (on: boolean) =>
    setting.mutate(
      { enabled: on },
      {
        onSuccess: () =>
          toast.add({
            title: on ? "Chat page on" : "Chat page off",
            type: "success",
          }),
        onError: (caught) =>
          toast.add({
            title: on
              ? "Couldn't turn on the chat page"
              : "Couldn't turn off the chat page",
            description: toApiError(caught).message,
            type: "error",
          }),
      }
    )

  const name = record.document.name || "the agent"
  const title = pageTitleOf(draft, record.document)
  return (
    <>
      <ShellHeaderActions>
        {dirty && (
          <span className="text-xs text-muted-foreground">Unsaved changes</span>
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={!dirty || setting.isPending}
          onClick={discard}
        >
          Discard
        </Button>
        <Button size="sm" disabled={!dirty || disabled} onClick={save}>
          {setting.isPending && <Spinner data-icon="inline-start" />}
          Save
        </Button>
      </ShellHeaderActions>
      {/* Fills the page's area: the settings scroll on their own, beside the preview. */}
      <div className="absolute inset-0 flex min-h-0 @max-[800px]/shell:flex-col">
        <aside
          aria-label="Chat page settings"
          className="flex w-[22rem] shrink-0 flex-col overflow-y-auto border-r bg-background @max-[800px]/shell:max-h-[45%] @max-[800px]/shell:w-full @max-[800px]/shell:border-r-0 @max-[800px]/shell:border-b"
        >
          <ConfigSection
            title="Sharing"
            description={
              published
                ? `Anyone with the link talks to its latest published version (v${record.published_version}), without signing in to Forge.`
                : "Publish it to share it: until then, the preview talks to the draft."
            }
          >
            <div className="flex items-center justify-between gap-3">
              <label
                htmlFor="chat-page-enabled"
                className="text-xs font-medium text-foreground"
              >
                {enabled ? "The link opens the chat" : "The link is off"}
              </label>
              <Switch
                id="chat-page-enabled"
                checked={enabled}
                disabled={disabled || !published}
                onCheckedChange={turn}
              />
            </div>
            {page && (
              <>
                <Copyable label="Link" value={chatPageLink(page.token)} />
                {enabled && (
                  <Button
                    variant="outline"
                    size="xs"
                    className="self-start"
                    nativeButton={false}
                    render={
                      <a
                        href={chatPageLink(page.token)}
                        target="_blank"
                        rel="noopener"
                      />
                    }
                  >
                    <Icon icon="external" data-icon="inline-start" />
                    Open the page
                  </Button>
                )}
              </>
            )}
          </ConfigSection>

          <ConfigSection
            title="Words"
            description="What the page says. Left empty, each shows what's in grey."
          >
            <TextField
              label="Name"
              description="Over the conversations and in the browser's tab."
              value={draft.title}
              placeholder={record.document.name || "Agent"}
              maxLength={PAGE_TEXT_LIMITS.title}
              disabled={disabled}
              onChange={(value) => set("title", value)}
            />
            <TextField
              label="Welcome heading"
              value={draft.welcome_title}
              placeholder={`Talk to ${title}`}
              maxLength={PAGE_TEXT_LIMITS.welcome_title}
              disabled={disabled}
              onChange={(value) => set("welcome_title", value)}
            />
            <TextField
              label="Welcome message"
              description="Under the heading, while a conversation is empty."
              value={draft.welcome_message}
              placeholder={record.document.description || "Ask it anything."}
              maxLength={PAGE_TEXT_LIMITS.welcome_message}
              multiline
              disabled={disabled}
              onChange={(value) => set("welcome_message", value)}
            />
            <TextField
              label="Message box"
              value={draft.placeholder}
              placeholder="Send a message..."
              maxLength={PAGE_TEXT_LIMITS.placeholder}
              disabled={disabled}
              onChange={(value) => set("placeholder", value)}
            />
            <TextField
              label="Note under the message box"
              description="e.g. that answers can be wrong, or where to get help."
              value={draft.footer_note}
              placeholder="None"
              maxLength={PAGE_TEXT_LIMITS.footer_note}
              disabled={disabled}
              onChange={(value) => set("footer_note", value)}
            />
          </ConfigSection>

          <ConfigSection
            title="Prompt suggestions"
            description="Under the message box while a conversation is empty: a click sends one."
          >
            <SuggestionsField
              key={generation}
              initial={draft.suggestions}
              disabled={disabled}
              onChange={(prompts) => set("suggestions", prompts)}
            />
          </ConfigSection>

          <ConfigSection title="What it shows">
            <ChatPageOptions
              bare
              page={draft}
              canManage={canManage}
              saving={setting.isPending}
              onChange={(option) =>
                setDraft((current) => ({ ...current, ...option }))
              }
            />
          </ConfigSection>

          <ConfigSection
            title="Look"
            description={`How the page around ${name} looks: its colours, corners, type and width.`}
          >
            <ChoiceField
              label="Theme"
              value={draft.theme}
              options={THEMES}
              disabled={disabled}
              onChange={(value) => set("theme", value)}
            />
            <SwitchField
              label="Let visitors switch the theme"
              description={
                draft.hide_top_bar
                  ? "A button over the page's corner that switches between light and dark. Each visitor's choice is kept in their browser."
                  : "A button in the top bar that switches between light and dark. Each visitor's choice is kept in their browser."
              }
              checked={draft.theme_toggle}
              disabled={disabled}
              onChange={(on) => set("theme_toggle", on)}
            />
            <AccentField
              value={draft.accent}
              disabled={disabled}
              onChange={(value) => set("accent", value)}
            />
            <ChoiceField
              label="Visitors' messages"
              value={draft.bubbles}
              options={BUBBLES}
              disabled={disabled}
              onChange={(value) => set("bubbles", value)}
            />
            <ChoiceField
              label="Corners"
              value={draft.corners}
              options={CORNERS}
              disabled={disabled}
              onChange={(value) => set("corners", value)}
            />
            <ChoiceField
              label="Font"
              value={draft.font}
              options={FONTS}
              disabled={disabled}
              onChange={(value) => set("font", value)}
            />
            <ChoiceField
              label="Conversation width"
              value={draft.width}
              options={WIDTHS}
              disabled={disabled}
              onChange={(value) => set("width", value)}
            />
          </ConfigSection>
        </aside>
        <section
          aria-label="Preview"
          className="min-h-0 min-w-0 flex-1 bg-muted/40"
        >
          <ChatPagePreview
            record={record}
            settings={draft}
            token={page?.token}
          />
        </section>
      </div>
    </>
  )
}
