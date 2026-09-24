import axios, { AxiosError } from "axios"

/** Why a request failed, independent of transport details. */
export type ApiErrorKind =
  /** The server answered with a non-2xx status. */
  | "http"
  /** No response: offline, DNS, CORS or a dropped connection. */
  | "network"
  /** The request exceeded its `timeout`. */
  | "timeout"
  /** The request was aborted through its `signal`. */
  | "cancelled"
  /** Anything else, e.g. an interceptor or `getAccessToken` threw. */
  | "unknown"

type ApiErrorInit<TData> = {
  kind: ApiErrorKind
  status?: number
  data?: TData
  method?: string
  url?: string
  retryAfter?: number
  cause?: unknown
}

/**
 * The one error type an API client rejects with. The original error stays
 * available as `cause`.
 */
export class ApiError<TData = unknown> extends Error {
  name = "ApiError"
  kind: ApiErrorKind
  /** HTTP status, when the server responded. */
  status: number | undefined
  /** Parsed response body, when the server responded. */
  data: TData | undefined
  /** Upper-case HTTP method of the failed request. */
  method: string | undefined
  /** Request URL as passed to the client (without `baseURL`). */
  url: string | undefined
  /** The server's `Retry-After`, in ms. */
  retryAfter: number | undefined
  /** Tries under the client's retry policy; 1 means it wasn't retried. */
  attempts = 1

  constructor(message: string, init: ApiErrorInit<TData>) {
    super(message, { cause: init.cause })
    this.kind = init.kind
    this.status = init.status
    this.data = init.data
    this.method = init.method
    this.url = init.url
    this.retryAfter = init.retryAfter
  }
}

/** Statuses worth retrying: request timeout, rate limit and server errors. */
export const RETRYABLE_STATUSES: readonly number[] = [
  408, 429, 500, 502, 503, 504,
]

/**
 * Whether trying again could succeed: network errors, timeouts and
 * retryable statuses. The request's method is the caller's concern.
 */
export function isRetryableError(
  error: unknown,
  statuses: readonly number[] = RETRYABLE_STATUSES
): error is ApiError {
  if (!(error instanceof ApiError)) return false
  if (error.kind === "network" || error.kind === "timeout") return true
  return error.status !== undefined && statuses.includes(error.status)
}

/** Reads a message out of an error response body. */
export type ErrorMessageReader = (data: unknown) => string | undefined

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null

const first = (value: unknown) => (Array.isArray(value) ? value[0] : value)

/**
 * Finds a human-readable message in the error body shapes common backends
 * send. Plain-text and HTML bodies are ignored so a proxy's error page never
 * ends up in a toast.
 */
export const getResponseMessage: ErrorMessageReader = (data) => {
  if (!isRecord(data)) return undefined
  const { error, error_description } = data
  const detail = first(data.detail)
  const firstError = first(data.errors)

  const candidates = [
    first(data.message), // Express, NestJS (string[] for validation errors)
    error_description, // OAuth
    isRecord(error) ? error.message : error,
    isRecord(detail) ? detail.msg : detail, // FastAPI, DRF, RFC 9457
    // GraphQL, JSON:API
    isRecord(firstError) ? (firstError.message ?? firstError.detail) : null,
    data.title, // RFC 9457
  ]
  return candidates.find(
    (candidate): candidate is string =>
      typeof candidate === "string" && candidate.trim() !== ""
  )
}

const FALLBACK_MESSAGES: Record<ApiErrorKind, string> = {
  http: "The server couldn't complete the request.",
  network: "Couldn't reach the server. Check your connection and try again.",
  timeout: "The server took too long to respond.",
  cancelled: "The request was cancelled.",
  unknown: "Something went wrong.",
}

function getKind(error: AxiosError): ApiErrorKind {
  if (axios.isCancel(error)) return "cancelled"
  if (error.response) return "http"
  if (
    error.code === AxiosError.ETIMEDOUT ||
    (error.code === AxiosError.ECONNABORTED && /timeout/i.test(error.message))
  ) {
    return "timeout"
  }
  if (error.code === AxiosError.ERR_NETWORK || error.request) return "network"
  return "unknown"
}

/** `Retry-After` is either delay-seconds or an HTTP date. */
function parseRetryAfter(value: unknown) {
  if (typeof value !== "string" || value.trim() === "") return undefined
  const seconds = Number(value)
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000)
  const date = Date.parse(value)
  return Number.isNaN(date) ? undefined : Math.max(0, date - Date.now())
}

/** Converts anything a request can throw into an `ApiError`. */
export function toApiError(
  error: unknown,
  readMessage: ErrorMessageReader = getResponseMessage
): ApiError {
  if (error instanceof ApiError) return error

  if (!axios.isAxiosError(error)) {
    const message =
      error instanceof Error && error.message
        ? error.message
        : FALLBACK_MESSAGES.unknown
    return new ApiError(message, { kind: "unknown", cause: error })
  }

  const kind = getKind(error)
  const { config, response } = error
  const message =
    (response && readMessage(response.data)) ?? FALLBACK_MESSAGES[kind]

  return new ApiError(message, {
    kind,
    status: response?.status,
    data: response?.data,
    method: config?.method?.toUpperCase(),
    url: config?.url,
    retryAfter: parseRetryAfter(response?.headers["retry-after"]),
    cause: error,
  })
}
