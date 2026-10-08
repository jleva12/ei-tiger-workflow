import { ViewToolbar } from "@/components/forge/app-shell"
import { ShellToolbar } from "@/components/forge/shell/index"
import { ViewTabsList, ViewTabsTrigger } from "@/components/forge/toolbar"
import { Tabs } from "@/components/ui/tabs"
import { OrganizationCodeRepositories } from "@/features/code-repositories/components/organization-code-repositories"
import { useOrganizationCodeRepositories } from "@/features/code-repositories/lib/api"
import { CODE_REPOSITORIES_ICON } from "@/features/code-repositories/lib/display"
import { useOrganizationKnowledgeBases } from "../lib/api"
import { KNOWLEDGE_ICON } from "../lib/knowledge"
import { OrganizationKnowledgeBases } from "./organization-knowledge-bases"

/** The Knowledge bases page's tabs: its knowledge bases, or its code repositories. */
export type KnowledgeTab = "bases" | "repositories"

/** A tab's count, quieter than its label. */
function TabCount({ value }: { value: number | undefined }) {
  if (value === undefined) return null
  return (
    <span className="text-2xs text-subtle tabular-nums">
      {value.toLocaleString()}
    </span>
  )
}

/**
 * An organization's Knowledge bases page: its knowledge bases (each opening
 * its own page), and, as a second tab (`knowledgeTab=repositories`), the
 * code repositories it ingests into the code graph, which its system design
 * knowledge bases include as applications; one opens in a side panel
 * (`repository`). Only the open tab renders, so its header actions are the
 * ones shown.
 */
export function OrganizationKnowledge({
  organizationId,
  organizationName,
  tab,
  onTabChange,
  openRepository,
  onOpenRepository,
}: {
  organizationId: string
  organizationName: string
  tab: KnowledgeTab
  onTabChange: (tab: KnowledgeTab) => void
  /** On Code repositories, the repository open in its side panel. */
  openRepository: string | undefined
  onOpenRepository: (id: string | undefined) => void
}) {
  // The pages' own lists, shared through the query cache.
  const bases = useOrganizationKnowledgeBases(organizationId)
  const repositories = useOrganizationCodeRepositories(organizationId)

  return (
    <Tabs
      value={tab}
      onValueChange={(value) =>
        onTabChange(value === "repositories" ? "repositories" : "bases")
      }
      className="gap-0"
    >
      <ShellToolbar>
        <ViewToolbar>
          <ViewTabsList aria-label="Knowledge">
            <ViewTabsTrigger value="bases" icon={KNOWLEDGE_ICON}>
              Knowledge bases
              <TabCount value={bases.data?.length} />
            </ViewTabsTrigger>
            <ViewTabsTrigger value="repositories" icon={CODE_REPOSITORIES_ICON}>
              Code repositories
              <TabCount value={repositories.data?.length} />
            </ViewTabsTrigger>
          </ViewTabsList>
        </ViewToolbar>
      </ShellToolbar>
      {tab === "repositories" ? (
        <OrganizationCodeRepositories
          organizationId={organizationId}
          organizationName={organizationName}
          openRepository={openRepository}
          onOpenRepository={onOpenRepository}
        />
      ) : (
        <OrganizationKnowledgeBases
          organizationId={organizationId}
          organizationName={organizationName}
        />
      )}
    </Tabs>
  )
}
