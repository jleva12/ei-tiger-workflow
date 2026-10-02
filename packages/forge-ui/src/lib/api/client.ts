import axios, {
    type AxiosInstance,
    type AxiosRequestConfig,
    type AxiosResponse,
    type CreateAxiosDefaults,
    type GenericAbortSignal,
    type InternalAxiosRequestConfig,
} from "axios"

import {
    ApiError,
    getResponseMessage,
    isRetryableError,
    RETRYABLE_STATUSES,
    toApiError,
    type ErrorMessageReader,
} from "./errors"

type Token = string | null | undefined

export type AuthOptions = {
    /**
     * Returns the current access token, sent as `Authorization: Bearer <token>`.
     * Omit it for cookie sessions. A falsy token sends no header.
     */
    getAccessToken?: () => Token | Promise<Token>
    /**
     * Renews the session after a 401. Store the new token where
     * `getAccessToken` reads it and resolve with it (or resolve with nothing
     * for cookie sessions); throw when the session can't be renewed. Concurrent
     * 401s share one call, and each failed request is retried once.
     *
     * If it calls this client, pass `skipAuth: true`, or the refresh request
     * will wait on itself.
     */
    refresh?: () => Promise<string | void>
    /**
     * Called when a request is still unauthorized: there is no `refresh`, it
     * threw, or the retried request got another 401. Sign the user out here.
     * These errors skip `onError`.
     */
    onUnauthorized?: (error: ApiError) => void
    /** Default `Authorization`. */
    header?: string
    /** Default `Bearer`; an empty string sends the bare token. */
    scheme?: string
}

export type RetryOptions = {
    /** Attempts after the first. Default 2. */
    retries: number
    /** Base backoff in ms, doubled each attempt, with jitter. Default 300. */
    delay: number
    /**
     * Longest backoff in ms. A `Retry-After` longer than this fails the request
     * instead of retrying. Default 10000.
     */
    maxDelay: number
    /** Methods safe to repeat. Default GET, HEAD, OPTIONS, PUT, DELETE. */
    methods: readonly string[]
    /**
     * Statuses worth retrying. Network errors and timeouts always are.
     * Default `RETRYABLE_STATUSES`: 408, 429, 500, 502, 503, 504.
     */
    statuses: readonly number[]
}

/**
 * axios defaults plus the client's own options. `auth` replaces axios's HTTP
 * basic auth; set an `Authorization` header if you need that.
 */
export type ApiClientOptions = Omit<CreateAxiosDefaults, "auth"> & {
    auth?: AuthOptions
    /**
     * Retry policy, or `false` to never retry. Pass `false` when TanStack Query
     * does the retrying (see `createQueryClient`).
     */
    retry?: Partial<RetryOptions> | false
    /**
     * Called once per failed request, after retries, except for cancellations,
     * `silent` requests and errors handled by `auth.onUnauthorized`. Show a
     * toast or report to monitoring here. With TanStack Query, use
     * `createQueryClient`'s `onError` instead: this one fires per attempt.
     */
    onError?: (error: ApiError) => void
    /** Reads a message from an error body. Default `getResponseMessage`. */
    getErrorMessage?: ErrorMessageReader
}

export type ApiRequestConfig<D = unknown> = AxiosRequestConfig<D> & {
    /** Send without the access token, and don't refresh on 401. */
    skipAuth?: boolean
    /** Override the client's retry policy, or `false` to never retry. */
    retry?: Partial<RetryOptions> | false
    /** Reject as usual, but don't call `onError`. */
    silent?: boolean
}

export type ApiClient = {
    /** The configured axios instance, for full responses or more interceptors. */
    instance: AxiosInstance
    request<T = unknown, D = unknown>(config: ApiRequestConfig<D>): Promise<T>
    get<T = unknown>(url: string, config?: ApiRequestConfig): Promise<T>
    delete<T = unknown>(url: string, config?: ApiRequestConfig): Promise<T>
    post<T = unknown, D = unknown>(
        url: string,
        data?: D,
        config?: ApiRequestConfig<D>
    ): Promise<T>
    put<T = unknown, D = unknown>(
        url: string,
        data?: D,
        config?: ApiRequestConfig<D>
    ): Promise<T>
    patch<T = unknown, D = unknown>(
        url: string,
        data?: D,
        config?: ApiRequestConfig<D>
    ): Promise<T>
}

/** What the interceptors see: per-request options plus retry bookkeeping. */
type RequestState = InternalAxiosRequestConfig & {
    skipAuth?: boolean
    retry?: Partial<RetryOptions> | false
    silent?: boolean
    /** Retries made so far. */
    _attempt?: number
    /** Already retried after a refresh; another 401 is final. */
    _refreshed?: boolean
    /** Token returned by `refresh`, used for this request's retries. */
    _token?: string
}

