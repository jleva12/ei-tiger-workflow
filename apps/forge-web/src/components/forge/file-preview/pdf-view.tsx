import "pdfjs-dist/web/pdf_viewer.css"
import "./file-preview.css"

import * as React from "react"
import {
  Search01Icon,
  SquareLockPasswordIcon,
} from "@hugeicons/core-free-icons"
import type { PDFDocumentProxy } from "pdfjs-dist"
import type { EventBus, PDFViewer } from "pdfjs-dist/web/pdf_viewer.mjs"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  BarButton,
  BarDivider,
  BarText,
  DownloadButton,
  PageControl,
  PreviewFrame,
  PreviewLoading,
  PreviewProblem,
  ZoomControl,
} from "./frame"
import { readFailure, type ViewProps } from "./preview"
import {
  isMac,
  stepZoom,
  useZoomKeys,
  type ZoomChange,
  type ZoomFit,
} from "./zoom"
import { loadPdfViewer, PDF_ASSETS, pdfjs } from "./pdfjs"

const FIT_VALUES: Record<ZoomFit, string> = {
  auto: "auto",
  width: "page-width",
  page: "page-fit",
}

type Matches = { current: number; total: number }
type Password = { submit: (password: string) => void; wrong: boolean }

/**
 * A PDF in pdf.js's own viewer: pages drawn as you scroll, text you can
 * select and copy, links that work, and find with every match highlighted.
 * Opens fitted to the view (never past 125%), and keeps fitting as the view
 * resizes until you pick a zoom.
 */
