import { apiAuthHeaders } from "@/lib/api-instance"

/**
 * The console's requests to the runtime: who's signed in (needed when the
 * runtime isn't public, and their conversations are theirs when it is), and
 * that it's Forge's console, which the audit lists as the builder's chat.
 */
export const consoleHeaders = () => ({
  ...apiAuthHeaders(),
  "X-Forge-Client": "console",
})
