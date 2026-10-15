import * as React from "react"
import { Link } from "@tanstack/react-router"

import { Copyable } from "@/components/forge/copyable"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { toast } from "@/components/ui/toast"
import {
  a2aCardAddress,
  runtimeAddress,
  useChatPage,
  useChatPageSetting,
  type ChatAgentRecord,
  type ChatPageChange,
} from "@/features/agents/lib/api"
import { chatPageLink } from "@/features/agents/lib/chat-page"
import { RuntimeAccess } from "@/features/api-keys/components/runtime-access"
import { ChatPageOptions } from "./chat-page-options"
import { ChatPageSuggestions } from "./chat-page-suggestions"
import { VersionChip } from "@/features/builder/components/versions"
import { toApiError } from "@/lib/api/index"

// The version pieces are shared with the workflows' builder.
export {
  PublishDialog,
  ReadOnlyBanner,
  VersionChip,
} from "@/features/builder/components/versions"

/*
 * How to run a chat agent, as its builder shows it; and, from the builder
 * kit (builder/components/versions), where it is between draft and
 * published: the chip by its name, the read-only banner, the publish dialog.
 */

/**
 * How to talk to the agent: the runtime's address and the app name each
 * version answers to (ADK's run API: POST run_sse), and each one's A2A card
 * (Google's A2A protocol, 1.0 and 0.3), which says where to call it; and
 * its chat page, for people to talk to it at a link.
 */
export function RunInfo({
  record,
  canManage,
}: {
  record: ChatAgentRecord
  canManage: boolean
}) {
  return (
    <>
      <RunEndpoints record={record} />
      <ChatPageSharing record={record} canManage={canManage} />
    </>
  )
}

function RunEndpoints({ record }: { record: ChatAgentRecord }) {
  const endpoint = runtimeAddress("/run_sse")
  const organizationId = record.organization_id
  return (
    <section className="flex flex-col gap-2.5 border-b px-4 py-3.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-foreground">Run it</h3>
        <VersionChip record={record} />
      </div>
      <p className="text-xs/[1.6] text-muted-foreground">
        Any app can talk to it over ADK&apos;s run API: create a session, then
        post each message with its <code className="font-mono">appName</code>.
      </p>
      <RuntimeAccess organizationId={organizationId} />
      <Copyable label="Endpoint" value={endpoint} />
      {record.published_version !== null && (
        <Copyable
          label={`appName: latest published (v${record.published_version})`}
          value={record.id}
        />
      )}
      {record.has_draft && (
        <Copyable label="appName: this draft" value={`${record.id}@draft`} />
      )}
      <p className="pt-1 text-xs/[1.6] text-muted-foreground">
        Other agents can call it over Google&apos;s A2A protocol (JSON-RPC, 1.0
        and 0.3): give them its card, which says where and how.
      </p>
      {record.published_version !== null && (
        <Copyable
          label={`A2A card: latest published (v${record.published_version})`}
          value={a2aCardAddress(record.id)}
        />
      )}
      {record.has_draft && (
        <Copyable
          label="A2A card: this draft"
          value={a2aCardAddress(`${record.id}@draft`)}
        />
      )}
    </section>
  )
}

/**
 * Its chat page: its latest published version as a full-page chat at a link
 * of its own, for anyone it's shared with, signed in to Forge or not. Off
 * keeps the link for turning on again; a new link stops the old one.
 */
