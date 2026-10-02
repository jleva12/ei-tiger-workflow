# Resource API reference

Source: `src/lib/api/resource.ts`. Import from `@/lib/api/resource`.

## Contents

1. Creating a resource
2. Config fields
3. Type overrides
4. What a resource returns
5. Hooks
6. Cache behaviour after mutations
7. Optimistic updates
8. Infinite lists
9. Nested resources

## 1. Creating a resource

```ts
createResource<Entity, Overrides?>(config)                  // "/tasks"
createNestedResource<Entity, Scope, Overrides?>(config)     // "/projects/:projectId/tasks"
```

- `Entity` must have `id: string | number`.
- `Overrides` is an object type naming only the types that differ from the
  defaults (see §3). Type arguments are explicit because TypeScript can't
  partially infer them.

Endpoints, for `path: "/tasks"`:

| Request | Method + URL |
| --- | --- |
| list | `GET /tasks?<params>` |
| detail | `GET /tasks/:id` (id is URL-encoded) |
| create | `POST /tasks` |
| update | `PATCH /tasks/:id` (`PUT` with `updateMethod: "put"`) |
| delete | `DELETE /tasks/:id` |

An API that doesn't fit this shape (RPC-style endpoints, different item
URLs) isn't a resource: use `api` + `useQuery`/`useMutation` directly.

## 2. Config fields

| Field | Type | Notes |
| --- | --- | --- |
| `api` | `ApiClient` | The app's client from `createApiClient`. Required. |
| `path` | `string` (flat) / `(scope) => string` (nested) | Collection URL. Required. Nested: encode scope values that can contain `/` or `?`. |
| `key` | `string` | Root of every query key. Flat: optional, defaults to `path`. Nested: required. |
| `label` | `string` | Singular noun for error notices: "Couldn't create/save/delete the task". |
| `updateMethod` | `"patch" \| "put"` | Default `"patch"`. |
| `mapItems` | `(list, map) => list` | Where the entities live in a list response. Required when `list` isn't an array: `(page, map) => ({ ...page, items: map(page.items) })`. |
| `optimistic` | `boolean` | Default for update/delete/create hooks. Default `false`. See §7. |
| `placeholder` | `(createBody) => Entity` | The item an optimistic create shows. Creates are optimistic only with this. |
| `applyUpdate` | `(item, updateBody) => Entity` | How an update body changes an item, for optimistic updates. Default: shallow merge. |
| `pagination` | `{ param, initialPageParam, getNextPageParam }` | Enables `useInfiniteList`. See §8. |

## 3. Type overrides

| Name | Default | Used by |
| --- | --- | --- |
| `create` | `Omit<Entity, "id">` | `useCreate().mutate(body)`, `requests.create` |
| `update` | `Partial<create>` | `useUpdate().mutate({ id, data })` |
| `params` | `Record<string, unknown>` | `useList(params)`, `useInfiniteList(params)` — sent as the query string |
| `list` | `Entity[]` | `data` of `useList`, pages of `useInfiniteList` |

```ts
createResource<Task, { create: NewTask; params: TaskFilters; list: Page<Task> }>({ … })
```

A misspelled override name is a type error, as is an entity without `id`.

## 4. What a resource returns

(For nested resources, everything below is returned by `.scope(scope)`.)

| Member | What it is |
| --- | --- |
| `keys.all` | Root key. Invalidate it to refetch everything about the resource. |
| `keys.lists()`, `keys.list(params?)` | Keys of `useList` queries |
| `keys.infiniteLists()`, `keys.infiniteList(params?)` | Keys of `useInfiniteList` queries |
| `keys.details()`, `keys.detail(id)` | Keys of `useDetail` queries |
| `requests.list(params?, signal?)`, `.detail(id, signal?)`, `.create(body)`, `.update({ id, data })`, `.delete(id)` | Plain promises; no cache involvement |
| `listOptions(params?)`, `detailOptions(id)`, `infiniteListOptions(params?)` | Typed `queryOptions` for `useSuspenseQuery`, `useQueries`, `queryClient.prefetchQuery / ensureQueryData / getQueryData` |
| `useList`, `useInfiniteList`, `useDetail`, `useCreate`, `useUpdate`, `useDelete` | Hooks (§5) |

Key shapes: flat `[key, "list", params]`, `[key, "detail", id]`,
`[key, "infinite", params]`; nested `[key, scope, "list", params]` and so on.
Custom queries keyed `[...resource.keys.all, "anything"]` are part of the
resource's collection (refreshed by its mutations).

## 5. Hooks

