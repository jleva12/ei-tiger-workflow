import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react"
import { labelled } from "../adapters"

/** The package's Select renderer with Forge's visible field label. */
export const Select = labelled(defaultGenerativeUILibrary.Select)
