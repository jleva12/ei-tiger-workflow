/** Controls that are a form's fields: inside one, its confirm sends them. */
const FIELDS = new Set([
  "Select",
  "Input",
  "DatePicker",
  "Checkbox",
  "Slider",
  "RadioGroup",
  "CheckboxGroup",
])

/**
 * A composition whose forms' fields don't act on their own: inside a Form,
 * or a Card with asForm, a field's `_action` is dropped, so choosing a date
 * doesn't send it before the rest is filled in; the form's confirm sends
 * every field together. Buttons keep theirs. Models add them anyway.
 */
export function withQuietFields(
  node: unknown,
  inForm = false,
  depth = 0
): unknown {
  if (depth > 32) return node
  if (Array.isArray(node))
    return node.map((each) => withQuietFields(each, inForm, depth + 1))
  if (typeof node !== "object" || node === null) return node
  const element = node as Record<string, unknown>
  const type = element._type ?? element.$type
  const form =
    inForm || type === "Form" || (type === "Card" && element.asForm === true)
  let kept = element
  if (inForm && FIELDS.has(String(type))) {
    kept = Object.fromEntries(
      Object.entries(element).filter(
        ([key]) => key !== "_action" && key !== "$action"
      )
    )
  }
  return "children" in kept
    ? { ...kept, children: withQuietFields(kept.children, form, depth + 1) }
    : kept
}

type Element = Record<string, unknown>

const isElement = (node: unknown): node is Element =>
  typeof node === "object" && node !== null && !Array.isArray(node)

const typeOfElement = (element: Element) => element._type ?? element.$type

const actionOf = (element: unknown) =>
  isElement(element) ? (element._action ?? element.$action) : undefined

/** The action of the first Form with one in a node's children. */
function formActionIn(node: unknown, depth = 0): unknown {
  if (depth > 32) return undefined
  if (Array.isArray(node)) {
    for (const each of node) {
      const found = formActionIn(each, depth + 1)
      if (found) return found
    }
    return undefined
  }
  if (!isElement(node)) return undefined
  if (typeOfElement(node) === "Form" && actionOf(node)) return actionOf(node)
  return formActionIn(node.children, depth + 1)
}

/** A node's children with each Form in them replaced by the Form's own children. */
function withoutForms(node: unknown, depth = 0): unknown {
  if (depth > 32) return node
  if (Array.isArray(node))
    return node.flatMap((each) => {
      const kept = withoutForms(each, depth + 1)
      return Array.isArray(kept) ? kept : [kept]
    })
  if (!isElement(node)) return node
  if (typeOfElement(node) === "Form")
    return withoutForms(node.children ?? [], depth + 1)
  return "children" in node
    ? { ...node, children: withoutForms(node.children, depth + 1) }
    : node
}

/**
 * A composition whose Cards send the forms in them: a Card with a confirm
 * that does nothing of its own (no action, not `asForm` with one), holding
 * a Form with an action, becomes that form (`asForm`, the Form's action, its
 * fields the Card's), so its confirm sends them. Models put the button on
 * the card and the action on the form inside it; a form can't hold another,
 * and without this the button does nothing.
 */
export function withCardForms(node: unknown, depth = 0): unknown {
  if (depth > 32) return node
  if (Array.isArray(node))
    return node.map((each) => withCardForms(each, depth + 1))
  if (!isElement(node)) return node
  const element =
    "children" in node
      ? { ...node, children: withCardForms(node.children, depth + 1) }
      : node
  const confirm = element.confirm
  const sends =
    actionOf(confirm) || (element.asForm === true && actionOf(element))
  if (typeOfElement(element) !== "Card" || !isElement(confirm) || sends)
    return element
  const action = formActionIn(element.children)
  if (!action) return element
  return {
    ...element,
    asForm: true,
    _action: action,
    children: withoutForms(element.children),
  }
}
