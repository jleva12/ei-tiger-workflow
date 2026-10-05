import * as React from "react"

export type AppShellContextValue = {
  /** Desktop workspace sidebar visibility. */
  sidebarOpen: boolean
  setSidebarOpen: (open: boolean) => void
  /** Narrow-layout navigation sheet visibility. */
  mobileNavOpen: boolean
  setMobileNavOpen: (open: boolean) => void
}

export const AppShellContext = React.createContext<AppShellContextValue | null>(
  null
)

export function useAppShell() {
  const context = React.useContext(AppShellContext)
  if (!context) throw new Error("useAppShell must be used within <AppShell>.")
  return context
}

export function useOptionalAppShell() {
  return React.useContext(AppShellContext)
}
