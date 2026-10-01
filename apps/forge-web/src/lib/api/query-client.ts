import {
    MutationCache,
    QueryCache,
    QueryClient,
    type QueryClientConfig,
} from "@tanstack/react-query"

import {ApiError, isRetryableError, toApiError} from "./errors"

/** `meta` on any `useQuery` or `useMutation`. */
export type ApiQueryMeta = {
    /** Title of the error notice, e.g. "Couldn't delete the task". */
    errorTitle?: string
    /** Don't send this one's errors to `onError`; the component shows them. */
    silent?: boolean
}

declare module "@tanstack/react-query" {
    interface Register {
        /**
         * Query and mutation functions that only call the API client reject with
         * an `ApiError`. Wrap anything else that can throw in `toApiError`.
         */
        defaultError: ApiError
        queryMeta: ApiQueryMeta
        mutationMeta: ApiQueryMeta
    }
}

export type ErrorNotice = {
    error: ApiError
    /** `meta.errorTitle`, or a generic title for the source. */
    title: string
    source: "query" | "mutation"
}

export type QueryClientOptions = Omit<
    QueryClientConfig,
    "queryCache" | "mutationCache"
> & {
    /** Retries for a failed query; mutations never retry. Default 2. */
    retries?: number
    /**
     * Longest wait between query retries in ms. A longer `Retry-After` fails
     * the query instead. Default 30000.
     */
    maxRetryDelay?: number
    /**
     * Shows a failure to the user, e.g. as a toast. Called for failed mutations
     * and failed background refetches; a first load's error belongs where the
     * data would render. Skips `meta.silent`, cancellations and 401s, which are
     * `auth.onUnauthorized`'s job.
     */
    onError?: (notice: ErrorNotice) => void
}

const DEFAULT_TITLES: Record<ErrorNotice["source"], string> = {
    query: "Couldn't refresh data",
    mutation: "Couldn't save changes",
}

/**
 * A QueryClient for queries that call the API client: errors typed as
 * `ApiError`, retries only for failures worth retrying (honouring
 * `Retry-After`), and one place to show failures. Create the API client with
 * `retry: false` and no `onError` so Query owns both.
 */
export function createQueryClient({
                                      retries = 2,
                                      maxRetryDelay = 30_000,
                                      onError,
                                      defaultOptions,
                                      ...config
                                  }: QueryClientOptions = {}) {
    function notify(
        error: unknown,
        source: ErrorNotice["source"],
        meta: ApiQueryMeta | undefined
    ) {
        if (!onError || meta?.silent) return
        const apiError = toApiError(error)
        if (apiError.kind === "cancelled" || apiError.status === 401) return
        onError({
            error: apiError,
            source,
            title: meta?.errorTitle ?? DEFAULT_TITLES[source],
        })
    }

    return new QueryClient({
        ...config,
        queryCache: new QueryCache({
            onError: (error, query) => {
                if (query.state.data !== undefined) notify(error, "query", query.meta)
            },
        }),
        mutationCache: new MutationCache({
            onError: (error, _variables, _result, mutation) =>
                notify(error, "mutation", mutation.meta),
        }),
        defaultOptions: {
            ...defaultOptions,
            queries: {
                retry: (failureCount, error) =>
                    failureCount < retries &&
                    isRetryableError(error) &&
                    // Retried by the API client already; don't multiply the attempts.
                    error.attempts === 1 &&
                    (error.retryAfter ?? 0) <= maxRetryDelay,
                // Query asks for the delay before deciding to retry, for any error.
                retryDelay: (failureCount, error) =>
                    (error instanceof ApiError ? error.retryAfter : undefined) ??
                    Math.min(1000 * 2 ** failureCount, maxRetryDelay),
                ...defaultOptions?.queries,
            },
        },
    })
}
