# API client and query client reference

Sources: `src/lib/api/client.ts`, `src/lib/api/errors.ts`,
`src/lib/api/query-client.ts`. Import the client and errors from
`@/lib/api`, the query client from `@/lib/api/query-client`.

## createApiClient(options)

Returns an `ApiClient`:

| Member | Returns |
| --- | --- |
| `get<T>(url, config?)`, `delete<T>(url, config?)` | `Promise<T>` — the response body |
| `post<T, D>(url, data?, config?)`, `put`, `patch` | `Promise<T>` |
| `request<T, D>(config)` | `Promise<T>` |
| `instance` | The configured axios instance, for full responses (headers, status) or extra interceptors |

Options — anything `axios.create` accepts (`baseURL`, `headers`,
`withCredentials`, `paramsSerializer`, …) plus:

| Option | Default | Notes |
| --- | --- | --- |
| `timeout` | `30_000` | ms |
| `auth.getAccessToken` | — | `() => token \| Promise<token>`; sent as `Authorization: Bearer <token>`. Omit for cookie sessions. |
| `auth.refresh` | — | Called once for a burst of 401s; resolve with the new token (after storing it where `getAccessToken` reads) or with nothing for cookie sessions; throw when the session can't be renewed. Each 401'd request is retried once. **If it calls this same client, pass `skipAuth: true`** or it waits on itself forever. |
| `auth.onUnauthorized` | — | Still unauthorized (no refresh, refresh threw, or the retry got another 401): sign out here and `queryClient.clear()`. |
| `auth.header`, `auth.scheme` | `"Authorization"`, `"Bearer"` | `scheme: ""` sends the bare token. |
| `retry` | 2 retries, 300 ms base backoff, max 10 s | `{ retries, delay, maxDelay, methods, statuses }` or `false`. With TanStack Query, pass `false`. Only idempotent methods (GET/HEAD/OPTIONS/PUT/DELETE) on network errors, timeouts, 408/429/5xx; honours `Retry-After`. |
| `onError` | — | Once per failed request after retries (not cancellations, `silent` requests or 401s sent to `onUnauthorized`). Leave it out with TanStack Query — use `createQueryClient`'s `onError`. |
| `getErrorMessage` | `getResponseMessage` | `(body) => string \| undefined` for backends with an unusual error body. |

Axios's own `auth` (HTTP basic) is replaced by the object above; set an
`Authorization` header if you need basic auth.

### Per-request config

Everything axios accepts (`params`, `headers`, `signal`, `timeout`, …), plus:

| Flag | Effect |
| --- | --- |
| `skipAuth: true` | No token; a 401 is final (no refresh). Use for login/refresh/public endpoints. |
| `retry: false \| { … }` | Override the retry policy for this call. |
| `silent: true` | Don't call the client's `onError`. |
| `signal` | `AbortSignal`; abort rejects with an `ApiError` of kind `"cancelled"`. |

## ApiError (`@/lib/api`)

| Field | Type | Meaning |
| --- | --- | --- |
| `message` | `string` | From the body (`message`, `error_description`, `error.message`, `detail`/`detail[0].msg`, `errors[0].message`, `title`), else a plain fallback per kind. HTML/text bodies are ignored. |
| `kind` | `"http" \| "network" \| "timeout" \| "cancelled" \| "unknown"` | |
| `status` | `number \| undefined` | HTTP status |
| `data` | `TData \| undefined` | Parsed body. `ApiError<ValidationBody>` to type it. |
| `method`, `url` | `string \| undefined` | Of the failed request (URL without `baseURL`) |
| `retryAfter` | `number \| undefined` | `Retry-After` in ms |
| `attempts` | `number` | Tries under the client's retry policy |
| `cause` | `unknown` | The original AxiosError |

Helpers: `toApiError(anything)` (normalize any thrown value),
`isRetryableError(error)`, `getResponseMessage(body)`,
`RETRYABLE_STATUSES`.

Narrow in code with `error instanceof ApiError` (only needed outside
TanStack hooks, where `error` is already typed as `ApiError`).

## createQueryClient(options) (`@/lib/api/query-client`)

Returns a TanStack `QueryClient`. Options — anything `new QueryClient`
accepts except the caches, plus:

| Option | Default | Notes |
| --- | --- | --- |
| `onError` | — | `({ error, title, source }) => void`, `source` is `"query"` or `"mutation"`. Called for failed mutations and failed background refetches; skips first-load query errors, cancellations, 401s and `meta.silent`. `title` is `meta.errorTitle`, else "Couldn't save changes" / "Couldn't refresh data". |
| `retries` | `2` | Query retries (mutations never retry). |
| `maxRetryDelay` | `30_000` | Longest wait between retries; a longer `Retry-After` fails instead. |

It also registers types globally: `error` is `ApiError` in every hook, and
`meta` on queries/mutations is `{ errorTitle?: string; silent?: boolean }`
(unknown keys are type errors). A query's own `retry` option still wins.

Retry rules: queries retry only `isRetryableError` failures (network,
timeout, 408/429/5xx), waiting `Retry-After` or exponential backoff up to
`maxRetryDelay`, and never retry something the API client already retried.
Retries pause while offline or while the tab is hidden, and
`failureCount` / `failureReason` on the query result can drive a
"Retrying…" state.
