import * as React from "react"
import L from "leaflet"
import "leaflet/dist/leaflet.css"

/** A place a rich answer's Map shows. */
export type MapPlace = {
  label: string
  lat: number
  lng: number
  detail?: string
}

/**
 * A rich answer's Map: its places on OpenStreetMap's tiles, numbered in
 * order, with a line through them when it's a route, and the places listed
 * under it (which screen readers read; the map itself is hidden from them).
 * Loaded only when an answer has a map.
 */
export default function RichMap({
  places,
  route,
}: {
  places: MapPlace[]
  route: boolean
}) {
  const frame = React.useRef<HTMLDivElement>(null)
  const key = JSON.stringify([places, route])
  React.useEffect(() => {
    const element = frame.current
    if (!element || !places.length) return
    const map = L.map(element, {
      scrollWheelZoom: false,
      attributionControl: true,
    })
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map)
    const points = places.map((place) => L.latLng(place.lat, place.lng))
    if (route && points.length > 1) {
      L.polyline(points, { className: "rich-map-route", weight: 3 }).addTo(map)
    }
    places.forEach((place, index) =>
      L.circleMarker(points[index], {
        className: "rich-map-pin",
        radius: 7,
        weight: 2,
      })
        .bindTooltip(`${index + 1}. ${place.label}`)
        .addTo(map)
    )
    if (points.length === 1) map.setView(points[0], 12)
    else map.fitBounds(L.latLngBounds(points), { padding: [28, 28] })
    return () => {
      map.remove()
    }
    // The places by value: a new array of the same places draws nothing new.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  if (!places.length) return null
  return (
    <figure data-slot="rich-map" className="flex min-w-0 flex-col gap-2">
      <div
        ref={frame}
        aria-hidden="true"
        className="isolate h-56 w-full overflow-hidden rounded-(--radius-item) border border-border"
      />
      <ol className="flex flex-col gap-1 text-xs">
        {places.map((place, index) => (
          <li key={index} className="flex gap-2">
            <span className="w-4 shrink-0 text-right text-muted-foreground tabular-nums">
              {index + 1}
            </span>
            <span className="min-w-0">
              <span className="text-foreground">{place.label}</span>
              {place.detail && (
                <span className="text-muted-foreground"> · {place.detail}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
    </figure>
  )
}