function ChatPageSharing({
  record,
  canManage,
}: {
  record: ChatAgentRecord
  canManage: boolean
}) {
  const setting = useChatPageSetting(record.organization_id, record.id)
  const [replacing, setReplacing] = React.useState(false)
  const page = useChatPage(record)
  const published = record.published_version !== null
  const enabled = Boolean(page?.enabled)
  const link = page ? chatPageLink(page.token) : undefined

  const change = (body: ChatPageChange) => {
    // A switch of what the page shows says itself: only a failure is told.
    const option =
      body.hide_history !== undefined ||
      body.hide_top_bar !== undefined ||
      body.follow_ups !== undefined
    setting.mutate(body, {
      onSuccess: (saved) => {
        setReplacing(false)
        if (option) return
        if (body.suggestions)
          toast.add({ title: "Saved the prompt suggestions", type: "success" })
        else if (body.new_link)
          toast.add({
            title: "Made a new link",
            description: "The old one no longer opens the chat.",
            type: "success",
          })
        else if (saved.chat_page?.enabled)
          toast.add({ title: "Chat page on", type: "success" })
      },
      onError: (caught) =>
        toast.add({
          title: option
            ? "Couldn't change the chat page"
            : body.suggestions
              ? "Couldn't save the prompt suggestions"
              : body.new_link
                ? "Couldn't make a new link"
                : body.enabled
                  ? "Couldn't turn on the chat page"
                  : "Couldn't turn off the chat page",
          description: toApiError(caught).message,
          type: "error",
        }),
    })
  }

  return (
    <section className="flex flex-col gap-2.5 border-b px-4 py-3.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-foreground">
          <label htmlFor={`chat-page-${record.id}`}>Chat page</label>
        </h3>
        <Switch
          id={`chat-page-${record.id}`}
          checked={enabled}
          disabled={!canManage || !published || setting.isPending}
          onCheckedChange={(checked) => change({ enabled: checked })}
        />
      </div>
      <p className="text-xs/[1.6] text-muted-foreground">
        {published
          ? enabled
            ? `A full-page chat with its latest published version (v${record.published_version}). Anyone with the link can talk to it, without signing in to Forge.`
            : "Share it as a full-page chat at a link of its own: anyone with the link can talk to its latest published version, without signing in to Forge."
          : "Publish it to share it as a full-page chat at a link of its own."}
      </p>
      {/* Its words, prompts and look, set up beside a live preview of it. */}
      <Button
        variant="outline"
        size="xs"
        className="self-start"
        nativeButton={false}
        render={
          <Link
            to="/organizations/$organizationId/chat-agents/$chatAgentId/chat-page"
            params={{
              organizationId: record.organization_id,
              chatAgentId: record.id,
            }}
          />
        }
      >
        <Icon icon="settings" data-icon="inline-start" />
        Open configuration page
      </Button>
      {enabled && link && (
        <>
          <Copyable label="Link" value={link} />
          {replacing ? (
            <div className="flex flex-col gap-2 rounded-(--radius-control) bg-warning-surface px-2.5 py-2">
              <p className="text-xs/[1.6] text-foreground">
                The current link stops opening the chat for everyone it was
                shared with.
              </p>
              <div className="flex gap-1.5">
                <Button
                  size="xs"
                  disabled={setting.isPending}
                  onClick={() => change({ enabled: true, new_link: true })}
                >
                  Make a new link
                </Button>
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => setReplacing(false)}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex gap-1.5">
              <Button
                variant="outline"
                size="xs"
                nativeButton={false}
                render={<a href={link} target="_blank" rel="noopener" />}
              >
                <Icon icon="external" data-icon="inline-start" />
                Open
              </Button>
              {canManage && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => setReplacing(true)}
                >
                  <Icon icon="refresh" data-icon="inline-start" />
                  New link
                </Button>
              )}
            </div>
          )}
          {page && (
            <ChatPageOptions
              page={page}
              canManage={canManage}
              saving={setting.isPending}
              onChange={(options) => change({ enabled: true, ...options })}
            />
          )}
          <ChatPageSuggestions
            // A save (or another tab's) starts the list afresh.
            key={(page?.suggestions ?? []).join("\n")}
            saved={page?.suggestions ?? []}
            canManage={canManage}
            saving={setting.isPending}
            onSave={(suggestions) => change({ enabled: true, suggestions })}
          />
        </>
      )}
    </section>
  )
}
