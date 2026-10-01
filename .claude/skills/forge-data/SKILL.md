---
name: forge-data
description: How to load and change server data in apps built on the Forge UI design system — the axios API client (`@/lib/api`), the TanStack Query client (`@/lib/api/query-client`) and the generic CRUD hooks from `createResource` / `createNestedResource` (`@/lib/api/resource`). Use this skill whenever code needs to call a backend or REST API, show lists or detail records from a server, submit forms, create/update/delete entities, paginate or infinite-scroll, make an update optimistic, handle API errors, 401s or auth tokens, or set up axios / TanStack Query — even if the user doesn't name these files, and before hand-writing any fetch, axios, useQuery or useMutation code.
---

# Forge data layer

The data layer has three layers, each built on the one below. Reach for the
highest one that fits, because each layer removes code you'd otherwise repeat
and get subtly wrong (auth headers, token refresh, cancellation, cache
invalidation, error toasts):

| You need… | Use | Source |
| --- | --- | --- |
| List / detail / create / update / delete on a REST collection, including nested (`/projects/:id/tasks`), paginated or infinite lists and optimistic updates | `createResource` / `createNestedResource` | `src/lib/api/resource.ts` |
| Any other endpoint: search, stats, actions like `POST /tasks/:id/archive`, uploads | the app's `api` client + `useQuery` / `useMutation` | `src/lib/api/client.ts` |
| Data outside React (route loaders, scripts, event handlers that don't need cache state) | `resource.requests.*`, `resource.*Options()` with the `queryClient`, or `api.get/post/…` | same |

Don't call `fetch` or a bare `axios` — requests would skip the auth header,
the shared token refresh, error normalization and cancellation. Every
failure from `api` is an `ApiError`, and TanStack Query is typed to expect
that.

The source files are short and heavily commented; read them when a detail
isn't covered here.

## 1. Find (or create) the app's clients

Look before creating: search for `createApiClient(` and `createQueryClient(`.
An app should have exactly one of each, usually exported from one module.
If they don't exist yet, create them like this:

```ts
// src/lib/api-instance.ts (the name is up to the app)
import { createApiClient } from "@/lib/api"
import { createQueryClient } from "@/lib/api/query-client"
import { toast } from "@/components/ui/toast"

export const api = createApiClient({
  baseURL: import.meta.env.VITE_API_URL,
  retry: false, // TanStack Query retries; two layers would multiply attempts
  auth: {
    getAccessToken: () => session.accessToken,
    refresh: async () => {
      const { accessToken } = await api.post<{ accessToken: string }>(
        "/auth/refresh",
        null,
        { skipAuth: true } // without this the refresh waits on itself
      )
      session.accessToken = accessToken
      return accessToken
    },
    onUnauthorized: () => {
      session.signOut()
      queryClient.clear()
    },
  },
  // No onError here: with Query it would fire once per attempt.
})

export const queryClient = createQueryClient({
  onError: ({ title, error }) =>
    toast.add({ title, description: error.message, type: "error" }),
})
```

Wrap the app once: `<QueryClientProvider client={queryClient}>` plus the
`<Toaster />` from `@/components/ui/toast`. Cookie-session apps omit
`getAccessToken`, set `withCredentials: true`, and have `refresh` resolve
without a token.

What `createQueryClient` does for you, so you don't re-implement it:
retries queries only for network errors, timeouts, 408/429/5xx (honouring
`Retry-After`); never retries 4xx or mutations; types `error` as `ApiError`
in every hook; and routes failures to `onError` — failed mutations and
failed *background* refetches, not a query's first-load error (render that
inline), cancellations, 401s, or anything with `meta: { silent: true }`.

## 2. Define resources — one line per collection

Put them together (e.g. `src/lib/resources.ts`) and import them wherever
needed. The entity interface needs an `id` (string or number).

```ts
import { createNestedResource, createResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"

export const tasks = createResource<Task>({ api, path: "/tasks", label: "task" })

// Override only the types that differ from the defaults, by name:
export const projects = createResource<
  Project,
  { create: NewProject; update: ProjectPatch; params: ProjectFilters; list: Page<Project> }
>({
  api,
  path: "/projects",
  label: "project",
  updateMethod: "put",
  // Required when the list response isn't a plain array:
  mapItems: (page, map) => ({ ...page, items: map(page.items) }),
})

// Collections under a parent: scope type is the 2nd type argument, `key` is required.
export const projectTasks = createNestedResource<Task, { projectId: string }>({
  api,
  key: "project-tasks",
  path: ({ projectId }) => `/projects/${projectId}/tasks`,
  label: "task",
})
```

Defaults when a type isn't overridden: create body = entity without `id`;
update body = partial create body; `params` = any record (sent as the query
string); `list` = `Entity[]`. `label` produces error notices like
"Couldn't delete the task". Opt-in features — optimistic updates
(`optimistic`, `placeholder`, `applyUpdate`) and infinite lists
(`pagination`) — are in [references/resource.md](references/resource.md).

