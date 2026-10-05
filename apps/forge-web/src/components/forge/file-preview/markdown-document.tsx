import Markdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import { cn } from "cn"

/*
 * Markdown laid out as a document (GitHub-flavoured: tables, task lists,
 * strikethrough, autolinks), in the workspace's type and tokens. Raw HTML in
 * the source is shown as text, never run; links open in a new tab.
 */

// react-markdown hands each component its syntax-tree node, which mustn't
// reach the DOM as an attribute.
function omitNode<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const { node, ...rest } = props
  void node
  return rest
}

const COMPONENTS: Components = {
  h1: ({ className, ...props }) => (
    <h1
      className={cn(
        "mt-8 mb-3 text-[1.375rem] font-semibold tracking-[-.3px] text-foreground first:mt-0",
        className
      )}
      {...omitNode(props)}
    />
  ),
  h2: ({ className, ...props }) => (
    <h2
      className={cn(
        "mt-7 mb-2.5 text-[1.0625rem] font-semibold text-foreground first:mt-0",
        className
      )}
      {...omitNode(props)}
    />
  ),
  h3: ({ className, ...props }) => (
    <h3
      className={cn(
        "mt-6 mb-2 text-[0.9375rem] font-semibold text-foreground first:mt-0",
        className
      )}
      {...omitNode(props)}
    />
  ),
  h4: ({ className, ...props }) => (
    <h4
      className={cn(
        "mt-5 mb-1.5 text-sm font-semibold text-foreground first:mt-0",
        className
      )}
      {...omitNode(props)}
    />
  ),
  h5: ({ className, ...props }) => (
    <h5
      className={cn("mt-4 mb-1 text-sm font-medium first:mt-0", className)}
      {...omitNode(props)}
    />
  ),
  h6: ({ className, ...props }) => (
    <h6
      className={cn(
        "mt-4 mb-1 text-sm font-medium text-muted-foreground first:mt-0",
        className
      )}
      {...omitNode(props)}
    />
  ),
  p: ({ className, ...props }) => (
    <p
      className={cn("my-3 first:mt-0 last:mb-0", className)}
      {...omitNode(props)}
    />
  ),
  a: ({ className, ...props }) => (
    <a
      className={cn(
        "text-foreground underline underline-offset-2 hover:text-muted-foreground",
        className
      )}
      target="_blank"
      rel="noopener noreferrer"
      {...omitNode(props)}
    />
  ),
  blockquote: ({ className, ...props }) => (
    <blockquote
      className={cn(
        "my-4 border-s-2 border-border ps-4 text-muted-foreground",
        className
      )}
      {...omitNode(props)}
    />
  ),
  ul: ({ className, ...props }) => (
    <ul
      className={cn(
        "my-3 ms-5 list-disc marker:text-subtle [&.contains-task-list]:ms-0 [&.contains-task-list]:list-none [&>li]:mt-1",
        className
      )}
      {...omitNode(props)}
    />
  ),
  ol: ({ className, ...props }) => (
    <ol
      className={cn(
        "my-3 ms-5 list-decimal marker:text-subtle [&>li]:mt-1",
        className
      )}
      {...omitNode(props)}
    />
  ),
  li: ({ className, ...props }) => (
    <li
      className={cn(
        "[&>input]:me-2 [&>input]:align-middle [&>p]:my-1",
        className
      )}
      {...omitNode(props)}
    />
  ),
  hr: ({ className, ...props }) => (
    <hr className={cn("my-6 border-border", className)} {...omitNode(props)} />
  ),
  table: ({ className, ...props }) => (
    <div className="my-4 overflow-x-auto rounded-(--radius-card) border">
      <table
        className={cn("w-full border-collapse text-xs", className)}
        {...omitNode(props)}
      />
    </div>
  ),
  th: ({ className, ...props }) => (
    <th
      className={cn(
        "border-b bg-muted px-3 py-1.5 text-start font-medium text-foreground [[align=center]]:text-center [[align=right]]:text-right",
        className
      )}
      {...omitNode(props)}
    />
  ),
  td: ({ className, ...props }) => (
    <td
      className={cn(
        "border-b px-3 py-1.5 align-top [[align=center]]:text-center [[align=right]]:text-right",
        className
      )}
      {...omitNode(props)}
    />
  ),
  tr: ({ className, ...props }) => (
    <tr
      className={cn("last:[&>td]:border-b-0", className)}
      {...omitNode(props)}
    />
  ),
  pre: ({ className, ...props }) => (
    <pre
      className={cn(
        "my-4 overflow-x-auto rounded-(--radius-card) bg-muted px-4 py-3 font-mono text-xs/relaxed [&>code]:bg-transparent [&>code]:p-0",
        className
      )}
      {...omitNode(props)}
    />
  ),
  code: ({ className, ...props }) => (
    <code
      className={cn(
        "rounded-[4px] bg-muted px-1 py-0.5 font-mono text-[0.85em]",
        className
      )}
      {...omitNode(props)}
    />
  ),
  img: ({ className, alt, ...props }) => (
    <img
      className={cn("my-4 max-w-full rounded-(--radius-card)", className)}
      alt={alt ?? ""}
      loading="lazy"
      {...omitNode(props)}
    />
  ),
}

const PLUGINS = [remarkGfm]

export function MarkdownDocument({ source }: { source: string }) {
  return (
    <div className="text-foreground">
      <Markdown remarkPlugins={PLUGINS} components={COMPONENTS}>
        {source}
      </Markdown>
    </div>
  )
}
