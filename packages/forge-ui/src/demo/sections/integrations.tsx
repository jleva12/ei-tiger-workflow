import * as React from "react"

import { Button } from "@/components/ui/button"
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { toast } from "@/components/ui/toast"
import {
  FigmaLogo,
  GitHubLogo,
  GitLabLogo,
  GoogleDriveLogo,
  JiraLogo,
  LinearLogo,
  SlackLogo,
} from "@/components/forge/brand-logos"
import { Icon } from "@/components/forge/icon"
import {
  IntegrationLogo,
  IntegrationRow,
  type IntegrationStatus,
} from "@/components/forge/integrations"
import { SettingsList, SettingsRow } from "@/components/forge/settings-list"
import { SectionPage, Specimen } from "../specimen"

type App = {
  id: string
  name: string
  logo: React.ReactNode
  description: string
  status: IntegrationStatus
  /** Who the demo "signs in" as when this app connects. */
  account: string
  detail?: string
}

const initialApps: App[] = [
  {
    id: "github",
    name: "GitHub",
    logo: <GitHubLogo />,
    description:
      "Push task branches, open pull requests and read CI results from your repositories.",
    status: "connected",
    account: "josephleva",
    detail: "Connected Sep 8 · 12 repositories",
  },
  {
    id: "gitlab",
    name: "GitLab",
    logo: <GitLabLogo />,
    description: "Work in GitLab projects and open merge requests for review.",
    status: "disconnected",
    account: "josephleva",
  },
  {
    id: "jira",
    name: "Jira",
    logo: <JiraLogo />,
    description:
      "Create tasks from Jira issues and keep their status in sync both ways.",
    status: "disconnected",
    account: "acme.atlassian.net",
  },
  {
    id: "linear",
    name: "Linear",
    logo: <LinearLogo />,
    description: "Turn Linear issues into coding tasks and post results back.",
    status: "connected",
    account: "Acme workspace",
    detail: "Connected Aug 21",
  },
  {
    id: "figma",
    name: "Figma",
    logo: <FigmaLogo />,
    description:
      "Attach design frames to a task so agents can build to the spec.",
    status: "disconnected",
    account: "Acme design",
  },
  {
    id: "slack",
    name: "Slack",
    logo: <SlackLogo />,
    description: "Get notified when a run finishes or needs your review.",
    status: "error",
    account: "#eng-agents",
    detail: "Access was revoked in Slack",
  },
  {
    id: "drive",
    name: "Google Drive",
    logo: <GoogleDriveLogo />,
    description: "Give agents read access to specs and docs in shared folders.",
    status: "disconnected",
    account: "joseph@acme.dev",
  },
]

function IntegrationsDemo() {
  const [apps, setApps] = React.useState(initialApps)

  const update = (id: string, patch: Partial<App>) =>
    setApps((current) =>
      current.map((app) => (app.id === id ? { ...app, ...patch } : app))
    )

  // Stands in for the OAuth round trip: a popup, the provider's consent
  // screen and the callback that stores the token.
  const connect = (app: App) => {
    update(app.id, { status: "connecting" })
    setTimeout(() => {
      update(app.id, { status: "connected", detail: "Connected just now" })
      toast.add({ title: `${app.name} connected`, type: "success" })
    }, 1400)
  }

  const disconnect = (app: App) => {
    update(app.id, { status: "disconnected", detail: undefined })
    toast.add({
      title: `${app.name} disconnected`,
      description: "Agents can no longer use this connection.",
      type: "info",
    })
  }

  return (
    <SettingsList>
      {apps.map((app) => (
        <IntegrationRow
          key={app.id}
          logo={app.logo}
          name={app.name}
          description={app.description}
          status={app.status}
          account={app.account}
          detail={app.detail}
          onConnect={() => connect(app)}
          onDisconnect={() => disconnect(app)}
          menu={
            app.id === "github" ? (
              <DropdownMenuItem>
                <Icon icon="folder" />
                Choose repositories…
              </DropdownMenuItem>
            ) : undefined
          }
        />
      ))}
    </SettingsList>
  )
}

const logos = [
  { name: "GitHub", logo: <GitHubLogo /> },
  { name: "GitLab", logo: <GitLabLogo /> },
  { name: "Jira", logo: <JiraLogo /> },
  { name: "Linear", logo: <LinearLogo /> },
  { name: "Figma", logo: <FigmaLogo /> },
  { name: "Slack", logo: <SlackLogo /> },
  { name: "Google Drive", logo: <GoogleDriveLogo /> },
]