## 3. Use the hooks

```tsx
const { data: openTasks, isPending, error } = tasks.useList({ status: "open" })
const { data: task } = tasks.useDetail(taskId) // waits while taskId is undefined

const create = tasks.useCreate({ onSuccess: (task) => navigate(`/tasks/${task.id}`) })
create.mutate({ title, done: false })

const update = tasks.useUpdate()
update.mutate({ id: task.id, data: { done: true } })

const remove = tasks.useDelete()
remove.mutate(task.id)

// Nested: bind the parent, then use the same hooks.
const scoped = projectTasks.scope({ projectId })
const { data } = scoped.useList()
```

Every hook takes the usual TanStack options last (`select`, `enabled`,
`placeholderData`, `onSuccess`, `onMutate`, `meta`, …). After a mutation
succeeds the resource updates the cache for you — the saved entity goes
into its detail query, a deleted one is dropped, and every list refetches
— and the mutation stays pending until that refetch finishes. So you don't
invalidate anything yourself, and it's safe to close a dialog or navigate
in `onSuccess`: the next screen shows fresh data.

## 4. Endpoints that aren't CRUD

Use `api` with `useQuery` / `useMutation`, and key queries under the
resource they belong to. Keys under `resource.keys.all` are refreshed by
that resource's mutations automatically, and invalidating `keys.all` after
a custom mutation refreshes the resource's lists.

```tsx
const stats = useQuery({
  queryKey: [...tasks.keys.all, "stats"],
  queryFn: ({ signal }) => api.get<TaskStats>("/tasks/stats", { signal }),
})

const queryClient = useQueryClient()
const archive = useMutation({
  mutationFn: (id: string) => api.post<Task>(`/tasks/${id}/archive`),
  meta: { errorTitle: "Couldn't archive the task" },
  onSuccess: () => queryClient.invalidateQueries({ queryKey: tasks.keys.all }),
})
```

Always pass `signal` into a custom `queryFn` so unmounting or a key change
cancels the request. Never hand-write keys like `["tasks"]` for resource
data — use `tasks.keys.*`, or invalidation silently misses.

## 5. Errors

`error` in every hook and callback is an `ApiError` (`@/lib/api`):

| Field | Meaning |
| --- | --- |
| `message` | Human-readable; read from common body shapes (`message`, `detail`, `errors[0].message`, …) |
| `kind` | `"http"`, `"network"`, `"timeout"`, `"cancelled"` or `"unknown"` |
| `status` | HTTP status, when the server answered |
| `data` | Parsed response body — field errors for a 422, for example |

- **First load failed:** render it where the data would go (e.g. the
  `ErrorCallout` from `@/components/forge/feedback`, with a retry that calls
  `refetch()`). No toast is sent for this case, on purpose.
- **Mutation failed:** a toast appears automatically (titled by `label` or
  `meta.errorTitle`). If the component shows the error itself — a form
  mapping a 422's `error.data` to fields — pass `meta: { silent: true }` so
  the user doesn't get both.
- **401:** handled centrally (refresh, retry once, else `onUnauthorized`).
  Don't catch or toast 401s in components.
- **A queryFn that can throw something else** (e.g. schema validation):
  wrap it with `toApiError(error)` so the `ApiError` typing stays true.

## Pitfalls

- A list response that isn't an array (`{ items, total }`) needs `mapItems`;
  TypeScript will insist.
- `useInfiniteList` needs `pagination` on the resource; optimistic creates
  need `placeholder`; an update body that isn't a partial entity needs
  `applyUpdate` for optimistic updates.
- Don't call hooks conditionally. Pass `undefined` to `useDetail`, or
  `enabled: false` to lists, until inputs are ready.
- Components must sit inside the `QueryClientProvider`; `useQueryClient()`
  gives you the client for `prefetchQuery`, `setQueryData`, etc.
- Show saving state with `mutation.isPending`; show "anything of this
  resource saving" with `useIsMutating({ mutationKey: tasks.keys.all })`.

## Client state

State that isn't server data (selection, filters, drafts, view mode) doesn't
belong in Query or in a hand-rolled context: use a context store from
`@/lib/context-store` — see the `forge-state` skill.

## References

- [references/resource.md](references/resource.md) — complete
  `createResource` / `createNestedResource` API: every config field, keys,
  hooks, options helpers, cache behaviour, optimistic and infinite details.
- [references/client.md](references/client.md) — `createApiClient` options,
  auth and refresh, retries, per-request flags (`skipAuth`, `silent`,
  `retry`), `ApiError` and helpers, `createQueryClient` options.
- [references/recipes.md](references/recipes.md) — complete worked examples:
  filtered/paged list page, detail page with a route loader, form with 422
  field errors, optimistic toggle, infinite scroll, search-as-you-type,
  prefetch on hover.
