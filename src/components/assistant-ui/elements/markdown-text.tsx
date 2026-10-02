"use client"

import "@assistant-ui/react-markdown/styles/dot.css"
import "katex/dist/katex.min.css"

import {
  type CodeHeaderProps,
  MarkdownTextPrimitive,
  unstable_memoizeMarkdownComponents as memoizeMarkdownComponents,
  useIsMarkdownCodeBlock,
} from "@assistant-ui/react-markdown"
import rehypeKatex from "rehype-katex"
import remarkGfm from "remark-gfm"
import remarkMath from "remark-math"
import type * as React from "react"
import { type FC, memo, useMemo, useRef } from "react"
import type { TextMessagePartProps } from "@assistant-ui/react"
import {
  CheckIcon,
  CopyIcon,
} from "@/components/assistant-ui/elements/aui-icons"

import { MermaidDiagram } from "@/components/assistant-ui/elements/mermaid-diagram"
import { SyntaxHighlighter } from "@/components/assistant-ui/elements/syntax-highlighter"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard"
import { cn } from "cn"

type RemarkPlugins = NonNullable<
  React.ComponentProps<typeof MarkdownTextPrimitive>["remarkPlugins"]
>

type MarkdownTextProps = Partial<TextMessagePartProps> & {
  components?: Parameters<typeof memoizeMarkdownComponents>[0]
  /** Run after GFM; pass a stable array. */
  remarkPlugins?: RemarkPlugins
}

// GFM, and math between $$ … $$ (inline or on their own lines). A single $
// stays text, so prices don't turn into formulas.
const basePlugins: RemarkPlugins = [
  remarkGfm,
  [remarkMath, { singleDollarTextMath: false }],
]
const rehypePlugins: React.ComponentProps<
  typeof MarkdownTextPrimitive
>["rehypePlugins"] = [[rehypeKatex, { throwOnError: false, strict: "ignore" }]]

// ```mermaid blocks draw as diagrams.
const componentsByLanguage = { mermaid: { SyntaxHighlighter: MermaidDiagram } }

/**
 * Models write math as \( … \) and \[ … \] too (OpenAI's do): as $$ … $$,
 * which remark-math reads, outside code spans and fences.
 */
function normalizeMath(text: string) {
  if (!text.includes("\\(") && !text.includes("\\[")) return text
  return text
    .split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/)
    .map((piece, index) =>
      index % 2 === 1
        ? piece
        : piece
            .replace(
              /\\\[([\s\S]+?)\\\]/g,
              (_, math) => `\n$$\n${math.trim()}\n$$\n`
            )
            .replace(/\\\(([\s\S]+?)\\\)/g, (_, math) => `$$${math.trim()}$$`)
    )
    .join("")
}

const useShallowStable = <T extends Record<string, unknown> | undefined>(
  value: T
): T => {
  const ref = useRef(value)
  if (value !== ref.current) {
    const prev = ref.current
    const stable =
      value !== undefined &&
      prev !== undefined &&
      Object.keys(prev).length === Object.keys(value).length &&
      Object.keys(value).every((key) => prev[key] === value[key])
    if (!stable) ref.current = value
  }
  return ref.current
}

const MarkdownTextImpl: FC<MarkdownTextProps> = ({
  components,
  remarkPlugins,
}) => {
  const stableComponents = useShallowStable(components)
  const markdownComponents = useMemo(() => {
    if (!stableComponents) return defaultComponents
    return {
      ...defaultComponents,
      ...memoizeMarkdownComponents(stableComponents),
    }
  }, [stableComponents])
  const plugins = useMemo(
    () => (remarkPlugins ? [...basePlugins, ...remarkPlugins] : basePlugins),
    [remarkPlugins]
  )

  return (
    <MarkdownTextPrimitive
      remarkPlugins={plugins}
      rehypePlugins={rehypePlugins}
      preprocess={normalizeMath}
      className="aui-md"
      components={markdownComponents}
      componentsByLanguage={componentsByLanguage}
      defer
    />
  )
}

export const MarkdownText = memo(MarkdownTextImpl)

const CodeHeader: FC<CodeHeaderProps> = ({ language, code }) => {
  const { isCopied, copyToClipboard } = useCopyToClipboard()
  const onCopy = () => {
    if (!code || isCopied) return
    copyToClipboard(code)
  }

  return (
    <div className="aui-code-header-root mt-3 flex items-center justify-between rounded-t-(--radius-band) border border-b-0 border-border bg-muted/50 px-3.5 py-1.5 text-xs">
      <span className="aui-code-header-language font-medium text-muted-foreground lowercase">
        {language}
      </span>
      <TooltipIconButton tooltip="Copy" onClick={onCopy}>
        {!isCopied && (
          <CopyIcon className="animate-in duration-150 zoom-in-75 fade-in" />
        )}
        {isCopied && (
          <CheckIcon className="animate-in duration-200 ease-out zoom-in-50 fade-in" />
        )}
      </TooltipIconButton>
    </div>
  )
}

