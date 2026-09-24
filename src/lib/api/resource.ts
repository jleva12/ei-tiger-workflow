import {
    infiniteQueryOptions,
    queryOptions,
    skipToken,
    useInfiniteQuery,
    useMutation,
    useQuery,
    useQueryClient,
    type InfiniteData,
    type QueryClient,
    type QueryKey,
    type UseInfiniteQueryOptions,
    type UseMutationOptions,
    type UseQueryOptions,
} from "@tanstack/react-query"

import type {ApiClient} from "./client"
import type {ApiError} from "./errors"
import type {ApiQueryMeta} from "./query-client"

export type Id = string | number

/** The types a resource uses where they differ from the defaults. */
export type ResourceTypes = {
    /** Create body. Default: the entity without `id`. */
    create?: unknown
    /** Update body. Default: a partial create body. */
    update?: unknown
    /** List query-string params. Default: any record. */
    params?: object
    /** List response, e.g. a page envelope. Default: an array of entities. */
    list?: unknown
}

type Resolve<TTypes, K extends keyof ResourceTypes, TDefault> =
    TTypes extends Record<K, infer T> ? T : TDefault

type CreateBody<TEntity, TTypes> = Resolve<
    TTypes,
    "create",
    Omit<TEntity, "id">
>
type UpdateBody<TEntity, TTypes> = Resolve<
    TTypes,
    "update",
    Partial<CreateBody<TEntity, TTypes>>
>
type ListParams<TTypes> = Resolve<TTypes, "params", Record<string, unknown>>
type ListResponse<TEntity, TTypes> = Resolve<TTypes, "list", TEntity[]>

/** Rewrites the entities inside a list response. */
export type MapItems<TEntity, TList> = (
    list: TList,
    map: (items: TEntity[]) => TEntity[]
) => TList

/** How a collection pages, for infinite lists. Names follow TanStack's. */
export type Pagination<TList> = {
    /** Query-string param that carries the page: "page", "cursor", "offset", … */
    param: string
    /** The first page's value; null or undefined sends no param. */
    initialPageParam: unknown
    /** The next page's value, or null or undefined after the last page. */
    getNextPageParam: (
        lastPage: TList,
        allPages: TList[],
        lastPageParam: unknown
    ) => unknown
}

type SharedConfig<TEntity extends { id: Id }, TTypes> = {
    api: ApiClient
    /** Singular noun for error notices: "task" → "Couldn't delete the task". */
    label?: string
    /** Method for updates. Default "patch". */
    updateMethod?: "patch" | "put"
    /**
     * Show updates, deletes and (with `placeholder`) creates before the server
     * confirms, rolling back if it fails. Hooks can override it. Default false.
     */
    optimistic?: boolean
    /** How an update changes an item, for optimistic updates. Default: merge. */
    applyUpdate?: (item: TEntity, data: UpdateBody<TEntity, TTypes>) => TEntity
    /** The item an optimistic create shows until the server responds. */
    placeholder?: (data: CreateBody<TEntity, TTypes>) => TEntity
    /** Enables `useInfiniteList`. */
    pagination?: Pagination<ListResponse<TEntity, TTypes>>
} & (ListResponse<TEntity, TTypes> extends TEntity[]
    ? { mapItems?: MapItems<TEntity, ListResponse<TEntity, TTypes>> }
    : {
        /**
         * Where the entities live in a list response, so optimistic updates can
         * reach them: `(page, map) => ({ ...page, items: map(page.items) })`.
         */
        mapItems: MapItems<TEntity, ListResponse<TEntity, TTypes>>
    })

export type ResourceConfig<
    TEntity extends { id: Id },
    TTypes = ResourceTypes,
> = SharedConfig<TEntity, TTypes> & {
    /** Collection URL, e.g. "/tasks"; items live at `${path}/${id}`. */
    path: string
    /** Root of the resource's query keys. Default `path`. */
    key?: string
}

export type NestedResourceConfig<
    TEntity extends { id: Id },
    TScope,
    TTypes = ResourceTypes,
> = SharedConfig<TEntity, TTypes> & {
    /**
     * Collection URL for one parent, e.g.
     * `({ projectId }) => \`/projects/${projectId}/tasks\``. Encode values that
     * can contain `/` or `?`.
     */
    path: (scope: TScope) => string
    /** Root of the query keys, shared by every scope, e.g. "project-tasks". */
    key: string
}

export type UpdateVariables<TId, TUpdate> = { id: TId; data: TUpdate }

type QueryOptions<TData, TSelected, TKey extends QueryKey> = Omit<
    UseQueryOptions<TData, ApiError, TSelected, TKey>,
    "queryKey" | "queryFn"
>

type InfiniteOptions<TList, TSelected, TKey extends QueryKey> = Omit<
    UseInfiniteQueryOptions<TList, ApiError, TSelected, TKey>,
    "queryKey" | "queryFn" | "initialPageParam" | "getNextPageParam"
