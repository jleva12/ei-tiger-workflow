import jsonata from "jsonata"

import { normalizeReferences } from "./references"
import { t, type DataType } from "./types"

/*
 * JSONata, the one expression language of a workflow's settings. Runs
 * evaluate it with Forge's own Python engine (packages/python/jsonata);
 * the builder only parses it here, with the reference parser, to check it
 * and to read the paths in it. Nothing here evaluates an expression.
 */

/** A node of the reference parser's syntax tree: the parts the builder reads. */
export type JsonataNode = {
  type: string
  value?: unknown
  /** Where the node's token ends in the text. */
  position?: number
  steps?: JsonataNode[]
  stages?: JsonataNode[]
  expr?: JsonataNode
  expression?: JsonataNode
  expressions?: JsonataNode[]
  lhs?: JsonataNode | JsonataNode[][]
  rhs?: JsonataNode
  arguments?: JsonataNode[]
  procedure?: JsonataNode
  condition?: JsonataNode
  then?: JsonataNode
  else?: JsonataNode
  body?: JsonataNode
  pattern?: JsonataNode
  update?: JsonataNode
  delete?: JsonataNode
  terms?: { expression: JsonataNode }[]
  group?: { lhs: JsonataNode[][] }
}

export type JsonataError = {
  /** Where in the text it went wrong: the end of the token it stopped at. */
  position: number
  token?: string
  code?: string
  message: string
}

export type Parsed = { ast: JsonataNode; error?: undefined } | { ast?: undefined; error: JsonataError }

const cache = new Map<string, Parsed>()
const CACHE_SIZE = 400

/** The syntax tree of an expression, or where and why it isn't JSONata. */
export function parseJsonata(text: string): Parsed {
  const known = cache.get(text)
  if (known) return known
  let parsed: Parsed
  try {
    parsed = { ast: jsonata(normalizeReferences(text)).ast() as JsonataNode }
  } catch (caught) {
    const error = caught as Partial<JsonataError> & { token?: unknown }
    parsed = {
      error: {
        position: typeof error.position === "number" ? error.position : text.length,
        token: typeof error.token === "string" ? error.token : undefined,
        code: typeof error.code === "string" ? error.code : undefined,
        message: typeof error.message === "string" ? error.message : "It isn't valid JSONata",
      },
    }
  }
  if (cache.size >= CACHE_SIZE) cache.clear()
  cache.set(text, parsed)
  return parsed
}

export type JsonataFunction = {
  /** How it's called, e.g. `$contains(str, pattern)`. */
  signature: string
  description: string
  returns: () => DataType
}

const text = () => t.string()
const number = () => t.number()
const integer = () => t.number(undefined, true)
const bool = () => t.boolean()
const list = (items: DataType = t.unknown()) => () => t.array(items)
const any = () => t.unknown()

