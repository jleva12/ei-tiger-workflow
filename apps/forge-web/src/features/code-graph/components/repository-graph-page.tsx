import { ViewTabsList, ViewTabsTrigger } from "@/components/forge/toolbar"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import { IngestionOverview } from "./ingestion-overview"
import { RepositoryCodeGraph } from "./repository-code-graph"

/** A repository's views: its code graph, and what its ingestions put in the system. */
export type RepositoryGraphSection = "graph" | "ingestion"

/**
 * An organization's code repository's page: its code graph to explore, or
 * the Ingestion tab auditing what its code graph ingestions hold, as tabs
 * in the toolbar. The graph stays mounted while the Ingestion tab shows, so
 * coming back finds it as it was left.
 */
export function RepositoryGraphPage({
  organizationId,
  repository,
  openAt,
  canIngest,
  section,
  onSectionChange,
}: {
  organizationId: string
  repository: CodeRepository
  /** A node for the code graph to open at, e.g. followed from a link. */
  openAt?: string
  /** `repositories:manage` in the organization. */
  canIngest: boolean
  section: RepositoryGraphSection
  onSectionChange: (section: RepositoryGraphSection) => void
}) {
  const tabs = (
    <ViewTabsList aria-label="Repository views">
      <ViewTabsTrigger value="graph" icon="layers">
        Code graph
      </ViewTabsTrigger>
      <ViewTabsTrigger value="ingestion" icon="activity">
        Ingestion
      </ViewTabsTrigger>
    </ViewTabsList>
  )
  return (
    <Tabs
      value={section}
      onValueChange={(value) =>
        onSectionChange(value === "ingestion" ? "ingestion" : "graph")
      }
      className="h-full gap-0 @max-[1270px]/shell:h-auto"
    >
      <TabsContent
        value="graph"
        keepMounted
        className="h-full min-h-0 @max-[1270px]/shell:h-auto"
      >
        <RepositoryCodeGraph
          organizationId={organizationId}
          repository={repository}
          openAt={openAt}
          canIngest={canIngest}
          tabs={tabs}
          active={section === "graph"}
        />
      </TabsContent>
      <TabsContent value="ingestion">
        <IngestionOverview
          organizationId={organizationId}
          repository={repository}
          canIngest={canIngest}
          tabs={tabs}
        />
      </TabsContent>
    </Tabs>
  )
}
