import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react"
import { labelled } from "../adapters"

/** The package's CheckboxGroup renderer with Forge's visible field label. */
export const CheckboxGroup = labelled(defaultGenerativeUILibrary.CheckboxGroup)
