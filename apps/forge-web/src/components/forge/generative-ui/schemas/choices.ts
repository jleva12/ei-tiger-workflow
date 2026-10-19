import { z } from "zod"

/** Forge's own: buttons whose pick is the person's next message. */
export const Choices = {
  description:
    "Buttons the person clicks instead of typing an answer: a click sends that option's reply to you as their next message, and the buttons turn off. Offer the next steps they're likely to want, or a pick between a few options.",
  properties: z.object({
    options: z
      .array(
        z.object({
          label: z.string().describe("What the button says, in a few words."),
          reply: z
            .string()
            .optional()
            .describe(
              "What the person says by clicking it, as their message; its label by default."
            ),
        })
      )
      .describe("The options, two to five."),
  }),
}