>

type MutationOptions<TData, TVariables, TOnMutateResult> = Omit<
    UseMutationOptions<TData, ApiError, TVariables, TOnMutateResult>,
    "mutationKey" | "mutationFn"
> & {
    /** Overrides the resource's `optimistic`. */
    optimistic?: boolean
}

/** False for an empty (e.g. 204) response, which has nothing to cache. */
const hasBody = (value: unknown) => typeof value === "object" && value !== null

/** Undo for each optimistic edit, keyed by the mutation run's context. */
const rollbacks = new WeakMap<object, () => void>()

/**
 * Query keys, requests and hooks for a REST collection: `GET path`,
 * `GET path/:id`, `POST path`, `PATCH path/:id` and `DELETE path/:id`.
 * Mutations keep the cache in step: a saved entity is written to its detail
 * query, a deleted one is dropped, and lists (plus any custom query keyed
 * under `keys.all`) refetch. Each mutation stays pending until they're
 * fresh, so a dialog can close on success.
 *
 * @example
 * const tasks = createResource<Task>({ api, path: "/tasks", label: "task" })
 * const tasks = createResource<Task, { params: TaskFilters; list: Page<Task> }>({ … })
 *
 * const { data } = tasks.useList({ status: "open" })
 * const update = tasks.useUpdate()
 * update.mutate({ id, data: { title } })
 */
export function createResource<
    TEntity extends { id: Id },
    TTypes extends ResourceTypes = ResourceTypes,
>(config: ResourceConfig<TEntity, TTypes>) {
    const root = [config.key ?? config.path] as const
    return buildResource<TEntity, TTypes, typeof root>(root, config.path, config)
}

/**
 * A resource under a parent, e.g. `/projects/:projectId/tasks`. `scope()`
 * returns the same hooks for one parent, whose queries are cached and
 * refreshed apart from every other parent's.
 *
 * @example
 * const projectTasks = createNestedResource<Task, { projectId: string }>({
 *   api,
 *   key: "project-tasks",
 *   path: ({ projectId }) => `/projects/${projectId}/tasks`,
 * })
 *
 * const tasks = projectTasks.scope({ projectId })
 * const { data } = tasks.useList()
 */
export function createNestedResource<
    TEntity extends { id: Id },
    TScope extends object,
    TTypes extends ResourceTypes = ResourceTypes,
>(config: NestedResourceConfig<TEntity, TScope, TTypes>) {
    return {
        /** Every scope's queries, e.g. to invalidate all projects' tasks. */
        keys: {all: [config.key] as const},
        scope: (scope: TScope) => {
            const root = [config.key, scope] as const
            return buildResource<TEntity, TTypes, typeof root>(
                root,
                config.path(scope),
                config
            )
        },
    }
}

function buildResource<
    TEntity extends { id: Id },
    TTypes,
    TRoot extends QueryKey,