const defaultComponents = memoizeMarkdownComponents({
  SyntaxHighlighter,
  h1: ({ className, ...props }) => (
    <h1
      className={cn(
        "aui-md-h1 mt-5 mb-2 scroll-m-20 text-lg font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  h2: ({ className, ...props }) => (
    <h2
      className={cn(
        "aui-md-h2 mt-5 mb-2 scroll-m-20 text-base font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  h3: ({ className, ...props }) => (
    <h3
      className={cn(
        "aui-md-h3 mt-4 mb-1.5 scroll-m-20 text-sm font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  h4: ({ className, ...props }) => (
    <h4
      className={cn(
        "aui-md-h4 mt-3.5 mb-1 scroll-m-20 text-sm font-medium first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  h5: ({ className, ...props }) => (
    <h5
      className={cn(
        "aui-md-h5 mt-3 mb-1 text-sm font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  h6: ({ className, ...props }) => (
    <h6
      className={cn(
        "aui-md-h6 mt-3 mb-1 text-sm font-medium first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  p: ({ className, ...props }) => (
    <p
      className={cn(
        "aui-md-p my-3 leading-relaxed first:mt-0 last:mb-0",
        className
      )}
      {...props}
    />
  ),
  a: ({ className, ...props }) => (
    <a
      className={cn(
        "aui-md-a text-primary underline underline-offset-2 hover:text-primary/80",
        className
      )}
      {...props}
    />
  ),
  blockquote: ({ className, ...props }) => (
    <blockquote
      className={cn(
        "aui-md-blockquote my-3 border-s-2 border-muted-foreground/30 ps-4 text-muted-foreground",
        className
      )}
      {...props}
    />
  ),
  ul: ({ className, ...props }) => (
    <ul
      className={cn(
        "aui-md-ul my-3 ms-5 list-disc marker:text-muted-foreground [&>li]:mt-1",
        className
      )}
      {...props}
    />
  ),
  ol: ({ className, ...props }) => (
    <ol
      className={cn(
        "aui-md-ol my-3 ms-5 list-decimal marker:text-muted-foreground [&>li]:mt-1",
        className
      )}
      {...props}
    />
  ),
  hr: ({ className, ...props }) => (
    <hr
      className={cn("aui-md-hr my-3 border-muted-foreground/20", className)}
      {...props}
    />
  ),
  table: ({ className, ...props }) => (
    <div className="aui-md-table-wrapper my-3 overflow-x-auto">
      <table
        className={cn(
          "aui-md-table w-full border-separate border-spacing-0",
          className
        )}
        {...props}
      />
    </div>
  ),
  th: ({ className, ...props }) => (
    <th
      className={cn(
        "aui-md-th bg-muted px-3 py-1.5 text-start font-medium first:rounded-ss-lg last:rounded-se-lg [[align=center]]:text-center [[align=right]]:text-right",
        className
      )}
      {...props}
    />
  ),
  td: ({ className, ...props }) => (
    <td
      className={cn(
        "aui-md-td border-s border-b border-muted-foreground/20 px-3 py-1.5 text-start last:border-e [[align=center]]:text-center [[align=right]]:text-right",
        className
      )}
      {...props}
    />
  ),
  tr: ({ className, ...props }) => (
    <tr
      className={cn(
        "aui-md-tr m-0 border-b p-0 first:border-t [&:last-child>td:first-child]:rounded-es-lg [&:last-child>td:last-child]:rounded-ee-lg",
        className
      )}
      {...props}
    />
  ),
  li: ({ className, ...props }) => (
    <li className={cn("aui-md-li leading-relaxed", className)} {...props} />
  ),
  strong: ({ className, ...props }) => (
    <strong
      className={cn("aui-md-strong font-semibold", className)}
      {...props}
    />
  ),
  sup: ({ className, ...props }) => (
    <sup
      className={cn("aui-md-sup [&>a]:text-xs [&>a]:no-underline", className)}
      {...props}
    />
  ),
  pre: ({ className, ...props }) => (
    <pre
      className={cn(
        "aui-md-pre overflow-x-auto rounded-t-none rounded-b-(--radius-band) border border-t-0 border-border bg-muted/30 p-3.5 text-xs leading-relaxed",
        className
      )}
      {...props}
    />
  ),
  code: function Code({ className, ...props }) {
    const isCodeBlock = useIsMarkdownCodeBlock()
    return (
      <code
        className={cn(
          !isCodeBlock &&
            "aui-md-inline-code rounded-(--radius-chip) bg-muted px-1.5 py-0.5 font-mono text-[0.85em]",
          className
        )}
        {...props}
      />
    )
  },
  CodeHeader,
})
