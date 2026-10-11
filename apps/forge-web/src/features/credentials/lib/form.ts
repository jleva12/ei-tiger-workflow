import * as React from "react"

import { defaultsOf } from "@/features/mcp-servers/lib/display"
import {
  startCredentialOAuth,
  useCredentialAuthMethods,
  type CredentialCreate,
  type CredentialRecord,
} from "./api"
import {
  CREDENTIAL_OAUTH_RETURN_KEY,
  type CredentialOAuthReturn,
} from "./display"

/*
 * A credential's form state, as the Credentials page's dialog and a step's or
 * tool's "New credential" card keep it (their fields are
 * components/credential-form.tsx), and signing in with one.
 */

/** What a new credential starts with: a name, and where it'll be sent. */
export type CredentialStart = { name?: string; urls?: string[] }

/** One of the addresses a credential may go to, as its form lists them. */
export type UrlDraft = { id: string; url: string }

export const draftId = () => Math.random().toString(36).slice(2, 10)

/** Where a credential may go: an http(s) origin, and optionally a path. */
export const CREDENTIAL_URL = /^https?:\/\/[^\s/?#]+(\/[^\s?#]*)?$/

/** A credential's form: what's typed, what's wrong, and what it saves as. */
export function useCredentialForm(
  credential?: CredentialRecord,
  start?: CredentialStart
) {
  const methods = useCredentialAuthMethods()
  const [name, setName] = React.useState(credential?.name ?? start?.name ?? "")
  const [description, setDescription] = React.useState(
    credential?.description ?? ""
  )
  const [urls, setUrls] = React.useState<UrlDraft[]>(() =>
    (credential?.urls ?? start?.urls ?? [""]).map((url) => ({
      id: draftId(),
      url,
    }))
  )
  const [kind, setKind] = React.useState(credential?.auth.kind ?? "bearer")
  const [settings, setSettings] = React.useState<Record<string, string>>(
    credential?.auth.settings ?? {}
  )
  // Secrets typed in; those left empty keep what's saved.
  const [secrets, setSecrets] = React.useState<Record<string, string>>({})
  // Saved secrets to remove.
  const [cleared, setCleared] = React.useState<string[]>([])
  const [dirty, setDirty] = React.useState(false)
  const [submitted, setSubmitted] = React.useState(false)

  const method = methods.data?.find((m) => m.kind === kind)
  // Saved secrets count only while the method stays the same.
  const saved =
    credential && credential.auth.kind === kind
      ? credential.auth.secrets_set
      : []
  const values = { ...defaultsOf(method), ...settings }
  const touch = () => setDirty(true)

  const pickKind = (next: string) => {
    setKind(next)
    setSecrets({})
    setCleared([])
    const defaults = defaultsOf(methods.data?.find((m) => m.kind === next))
    setSettings(
      credential && credential.auth.kind === next
        ? { ...defaults, ...credential.auth.settings }
        : defaults
    )
    touch()
  }

  const written = urls.map((u) => u.url.trim()).filter(Boolean)
  const problems = {
    name: !name.trim() ? "Give the credential a name." : undefined,
    urls: !written.length
      ? "Say where it may be sent: the API's address."
      : written.find((url) => !CREDENTIAL_URL.test(url))
        ? "Each is an https:// (or http://) address, without ? or #."
        : undefined,
    fields: Object.fromEntries(
      (method?.fields ?? [])
        .filter((f) => f.required)
        .filter((f) =>
          f.secret
            ? !secrets[f.name]?.trim() &&
              (!saved.includes(f.name) || cleared.includes(f.name))
            : !values[f.name]?.trim()
        )
        .map((f) => [f.name, `${f.label} is needed.`])
    ) as Record<string, string>,
  }
  const invalid =
    Boolean(problems.name || problems.urls) ||
    Object.keys(problems.fields).length > 0 ||
    !method

  /** What it saves as. */
  const input = (): CredentialCreate => ({
    name: name.trim(),
    description: description.trim(),
    urls: [...new Set(written)],
    auth: {
      kind,
      settings: Object.fromEntries(
        (method?.fields ?? [])
          .filter((f) => !f.secret)
          .map((f) => [f.name, (values[f.name] ?? "").trim()])
      ),
      secrets: {
        ...Object.fromEntries(cleared.map((field) => [field, null])),
        ...Object.fromEntries(
          Object.entries(secrets).filter(([, value]) => value.trim())
        ),
      },
    },
  })

  /** After a save: what's typed starts over from what's saved. */
  const saved_ = () => {
    setSecrets({})
    setCleared([])
    setDirty(false)
    setSubmitted(false)
  }

  return {
    methods,
    method,
    kind,
    pickKind,
    name,
    setName: (value: string) => {
      setName(value)
      touch()
    },
    description,
    setDescription: (value: string) => {
      setDescription(value)
      touch()
    },
    urls,
    setUrls: (value: UrlDraft[]) => {
      setUrls(value)
      touch()
    },
    values,
    setSetting: (field: string, value: string) => {
      setSettings((prev) => ({ ...prev, [field]: value }))
      touch()
    },
    secrets,
    setSecret: (field: string, value: string) => {
      setSecrets((prev) => ({ ...prev, [field]: value }))
      setCleared((prev) => prev.filter((other) => other !== field))
      touch()
    },
    saved,
    cleared,
    clearSecret: (field: string) => {
      setCleared((prev) => [...prev, field])
      setSecrets((prev) => ({ ...prev, [field]: "" }))
      touch()
    },
    dirty,
    submitted,
    setSubmitted,
    problems,
    invalid,
    input,
    reset: saved_,
  }
}

export type CredentialForm = ReturnType<typeof useCredentialForm>

/**
 * Sends the browser to sign in with a credential; it comes back to
 * /oauth/credentials/callback, then to the page it left.
 */
export async function signInWith(credential: CredentialRecord) {
  const { authorization_url } = await startCredentialOAuth(
    credential.organization_id,
    credential.id
  )
  const back: CredentialOAuthReturn = {
    organizationId: credential.organization_id,
    credentialId: credential.id,
    path: `${window.location.pathname}${window.location.search}`,
  }
  try {
    sessionStorage.setItem(CREDENTIAL_OAUTH_RETURN_KEY, JSON.stringify(back))
  } catch {
    // Without storage the callback goes to the Credentials page.
  }
  window.location.assign(authorization_url)
}