>(root: TRoot, path: string, config: SharedConfig<TEntity, TTypes>) {
    type TId = TEntity["id"]
    type TCreate = CreateBody<TEntity, TTypes>
    type TUpdate = UpdateBody<TEntity, TTypes>
    type TParams = ListParams<TTypes>
    type TList = ListResponse<TEntity, TTypes>

    const {api, label, updateMethod = "patch", pagination} = config
    // SharedConfig requires `mapItems` unless the list is an array of entities.
    const mapItems =
        (config.mapItems as MapItems<TEntity, TList> | undefined) ??
        ((list: TList, map: (items: TEntity[]) => TEntity[]) =>
            map(list as TEntity[]) as TList)
    const applyUpdate =
        config.applyUpdate ??
        ((item: TEntity, data: TUpdate) => ({
            ...item,
            ...(data as Partial<TEntity>),
        }))

    const keys = {
        all: root,
        lists: () => [...root, "list"] as const,
        list: (params?: TParams) => [...root, "list", params] as const,
        infiniteLists: () => [...root, "infinite"] as const,
        infiniteList: (params?: TParams) => [...root, "infinite", params] as const,
        details: () => [...root, "detail"] as const,
        detail: (id: TId) => [...root, "detail", id] as const,
    }

    const itemPath = (id: TId) => `${path}/${encodeURIComponent(id)}`

    const requests = {
        list: (params?: TParams, signal?: AbortSignal) =>
            api.get<TList>(path, {params, signal}),
        detail: (id: TId, signal?: AbortSignal) =>
            api.get<TEntity>(itemPath(id), {signal}),
        create: (data: TCreate) => api.post<TEntity, TCreate>(path, data),
        update: ({id, data}: UpdateVariables<TId, TUpdate>) =>
            api.request<TEntity, TUpdate>({
                method: updateMethod,
                url: itemPath(id),
                data,
            }),
        delete: (id: TId) => api.delete<void>(itemPath(id)),
    }

    /** For `useSuspenseQuery`, `prefetchQuery`, `ensureQueryData`, … */
    const listOptions = (params?: TParams) =>
        queryOptions({
            queryKey: keys.list(params),
            queryFn: ({signal}) => requests.list(params, signal),
        })

    const detailOptions = (id: TId) =>
        queryOptions({
            queryKey: keys.detail(id),
            queryFn: ({signal}) => requests.detail(id, signal),
        })

    const infiniteList = (params?: TParams) => {
        if (!pagination) {
            throw new Error(
                `Add \`pagination\` to the ${JSON.stringify(root[0])} resource to use infinite lists.`
            )
        }
        const {param, initialPageParam, getNextPageParam} = pagination
        return {
            queryKey: keys.infiniteList(params),
            queryFn: ({
                          pageParam,
                          signal,
                      }: {
                pageParam: unknown
                signal: AbortSignal
            }) =>
                requests.list(
                    pageParam == null
                        ? params
                        : (Object.assign({}, params, {[param]: pageParam}) as TParams),
                    signal
                ),
            initialPageParam,
            getNextPageParam,
        }
    }

    const infiniteListOptions = (params?: TParams) =>
        infiniteQueryOptions(infiniteList(params))

    const errorMeta = (action: string): ApiQueryMeta | undefined =>
        label ? {errorTitle: `Couldn't ${action} the ${label}`} : undefined

    /**
     * Refetches lists and custom queries under `keys.all` (not item details),
     * once no other mutation of this resource is running, so an in-flight
     * optimistic edit isn't overwritten by stale data.
     */
    function refreshCollection(queryClient: QueryClient) {
        // The calling mutation still counts as running during its callbacks.
        if (queryClient.isMutating({mutationKey: root}) > 1) return
        return queryClient.invalidateQueries({
            queryKey: root,
            predicate: ({queryKey}) => queryKey[root.length] !== "detail",
        })
    }

    /** Applies `edit` to the entities of every cached list; returns the undo. */
    async function editLists(
        queryClient: QueryClient,
        edit: (items: TEntity[]) => TEntity[],
        {lastPageOnly = false} = {}
    ) {
        // Keep in-flight refetches from overwriting the edit.
        await Promise.all([
            queryClient.cancelQueries({queryKey: keys.lists()}),
            queryClient.cancelQueries({queryKey: keys.infiniteLists()}),
        ])
        const lists = queryClient.getQueriesData<TList>({
            queryKey: keys.lists(),
        })
        const infinite = queryClient.getQueriesData<InfiniteData<TList>>({
            queryKey: keys.infiniteLists(),
        })
        for (const [queryKey, list] of lists) {
            if (list !== undefined) {
                queryClient.setQueryData(queryKey, mapItems(list, edit))
            }
        }
        for (const [queryKey, data] of infinite) {
            if (!data) continue
            const last = data.pages.length - 1
            const pages = data.pages.map((page, index) =>
                lastPageOnly && index !== last ? page : mapItems(page, edit)
            )
            queryClient.setQueryData(queryKey, {...data, pages})
        }
        return () => {
            for (const [queryKey, data] of [...lists, ...infinite]) {
                queryClient.setQueryData(queryKey, data)
            }
        }
    }

    async function editItem(
        queryClient: QueryClient,
        {id, data}: UpdateVariables<TId, TUpdate>
    ) {
        const queryKey = keys.detail(id)
        await queryClient.cancelQueries({queryKey})
        const previous = queryClient.getQueryData<TEntity>(queryKey)
        if (previous) {
            queryClient.setQueryData<TEntity>(queryKey, applyUpdate(previous, data))
        }
        const undoLists = await editLists(queryClient, (items) =>
            items.map((item) => (item.id === id ? applyUpdate(item, data) : item))
        )
        return () => {
            undoLists()
            if (previous) queryClient.setQueryData<TEntity>(queryKey, previous)
        }
    }

    /** Rolls back an optimistic edit and resyncs; false if there was none. */
    function rollBack(queryClient: QueryClient, context: object, id?: TId) {
        const rollback = rollbacks.get(context)
        if (!rollback) return false
        rollback()
        return Promise.all([
            refreshCollection(queryClient),
            id !== undefined &&
            queryClient.invalidateQueries({queryKey: keys.detail(id)}),
        ])
    }

    function useList<TSelected = TList>(
        params?: TParams,
        options?: QueryOptions<TList, TSelected, ReturnType<typeof keys.list>>
    ) {
        return useQuery({
            ...options,
            queryKey: keys.list(params),
            queryFn: ({signal}) => requests.list(params, signal),
        })
    }

    /** Loads page after page; needs `pagination` on the resource. */
    function useInfiniteList<TSelected = InfiniteData<TList>>(
        params?: TParams,
        options?: InfiniteOptions<
            TList,
            TSelected,
            ReturnType<typeof keys.infiniteList>
        >
    ) {
        return useInfiniteQuery({...options, ...infiniteList(params)})
    }

    /** Waits without fetching while `id` is null or undefined. */
    function useDetail<TSelected = TEntity>(
        id: TId | null | undefined,
        options?: QueryOptions<TEntity, TSelected, ReturnType<typeof keys.detail>>
    ) {
        return useQuery({
            ...options,
            // Never fetched without an id (skipToken), so the key is a placeholder.
            queryKey: keys.detail(id as TId),
            queryFn:
                id == null ? skipToken : ({signal}) => requests.detail(id, signal),
        })
    }

    /** Optimistic only with the resource's `placeholder`. */
    function useCreate<TOnMutateResult = unknown>({
                                                      optimistic = config.optimistic,
                                                      ...options
                                                  }: MutationOptions<TEntity, TCreate, TOnMutateResult> = {}) {
        const queryClient = useQueryClient()
        const placeholder = optimistic ? config.placeholder : undefined
        return useMutation({
            ...options,
            mutationKey: [...root, "create"],
            mutationFn: requests.create,
            meta: {...errorMeta("create"), ...options.meta},
            onMutate: async (data, context) => {
                if (placeholder) {
                    const item = placeholder(data)
                    const undo = await editLists(
                        queryClient,
                        (items) => [...items, item],
                        {lastPageOnly: true}
                    )
                    rollbacks.set(context, undo)
                }
                return options.onMutate?.(data, context) as TOnMutateResult
            },
            onSuccess: async (...args) => {
                const [created] = args
                if (hasBody(created)) {
                    queryClient.setQueryData<TEntity>(keys.detail(created.id), created)
                }
                await Promise.all([
                    refreshCollection(queryClient),
                    options.onSuccess?.(...args),
                ])
            },
            onError: async (...args) => {
                await Promise.all([
                    rollBack(queryClient, args[3]),
                    options.onError?.(...args),
                ])
            },
        })
    }

    function useUpdate<TOnMutateResult = unknown>({
                                                      optimistic = config.optimistic,
                                                      ...options
                                                  }: MutationOptions<
        TEntity,
        UpdateVariables<TId, TUpdate>,
        TOnMutateResult
    > = {}) {
        const queryClient = useQueryClient()
        return useMutation({
            ...options,
            mutationKey: [...root, "update"],
            mutationFn: requests.update,
            meta: {...errorMeta("save"), ...options.meta},
            onMutate: async (variables, context) => {
                if (optimistic) {
                    rollbacks.set(context, await editItem(queryClient, variables))
                }
                return options.onMutate?.(variables, context) as TOnMutateResult
            },
            onSuccess: async (...args) => {
                const [updated, {id}] = args
                const refreshes = [refreshCollection(queryClient)]
                if (hasBody(updated)) {
                    queryClient.setQueryData<TEntity>(keys.detail(id), updated)
                } else {
                    refreshes.push(
                        queryClient.invalidateQueries({queryKey: keys.detail(id)})
                    )
                }
                await Promise.all([...refreshes, options.onSuccess?.(...args)])
            },
            onError: async (...args) => {
                const [, {id}, , context] = args
                await Promise.all([
                    rollBack(queryClient, context, id),
                    options.onError?.(...args),
                ])
            },
        })
    }

    function useDelete<TOnMutateResult = unknown>({
                                                      optimistic = config.optimistic,
                                                      ...options
                                                  }: MutationOptions<void, TId, TOnMutateResult> = {}) {
        const queryClient = useQueryClient()
        return useMutation({
            ...options,
            mutationKey: [...root, "delete"],
            mutationFn: requests.delete,
            meta: {...errorMeta("delete"), ...options.meta},
            onMutate: async (id, context) => {
                if (optimistic) {
                    const undo = await editLists(queryClient, (items) =>
                        items.filter((item) => item.id !== id)
                    )
                    rollbacks.set(context, undo)
                }
                return options.onMutate?.(id, context) as TOnMutateResult
            },
            onSuccess: async (...args) => {
                const [, id] = args
                queryClient.removeQueries({queryKey: keys.detail(id), exact: true})
                await Promise.all([
                    refreshCollection(queryClient),
                    options.onSuccess?.(...args),
                ])
            },
            onError: async (...args) => {
                await Promise.all([
                    rollBack(queryClient, args[3]),
                    options.onError?.(...args),
                ])
            },
        })
    }

    return {
        keys,
        requests,
        listOptions,
        infiniteListOptions,
        detailOptions,
        useList,
        useInfiniteList,
        useDetail,
        useCreate,
        useUpdate,
        useDelete,
    }
}
