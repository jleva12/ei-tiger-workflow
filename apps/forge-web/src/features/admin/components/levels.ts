import type { IconName } from "@/components/forge/icons"

/** How the admin UI names and draws one level of the hierarchy. */
export type Level = {
  /** For sentences: "organization". */
  noun: string
  /** Its indefinite article: "an organization". */
  article: "a" | "an"
  /** For titles and buttons: "Organization". */
  title: string
  plural: string
  icon: IconName
  /** Its resource in permission keys: "organizations" in `organizations:create`. */
  resource: string
}

export const LEVELS = {
  organization: {
    noun: "organization",
    article: "an",
    title: "Organization",
    plural: "Organizations",
    icon: "layers",
    resource: "organizations",
  },
} as const satisfies Record<string, Level>