| Hook | Signature | Returns |
| --- | --- | --- |
| `useList` | `(params?, options?)` | `UseQueryResult<List or select result, ApiError>` |
| `useInfiniteList` | `(params?, options?)` | `UseInfiniteQueryResult<InfiniteData<List> or select result, ApiError>` |
| `useDetail` | `(id \| null \| undefined, options?)` | `UseQueryResult<Entity or select result, ApiError>`; doesn't fetch without an id |
| `useCreate` | `(options?)` | mutation; `mutate(createBody)`, resolves to the created `Entity` |
| `useUpdate` | `(options?)` | mutation; `mutate({ id, data })`, resolves to the saved `Entity` |
| `useDelete` | `(options?)` | mutation; `mutate(id)` |

`options` are TanStack's (`useQuery`/`useInfiniteQuery`/`useMutation`
options) minus the key and function, plus `optimistic?: boolean` on mutation
hooks. Your `onSuccess`/`onError`/`onMutate` run alongside the resource's
own; `meta` is merged over the resource's error title.

Mutation keys are `[...keys.all, "create" | "update" | "delete"]`, so
`useIsMutating({ mutationKey: tasks.keys.all })` counts a resource's saves.

## 6. Cache behaviour after mutations

| Mutation | On success |
| --- | --- |
| create | Response written to its detail query; collection refetches |
| update | Response written to its detail query (on a 204, the detail refetches instead); collection refetches |
| delete | Detail query removed; collection refetches |

"Collection" = every list, infinite list and custom query under
`keys.all` — not other items' details. The mutation stays pending until the
refetch finishes (only active queries refetch; inactive ones are marked
stale). If other mutations of the same resource (same scope) are still
running, the refetch waits for the last one, so it can't overwrite their
optimistic edits.

## 7. Optimistic updates

Enable per resource (`optimistic: true`) or per hook
(`useUpdate({ optimistic: true })`, `useDelete({ optimistic: false })`).

| Mutation | Shown immediately | Needs |
| --- | --- | --- |
| update | Item changed in every cached list and in its detail | `applyUpdate` if the update body isn't a partial entity |
| delete | Item removed from every cached list | — |
| create | `placeholder(body)` appended to every cached list (last loaded page of infinite lists) | `placeholder`, e.g. `(body) => ({ ...body, id: \`temp-${crypto.randomUUID()}\` })` |

On failure the edit is rolled back, the collection refetches, and the usual
error notice is sent. The follow-up refetch also corrects anything the guess
got wrong (filters, sort order, totals). Your own `onMutate` still works;
its return value is untouched.

Good fits: toggles, renames, reordering, deletes from a list. Poor fits:
creates where the server computes important fields; anything the user must
see confirmed (payments).

## 8. Infinite lists

```ts
// Cursor: { items, nextCursor } — first request sends no cursor
pagination: {
  param: "cursor",
  initialPageParam: null,
  getNextPageParam: (lastPage) => lastPage.nextCursor, // null/undefined = no more
}

// Page numbers: { items, hasMore }
pagination: {
  param: "page",
  initialPageParam: 1,
  getNextPageParam: (lastPage, allPages) => (lastPage.hasMore ? allPages.length + 1 : undefined),
}
```

`param` is the query-string key the page value is sent in, merged with the
hook's `params`. `lastPage`/`allPages` are typed as the `list` type.

```tsx
const { data, fetchNextPage, hasNextPage, isFetchingNextPage } = feed.useInfiniteList(
  { tag },
  { select: (data) => data.pages.flatMap((page) => page.items) }
)
```

Without `select`, `data` is `InfiniteData<List>` (`{ pages, pageParams }`).
Mutations and optimistic edits reach infinite lists too. Calling
`useInfiniteList` on a resource without `pagination` throws an error naming
the resource.

For numbered pages with Previous/Next buttons, don't use infinite lists:
`useList({ page }, { placeholderData: keepPreviousData })`.

## 9. Nested resources

```ts
export const projectTasks = createNestedResource<Task, { projectId: string }>({
  api,
  key: "project-tasks",
  path: ({ projectId }) => `/projects/${projectId}/tasks`,
})

const tasks = projectTasks.scope({ projectId }) // fine to call during render
tasks.useList()
tasks.keys.all          // ["project-tasks", { projectId }] — this parent only
projectTasks.keys.all   // ["project-tasks"] — every parent
```

Each scope is cached, refreshed and optimistically edited separately; a
mutation under project A doesn't refetch project B's lists. Item URLs are
`${path(scope)}/:id`. If the parent id can be missing (a route param that
hasn't resolved), render the component that calls `scope()` only once it's
known; failing that, scope with `projectId ?? ""` and pass
`enabled: Boolean(projectId)` to the query hooks.
