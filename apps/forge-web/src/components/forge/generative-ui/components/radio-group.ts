import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react"
import { labelled } from "../adapters"

/** The package's RadioGroup renderer with Forge's visible field label. */
export const RadioGroup = labelled(defaultGenerativeUILibrary.RadioGroup)
