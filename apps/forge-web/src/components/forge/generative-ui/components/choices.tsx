import * as React from "react"
import { cn } from "cn"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { AnswerContext } from "../answer-context"
import { choicesOf } from "../choices-options"

export function RichChoices({ options }: { options?: unknown }) {
  const { live, picked, send } = React.useContext(AnswerContext)
  const choices = choicesOf(options)
  if (!choices.length) return null
  return (
    <div
      data-slot="rich-choices"
      role="group"
      aria-label="Choices"
      className="flex flex-wrap gap-1.5"
    >
      {choices.map((choice, index) => {
        const chosen = picked === choice.reply
        return (
          <Button
            key={index}
            type="button"
            size="sm"
            variant={chosen ? "secondary" : "outline"}
            disabled={!live}
            aria-pressed={picked === undefined ? undefined : chosen}
            title={choice.reply === choice.label ? undefined : choice.reply}
            onClick={() => send(choice.reply)}
            className={cn(
              "h-auto min-h-7 py-1 text-start whitespace-normal",
              chosen && "disabled:opacity-100"
            )}
          >
            {chosen && <Icon icon="check" data-icon="inline-start" />}
            {choice.label}
          </Button>
        )
      })}
    </div>
  )
}
