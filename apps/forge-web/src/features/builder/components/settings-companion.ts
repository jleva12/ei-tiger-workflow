import * as React from "react"

/*
 * A settings dialog's cards (settings-dialog.tsx), as a stack up to four
 * deep: the settings, and the cards opened beside them. Any card can open
 * the next (the settings open the second, the second the third, the third
 * the fourth), so the levels being worked across stay in view together.
 *
 * - The deepest two cards show in full (three from 1700px wide). Earlier
 *   ones fold into narrow spines on the left showing their title; clicking
 *   a spine goes back to that card. Under 1080px only the deepest shows.
 * - Opening a card from a card closes anything deeper than it first.
 * - Escape or a click outside closes the deepest card (unsaved), then the
 *   next; a card's ✕ closes that card and everything deeper.
 * - Four is the limit: `useCompanionCard(id).canOpen` is false in the
 *   fourth card, and `show()` does nothing there.
 */

/** Cards in all, the settings included. */
export const MAX_CARDS = 4
export const MAX_COMPANIONS = MAX_CARDS - 1

/** The id `useSettingsCompanion` and `SettingsCompanion` use when given none. */
export const DEFAULT_COMPANION = "companion"

/** The cards open beside a settings dialog's settings. */
export type CardStack = {
  /** Where each card beside the settings renders: `slots[0]` is the second card, and so on. */
  slots: (HTMLElement | null)[]
  /** The open cards' ids, in order: `[second, third, fourth]`. */
  ids: string[]
  /** How many cards show in full at this window's width; earlier ones fold to spines. */
  fullCards: number
  /**
   * Opens `id` as the card after `fromLevel` (0 = the settings), closing
   * anything deeper. `opener` gets focus back when the card closes.
   */
  open: (fromLevel: number, id: string, opener: HTMLElement | null) => void
  /** Closes the card at `level` and everything deeper (`level` ≥ 1). */
  closeFrom: (level: number) => void
  /** Closes `id` and anything deeper without motion, when what opened it goes away. */
  release: (id: string) => void
  /**
   * Shows these cards after the settings, in order, in place of those open
   * (as many as fit): where something deep in them is, to go to it.
   */
  reveal: (ids: string[]) => void
}

export const CardStackContext = React.createContext<CardStack | null>(null)

/** What a folded card shows on its spine: its title, and its glyph. */
export type Spine = { title: React.ReactNode; glyph?: React.ReactNode }

/** The card something sits in: 0 is the settings. */
export type CardInfo = {
  level: number
  /** Names the card's spine, for when it's folded. */
  setSpine: (spine: Spine) => void
}

export const CardContext = React.createContext<CardInfo>({
  level: 0,
  setSpine: () => {},
})

/** Names the spine of the card it's called in, for when it's folded. */
export function useCardSpine(title: React.ReactNode, glyph?: React.ReactNode) {
  const { setSpine } = React.useContext(CardContext)
  React.useEffect(() => setSpine({ title, glyph }), [setSpine, title, glyph])
}

export type CompanionCard = {
  open: boolean
  /** False in the fourth card: nothing can open deeper. */
  canOpen: boolean
  /** Opens it after the card this is called in; the focused control gets focus back when it closes. */
  show: () => void
  /** Closes it and anything deeper. */
  hide: () => void
  toggle: () => void
}

const NO_CARD: CompanionCard = {
  open: false,
  canOpen: false,
  show: () => {},
  hide: () => {},
  toggle: () => {},
}

/**
 * Opens and closes one card (a `SettingsCompanion` with this `id`) from
 * anywhere inside a settings dialog. Call it in a component rendered inside
 * a card, not in the one that renders the dialog: the card it's called from
 * is where the new card opens after. Outside a settings dialog it's never
 * open and can't open.
 */
export function useCompanionCard(id: string): CompanionCard {
  const stack = React.useContext(CardStackContext)
  const { level } = React.useContext(CardContext)
  const index = stack ? stack.ids.indexOf(id) : -1
  const openCard = stack?.open
  const closeFrom = stack?.closeFrom
  return React.useMemo(() => {
    if (!openCard || !closeFrom) return NO_CARD
    const open = index !== -1
    const show = () =>
      openCard(
        level,
        id,
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null
      )
    const hide = () => {
      if (index !== -1) closeFrom(index + 1)
    }
    return {
      open,
      canOpen: level < MAX_COMPANIONS,
      show,
      hide,
      toggle: () => (open ? hide() : show()),
    }
  }, [openCard, closeFrom, level, id, index])
}

/**
 * Opens a card by its `id` after the card this is called in, as a
 * `CompanionCard`'s `show()` does: for one decided as it's opened (a row
 * just added). Does nothing in the fourth card, or outside a settings dialog.
 */
export function useOpenCompanion(): (id: string) => void {
  const openCard = React.useContext(CardStackContext)?.open
  const { level } = React.useContext(CardContext)
  return React.useCallback(
    (id: string) =>
      openCard?.(
        level,
        id,
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null
      ),
    [openCard, level]
  )
}

/**
 * Shows a row of cards after the settings at once, closing any others
 * (`[]` closes them all): to go to something deep in them, as an issue
 * clicked in the settings does. Outside a settings dialog it does nothing.
 */
export function useRevealCompanions(): (ids: string[]) => void {
  const reveal = React.useContext(CardStackContext)?.reveal
  return React.useCallback((ids: string[]) => reveal?.(ids), [reveal])
}

/**
 * Opens and closes the card beside the settings, gliding: `[open, setOpen]`.
 * Inside a `SettingsDialog` only. `id` names the card, for a card that opens
 * more than one (`SettingsCompanion` takes the same `id`).
 */
export function useSettingsCompanion(
  id = DEFAULT_COMPANION
): [boolean, (open: boolean) => void] {
  if (!React.useContext(CardStackContext))
    throw new Error("useSettingsCompanion needs a SettingsDialog")
  const card = useCompanionCard(id)
  const { show, hide } = card
  const setOpen = React.useCallback(
    (next: boolean) => (next ? show() : hide()),
    [show, hide]
  )
  return [card.open, setOpen]
}

/**
 * Where a component sits: inside a settings dialog (`inDialog`), at which
 * card (`level`, 0 = the settings), and whether it can still open a card
 * after its own (`canOpen`; false in the fourth).
 */
export function useSettingsCard() {
  const stack = React.useContext(CardStackContext)
  const { level } = React.useContext(CardContext)
  return {
    inDialog: stack !== null,
    level,
    canOpen: stack !== null && level < MAX_COMPANIONS,
  }
}
