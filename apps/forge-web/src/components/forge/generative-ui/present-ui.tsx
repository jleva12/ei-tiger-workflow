import {
  renderGenerativeUI,
  type GenerativeUIAction,
} from "@assistant-ui/generative-ui/react"
import type { ToolCallMessagePartProps } from "@assistant-ui/react"
import { replyOf } from "./actions"
import { AnswerContext } from "./answer-context"
import { withCardForms, withQuietFields } from "./composition"
import { LIBRARY } from "./library"
import { shownOf } from "./messages"
import { treeOf } from "./result"
import { useAnswer } from "./use-answer"
import "./styles.css"

/**
 * A `present` call in the conversation: the composition its arguments
 * describe, or the base tree the server drew its team components as
 * (`treeOf`), once the agent server has taken it. Its controls work while
 * it's live; otherwise they're kept, off.
 */
export function PresentUI(props: ToolCallMessagePartProps) {
  const answer = useAnswer()
  const shown = shownOf(props.result)
  // Drawn once the server took it: a refused call is sent again, fixed, and
  // only that one shows. One with no verdict at all is drawn once it's done.
  if (
    shown === false ||
    (shown === undefined && props.status.type === "running")
  )
    return null
  const tree = treeOf(props.args, props.result)
  if (tree === undefined) {
    // Team components with no tree drawn for them: never their names drawn
    // as base components. The agent says in words what it showed.
    if (props.status.type === "running") return null
    return (
      <p
        data-slot="rich-answer-unavailable"
        className="my-2 text-xs text-muted-foreground"
      >
        This answer can't be shown here.
      </p>
    )
  }
  const dispatch = (action: GenerativeUIAction) => answer.send(replyOf(action))
  const drawn = renderGenerativeUI(
    withQuietFields(withCardForms(tree)),
    LIBRARY,
    {
      // The server has its whole arguments by now.
      status: "done",
      ...(answer.live && { dispatch }),
    }
  )
  return (
    <AnswerContext.Provider value={answer}>
      <div data-slot="rich-answer" className="my-2 min-w-0">
        <div data-aui="root">
          {/* The same fieldset live or answered, only switched off: drawn
              anew, its fields would lose what the person entered. */}
          <fieldset
            disabled={!answer.live}
            data-aui={answer.live ? "live" : "answered"}
          >
            {drawn}
          </fieldset>
        </div>
      </div>
    </AnswerContext.Provider>
  )
}