export default function PdfView({ data, toolbarEnd, onDownload }: ViewProps) {
  const containerRef = React.useRef<HTMLDivElement>(null)
  const viewerRef = React.useRef<HTMLDivElement>(null)
  // pdf.js's viewer, driven through its setters; the file's events once open.
  const pdfRef = React.useRef<PDFViewer | null>(null)
  const [open, setOpen] = React.useState<{ bus: EventBus; pages: number }>()
  const [page, setPage] = React.useState(1)
  const [scale, setScale] = React.useState(1)
  const [fit, setFit] = React.useState<ZoomFit | null>("auto")
  const fitRef = React.useRef(fit)
  const [password, setPassword] = React.useState<Password>()
  const [error, setError] = React.useState<Error>()

  // Each file is a new view (FilePreview remounts it), so this runs once.
  React.useEffect(() => {
    const container = containerRef.current
    const element = viewerRef.current
    if (!container || !element) return
    let cancelled = false
    let task: ReturnType<typeof pdfjs.getDocument> | undefined
    void (async () => {
      const components = await loadPdfViewer()
      const bytes = new Uint8Array(await data.arrayBuffer())
      if (cancelled) return
      const bus = new components.EventBus()
      const links = new components.PDFLinkService({
        eventBus: bus,
        externalLinkTarget: components.LinkTarget.BLANK,
        externalLinkRel: "noopener noreferrer nofollow",
      })
      const find = new components.PDFFindController({
        eventBus: bus,
        linkService: links,
      })
      const pdf = new components.PDFViewer({
        container,
        viewer: element,
        eventBus: bus,
        linkService: links,
        findController: find,
        removePageBorders: true,
        // Links and notes, but forms as they're printed: it's a viewer.
        annotationMode: pdfjs.AnnotationMode.ENABLE,
        imagesRightClickMinSize: -1,
      })
      pdfRef.current = pdf
      links.setViewer(pdf)
      bus.on("pagesinit", () => {
        pdf.currentScaleValue = FIT_VALUES[fitRef.current ?? "auto"]
      })
      bus.on("pagechanging", (event: { pageNumber: number }) =>
        setPage(event.pageNumber)
      )
      bus.on("scalechanging", (event: { scale: number }) =>
        setScale(event.scale)
      )
      task = pdfjs.getDocument({
        data: bytes,
        ...PDF_ASSETS,
        cMapPacked: true,
        enableXfa: false,
      })
      task.onPassword = (
        submit: (password: string) => void,
        reason: number
      ) => {
        if (!cancelled)
          setPassword({
            submit,
            wrong: reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD,
          })
      }
      const document = await task.promise
      if (cancelled) return
      setPassword(undefined)
      pdf.setDocument(document)
      links.setDocument(document, null)
      setOpen({ bus, pages: document.numPages })
    })().catch((problem: unknown) => {
      if (!cancelled)
        setError(
          problem instanceof Error ? problem : new Error(String(problem))
        )
    })
    return () => {
      cancelled = true
      // Resets the viewer (pdf.js takes null) before the document goes.
      pdfRef.current?.setDocument(null as unknown as PDFDocumentProxy)
      pdfRef.current = null
      void task?.destroy()
      element.replaceChildren()
    }
  }, [data])

  const zoom = React.useCallback((change: ZoomChange) => {
    const pdf = pdfRef.current
    if (!pdf) return
    if ("fit" in change) {
      fitRef.current = change.fit
      setFit(change.fit)
      pdf.currentScaleValue = FIT_VALUES[change.fit]
    } else {
      fitRef.current = null
      setFit(null)
      pdf.currentScale = change.scale
    }
  }, [])

  const goTo = React.useCallback((next: number) => {
    const pdf = pdfRef.current
    if (pdf) pdf.currentPageNumber = next
  }, [])

  // A fit keeps fitting as the view resizes (the details panel, the window).
  React.useEffect(() => {
    const container = containerRef.current
    if (!container || !open) return
    const observer = new ResizeObserver(() => {
      const pdf = pdfRef.current
      if (pdf && fitRef.current)
        pdf.currentScaleValue = FIT_VALUES[fitRef.current]
    })
    observer.observe(container)
    return () => observer.disconnect()
  }, [open])

  // Pinching a trackpad (a wheel with ctrl) zooms about the pointer.
  React.useEffect(() => {
    const container = containerRef.current
    if (!container || !open) return
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return
      event.preventDefault()
      fitRef.current = null
      setFit(null)
      pdfRef.current?.updateScale({
        scaleFactor: Math.exp(-event.deltaY / 100),
        origin: [event.clientX, event.clientY],
        drawingDelay: 300,
      })
    }
    container.addEventListener("wheel", onWheel, { passive: false })
    return () => container.removeEventListener("wheel", onWheel)
  }, [open])

  const frameRef = React.useRef<HTMLDivElement>(null)
  useZoomKeys(frameRef, (direction) =>
    direction === 0
      ? zoom({ fit: "auto" })
      : zoom({ scale: stepZoom(scale, direction) })
  )

  const [finding, setFinding] = React.useState(false)
  // ⌘F inside the preview finds in the document, not the page.
  React.useEffect(() => {
    const frame = frameRef.current
    if (!frame || !open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
        event.preventDefault()
        setFinding(true)
      }
    }
    frame.addEventListener("keydown", onKeyDown)
    return () => frame.removeEventListener("keydown", onKeyDown)
  }, [open])

  const toolbar = open && open.pages > 0 && (
    <>
      <PageControl page={page} count={open.pages} onPage={goTo} />
      <BarDivider />
      <ZoomControl
        scale={scale}
        fit={fit}
        fits={["auto", "width", "page"]}
        onChange={zoom}
      />
      <BarDivider />
      {finding ? (
        <FindBar bus={open.bus} onClose={() => setFinding(false)} />
      ) : (
        <BarButton
          label="Find in document"
          icon={Search01Icon}
          shortcut={isMac() ? "⌘ F" : "Ctrl F"}
          onClick={() => setFinding(true)}
        />
      )}
    </>
  )

  return (
    <div ref={frameRef} className="flex min-h-0 min-w-0 flex-1 flex-col">
      <PreviewFrame toolbar={toolbar} toolbarEnd={toolbarEnd} desk>
        <div
          ref={containerRef}
          tabIndex={0}
          role="document"
          aria-label="PDF pages"
          data-slot="pdf-pages"
          className="absolute inset-0 overflow-auto outline-none"
        >
          <div ref={viewerRef} className="pdfViewer" />
        </div>
        {error ? (
          <div className="absolute inset-0 bg-muted">
            <PreviewProblem
              kind="pdf"
              tone="danger"
              title="Couldn't show this PDF"
              description={readFailure("pdf", error)}
              actions={<DownloadButton onDownload={onDownload} />}
            />
          </div>
        ) : password ? (
          <div className="absolute inset-0 bg-muted">
            <PasswordForm password={password} />
          </div>
        ) : (
          !open && (
            <div className="absolute inset-0 bg-muted">
              <PreviewLoading kind="pdf" label="Opening the PDF…" />
            </div>
          )
        )}
      </PreviewFrame>
    </div>
  )
}

