import { Key01Icon, UserShield01Icon } from "@hugeicons/core-free-icons"

import { useShellPage, type ShellConfig } from "@/components/forge/shell"
import { SITE_ADMIN } from "@/lib/workspace-scope"

const adminSidebar: ShellConfig["sidebar"] = {
  label: "Site administration",
  sections: [
    {
      id: "administration",
      title: "Administration",
      items: [
        {
          id: "organizations",
          label: "Organizations",
          icon: "layers",
          href: "/admin/organizations",
          role: SITE_ADMIN,
        },
      ],
    },
    {
      id: "access",
      title: "Access",
      items: [
        {
          id: "users",
          label: "Users",
          icon: "team",
          href: "/admin/users",
          role: SITE_ADMIN,
        },
        {
          id: "roles",
          label: "Roles",
          icon: UserShield01Icon,
          href: "/admin/roles",
          role: SITE_ADMIN,
        },
        {
          id: "permissions",
          label: "Permissions",
          icon: Key01Icon,
          href: "/admin/permissions",
          role: SITE_ADMIN,
        },
      ],
    },
  ],
}

/**
 * Site administration's sub nav: Organizations, then Users, Roles and
 * Permissions, for site administrators. Only the admin scope shows it (its
 * layout, admin/route.tsx, and error pages while you work in it); the
 * shell's own sub nav is empty, so no organization's page falls back to it.
 */
export function useAdminNav() {
  useShellPage({ sidebar: adminSidebar })
}
