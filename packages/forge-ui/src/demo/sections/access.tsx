import * as React from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { PanelEmpty } from "@/components/forge/empty-state"
import { Icon } from "@/components/forge/icon"
import { PreferenceOptions } from "@/components/forge/preferences"
import {
  fromCasbin,
  useGuard,
  useUserAccess,
  type UserAccess,
} from "@/lib/user-access"
import { Guard, UserAccessProvider } from "@/lib/user-access-provider"
import { CodeBlock, SectionPage, Specimen } from "../specimen"

/*
 * A stand-in for a Casbin backend with this policy:
 *
 *   p, admin, *, *
 *   p, editor, tasks, (read)|(create)|(update)
 *   p, editor, projects/*, read
 *   p, viewer, tasks, read
 *   p, viewer, projects/*, read
 *   g, admin, editor
 *   g, ava, admin
 *   g, ben, editor
 *   g, cam, viewer
 */
const backend: Record<string, { roles: string[]; policies: string[][] }> = {
  ava: { roles: ["admin", "editor"], policies: [["admin", "*", "*"]] },
  ben: {
    roles: ["editor"],
    policies: [
      ["editor", "tasks", "(read)|(create)|(update)"],
      ["editor", "projects/*", "read"],
    ],
  },
  cam: {
    roles: ["viewer"],
    policies: [
      ["viewer", "tasks", "read"],
      ["viewer", "projects/*", "read"],
    ],
  },
}

const users = [
  { value: "ava", label: "Ava · admin" },
  { value: "ben", label: "Ben · editor" },
  { value: "cam", label: "Cam · viewer" },
]

const loadAccess =
  (user: string) =>
  ({ signal }: { signal: AbortSignal }) =>
    new Promise<UserAccess>((resolve, reject) => {
      const timer = setTimeout(() => resolve(fromCasbin(backend[user]!)), 700)
      signal.addEventListener("abort", () => {
        clearTimeout(timer)
        reject(signal.reason)
      })
    })

function Roles() {
  const roles = useUserAccess((s) => s.roles)
  return (
    <Guard loading={<Skeleton className="h-5 w-28" />}>
      <span className="flex items-center gap-1.5">
        {roles.map((role) => (
          <Badge key={role} variant="secondary">
            {role}
          </Badge>
        ))}
      </span>
    </Guard>
  )
}

function DeleteProject() {
  // Disabled rather than hidden, so people know the action exists.
  const { allowed, status } = useGuard({
    permission: ["projects/42", "delete"],
  })
  return (
    <Button
      variant="destructive"
      size="sm"
      disabled={!allowed}
      title={
        status === "ready" && !allowed
          ? "Needs delete on projects/42"
          : undefined
      }
    >
      <Icon icon="close" data-icon="inline-start" />
      Delete project
    </Button>
  )
}

function AccessDemo() {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Roles</span>
        <Roles />
        <div className="ml-auto flex items-center gap-2">
          <Guard
            permission={["tasks", "create"]}
            loading={<Skeleton className="h-7 w-24" />}
          >
            <Button size="sm">
              <Icon icon="plus" data-icon="inline-start" />
              New task
            </Button>
          </Guard>
          <Guard
            permission={["tasks", "update"]}
            loading={<Skeleton className="h-7 w-20" />}
          >
            <Button variant="outline" size="sm">
              <Icon icon="task" data-icon="inline-start" />
              Edit task
            </Button>
          </Guard>
          <DeleteProject />
        </div>
      </div>
      <div className="rounded-(--radius-card) border">
        <div className="flex items-center justify-between border-b px-4 py-2.5 text-xs">
          <span className="font-medium">Billing</span>
          <span className="text-2xs text-muted-foreground">role: admin</span>
        </div>
        <Guard
          role="admin"
          loading={
            <div className="flex flex-col gap-2 p-4">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          }
          fallback={
            <PanelEmpty illustration="locked">
              Billing is for admins. Ask an admin for access.
            </PanelEmpty>
          }
        >
          <div className="grid grid-cols-3 gap-4 p-4 text-xs">
            <div>
              <div className="text-2xs text-muted-foreground">Plan</div>
              Team
            </div>
            <div>
              <div className="text-2xs text-muted-foreground">Seats</div>
              12 of 15
            </div>
            <div>
              <div className="text-2xs text-muted-foreground">Renews</div>
              Oct 1, 2026
            </div>
          </div>
        </Guard>
      </div>
    </div>
  )
}

export function AccessSection() {
  const [user, setUser] = React.useState("ben")
  return (
    <SectionPage
      icon="team"
      eyebrow="Foundations"
      title="Roles & permissions"
      description="The signed-in user's roles and permissions, loaded once from your Casbin backend, drive Guard and the access hooks: parts of the screen show, hide or disable themselves. Casbin still enforces every request on the server."
    >
      <Specimen
        title="Guards"
        description="Switch user: access reloads from a simulated Casbin backend, and each Guard shows its loading state, then its content or fallback."
        code={`<Guard permission={["tasks", "create"]}>
  <Button>New task</Button>
</Guard>

// Disable instead of hide:
const { allowed } = useGuard({ permission: ["projects/42", "delete"] })
<Button disabled={!allowed}>Delete project</Button>

<Guard
  role="admin"
  fallback={<PanelEmpty illustration="locked">Billing is for admins.</PanelEmpty>}
>
  <Billing />
</Guard>`}
      >
        <div className="flex flex-col gap-4">
          <PreferenceOptions
            label="Signed in as"
            value={user}
            onValueChange={setUser}
            options={users}
          />
          {/* Keyed by user: signing in as someone else starts fresh. */}
          <UserAccessProvider key={user} load={loadAccess(user)}>
            <AccessDemo />
          </UserAccessProvider>
        </div>
      </Specimen>

      <Specimen
        title="Connect your Casbin backend"
        description="One endpoint returns the user's implicit roles and permissions from your Casbin enforcer; fromCasbin turns its policy rows into permissions."
      >
        <CodeBlock
          code={`// App root — loaded once when the user signs in.
<UserProvider
  key={session.userId}
  access={{
    load: async ({ signal }) =>
      fromCasbin((await api.get("/me/access", { signal })).data),
    fallback: <AppLoading />,
  }}
  preferences={{ initial: profile.preferences }}
>
  <App />
</UserProvider>

// Anywhere
const canEdit = useCan("tasks", "update")
const isAdmin = useHasRole("admin")
useUserAccess((s) => s.reload)()  // after the user's roles change`}
        />
        <CodeBlock
          code={`# Python (FastAPI + pycasbin)
@app.get("/me/access")
def my_access(user = Depends(current_user)):
    return {
        "roles": enforcer.get_implicit_roles_for_user(user.id),
        "policies": enforcer.get_implicit_permissions_for_user(user.id),
    }

// Go (casbin v2)
roles, _ := e.GetImplicitRolesForUser(userID)
policies, _ := e.GetImplicitPermissionsForUser(userID)
c.JSON(http.StatusOK, gin.H{"roles": roles, "policies": policies})

// Node (node-casbin)
res.json({
  roles: await e.getImplicitRolesForUser(userId),
  policies: await e.getImplicitPermissionsForUser(userId),
})

// With domains (p = sub, dom, obj, act):
fromCasbin(data, ["sub", "dom", "obj", "act"]) + <UserAccessProvider domain={tenantId}>`}
        />
      </Specimen>
    </SectionPage>
  )
}
