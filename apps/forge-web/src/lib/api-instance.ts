import {toast} from "@/components/ui/toast"
import {createApiClient} from "@/lib/api"
import {createQueryClient} from "@/lib/api/query-client"
import {fromCasbin, type AccessLoader} from "@/lib/user-access"

// Local development: the bearer token make web-token mints for the seeded site
// administrator. Without it the admin API sees no user and answers 401.
const devToken: string | undefined = import.meta.env.VITE_API_TOKEN

export const api = createApiClient({
    baseURL: import.meta.env.VITE_API_URL,
    retry: false, // TanStack Query retries; two layers would multiply attempts
    auth: {getAccessToken: () => devToken},
    // No onError here: with Query it would fire once per attempt.
})

/**
 * An instance of the query client, configured to handle global query settings
 * and actions for reacting to query errors.
 *
 * The `queryClient` is initialized with a custom error handler that displays
 * toast notifications for errors encountered during queries. This includes
 * displaying the error's title and message in the notification as an error type.
 *
 * It serves as a centralized client to manage and cache server state efficiently
 * across the application.
 */
export const queryClient = createQueryClient({
    onError: ({title, error}) =>
        toast.add({title, description: error.message, type: "error"}),
})

/**
 * Represents access control information using the Casbin framework.
 *
 * This object defines the roles and policies associated with a particular entity or user.
 * It is used to determine the permissions or access rights in a system leveraging Casbin.
 *
 * @typedef {Object} CasbinAccess
 * @property {string[]} [roles] - An optional array of roles assigned to the entity. Each role represents a specific set of permissions or responsibilities.
 * @property {string[][]} [policies] - An optional array of policies, where each policy is represented as a nested array of strings.
 * These policies define access rules, specifying which actions are allowed or prohibited in relation to specific resources.
 */
type CasbinAccess = { roles?: string[]; policies?: string[][] }

/**
 * Asynchronous function assigned to the `loadAccess` variable.
 *
 * This function is responsible for loading and processing user access control data.
 * It retrieves the access information from a remote API endpoint using an HTTP GET request
 * and converts it into a format compatible with the Casbin access control model.
 *
 * @type {AccessLoader}
 *
 * @param {Object} options - An object containing configuration options for the function.
 * @param {AbortSignal} options.signal - A signal to allow aborting the HTTP request if needed.
 *
 * @returns {Promise<CasbinAccess>} A promise that resolves to a processed CasbinAccess object
 * that can be used for access control evaluation.
 */
export const loadAccess: AccessLoader = async ({signal}) =>
    fromCasbin(await api.get<CasbinAccess>("/me/access", {signal}))
