import * as React from "react"

/**
 * Whether the settings around are shown, not edited: a published version,
 * or someone who can't change it. Fields that aren't native inputs (the
 * expression editor) read it; native ones are disabled by their fieldset.
 */
export const FieldsReadOnly = React.createContext(false)