/** JSONata's built-in functions, by name without the `$`. */
export const JSONATA_FUNCTIONS: Record<string, JsonataFunction> = {
  // Text
  string: { signature: "$string(value, prettify?)", description: "The value as text; objects and lists as JSON.", returns: text },
  length: { signature: "$length(str)", description: "How many characters the text has.", returns: integer },
  substring: { signature: "$substring(str, start, length?)", description: "Part of the text, from start (0 is the first character).", returns: text },
  substringBefore: { signature: "$substringBefore(str, chars)", description: "The text before the first occurrence of chars.", returns: text },
  substringAfter: { signature: "$substringAfter(str, chars)", description: "The text after the first occurrence of chars.", returns: text },
  uppercase: { signature: "$uppercase(str)", description: "The text in capitals.", returns: text },
  lowercase: { signature: "$lowercase(str)", description: "The text in lowercase.", returns: text },
  trim: { signature: "$trim(str)", description: "The text without surrounding spaces, runs of spaces made one.", returns: text },
  pad: { signature: "$pad(str, width, char?)", description: "The text padded to width (negative pads on the left).", returns: text },
  contains: { signature: "$contains(str, pattern)", description: "Whether the text contains a string or matches a regex.", returns: bool },
  split: { signature: "$split(str, separator, limit?)", description: "The text split into a list of strings.", returns: list(t.string()) },
  join: { signature: "$join(array, separator?)", description: "A list of strings joined into one.", returns: text },
  match: { signature: "$match(str, pattern, limit?)", description: "The regex's matches in the text.", returns: list() },
  replace: { signature: "$replace(str, pattern, replacement, limit?)", description: "The text with a string or regex replaced.", returns: text },
  base64encode: { signature: "$base64encode(str)", description: "The text in base64.", returns: text },
  base64decode: { signature: "$base64decode(str)", description: "Base64 decoded to text.", returns: text },
  encodeUrlComponent: { signature: "$encodeUrlComponent(str)", description: "The text escaped for a URL's query or path part.", returns: text },
  encodeUrl: { signature: "$encodeUrl(str)", description: "The text escaped as a whole URL.", returns: text },
  decodeUrlComponent: { signature: "$decodeUrlComponent(str)", description: "A URL part unescaped.", returns: text },
  decodeUrl: { signature: "$decodeUrl(str)", description: "A URL unescaped.", returns: text },
  formatNumber: { signature: "$formatNumber(number, picture, options?)", description: "The number as text, by a picture like #,##0.00.", returns: text },
  formatBase: { signature: "$formatBase(number, radix?)", description: "The number as text in another base.", returns: text },
  formatInteger: { signature: "$formatInteger(number, picture)", description: "The whole number as text, e.g. in words or roman numerals.", returns: text },
  parseInteger: { signature: "$parseInteger(str, picture)", description: "Text read back as a whole number, by a picture.", returns: integer },
  // Numbers
  number: { signature: "$number(value)", description: "The value as a number.", returns: number },
  abs: { signature: "$abs(number)", description: "The number without its sign.", returns: number },
  floor: { signature: "$floor(number)", description: "The number rounded down.", returns: integer },
  ceil: { signature: "$ceil(number)", description: "The number rounded up.", returns: integer },
  round: { signature: "$round(number, precision?)", description: "The number rounded, half to even.", returns: number },
  power: { signature: "$power(base, exponent)", description: "base to the power of exponent.", returns: number },
  sqrt: { signature: "$sqrt(number)", description: "The square root.", returns: number },
  random: { signature: "$random()", description: "A random number from 0 up to 1.", returns: number },
  sum: { signature: "$sum(array)", description: "The numbers added up.", returns: number },
  max: { signature: "$max(array)", description: "The largest number.", returns: number },
  min: { signature: "$min(array)", description: "The smallest number.", returns: number },
  average: { signature: "$average(array)", description: "The mean of the numbers.", returns: number },
  // True or false
  boolean: { signature: "$boolean(value)", description: "Whether the value counts as true.", returns: bool },
  not: { signature: "$not(value)", description: "The opposite of the value, as true or false.", returns: bool },
  exists: { signature: "$exists(value)", description: "Whether the value is there at all.", returns: bool },
  // Lists
  count: { signature: "$count(array)", description: "How many items the list has.", returns: integer },
  append: { signature: "$append(array1, array2)", description: "Two lists as one.", returns: list() },
  sort: { signature: "$sort(array, function?)", description: "The list in order.", returns: list() },
  reverse: { signature: "$reverse(array)", description: "The list backwards.", returns: list() },
  shuffle: { signature: "$shuffle(array)", description: "The list in random order.", returns: list() },
  distinct: { signature: "$distinct(array)", description: "The list without repeats.", returns: list() },
  zip: { signature: "$zip(array1, …)", description: "The lists' items side by side.", returns: list() },
  map: { signature: "$map(array, function)", description: "Each item made into something else.", returns: list() },
  filter: { signature: "$filter(array, function)", description: "The items the function says true for.", returns: list() },
  single: { signature: "$single(array, function?)", description: "The one item that matches; an error for none or more.", returns: any },
  reduce: { signature: "$reduce(array, function, init?)", description: "The list folded into one value.", returns: any },
  // Objects
  keys: { signature: "$keys(object)", description: "The object's field names.", returns: list(t.string()) },
  lookup: { signature: "$lookup(object, key)", description: "A field by a name worked out at run time.", returns: any },
  spread: { signature: "$spread(object)", description: "One object per field.", returns: list() },
  merge: { signature: "$merge(array)", description: "A list of objects as one object.", returns: () => t.object({}, undefined, { open: true }) },
  sift: { signature: "$sift(object, function)", description: "The fields the function says true for.", returns: () => t.object({}, undefined, { open: true }) },
  each: { signature: "$each(object, function)", description: "Each field made into something.", returns: list() },
  type: { signature: "$type(value)", description: 'The value\'s type: "string", "number", "array"…', returns: text },
  error: { signature: "$error(message)", description: "Stops the step with this message.", returns: any },
  assert: { signature: "$assert(condition, message)", description: "Stops the step with the message unless condition is true.", returns: any },
  // Time
  now: { signature: "$now(picture?, timezone?)", description: "The time now, as ISO 8601 text.", returns: text },
  millis: { signature: "$millis()", description: "The time now, in milliseconds since 1970.", returns: integer },
  fromMillis: { signature: "$fromMillis(number, picture?, timezone?)", description: "Milliseconds since 1970 as ISO 8601 text.", returns: text },
  toMillis: { signature: "$toMillis(timestamp, picture?)", description: "ISO 8601 text as milliseconds since 1970.", returns: integer },
}
