import { PreferencesPanel } from "@/components/forge/preferences"
import { CodeBlock, SectionPage, Specimen } from "../specimen"

export function PreferencesSection() {
  return (
    <SectionPage
      icon="settings"
      eyebrow="Foundations"
      title="User preferences"
      description="The signed-in user's display settings — density, text size, contrast and motion, plus any your app adds — held in a context store, saved in the browser and applied to the whole app. Change them here: this site is running on them."
    >
      <Specimen
        title="Display settings"
        description="PreferencesPanel edits the preferences from UserPreferencesProvider. Every change applies at once, across the whole app, and is remembered."
        code={`<UserPreferencesProvider
  defaults={{ density: "comfortable" }}
  initial={user.preferences}
  onChange={(preferences) => api.put("/me/preferences", preferences)}
>
  <App />
</UserPreferencesProvider>

// A settings page, sheet or popover:
<PreferencesPanel />`}
      >
        <div className="max-w-xl">
          <PreferencesPanel />
        </div>
      </Specimen>

      <Specimen
        title="Reading and adding preferences"
        description="Any component reads a preference with a selector. Apps add their own preferences by augmenting CustomUserPreferences and giving them defaults."
      >
        <CodeBlock
          code={`// Read one preference; re-renders only when it changes.
const density = useUserPreference("density")
const setPreference = useSetUserPreference()
setPreference("fontScale", 1.25)

// DataTable and TaskList follow the density on their own. A table's
// View menu can still pick one for that table, or go back to Automatic.

// App-specific preferences, fully typed:
declare module "@/lib/user-preferences" {
  interface CustomUserPreferences {
    defaultTaskView: "list" | "kanban"
  }
}

<UserPreferencesProvider defaults={{ defaultTaskView: "list" }}>

<PreferencesPanel>
  <PreferenceRow title="Open tasks in">
    <PreferenceOptions
      label="Open tasks in"
      value={useUserPreference("defaultTaskView")}
      onValueChange={(view) => setPreference("defaultTaskView", view)}
      options={[
        { value: "list", label: "List" },
        { value: "kanban", label: "Board" },
      ]}
    />
  </PreferenceRow>
</PreferencesPanel>`}
        />
      </Specimen>
    </SectionPage>
  )
}
