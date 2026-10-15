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
import { Icon } from "@/components/forge/icon"
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
   * the width, and the top bar has the new-conversation button.
   */
  sidebar?: React.ReactNode | false
  /**
   * `false` drops the top bar (the conversation's title, the agent and
   * artifacts): only the conversation and its composer.
   */
  topbar?: boolean
  /** Extra top bar actions, after the agent and artifacts. */
  actions?: React.ReactNode
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
 * top bar (or none, with `topbar={false}`), and the thread with its
 * composer. ADK tool confirmations, sign-in and input requests render as
 * cards inline in the conversation.
 */
function AssistantScreen({
  sidebar,
  topbar = true,
  actions,
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
          {topbar && (
            <Topbar sidebarTrigger={hasSidebar}>
              <TopbarBreadcrumb>
                <TopbarPage icon="sparkles">
                  <CurrentThreadTitle fallback="New conversation" />
                </TopbarPage>
              </TopbarBreadcrumb>
              <TopbarActions>
                <AdkAgentIndicator />
                <AdkArtifactsMenu />
                {actions}
                {/* Without the sidebar, a new conversation starts here. */}
                {!hasSidebar && (
                  <ThreadListNew className="h-7 w-auto gap-1.5 px-2 text-xs">
                    <Icon icon="plus" data-icon="inline-start" />
                    New conversation
                  </ThreadListNew>
                )}
              </TopbarActions>
            </Topbar>
          )}
          <AdkEscalationBanner />
          <div className="min-h-0 flex-1">
            <AssistantThread />
          </div>
        </MainPanel>
      </AppShell>
    </AssistantProvider>
  )
}

export { AssistantScreen }