export function IntegrationsSection() {
  return (
    <SectionPage
      icon="link"
      eyebrow="Workspace"
      title="Integrations"
      description="Settings rows for connecting third-party apps over OAuth: a brand logo in an app tile, what the connection is for, who it's connected as, and one outline button for the next step. The rows render the state; your app runs the OAuth flow."
    >
      <Specimen
        title="Connected apps"
        description="Live: connect, reconnect Slack, or open Manage on a connected app to disconnect it."
        code={`<SettingsList>
  <IntegrationRow
    logo={<GitHubLogo />}
    name="GitHub"
    description="Push task branches and open pull requests."
    status={github.status}            // "disconnected" | "connecting" | "connected" | "error"
    account={github.login}            // shown once connected
    detail="Connected Sep 8"
    onConnect={() => {
      // Redirect (or open a popup) to your OAuth start route
      window.location.assign("/api/oauth/github/start")
    }}
    onDisconnect={() => disconnect.mutate("github")}
    menu={<DropdownMenuItem>Choose repositories…</DropdownMenuItem>}
  />
</SettingsList>`}
      >
        <IntegrationsDemo />
      </Specimen>
      <Specimen
        title="States"
        description="Not connected shows Connect with an external-link glyph (it leaves for the provider); connecting disables it with a spinner; connected shows the account and a Manage menu; a revoked or expired token asks to reconnect."
      >
        <SettingsList>
          <IntegrationRow
            logo={<JiraLogo />}
            name="Jira"
            description="Create tasks from Jira issues."
            status="disconnected"
          />
          <IntegrationRow
            logo={<JiraLogo />}
            name="Jira"
            description="Create tasks from Jira issues."
            status="connecting"
          />
          <IntegrationRow
            logo={<JiraLogo />}
            name="Jira"
            description="Create tasks from Jira issues."
            status="connected"
            account="acme.atlassian.net"
            detail="Connected Sep 8"
            onConnect={() => {}}
            onDisconnect={() => {}}
          />
          <IntegrationRow
            logo={<JiraLogo />}
            name="Jira"
            description="Create tasks from Jira issues."
            status="error"
            detail="The token expired on Sep 20"
          />
        </SettingsList>
      </Specimen>
      <Specimen
        title="Settings rows"
        description="IntegrationRow is built on SettingsRow, which takes any title, description, current value and action — for security, notifications or billing settings."
        code={`<SettingsList>
  <SettingsRow
    title="SMS number"
    description="A one-time code is sent to your registered mobile number."
    meta={<span className="font-medium text-foreground">+4 0123 456 789</span>}
    action={<Button variant="outline" size="sm">Edit</Button>}
  />
</SettingsList>`}
      >
        <SettingsList>
          <SettingsRow
            title="Security keys"
            description="Enhance account security by registering physical security keys."
            meta="No security keys"
            action={
              <Button variant="outline" size="sm" className="min-w-[104px]">
                Add
              </Button>
            }
          />
          <SettingsRow
            title="SMS number"
            description="Opt for SMS-based authentication, where a unique code is sent to your registered mobile number."
            meta={
              <span className="font-medium text-foreground">
                +4 0123 456 789
              </span>
            }
            action={
              <Button variant="outline" size="sm" className="min-w-[104px]">
                Edit
              </Button>
            }
          />
          <SettingsRow
            title="Authenticator app"
            description="Generate time-based one-time passwords (TOTPs) for authentication."
            meta="Not configured"
            action={
              <Button variant="outline" size="sm" className="min-w-[104px]">
                Add
              </Button>
            }
          />
        </SettingsList>
      </Specimen>
      <Specimen
        title="Brand logos"
        description="Inline SVG marks in their official colours; GitHub follows the text colour so it works in dark mode. Frame them with IntegrationLogo, or use them bare at any size."
        code={`import { GitHubLogo, JiraLogo } from "@/components/forge/brand-logos"

<IntegrationLogo><GitHubLogo /></IntegrationLogo>
<JiraLogo size={32} />`}
      >
        <div className="flex flex-wrap gap-x-8 gap-y-5">
          {logos.map((item) => (
            <div
              key={item.name}
              className="flex flex-col items-center gap-2 text-2xs text-muted-foreground"
            >
              <IntegrationLogo className="size-12 [&_svg]:size-6">
                {item.logo}
              </IntegrationLogo>
              {item.name}
            </div>
          ))}
        </div>
      </Specimen>
    </SectionPage>
  )
}
