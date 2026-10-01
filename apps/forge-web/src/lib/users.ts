import * as React from "react"
import { useQuery } from "@tanstack/react-query"

import { createResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { Audited } from "@/lib/hierarchy"

/*
 * The people who use Forge, from the admin API's `/users`. Their `id` is the
 * subject their role assignments name. Anyone signed in can list them;
 * changing them takes `users:*` on the site.
 */

export type User = Audited & {
  id: string
  first_name: string
  last_name: string
  /** Lowercase; unique. */
  email: string
  /** Their MS ID (network user ID); lowercase, unique. */
  msid: string
}

/** What creates or edits a user. */
export type UserInput = Pick<
  User,
  "first_name" | "last_name" | "email" | "msid"
>

/** "Ada Lovelace". */
export const userName = (user: Pick<User, "first_name" | "last_name">) =>
  `${user.first_name} ${user.last_name}`

export const users = createResource<
  User,
  { create: UserInput; update: Partial<UserInput> }
>({
  api,
  path: "/users",
  key: "users",
  label: "user",
})

// ID → email, rebuilt only when the users list changes.
const toEmails = (list: User[]) =>
  new Map(list.map((user) => [user.id, user.email]))

/**
 * Names whoever an audit column (`created_by`, `updated_by`) records: a user
 * by their email, anything else (`seed`, `system`, a service) as it is. Until
 * the users have loaded, the raw value.
 */
export function useActorLabel() {
  const { data: emails } = users.useList(undefined, { select: toEmails })
  return React.useCallback(
    (actor: string) => emails?.get(actor) ?? actor,
    [emails]
  )
}

/**
 * Rows with their `created_by` and `updated_by` named by `useActorLabel`, so a
 * table shows, sorts, searches and exports emails rather than user IDs.
 */
export function useActorLabels<T extends Audited>(rows: T[] | undefined): T[] {
  const label = useActorLabel()
  return React.useMemo(
    () =>
      (rows ?? []).map((row) => ({
        ...row,
        created_by: label(row.created_by),
        updated_by: label(row.updated_by),
      })),
    [rows, label]
  )
}

/** Who the API says is signed in: their subject ID and, if a user, profile. */
export type Me = { subject: string; user: User | null }

/**
 * The signed-in user, from `GET /me`. Keyed under the users, so editing a
 * user (yourself included) refreshes it.
 */
export function useMe() {
  return useQuery({
    queryKey: [...users.keys.all, "me"],
    queryFn: ({ signal }) => api.get<Me>("/me", { signal }),
  })
}
