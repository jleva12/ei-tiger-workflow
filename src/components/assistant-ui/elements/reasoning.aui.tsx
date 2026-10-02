"use client"

import { memo, useCallback, useRef } from "react"
import {
  useAuiState,
  type ReasoningMessagePartComponent,
  type ReasoningGroupComponent,
} from "@assistant-ui/react"
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text"
import {
  ANIMATION_DURATION,
  ReasoningRoot as ReasoningRootBase,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
  ReasoningFade,
  reasoningVariants,
  type ReasoningRootProps,
} from "./reasoning"
import { useDisclosureAnchor } from "./use-disclosure-anchor"
import { useReasoningDuration } from "./use-reasoning-duration"

export type { ReasoningRootProps } from "./reasoning"

/** `ReasoningRoot` held in place on screen while it opens and closes (`useDisclosureAnchor`). */
function ReasoningRoot({
  ref,
  onAnimationStart,
  ...props
}: ReasoningRootProps) {
  const collapsibleRef = useRef<HTMLDivElement | null>(null)
  const holdInPlace = useDisclosureAnchor(collapsibleRef, ANIMATION_DURATION)

  const handleAnimationStart = useCallback(() => {
    holdInPlace()
    onAnimationStart?.()
  }, [holdInPlace, onAnimationStart])

  const composedRef = useCallback(
    (node: HTMLDivElement | null) => {
      collapsibleRef.current = node
      if (typeof ref === "function") {
        ref(node)
      } else if (ref) {
        ref.current = node
      }
    },
    [ref]
  )

  return (
    <ReasoningRootBase
      ref={composedRef}
      onAnimationStart={handleAnimationStart}
      {...props}
    />
  )
}

const ReasoningImpl: ReasoningMessagePartComponent = () => <MarkdownText />

const ReasoningGroupImpl: ReasoningGroupComponent = ({
  children,
  startIndex,
  endIndex,
}) => {
  const isReasoningStreaming = useAuiState((s) => {
    if (s.message.status?.type !== "running") return false
    for (let index = startIndex; index <= endIndex; index++) {
      if (s.message.parts[index]?.status.type === "running") return true
    }
    return false
  })
  const duration = useReasoningDuration(startIndex, isReasoningStreaming)

  return (
    <ReasoningRoot streaming={isReasoningStreaming}>
      <ReasoningTrigger active={isReasoningStreaming} duration={duration} />
      <ReasoningContent aria-busy={isReasoningStreaming}>
        <ReasoningText>{children}</ReasoningText>
      </ReasoningContent>
    </ReasoningRoot>
  )
}

const Reasoning = memo(
  ReasoningImpl
) as unknown as ReasoningMessagePartComponent & {
  Root: typeof ReasoningRoot
  Trigger: typeof ReasoningTrigger
  Content: typeof ReasoningContent
  Text: typeof ReasoningText
  Fade: typeof ReasoningFade
}

Reasoning.displayName = "Reasoning"
Reasoning.Root = ReasoningRoot
Reasoning.Trigger = ReasoningTrigger
Reasoning.Content = ReasoningContent
Reasoning.Text = ReasoningText
Reasoning.Fade = ReasoningFade

/**
 * @deprecated This wrapper targets the legacy `components.ReasoningGroup`
 * prop on `<MessagePrimitive.Parts>`. Use `<MessagePrimitive.GroupedParts>`
 * with a `groupBy` returning `"group-reasoning"` and compose `ReasoningRoot`
 * / `ReasoningTrigger` / `ReasoningContent` / `ReasoningText` directly.
 * See `thread.aui.tsx` for an example.
 */
const ReasoningGroup = memo(ReasoningGroupImpl)
ReasoningGroup.displayName = "ReasoningGroup"

export {
  Reasoning,
  ReasoningGroup,
  ReasoningRoot,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
  ReasoningFade,
  reasoningVariants,
}
