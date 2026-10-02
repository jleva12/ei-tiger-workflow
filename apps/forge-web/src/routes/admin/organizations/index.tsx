import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"

import { LEVELS } from "@/features/admin/components/levels"
import { ChildNodes } from "@/features/admin/components/node-table"
import { PrimaryAction } from "@/components/forge/app-shell"
import { ShellHeaderActions, useShellPage } from "@/components/forge/shell"
import { memberKeys } from "@/lib/access"
import { organizations, useScopeAccess } from "@/lib/hierarchy"

/** The site's organizations to configure, each opening its own. */
export const Route = createFileRoute("/admin/organizations/")({
  // Site administration: the /admin layout lets only site administrators in.
  component: OrganizationsPage,
})

// The dialogs show failures themselves, so skip the error toast.
const SILENT = { meta: { silent: true } }

function OrganizationsPage() {
  const navigate = useNavigate()
  const can = useScopeAccess("site")
  const list = organizations.useList()
  const queryClient = useQueryClient()
  // Its creator becomes its administrator: the switcher lists it at once.
  const create = organizations.useCreate({
    ...SILENT,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: memberKeys.all }),
  })
  const update = organizations.useUpdate(SILENT)
  const remove = organizations.useDelete(SILENT)
  const [creating, setCreating] = React.useState(false)

  useShellPage({ header: { title: "Organizations", icon: "layers" } })

  return (
    <>
      <ShellHeaderActions>
        {can("organizations:create") && (
          <PrimaryAction onClick={() => setCreating(true)}>
            Organization
          </PrimaryAction>
        )}
      </ShellHeaderActions>
      <ChildNodes
        level={LEVELS.organization}
        list={list}
        can={can}
        creating={creating}
        onCreatingChange={setCreating}
        create={(input) => create.mutateAsync(input)}
        update={(id, input) => update.mutateAsync({ id, data: input })}
        remove={(id) => remove.mutateAsync(id)}
        onOpen={(organization) =>
          void navigate({
            to: "/admin/organizations/$organizationId",
            params: { organizationId: organization.id },
          })
        }
        description="Each organization holds its members, ADK workflows and agents."
      />
    </>
  )
}
