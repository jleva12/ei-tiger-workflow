/**
 * In expressions, {{ value }} is a grouped value, never text interpolation.
 * Keep every source offset intact so completion, hover and errors still point
 * into what the user wrote. Keep this scanner in sync with the runner's
 * forge_task_adk_workflows/support/references.py.
 */
export function normalizeReferences(text: string, strict = true): string {
  const result = text.split("")
  const braces: { reference: boolean; at: number }[] = []
  let operand = true
  let i = 0
  const fail = (message: string, at: number): never => {
    throw Object.assign(new Error(message), { position: at + 2, token: "{{" })
  }
  while (i < text.length) {
    const c = text[i]
    const pair = text.slice(i, i + 2)
    if (/\s/.test(c)) {
      i++
      continue
    }
    if (pair === "/*") {
      const end = text.indexOf("*/", i + 2)
      i = end < 0 ? text.length : end + 2
      continue
    }
    if (c === '"' || c === "'" || c === "`" || (c === "/" && operand)) {
      const quote = c
      let characterClass = false
      i++
      while (i < text.length) {
        const next = text[i++]
        if (next === "\\" && quote !== "`") i++
        else if (quote === "/" && next === "[") characterClass = true
        else if (quote === "/" && next === "]") characterClass = false
        else if (next === quote && !characterClass) break
      }
      operand = false
      continue
    }
    if (pair === "{{") {
      braces.push({ reference: true, at: i })
      result[i] = "("
      result[i + 1] = " "
      i += 2
      operand = true
      continue
    }
    const top = braces.at(-1)
    if (pair === "}}" && top?.reference) {
      if (strict && !text.slice(top.at + 2, i).trim()) fail("Choose a field inside {{ }}.", top.at)
      braces.pop()
      result[i] = " "
      result[i + 1] = ")"
      i += 2
      operand = false
      continue
    }
    if (c === "{") braces.push({ reference: false, at: i })
    else if (c === "}" && top && !top.reference) braces.pop()
    const word = /^[\w$]+/.exec(text.slice(i))?.[0]
    if (word) {
      operand = word === "and" || word === "or" || word === "in"
      i += word.length
    } else {
      operand = c === "*" || c === "%" ? !operand : !")]}".includes(c)
      i++
    }
  }
  const unclosed = braces.find((brace) => brace.reference)
  if (strict && unclosed) fail("Close this reference with }}.", unclosed.at)
  return result.join("")
}
