import * as React from "react"

/** What a composition may do: act, while it's live; what its Choices picked. */
export const AnswerContext = React.createContext<{
  live: boolean
  picked?: string
  send: (text: string) => void
}>({ live: false, send: () => {} })
