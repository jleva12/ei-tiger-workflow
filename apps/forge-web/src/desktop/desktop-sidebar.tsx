import * as React from "react"
import { PencilEdit02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"

import type { AgentConversations, Conversation } from "./conversations"
import { keyOf, type DesktopAgent } from "./library"
import { DRAG_REGION } from "./window-chrome"

/** How many of an agent's conversations show under it before "Show more". */
const NESTED = 3
/** How many conversations Recents shows, across agents. */
const RECENT = 8

const UNTITLED = "New conversation"

/**
 * The desktop app's sidebar: a new conversation with the open agent, every
 * added agent with its latest conversations under it, and the latest
 * conversations across them all.
 */
export function DesktopSidebar({
  groups,
  open,
  shown,
  replying,
  onNewChat,
  onOpenAgent,
  onOpenConversation,
  onRemoveAgent,
  onDeleteConversation,
  onAdd,
}: {
  groups: AgentConversations[]
  /** The agent open in the chat beside it. */
  open: DesktopAgent | undefined
  /** The conversation the chat shows; none for a new one. */
  shown: string | undefined
  /** Each agent's conversations with a reply running, by its key. */
  replying: ReadonlyMap<string, readonly string[]>
  onNewChat: () => void
  onOpenAgent: (agent: DesktopAgent) => void
  onOpenConversation: (agent: DesktopAgent, id: string) => void
  onRemoveAgent: (agent: DesktopAgent) => void
  onDeleteConversation: (agent: DesktopAgent, id: string) => void
  onAdd: () => void
}) {
  const recents = React.useMemo(
    () =>
      groups
        .flatMap(({ agent, conversations }) =>
          conversations.map((conversation) => ({ agent, conversation }))
        )
        .sort((a, b) => b.conversation.updated - a.conversation.updated)
        .slice(0, RECENT),
    [groups]
  )
  const openKey = open && keyOf(open)
  const isShown = (agent: DesktopAgent, id: string) =>
    keyOf(agent) === openKey && id === shown
  const isReplying = (agent: DesktopAgent, id: string) =>
    replying.get(keyOf(agent))?.includes(id) ?? false

  return (
    <aside
      aria-label="Agents and conversations"
      data-slot="desktop-sidebar"
      className="flex w-[264px] shrink-0 flex-col border-r bg-muted/40"
    >
      <div
        {...DRAG_REGION}
        className="flex h-12 shrink-0 items-center pr-2 pl-4"
      >
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground">
          Forge Agents
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Add an agent"
          title="Add an agent"
          onClick={onAdd}
        >
          <Icon icon="plus" />
        </Button>
      </div>
      <div className="shrink-0 px-2">
        <Row
          icon={PencilEdit02Icon}
          disabled={!open}
          active={Boolean(open) && shown === undefined}
          onClick={onNewChat}
          title={open ? `A new conversation with ${open.name}` : undefined}
        >
          New chat
        </Row>
      </div>
      <nav
        aria-label="Agents"
        tabIndex={0}
        className="min-h-0 flex-1 overflow-y-auto px-2 pb-3 outline-none"
      >
        <SectionLabel>Agents</SectionLabel>
        {groups.length === 0 && (
          <p className="px-2 py-1 text-xs text-muted-foreground">
            No agents yet. Add one from its chat page's link.
          </p>
        )}
        {groups.map((group) => (
          <AgentGroup
            key={keyOf(group.agent)}
            group={group}
            isOpen={keyOf(group.agent) === openKey}
            replying={replying.has(keyOf(group.agent))}
            isShown={(id) => isShown(group.agent, id)}
            isReplying={(id) => isReplying(group.agent, id)}
            onOpenAgent={() => onOpenAgent(group.agent)}
            onOpenConversation={(id) => onOpenConversation(group.agent, id)}
            onRemoveAgent={() => onRemoveAgent(group.agent)}
            onDeleteConversation={(id) => onDeleteConversation(group.agent, id)}
          />
        ))}
        {recents.length > 0 && (
          <>
            <SectionLabel>Recents</SectionLabel>
            {recents.map(({ agent, conversation }) => (
              <ConversationRow
                key={`${keyOf(agent)} ${conversation.id}`}
                conversation={conversation}
                agentName={agent.name}
                active={isShown(agent, conversation.id)}
                replying={isReplying(agent, conversation.id)}
                onOpen={() => onOpenConversation(agent, conversation.id)}
                onDelete={() => onDeleteConversation(agent, conversation.id)}
              />
            ))}
          </>
        )}
      </nav>
    </aside>
  )
}

/** An agent, and its latest conversations under it. */
function AgentGroup({
  group: { agent, conversations, loading, error, retry },
  isOpen,
  replying,
  isShown,
  isReplying,
  onOpenAgent,
  onOpenConversation,
  onRemoveAgent,
  onDeleteConversation,
}: {
  group: AgentConversations
  isOpen: boolean
  /** Whether any of its conversations has a reply running. */
  replying: boolean
  isShown: (id: string) => boolean
  isReplying: (id: string) => boolean
  onOpenAgent: () => void
  onOpenConversation: (id: string) => void
  onRemoveAgent: () => void
  onDeleteConversation: (id: string) => void
}) {
  const [all, setAll] = React.useState(false)
  const listed = all ? conversations : conversations.slice(0, NESTED)
  return (
    <div role="group" aria-label={agent.name} className="mb-1">
      <Actions
        label={`${agent.name}: more`}
        items={[{ label: "Remove from this app", onSelect: onRemoveAgent }]}
      >
        <Row
          icon="sparkles"
          onClick={onOpenAgent}
          // Room for its menu at the end.
          className={cn("pr-8", isOpen && "font-medium text-foreground")}
          end={replying && <Replying />}
          title={`${
            isOpen
              ? `A new conversation with ${agent.name}`
              : `Open ${agent.name}`
          } · ${
            agent.kind === "member"
              ? agent.organizationName
              : "from a chat page's link"
          }`}
        >
          {agent.name}
        </Row>
      </Actions>
      {error ? (
        <p className="py-1 pr-2 pl-8 text-xs text-muted-foreground">
          Couldn't load its conversations.{" "}
          <button
            type="button"
            className="font-medium text-foreground underline-offset-2 hover:underline"
            onClick={retry}
          >
            Retry
          </button>
        </p>
      ) : loading ? (
        <div className="flex flex-col gap-2 py-1.5 pr-2 pl-8" aria-busy="true">
          <Skeleton className="h-3 w-36" />
          <Skeleton className="h-3 w-28" />
        </div>
      ) : (
        <>
          {listed.map((conversation) => (
            <ConversationRow
              key={conversation.id}
              nested
              conversation={conversation}
              active={isShown(conversation.id)}
              replying={isReplying(conversation.id)}
              onOpen={() => onOpenConversation(conversation.id)}
              onDelete={() => onDeleteConversation(conversation.id)}
            />
          ))}
          {conversations.length > NESTED && (
            <button
              type="button"
              onClick={() => setAll((shown) => !shown)}
              className="flex h-7 w-full items-center rounded-(--radius-control) pr-2 pl-8 text-left text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              {all ? "Show less" : "Show more"}
            </button>
          )}
        </>
      )}
    </div>
  )
}

function ConversationRow({
  conversation,
  agentName,
  nested = false,
  active,
  replying,
  onOpen,
  onDelete,
}: {
  conversation: Conversation
  /** Its agent's name, where it's listed away from it (Recents). */
  agentName?: string
  nested?: boolean
  active: boolean
  /** Whether a reply is running in it. */
  replying: boolean
  onOpen: () => void
  onDelete: () => void
}) {
  const title = conversation.title ?? UNTITLED
  return (
    <Actions
      label={`${title}: more`}
      items={[
        { label: "Delete conversation", onSelect: onDelete, destructive: true },
      ]}
    >
      <Row
        active={active}
        onClick={onOpen}
        className={cn("pr-8", nested && "pl-8")}
        end={replying && <Replying />}
        title={agentName ? `${title} · ${agentName}` : title}
      >
        {title}
      </Row>
    </Actions>
  )
}

/** A reply running there, at the end of its row. */
function Replying() {
  return (
    <Spinner
      aria-label="Replying"
      className="size-3.5 shrink-0 text-muted-foreground"
    />
  )
}

/** One line of the sidebar: an icon (or none, nested), its words, its marks. */
function Row({
  icon,
  active = false,
  end,
  className,
  children,
  ...props
}: React.ComponentProps<"button"> & {
  icon?: IconProp
  active?: boolean
  /** What follows its words: a reply running there. */
  end?: React.ReactNode
}) {
  return (
    <button
      type="button"
      aria-current={active ? "page" : undefined}
      data-active={active || undefined}
      className={cn(
        "flex h-8 w-full min-w-0 items-center gap-2 rounded-(--radius-control) px-2 text-left text-sm text-foreground/85 outline-none hover:bg-accent hover:text-foreground focus-visible:bg-accent disabled:pointer-events-none disabled:opacity-50 data-active:bg-accent data-active:font-medium data-active:text-foreground",
        className
      )}
      {...props}
    >
      {icon && (
        <Icon
          icon={icon}
          size={16}
          className="shrink-0 text-muted-foreground"
        />
      )}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {end}
    </button>
  )
}

/** A row with a "more" menu over its end, shown on hover or focus. */
function Actions({
  label,
  items,
  children,
}: {
  label: string
  items: { label: string; onSelect: () => void; destructive?: boolean }[]
  children: React.ReactNode
}) {
  return (
    <div className="group/row relative">
      {children}
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label={label}
              className="absolute top-1 right-1 opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 data-popup-open:opacity-100"
            />
          }
        >
          <Icon icon="more" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {items.map((item) => (
            <DropdownMenuItem
              key={item.label}
              variant={item.destructive ? "destructive" : undefined}
              onClick={item.onSelect}
            >
              {item.label}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="px-2 pt-4 pb-1 text-xs font-medium text-muted-foreground">
      {children}
    </h2>
  )
}
