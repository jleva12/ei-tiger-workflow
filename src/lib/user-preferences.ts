import { createStore, type StoreApi } from "zustand"
import { createJSONStorage, persist } from "zustand/middleware"

import { createContextStore } from "@/lib/context-store"

/** The display preferences every Forge app has. */
export interface BaseUserPreferences {
  /** Spacing and row height. `comfortable` is the Forge design as drawn. */
  density: "comfortable" | "compact"
  /** Multiplies every font size (and rem-based spacing with it). 1 = 100%. */
  fontScale: number
  /** `more` strengthens borders and secondary text; `system` follows the OS. */
  contrast: "system" | "standard" | "more"
  /** `reduce` turns animations off; `system` follows the OS setting. */
  motion: "system" | "reduce"
}

/**
 * App-specific preferences. Add yours by augmenting this interface, then
 * give them defaults on `UserPreferencesProvider`:
 *
 * ```ts
 * declare module "@/lib/user-preferences" {
 *   interface CustomUserPreferences {
 *     defaultTaskView: "list" | "kanban"
 *   }
 * }
 * ```
 */
// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface CustomUserPreferences {}

export type UserPreferences = BaseUserPreferences & CustomUserPreferences

export type UserPreferenceKey = keyof UserPreferences

export const defaultUserPreferences: BaseUserPreferences = {
  density: "comfortable",
  fontScale: 1,
  contrast: "system",
  motion: "system",
}

export const MIN_FONT_SCALE = 0.75
export const MAX_FONT_SCALE = 2

/** The allowed values of the built-in choice preferences. */
const choices: Record<string, readonly string[]> = {
  density: ["comfortable", "compact"],
  contrast: ["system", "standard", "more"],
  motion: ["system", "reduce"],
} satisfies {
  [
    K in Exclude<keyof BaseUserPreferences, "fontScale">
  ]: readonly BaseUserPreferences[K][]
}

/** The text sizes settings pages offer, as multipliers. */
export const fontScalePresets = [0.9, 1, 1.1, 1.25, 1.5] as const

export const clampFontScale = (scale: number) =>
  Number.isFinite(scale)
    ? Math.min(MAX_FONT_SCALE, Math.max(MIN_FONT_SCALE, scale))
    : 1

/** App defaults: required for custom preferences, optional for the rest. */
export type UserPreferenceDefaults = Partial<BaseUserPreferences> &
  CustomUserPreferences

export type UserPreferencesState = UserPreferences & {
  /** Sets one preference. */
  setPreference: <K extends UserPreferenceKey>(
    key: K,
    value: UserPreferences[K]
  ) => void
  /** Sets several preferences at once. */
  updatePreferences: (changes: Partial<UserPreferences>) => void
  /** Back to the defaults: every preference, or only `keys`. */
  resetPreferences: (keys?: UserPreferenceKey[]) => void
}

export type UserPreferencesStoreOptions = {
  defaults?: UserPreferenceDefaults
  /** Wins over stored values, e.g. the signed-in user's saved preferences. */
  initial?: Partial<UserPreferences>
  /** localStorage key, or `false` to keep preferences in memory only. */
  storageKey?: string | false
}

/** Only the preference values, without the store's actions. */
export const preferencesOf = (state: UserPreferencesState): UserPreferences =>
  Object.fromEntries(
    Object.entries(state).filter(([, value]) => typeof value !== "function")
  ) as UserPreferences

const sanitize = (
  changes: Partial<UserPreferences>
): Partial<UserPreferences> =>
  changes.fontScale === undefined
    ? changes
    : { ...changes, fontScale: clampFontScale(changes.fontScale) }

function createUserPreferencesStore({
  defaults,
  initial,
  storageKey = "forge-user-preferences",
}: UserPreferencesStoreOptions): StoreApi<UserPreferencesState> {
  const baseline = {
    ...defaultUserPreferences,
    ...defaults,
  } as UserPreferences
  const knownTypes = new Map(
    Object.entries(baseline).map(([key, value]) => [key, typeof value])
  )

  const state = (
    set: StoreApi<UserPreferencesState>["setState"]
  ): UserPreferencesState => ({
    ...baseline,
    setPreference: (key, value) =>
      set(sanitize({ [key]: value } as Partial<UserPreferences>)),
    updatePreferences: (changes) => set(sanitize(changes)),
    resetPreferences: (keys) =>
      set(
        keys
          ? (Object.fromEntries(
              keys.map((key) => [key, baseline[key]])
            ) as Partial<UserPreferences>)
          : baseline
      ),
  })

  const store = storageKey
    ? createStore<UserPreferencesState>()(
        persist((set) => state(set), {
          name: storageKey,
          version: 1,
          storage: createJSONStorage(() => localStorage),
          partialize: preferencesOf,
          // Only known keys with the right type survive a stale or edited
          // entry; everything else falls back to the defaults.
          merge: (persisted, current) => {
            const stored = (persisted ?? {}) as Record<string, unknown>
            const valid = Object.fromEntries(
              Object.entries(stored).filter(
                ([key, value]) =>
                  knownTypes.get(key) === typeof value &&
                  (!(key in choices) || choices[key]!.includes(value as string))
              )
            )
            return {
              ...current,
              ...sanitize(valid as Partial<UserPreferences>),
            }
          },
        })
      )
    : createStore<UserPreferencesState>()((set) => state(set))

  if (initial) store.setState(sanitize(initial))
  return store
}

export const {
  Provider: UserPreferencesStoreProvider,
  useStore: useUserPreferences,
  useStoreApi: useUserPreferencesApi,
} = createContextStore(createUserPreferencesStore, {
  name: "UserPreferences",
})

/** One preference, re-rendering only when it changes. */
export function useUserPreference<K extends UserPreferenceKey>(
  key: K
): UserPreferences[K] {
  return useUserPreferences((state) => state[key])
}

/** `setPreference(key, value)`, stable across renders. */
export function useSetUserPreference() {
  return useUserPreferences((state) => state.setPreference)
}
