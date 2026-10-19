import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClientProvider } from "@tanstack/react-query"

import "../index.css"
import { TooltipProvider } from "@/components/ui/tooltip"
import { toast, Toaster } from "@/components/ui/toast"
import { createQueryClient } from "@/lib/api/query-client"

import { DesktopApp } from "./desktop-app"

/*
 * The desktop app's screen (apps/forge-desktop runs it in a Tauri window):
 * the web console's chat pages, without the console. Built from this
 * folder by vite.desktop.config.ts; no sign-in, no router.
 */

const queryClient = createQueryClient({
  onError: ({ title, error }) =>
    toast.add({ title, description: error.message, type: "error" }),
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Toaster>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delay={250}>
          <DesktopApp />
        </TooltipProvider>
      </QueryClientProvider>
    </Toaster>
  </StrictMode>
)
