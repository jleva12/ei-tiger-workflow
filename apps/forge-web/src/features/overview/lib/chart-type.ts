import * as React from "react"

/*
 * How a widget's chart draws its series, which a person picks while
 * customizing: stacked bars (each bucket's make-up, the default), stacked
 * areas (the make-up flowing over the period) or lines (each series on its
 * own, to compare them). The widget provides it; the chart, its legend and
 * its tooltip read it.
 */

export const CHART_TYPES = ["bars", "area", "line"] as const

export type ChartType = (typeof CHART_TYPES)[number]

export const DEFAULT_CHART_TYPE: ChartType = "bars"

export const ChartTypeContext =
  React.createContext<ChartType>(DEFAULT_CHART_TYPE)

/** A widget's chart type from its settings; bars when they don't say. */
export const chartTypeOf = (settings: Record<string, unknown>): ChartType =>
  (CHART_TYPES as readonly unknown[]).includes(settings.chart)
    ? (settings.chart as ChartType)
    : DEFAULT_CHART_TYPE
