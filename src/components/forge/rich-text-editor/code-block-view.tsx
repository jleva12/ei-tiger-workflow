import * as React from "react"
import {
  NodeViewContent,
  NodeViewWrapper,
  type ReactNodeViewProps,
} from "@tiptap/react"

import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Icon } from "../icon"
import { lowlight } from "./lowlight"

const LABELS: Record<string, string> = {
  arduino: "Arduino",
  bash: "Bash",
  c: "C",
  cpp: "C++",
  csharp: "C#",
  css: "CSS",
  diff: "Diff",
  dockerfile: "Dockerfile",
  go: "Go",
  graphql: "GraphQL",
  ini: "INI / TOML",
  java: "Java",
  javascript: "JavaScript",
  json: "JSON",
  kotlin: "Kotlin",
  less: "Less",
  lua: "Lua",
  makefile: "Makefile",
  markdown: "Markdown",
  objectivec: "Objective-C",
  perl: "Perl",
  php: "PHP",
  "php-template": "PHP template",
  plaintext: "Plain text",
  python: "Python",
  "python-repl": "Python REPL",
  r: "R",
  ruby: "Ruby",
  rust: "Rust",
  scss: "SCSS",
  shell: "Shell session",
  sql: "SQL",
  swift: "Swift",
  typescript: "TypeScript",
  vbnet: "VB.NET",
  wasm: "WebAssembly",
  xml: "HTML / XML",
  yaml: "YAML",
}

const AUTO = "auto"

const LANGUAGES = [
  { value: AUTO, label: "Auto-detect" },
  ...lowlight
    .listLanguages()
    .map((value) => ({ value, label: LABELS[value] ?? value }))
    .sort((a, b) => a.label.localeCompare(b.label)),
]

/**
 * A code block with a language picker and a copy button above the code.
 * Highlighting itself comes from CodeBlockLowlight's decorations.
 */
export function CodeBlockView({
  node,
  editor,
  updateAttributes,
}: ReactNodeViewProps) {
  const language = (node.attrs.language as string | null) ?? AUTO
  const [copied, setCopied] = React.useState(false)

  React.useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(timer)
  }, [copied])

  const copy = () => {
    void navigator.clipboard
      .writeText(node.textContent)
      .then(() => setCopied(true))
  }

  return (
    <NodeViewWrapper data-slot="code-block" className="code-block">
      <div contentEditable={false} className="code-block-bar">
        <Select
          items={LANGUAGES}
          value={LANGUAGES.some((l) => l.value === language) ? language : AUTO}
          disabled={!editor.isEditable}
          onValueChange={(value) =>
            value &&
            updateAttributes({ language: value === AUTO ? null : value })
          }
        >
          <SelectTrigger
            size="sm"
            aria-label="Code language"
            className="h-6 border-transparent bg-transparent px-1.5 text-2xs text-muted-foreground hover:bg-muted"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent
            alignItemWithTrigger={false}
            className="max-h-72"
            finalFocus={() => editor.view.dom as HTMLElement}
          >
            {LANGUAGES.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={copy}
          className="text-2xs text-muted-foreground"
        >
          <Icon icon={copied ? "check" : "copy"} data-icon="inline-start" />
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre spellCheck={false}>
        <NodeViewContent<"code">
          as="code"
          className={language === AUTO ? "hljs" : `hljs language-${language}`}
        />
      </pre>
    </NodeViewWrapper>
  )
}
