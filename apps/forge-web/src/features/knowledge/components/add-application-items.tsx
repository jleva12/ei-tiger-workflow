import { GithubIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import {
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
} from "@/components/ui/dropdown-menu"
import { CODE_REPOSITORIES_ICON } from "@/features/code-repositories/lib/display"

/** Where a new application comes from: the organization, or GitHub. */
export type ApplicationSource = "existing" | "new"

/**
 * A menu's ways to add an application to a system design knowledge base:
 * one of the organization's code repositories, or a GitHub repository by
 * its URL.
 */
export function AddApplicationItems({
  organizationName,
  onPick,
}: {
  organizationName: string
  onPick: (from: ApplicationSource) => void
}) {
  return (
    <DropdownMenuGroup>
      <DropdownMenuLabel>Add an application…</DropdownMenuLabel>
      <DropdownMenuItem
        onClick={() => onPick("existing")}
        className="items-start"
      >
        <Icon icon={CODE_REPOSITORIES_ICON} className="mt-0.5" />
        <span className="flex min-w-0 flex-col">
          <span className="text-foreground">
            From {organizationName}'s repositories
          </span>
          <span className="text-2xs text-muted-foreground">
            One under Knowledge bases → Code repositories
          </span>
        </span>
      </DropdownMenuItem>
      <DropdownMenuItem onClick={() => onPick("new")} className="items-start">
        <Icon icon={GithubIcon} className="mt-0.5" />
        <span className="flex min-w-0 flex-col">
          <span className="text-foreground">A GitHub repository</span>
          <span className="text-2xs text-muted-foreground">
            By its URL, added to {organizationName} and ingested
          </span>
        </span>
      </DropdownMenuItem>
    </DropdownMenuGroup>
  )
}
