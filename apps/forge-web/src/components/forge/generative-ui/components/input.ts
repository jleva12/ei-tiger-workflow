import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react"
import { labelled } from "../adapters"

/** The package's Input renderer with Forge's visible field label. */
export const Input = labelled(defaultGenerativeUILibrary.Input)
