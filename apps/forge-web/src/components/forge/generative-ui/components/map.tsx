import * as React from "react"
import { Skeleton } from "@/components/ui/skeleton"
import type { MapPlace } from "./lazy/map"

const LazyMap = React.lazy(() => import("./lazy/map"))

const placesOf = (places: unknown): MapPlace[] =>
  (Array.isArray(places) ? places : []).flatMap((place: unknown) => {
    if (typeof place !== "object" || place === null) return []
    const { label, lat, lng, detail } = place as Record<string, unknown>
    return typeof label === "string" &&
      typeof lat === "number" &&
      typeof lng === "number" &&
      Math.abs(lat) <= 90 &&
      Math.abs(lng) <= 180
      ? [
          {
            label,
            lat,
            lng,
            detail: typeof detail === "string" ? detail : undefined,
          },
        ]
      : []
  })

export function RichMap({
  places,
  route,
}: {
  places?: unknown
  route?: unknown
}) {
  const pins = placesOf(places)
  if (!pins.length) return null
  return (
    <React.Suspense fallback={<Skeleton className="h-56 w-full" />}>
      <LazyMap places={pins} route={route === true} />
    </React.Suspense>
  )
}
