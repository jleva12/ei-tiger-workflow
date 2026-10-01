import {
  PreferenceOptions,
  PreferenceRow,
} from "@/components/forge/preferences"
import {
  useAssistantPlacement,
  type AssistantPlacement,
} from "@/lib/assistant-window"

const placementOptions: { value: AssistantPlacement; label: string }[] = [
  { value: "panel", label: "Panel" },
  { value: "window", label: "Window" },
]

/**
 * Where the assistant opens: a panel over the page, or its own window
 * beside Forge. The panel's pop-out button and the window's move-back
 * button change it too.
 */
export function AssistantPlacementRow() {
  const [placement, setPlacement] = useAssistantPlacement()
  return (
    <PreferenceRow
      title="Assistant"
      description="A window stays open beside Forge and follows the page you're on."
    >
      <PreferenceOptions
        label="Assistant"
        value={placement}
        onValueChange={setPlacement}
        options={placementOptions}
      />
    </PreferenceRow>
  )
}
