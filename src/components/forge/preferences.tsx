import * as React from "react"
import { cn } from "cn"

import { useTheme } from "@/components/theme-provider"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldGroup,
  FieldTitle,
} from "@/components/ui/field"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  fontScalePresets,
  useUserPreferences,
  type BaseUserPreferences,
} from "@/lib/user-preferences"

type Option<T> = { value: T; label: React.ReactNode }

/**
 * A segmented control for one preference: the options side by side, the
 * chosen one raised. Values can be strings or numbers.
 */
function PreferenceOptions<T extends string | number>({
  value,
  onValueChange,
  options,
  label,
  className,
}: {
  value: T
  onValueChange: (value: T) => void
  options: Option<T>[]
  /** Accessible name for the group, usually the row's title. */
  label: string
  className?: string
}) {
  return (
    <ToggleGroup
      aria-label={label}
      value={[String(value)]}
      onValueChange={(next) => {
        const picked = options.find(
          (option) => String(option.value) === next[0]
        )
        if (picked) onValueChange(picked.value)
      }}
      spacing={0}
      className={cn(
        "gap-0.5 rounded-(--radius-control) bg-muted p-[3px]",
        className
      )}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={String(option.value)}
          value={String(option.value)}
          className="h-7 rounded-(--radius-item)! px-2.5 text-xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
        >
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** One preference: a title and description beside its control. */
function PreferenceRow({
  title,
  description,
  children,
  className,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <Field
      orientation="responsive"
      data-slot="preference-row"
      className={cn("gap-3", className)}
    >
      <FieldContent>
        <FieldTitle className="text-xs">{title}</FieldTitle>
        {description && (
          <FieldDescription className="text-2xs">
            {description}
          </FieldDescription>
        )}
      </FieldContent>
      {children}
    </Field>
  )
}

const densityOptions: Option<BaseUserPreferences["density"]>[] = [
  { value: "comfortable", label: "Comfortable" },
  { value: "compact", label: "Compact" },
]

const contrastOptions: Option<BaseUserPreferences["contrast"]>[] = [
  { value: "system", label: "System" },
  { value: "standard", label: "Standard" },
  { value: "more", label: "More" },
]

const motionOptions: Option<BaseUserPreferences["motion"]>[] = [
  { value: "system", label: "System" },
  { value: "reduce", label: "Reduced" },
]

const themeOptions: Option<"light" | "dark" | "system">[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
]

function ThemeRow() {
  const { theme, setTheme } = useTheme()
  return (
    <PreferenceRow title="Theme" description="Press D to switch anytime.">
      <PreferenceOptions
        label="Theme"
        value={theme}
        onValueChange={setTheme}
        options={themeOptions}
      />
    </PreferenceRow>
  )
}

/**
 * The display settings from `UserPreferencesProvider` as a form: theme,
 * density, text size, contrast and motion, applied as they change. Put it
 * on a settings page, in a Sheet or in a Popover; add rows for your own
 * preferences with `PreferenceRow` + `PreferenceOptions` as `children`.
 */
function PreferencesPanel({
  showTheme = true,
  fontScales = fontScalePresets,
  children,
  className,
}: {
  /** Include the theme row (needs `ThemeProvider`). Default true. */
  showTheme?: boolean
  /** The text sizes offered, as multipliers. Default 90%–150%. */
  fontScales?: readonly number[]
  /** Extra rows, after the built-in ones. */
  children?: React.ReactNode
  className?: string
}) {
  const density = useUserPreferences((s) => s.density)
  const fontScale = useUserPreferences((s) => s.fontScale)
  const contrast = useUserPreferences((s) => s.contrast)
  const motion = useUserPreferences((s) => s.motion)
  const setPreference = useUserPreferences((s) => s.setPreference)
  const resetPreferences = useUserPreferences((s) => s.resetPreferences)

  return (
    <FieldGroup
      data-slot="preferences-panel"
      className={cn("gap-5", className)}
    >
      {showTheme && <ThemeRow />}
      <PreferenceRow
        title="Density"
        description="Compact fits more rows and controls on screen."
      >
        <PreferenceOptions
          label="Density"
          value={density}
          onValueChange={(value) => setPreference("density", value)}
          options={densityOptions}
        />
      </PreferenceRow>
      <PreferenceRow
        title="Text size"
        description="Scales every font size in the app."
      >
        <PreferenceOptions
          label="Text size"
          value={fontScale}
          onValueChange={(value) => setPreference("fontScale", value)}
          options={fontScales.map((scale) => ({
            value: scale,
            label: `${Math.round(scale * 100)}%`,
          }))}
        />
      </PreferenceRow>
      <PreferenceRow
        title="Contrast"
        description="More darkens secondary text and borders."
      >
        <PreferenceOptions
          label="Contrast"
          value={contrast}
          onValueChange={(value) => setPreference("contrast", value)}
          options={contrastOptions}
        />
      </PreferenceRow>
      <PreferenceRow
        title="Motion"
        description="Reduced turns off animations and transitions."
      >
        <PreferenceOptions
          label="Motion"
          value={motion}
          onValueChange={(value) => setPreference("motion", value)}
          options={motionOptions}
        />
      </PreferenceRow>
      {children}
      <div className="flex justify-end">
        <Button variant="ghost" size="sm" onClick={() => resetPreferences()}>
          Reset to defaults
        </Button>
      </div>
    </FieldGroup>
  )
}

export { PreferenceOptions, PreferenceRow, PreferencesPanel }
