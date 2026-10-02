import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "./index.css"
import App from "./App.tsx"
import { ThemeProvider } from "@/components/theme-provider.tsx"
import { Toaster } from "@/components/ui/toast.tsx"
import { UserPreferencesProvider } from "@/lib/user-preferences-provider.tsx"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider defaultTheme="light" storageKey="forge-ui-theme">
      <UserPreferencesProvider storageKey="forge-ui-preferences">
        <Toaster>
          <App />
        </Toaster>
      </UserPreferencesProvider>
    </ThemeProvider>
  </StrictMode>
)