const DEFAULT_RETRY: RetryOptions = {
    retries: 2,
    delay: 300,
    maxDelay: 10_000,
    methods: ["get", "head", "options", "put", "delete"],
    statuses: RETRYABLE_STATUSES,
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
    const {
        auth,
        retry,
        onError,
        getErrorMessage = getResponseMessage,
        ...defaults
    } = options

    const instance = axios.create({
        timeout: 30_000,
        ...defaults,
        // Report timeouts as ETIMEDOUT rather than the ambiguous ECONNABORTED.
        transitional: {clarifyTimeoutError: true, ...defaults.transitional},
    })

    const authHeader = auth?.header ?? "Authorization"
    const scheme = auth?.scheme ?? "Bearer"
    const formatToken = (token: string) => (scheme ? `${scheme} ${token}` : token)

    let refreshing: Promise<string | void> | undefined

    function refreshSession(
        renew: () => Promise<string | void>,
        error: ApiError
    ) {
        if (!refreshing) {
            refreshing = Promise.resolve()
                .then(renew)
                .finally(() => {
                    refreshing = undefined
                })
            refreshing.catch(() => auth?.onUnauthorized?.(error))
        }
        return refreshing
    }

    instance.interceptors.request.use(async (config: RequestState) => {
        if (!auth || config.skipAuth) return config
        // Requests sent mid-refresh wait for the new token instead of failing.
        if (refreshing) await refreshing.catch(() => undefined)
        const token = config._token ?? (await auth.getAccessToken?.())
        if (token) config.headers.set(authHeader, formatToken(token))
        return config
    })

    instance.interceptors.response.use(undefined, async (error: unknown) => {
        const apiError = toApiError(error, getErrorMessage)
        const config = axios.isAxiosError(error)
            ? (error.config as RequestState | undefined)
            : undefined
        apiError.attempts = (config?._attempt ?? 0) + 1

        if (apiError.kind === "cancelled") throw apiError

        if (config && auth && !config.skipAuth && apiError.status === 401) {
            if (auth.refresh && !config._refreshed) {
                config._refreshed = true
                const renewed = await refreshSession(auth.refresh, apiError).then(
                    (token) => {
                        if (token) config._token = token
                        return true
                    },
                    () => false
                )
                if (renewed) return instance.request(config)
                // refreshSession already reported the failure to onUnauthorized.
                if (auth.onUnauthorized) throw apiError
            } else if (auth.onUnauthorized) {
                auth.onUnauthorized(apiError)
                throw apiError
            }
        }

        const delay = config && getRetryDelay(config, apiError, retry)
        if (config && delay !== undefined) {
            config._attempt = (config._attempt ?? 0) + 1
            await wait(delay, config.signal)
            // An abort during the wait makes axios reject this as cancelled.
            return instance.request(config)
        }

        if (!config?.silent) onError?.(apiError)
        throw apiError
    })

    const unwrap = <T>(response: Promise<AxiosResponse<T>>) =>
        response.then(({data}) => data)

    return {
        instance,
        request: <T, D>(config: ApiRequestConfig<D>) =>
            unwrap(instance.request<T, AxiosResponse<T>, D>(config)),
        get: <T>(url: string, config?: ApiRequestConfig) =>
            unwrap(instance.get<T>(url, config)),
        delete: <T>(url: string, config?: ApiRequestConfig) =>
            unwrap(instance.delete<T>(url, config)),
        post: <T, D>(url: string, data?: D, config?: ApiRequestConfig<D>) =>
            unwrap(instance.post<T, AxiosResponse<T>, D>(url, data, config)),
        put: <T, D>(url: string, data?: D, config?: ApiRequestConfig<D>) =>
            unwrap(instance.put<T, AxiosResponse<T>, D>(url, data, config)),
        patch: <T, D>(url: string, data?: D, config?: ApiRequestConfig<D>) =>
            unwrap(instance.patch<T, AxiosResponse<T>, D>(url, data, config)),
    }
}

/** Milliseconds to wait before retrying, or undefined to give up. */
function getRetryDelay(
    config: RequestState,
    error: ApiError,
    clientPolicy: Partial<RetryOptions> | false | undefined
) {
    if (config.retry === false) return undefined
    if (clientPolicy === false && !config.retry) return undefined
    const policy = {...DEFAULT_RETRY, ...clientPolicy, ...config.retry}

    const attempt = config._attempt ?? 0
    const method = config.method ?? "get"
    if (
        attempt >= policy.retries ||
        !isRetryableError(error, policy.statuses) ||
        !policy.methods.some((allowed) => allowed.toLowerCase() === method)
    ) {
        return undefined
    }

    if (error.retryAfter !== undefined) {
        return error.retryAfter <= policy.maxDelay ? error.retryAfter : undefined
    }
    const backoff = Math.min(policy.maxDelay, policy.delay * 2 ** attempt)
    return backoff / 2 + Math.random() * (backoff / 2)
}

/** Resolves after `ms`, or as soon as `signal` aborts. */
function wait(ms: number, signal?: GenericAbortSignal) {
    return new Promise<void>((resolve) => {
        if (signal?.aborted) return resolve()
        const done = () => {
            clearTimeout(timer)
            signal?.removeEventListener?.("abort", done)
            resolve()
        }
        const timer = setTimeout(done, ms)
        signal?.addEventListener?.("abort", done)
    })
}
