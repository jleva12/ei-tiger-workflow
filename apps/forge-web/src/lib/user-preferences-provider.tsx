import * as React from "react"
import {useShallow} from "zustand/react/shallow"

import {
    clampFontScale,
    preferencesOf,
    UserPreferencesStoreProvider,
    useUserPreferences,
    useUserPreferencesApi,
    type CustomUserPreferences,
    type UserPreferenceDefaults,
    type UserPreferences,
} from "@/lib/user-preferences"

// `defaults` is required once the app adds required custom preferences.
type Defaults = Partial<CustomUserPreferences> extends CustomUserPreferences
    ? { defaults?: UserPreferenceDefaults }
    : { defaults: UserPreferenceDefaults }

export type UserPreferencesProviderProps = Defaults & {
    children?: React.ReactNode
    /**
     * The user's saved preferences (e.g. from their profile). Applied once on
     * mount, over anything stored in the browser.
     */
    initial?: Partial<UserPreferences>
    /** localStorage key. Default "forge-user-preferences"; `false` keeps them in memory. */
    storageKey?: string | false
    /**
     * Called after the user changes a preference, with every preference and
     * the ones that changed — e.g. to save them to the user's profile.
     */
    onChange?: (
        preferences: UserPreferences,
        changed: Partial<UserPreferences>
    ) => void
}

/** Writes the display preferences onto <html>, where the theme reads them. */
function ApplyPreferences() {
    const {density, fontScale, contrast, motion} = useUserPreferences(
        useShallow((state) => ({
            density: state.density,
            fontScale: state.fontScale,
            contrast: state.contrast,
            motion: state.motion,
        }))
    )

    // Layout effect: applied before paint, so there's no flash of defaults.
    React.useLayoutEffect(() => {
        const root = document.documentElement
        root.dataset.forgeDensity = density
        root.dataset.forgeContrast = contrast
        root.dataset.forgeMotion = motion
        root.style.setProperty("--forge-font-scale", String(clampFontScale(fontScale)))
    }, [density, fontScale, contrast, motion])

    React.useLayoutEffect(
        () => () => {
            const root = document.documentElement
            delete root.dataset.forgeDensity
            delete root.dataset.forgeContrast
            delete root.dataset.forgeMotion
            root.style.removeProperty("--forge-font-scale")
        },
        []
    )

    return null
}

/** Reports user changes and follows changes made in other tabs. */
function SyncPreferences({
                             onChange,
                             storageKey,
                         }: Pick<UserPreferencesProviderProps, "onChange" | "storageKey">) {
    const api = useUserPreferencesApi()
    const onChangeRef = React.useRef(onChange)
    React.useEffect(() => {
        onChangeRef.current = onChange
    })

    React.useEffect(
        () =>
            api.subscribe((state, previous) => {
                const next = preferencesOf(state)
                const before = preferencesOf(previous)
                const changed = Object.fromEntries(
                    Object.entries(next).filter(
                        ([key, value]) => before[key as keyof UserPreferences] !== value
                    )
                ) as Partial<UserPreferences>
                if (Object.keys(changed).length > 0) {
                    onChangeRef.current?.(next, changed)
                }
            }),
        [api]
    )

    React.useEffect(() => {
        if (!storageKey) return
        const persistApi = (api as { persist?: { rehydrate: () => unknown } })
            .persist
        const handleStorage = (event: StorageEvent) => {
            if (event.storageArea === localStorage && event.key === storageKey) {
                void persistApi?.rehydrate()
            }
        }
        window.addEventListener("storage", handleStorage)
        return () => window.removeEventListener("storage", handleStorage)
    }, [api, storageKey])

    return null
}

/**
 * The signed-in user's display preferences — density, text size, contrast,
 * motion, and any you add — for the whole app. Preferences come from the
 * library defaults, then `defaults`, then what the browser stored, then
 * `initial`; they're applied to the page as `data-forge-*` attributes and
 * `--forge-font-scale`, which the Forge theme responds to. Render it once, near
 * the root, next to `ThemeProvider`.
 */
export function UserPreferencesProvider({
                                            children,
                                            defaults,
                                            initial,
                                            storageKey = "forge-user-preferences",
                                            onChange,
                                        }: UserPreferencesProviderProps) {
    return (
        <UserPreferencesStoreProvider initial={{defaults, initial, storageKey}}>
            <ApplyPreferences/>
            <SyncPreferences onChange={onChange} storageKey={storageKey}/>
            {children}
        </UserPreferencesStoreProvider>
    )
}
