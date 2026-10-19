import * as React from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { isText } from "../value"

const LazyDiagram = React.lazy(() => import("./lazy/diagram"))

export function RichDiagram({
  code,
  title,
}: {
  code?: unknown
  title?: unknown
}) {
  if (typeof code !== "string" || !code.trim()) return null
  return (
    <React.Suspense fallback={<Skeleton className="h-32 w-full" />}>
      <LazyDiagram
        code={code}
        title={isText(title) ? String(title) : undefined}
      />
    </React.Suspense>
  )
}
