import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClientProvider } from "@tanstack/react-query"
import { RouterProvider } from "@tanstack/react-router"

import "./index.css"
import { ThemeProvider } from "@/components/theme-provider.tsx"
import { Toaster } from "@/components/ui/toast"
import { loadAccess, queryClient } from "@/lib/api-instance"
import { DEFAULT_ASSISTANT_PLACEMENT } from "@/features/assistant/lib/assistant-window"
import { DEFAULT_ASSISTANT_MODEL_PREFERENCES } from "@/features/assistant/lib/forge-agent"
import { UserProvider } from "@/lib/user-provider"
import { router } from "@/router"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <Toaster>
        <QueryClientProvider client={queryClient}>
          <UserProvider
            access={{ load: loadAccess }}
            preferences={{
              defaults: {
                assistantPlacement: DEFAULT_ASSISTANT_PLACEMENT,
                ...DEFAULT_ASSISTANT_MODEL_PREFERENCES,
              },
            }}
          >
            <RouterProvider router={router} />
          </UserProvider>
        </QueryClientProvider>
      </Toaster>
    </ThemeProvider>
  </StrictMode>
)
