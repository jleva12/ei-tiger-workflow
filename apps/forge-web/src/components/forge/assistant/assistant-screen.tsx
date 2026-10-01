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
  /** Extra sidebar content above the conversations, e.g. app navigation. */
  sidebar?: React.ReactNode
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
 * A full-screen assistant in the Forge shell: conversations in the sidebar, the
 * active agent and artifacts in the top bar, and the thread with its
 * composer. ADK tool confirmations, sign-in and input requests render as
 * cards inline in the conversation.
 */
function AssistantScreen({
  sidebar,
  actions,
  className,
  ...options
}: AssistantScreenProps) {
  const title = options.title ?? "Assistant"
  return (
    <AssistantProvider {...options}>
      <AppShell className={className}>
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
        <MainPanel>
          <Topbar>
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
          <div className="min-h-0 flex-1">
            <AssistantThread />
          </div>
        </MainPanel>
      </AppShell>
    </AssistantProvider>
  )
}

export { AssistantScreen }
