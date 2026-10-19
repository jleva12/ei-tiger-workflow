/** A choice as the chat shows it: what it says, and what clicking it sends. */
export type Choice = { label: string; reply: string }

/** The options of a Choices node that can be shown, as the model wrote them. */
export const choicesOf = (options: unknown): Choice[] =>
  (Array.isArray(options) ? options : [])
    .flatMap((option: { label?: unknown; reply?: unknown } | undefined) => {
      const label = typeof option?.label === "string" ? option.label.trim() : ""
      if (!label) return []
      const reply =
        typeof option?.reply === "string" && option.reply.trim()
          ? option.reply.trim()
          : label
      return [{ label, reply }]
    })
    .slice(0, 8)
