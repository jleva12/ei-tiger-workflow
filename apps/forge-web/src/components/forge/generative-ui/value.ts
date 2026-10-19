export const isText = (value: unknown): value is string | number =>
  typeof value === "string" ||
  (typeof value === "number" && Number.isFinite(value))
