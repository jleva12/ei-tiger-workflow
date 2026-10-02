import * as React from "react"

import type { CustomUserPreferences } from "@/lib/user-preferences"
import {
  UserAccessProvider,
  type UserAccessProviderProps,
} from "@/lib/user-access-provider"
import {
  UserPreferencesProvider,
  type UserPreferencesProviderProps,
} from "@/lib/user-preferences-provider"

type PreferencesProp = Omit<UserPreferencesProviderProps, "children">

// `preferences` is required once the app adds required custom preferences,
// because their defaults are.
type Preferences =
  Partial<CustomUserPreferences> extends CustomUserPreferences
    ? { preferences?: PreferencesProp }
    : { preferences: PreferencesProp }

export type UserProviderProps = Preferences & {
  children?: React.ReactNode
  /** Roles and permissions from your Casbin backend; see `UserAccessProvider`. */
  access: Omit<UserAccessProviderProps, "children">
}

/**
 * The signed-in user's context in one provider: their display preferences
 * (`UserPreferencesProvider`) and their roles and permissions
 * (`UserAccessProvider`), each usable on its own too. Put it inside
 * `ThemeProvider`, and key it by the user's id.
 *
 * ```tsx
 * <UserProvider
 *   key={session.userId}
 *   access={{ load: loadAccess, fallback: <AppLoading /> }}
 *   preferences={{ initial: profile.preferences, onChange: savePreferences }}
 * >
 *   <App />
 * </UserProvider>
 * ```
 */
export function UserProvider({
  children,
  access,
  preferences,
}: UserProviderProps) {
  return (
    <UserPreferencesProvider {...(preferences as UserPreferencesProviderProps)}>
      <UserAccessProvider {...access}>{children}</UserAccessProvider>
    </UserPreferencesProvider>
  )
}