/** Find in the document: every match highlighted, Enter for the next. */
function FindBar({ bus, onClose }: { bus: EventBus; onClose: () => void }) {
  const [query, setQuery] = React.useState("")
  const [matches, setMatches] = React.useState<Matches>()
  const [missing, setMissing] = React.useState(false)
  const input = React.useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    input.current?.focus()
    const onCount = (event: { matchesCount: Matches }) =>
      setMatches(event.matchesCount)
    const onState = (event: { state: number; matchesCount: Matches }) => {
      // FindState.NOT_FOUND
      setMissing(event.state === 1)
      setMatches(event.matchesCount)
    }
    bus.on("updatefindmatchescount", onCount)
    bus.on("updatefindcontrolstate", onState)
    return () => {
      bus.off("updatefindmatchescount", onCount)
      bus.off("updatefindcontrolstate", onState)
      bus.dispatch("findbarclose", { source: null })
    }
  }, [bus])

  const search = (type: "" | "again", previous = false) =>
    bus.dispatch("find", {
      source: null,
      type,
      query,
      caseSensitive: false,
      entireWord: false,
      highlightAll: true,
      findPrevious: previous,
      matchDiacritics: false,
    })

  // Finds as you type, once typing pauses.
  React.useEffect(() => {
    if (!query) {
      bus.dispatch("findbarclose", { source: null })
      return
    }
    const timer = window.setTimeout(
      () =>
        bus.dispatch("find", {
          source: null,
          type: "",
          query,
          caseSensitive: false,
          entireWord: false,
          highlightAll: true,
          findPrevious: false,
          matchDiacritics: false,
        }),
      150
    )
    return () => window.clearTimeout(timer)
  }, [query, bus])

  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <div className="relative">
        <Icon
          icon={Search01Icon}
          size={14}
          className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-subtle"
        />
        <input
          ref={input}
          type="search"
          value={query}
          aria-label="Find in document"
          placeholder="Find in document"
          aria-invalid={(query !== "" && missing) || undefined}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault()
              if (query) search("again", event.shiftKey)
            }
            if (event.key === "Escape") {
              event.preventDefault()
              onClose()
            }
          }}
          className="h-7 w-52 rounded-(--radius-control) border border-input bg-background pr-2 pl-7 text-xs text-foreground outline-none placeholder:text-subtle focus-visible:border-ring aria-invalid:border-destructive/60 [&::-webkit-search-cancel-button]:hidden"
        />
      </div>
      <BarText aria-live="polite" className="min-w-16">
        {query
          ? missing
            ? "No matches"
            : matches && matches.total > 0
              ? `${matches.current} of ${matches.total}`
              : ""
          : ""}
      </BarText>
      <BarButton
        label="Previous match"
        icon="up"
        shortcut="⇧ Enter"
        disabled={!query || missing}
        onClick={() => search("again", true)}
      />
      <BarButton
        label="Next match"
        icon="down"
        shortcut="Enter"
        disabled={!query || missing}
        onClick={() => search("again")}
      />
      <BarButton label="Close find" icon="close" onClick={onClose} />
    </div>
  )
}

/** A password-protected PDF asks for its password here. */
function PasswordForm({ password }: { password: Password }) {
  const [value, setValue] = React.useState("")
  const [sent, setSent] = React.useState(false)
  // A wrong password asks again with a fresh request.
  const [lastPassword, setLastPassword] = React.useState(password)
  if (password !== lastPassword) {
    setLastPassword(password)
    setSent(false)
  }
  return (
    <PreviewProblem
      kind="pdf"
      title="This PDF is password-protected"
      description={
        password.wrong && !sent
          ? "That password isn't right. Try again."
          : "Enter its password to open it here. The password stays in your browser."
      }
      actions={
        <form
          className="flex w-full max-w-72 items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (!value) return
            setSent(true)
            password.submit(value)
          }}
        >
          <Input
            type="password"
            autoFocus
            autoComplete="off"
            aria-label="PDF password"
            aria-invalid={(password.wrong && !sent) || undefined}
            placeholder="Password"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            className="flex-1"
          />
          <Button type="submit" size="sm" disabled={!value || sent}>
            <Icon icon={SquareLockPasswordIcon} data-icon="inline-start" />
            Open
          </Button>
        </form>
      }
    />
  )
}
