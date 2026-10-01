import { UnfoldMoreIcon, UserShield01Icon } from "@hugeicons/core-free-icons"
import { useNavigate } from "@tanstack/react-router"

import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import { useMyOrganizations } from "@/lib/access"
import { useHasRole } from "@/lib/user-access"
import {
  SITE_ADMIN,
  useWorkspaceScope,
  type WorkspaceScope,
} from "@/lib/workspace-scope"

/** Your roles in an organization, e.g. "Admin, Member". */
const roleNames = (roles: string[]) =>
  roles
    .map((role) => role.slice(role.indexOf(":") + 1).replaceAll("_", " "))
    .map((name) => name.charAt(0).toUpperCase() + name.slice(1))
    .join(", ")

function Tile({ icon }: { icon: IconProp }) {
  return (
    <span className="flex size-7 shrink-0 items-center justify-center rounded-(--radius-item) border bg-background text-foreground">
      <Icon icon={icon} size={15} />
    </span>
  )
}

function Choice({
  icon,
  title,
  detail,
  current,
}: {
  icon: IconProp
  title: string
  detail: string
  current: boolean
}) {
  return (
    <>
      <Tile icon={icon} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-xs text-foreground">{title}</span>
        <span className="truncate text-2xs text-muted-foreground">
          {detail}
        </span>
      </span>
      {current && <Icon icon="check" className="ml-auto" />}
    </>
  )
}

/**
 * Where you're working, first in the sub nav: the admin scope (site
 * administrators only) or one of your organizations. Choosing one opens its page:
 * site administration, or the organization's page.
 */
export function ScopeSwitcher() {
  const isAdmin = useHasRole(SITE_ADMIN)
  const organizations = useMyOrganizations()
  const scope = useWorkspaceScope((state) => state.scope)
  const setScope = useWorkspaceScope((state) => state.setScope)
  const navigate = useNavigate()

  const mine = organizations.data ?? []
  const organization = mine.find((o) => `org:${o.id}` === scope)
  const current =
    scope === "admin" && isAdmin
      ? {
          icon: UserShield01Icon,
          title: "Admin",
          detail: "Site administration",
        }
      : organization
        ? {
            icon: "layers" as const,
            title: organization.name,
            detail: "Organization",
          }
        : {
            icon: "layers" as const,
            title: "Choose where to work",
            detail: isAdmin
              ? "Admin or an organization"
              : "One of your organizations",
          }

  function choose(next: WorkspaceScope) {
    setScope(next)
    if (next === "admin") void navigate({ to: "/admin/organizations" })
    else
      void navigate({
        to: "/organizations/$organizationId",
        params: { organizationId: next.slice("org:".length) },
      })
  }

  // One root element throughout: pages add their own sub nav after it, in
  // the same place, and a root that remounted would land below theirs.
  return (
    <div
      data-slot="scope-switcher"
      className="px-2 pt-2"
      aria-busy={organizations.isPending || undefined}
    >
      {organizations.isPending ? (
        <Skeleton className="h-11" />
      ) : (
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <button
                type="button"
                aria-label={`Working in ${current.title}; switch`}
                className="flex w-full min-w-0 items-center gap-2.5 rounded-(--radius-item) border bg-background px-2 py-1.5 text-left hover:bg-accent data-popup-open:bg-accent"
              />
            }
          >
            <Tile icon={current.icon} />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-xs font-medium text-foreground">
                {current.title}
              </span>
              <span className="truncate text-2xs text-muted-foreground">
                {current.detail}
              </span>
            </span>
            <Icon
              icon={UnfoldMoreIcon}
              size={15}
              className="shrink-0 text-muted-foreground"
            />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="start"
            className="w-(--anchor-width) min-w-60"
          >
            {isAdmin && (
              <DropdownMenuGroup>
                <DropdownMenuLabel>Administration</DropdownMenuLabel>
                <DropdownMenuItem onClick={() => choose("admin")}>
                  <Choice
                    icon={UserShield01Icon}
                    title="Admin"
                    detail="Organizations, users and access"
                    current={scope === "admin"}
                  />
                </DropdownMenuItem>
              </DropdownMenuGroup>
            )}
            {isAdmin && mine.length > 0 && <DropdownMenuSeparator />}
            {mine.length > 0 && (
              <DropdownMenuGroup>
                <DropdownMenuLabel>Your organizations</DropdownMenuLabel>
                {mine.map((o) => (
                  <DropdownMenuItem
                    key={o.id}
                    onClick={() => choose(`org:${o.id}`)}
                  >
                    <Choice
                      icon="layers"
                      title={o.name}
                      detail={roleNames(o.roles)}
                      current={scope === `org:${o.id}`}
                    />
                  </DropdownMenuItem>
                ))}
              </DropdownMenuGroup>
            )}
            {!isAdmin && mine.length === 0 && (
              <DropdownMenuGroup>
                <DropdownMenuLabel>
                  You're not in an organization yet. Ask an organization admin
                  or a site administrator to add you.
                </DropdownMenuLabel>
              </DropdownMenuGroup>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}
