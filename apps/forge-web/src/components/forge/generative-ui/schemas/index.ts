import type { z } from "zod"
import { Choices } from "./choices"

/** Forge schemas; all other components use the package's schema unchanged. */
export const CUSTOM_SCHEMAS: Record<
  string,
  { description: string; properties: z.ZodType } | undefined
> = { Choices }
