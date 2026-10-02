import * as React from "react"
import { useAuiState } from "@assistant-ui/react"

import {
  ThreadListItems,
  ThreadListNew,
  ThreadListRoot,
} from "@/components/assistant-ui/elements/thread-list.aui"
import {
  AppShell,
  MainPanel,
  Topbar,
  TopbarActions,
  TopbarBreadcrumb,
  TopbarPage,
} from "@/components/forge/app-shell"
import { WorkspaceOrb } from "@/components/forge/avatars"
import {
  NavSectionHeading,
  SidebarBrand,
  SidebarBrandName,
  SidebarCollapseButton,
  SidebarSection,
  WorkspaceSidebar,
} from "@/components/forge/workspace-sidebar"

import { AdkAgentIndicator, AdkArtifactsMenu, AdkEscalationBanner } from "./adk"
import {
  AssistantProvider,
  AssistantThread,
  type AssistantOptions,
} from "./assistant-provider"

export type AssistantScreenProps = AssistantOptions & {
  /**
   * Extra sidebar content above the conversations, e.g. app navigation.
   * `false` drops the sidebar and its conversation list: the thread fills
   * the width, and chats are reached some other way (`navigation`, or a
   * history palette in `composerTrailingActions`).
   */
  sidebar?: React.ReactNode | false
  /** Before the title in the top bar, e.g. a menu of the app's pages. */
  navigation?: React.ReactNode
  /** Extra top bar actions, after the agent and artifacts. */
  actions?: React.ReactNode
  /**
   * Beside the conversation, on the right: e.g. a panel tool UIs open. It's
   * inside the assistant's providers, so it can read the session
   * (`useAdkSessionState`) and the thread.
   */
  aside?: React.ReactNode
  className?: string
}

function CurrentThreadTitle({ fallback }: { fallback: string }) {
  const title = useAuiState(
    (s) =>
      s.threads.threadItems.find((t) => t.id === s.threads.mainThreadId)?.title
  )
  return <>{title || fallback}</>
}

/**
 * A full-screen assistant in the Forge shell: conversations in the sidebar
 * (or none, with `sidebar={false}`), the active agent and artifacts in the
 * top bar, the thread with its composer, and an optional panel beside it.
 * ADK tool confirmations, sign-in and input requests render as cards inline
 * in the conversation.
 */
function AssistantScreen({
  sidebar,
  navigation,
  actions,
  aside,
  className,
  ...options
}: AssistantScreenProps) {
  const title = options.title ?? "Assistant"
  const hasSidebar = sidebar !== false
  return (
    <AssistantProvider {...options}>
      <AppShell className={className}>
        {hasSidebar && (
          <WorkspaceSidebar label={title}>
            <SidebarBrand>
              <WorkspaceOrb />
              <SidebarBrandName name={title} />
              <SidebarCollapseButton />
            </SidebarBrand>
            {sidebar}
            <ThreadListRoot className="contents">
              <SidebarSection variant="primary">
                <ThreadListNew className="w-full" />
              </SidebarSection>
              <SidebarSection variant="flush" className="flex-1 border-b-0">
                <NavSectionHeading>Conversations</NavSectionHeading>
                <ThreadListItems />
              </SidebarSection>
            </ThreadListRoot>
          </WorkspaceSidebar>
        )}
        <MainPanel>
          <Topbar sidebarTrigger={hasSidebar}>
            {navigation}
            <TopbarBreadcrumb>
              <TopbarPage icon="sparkles">
                <CurrentThreadTitle fallback="New conversation" />
              </TopbarPage>
            </TopbarBreadcrumb>
            <TopbarActions>
              <AdkAgentIndicator />
              <AdkArtifactsMenu />
              {actions}
            </TopbarActions>
          </Topbar>
          <AdkEscalationBanner />
          <div className="flex min-h-0 flex-1">
            <div className="min-h-0 min-w-0 flex-1">
              <AssistantThread />
            </div>
            {aside}
          </div>
        </MainPanel>
      </AppShell>
    </AssistantProvider>
  )
}

export { AssistantScreen }
