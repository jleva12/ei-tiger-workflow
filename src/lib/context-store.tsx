import { createContext, useContext, useState, type ReactNode } from "react"
import {
  useStore as useZustandStore,
  type ExtractState,
  type StoreApi,
} from "zustand"

/** The Provider's `initial` prop: required unless the factory takes nothing. */
type InitialProp<Initial> = [Initial] extends [void]
  ? { initial?: undefined }
  : { initial: Initial }

export type ContextStoreProviderProps<Initial> = InitialProp<Initial> & {
  children?: ReactNode
}

const identity = <T,>(value: T) => value

/**
 * A Zustand store per Provider, seeded from props or context.
 *
 * React context carries only the store (it never changes), so it never
 * re-renders anything; components subscribe to slices through `useStore`
 * selectors, as with a global Zustand store. Each `<Provider>` gets its own
 * store, created once from `initial`: later changes to `initial` don't reset
 * it (give the Provider a `key` to start over, or `setState` to sync).
 *
 * ```tsx
 * export const {
 *   Provider: BoardProvider,
 *   useStore: useBoardStore,
 *   useStoreApi: useBoardStoreApi,
 * } = createContextStore(
 *   (initial: { view: View }) =>
 *     createStore<BoardState>()((set) => ({
 *       ...initial,
 *       setView: (view) => set({ view }),
 *     })),
 *   { name: "Board" }
 * )
 *
 * <BoardProvider initial={{ view: "kanban" }}>…</BoardProvider>
 * const view = useBoardStore((s) => s.view)
 * ```
 *
 * The factory returns the store, so middleware (`persist`, `devtools`,
 * `subscribeWithSelector`) goes where it always does, inside `createStore`,
 * and `useStoreApi()` keeps its extra API (e.g. `.persist`).
 */
export function createContextStore<
  Store extends StoreApi<unknown>,
  Initial = void,
>(
  factory: (initial: Initial) => Store,
  { name = "ContextStore" }: { name?: string } = {}
) {
  type State = ExtractState<Store>

  const StoreContext = createContext<Store | null>(null)
  StoreContext.displayName = `${name}Context`

  function Provider({ initial, children }: ContextStoreProviderProps<Initial>) {
    // Created once per Provider; `initial` only seeds it.
    const [store] = useState(() => factory(initial as Initial))
    return (
      <StoreContext.Provider value={store}>{children}</StoreContext.Provider>
    )
  }
  Provider.displayName = `${name}Provider`

  /**
   * The store itself, for reading or writing outside render (event
   * handlers, effects) without subscribing: `getState()`, `setState()`,
   * `subscribe()`.
   */
  function useStoreApi(): Store {
    const store = useContext(StoreContext)
    if (!store) {
      throw new Error(`${name} hooks must be used inside <${name}Provider>.`)
    }
    return store
  }

  /**
   * Subscribes to the store. With a selector, re-renders only when the
   * selected value changes; wrap object-returning selectors in `useShallow`.
   */
  function useStore(): State
  function useStore<T>(selector: (state: State) => T): T
  function useStore<T>(selector: (state: State) => T = identity as never) {
    return useZustandStore(useStoreApi(), selector)
  }

  return { Provider, useStore, useStoreApi, Context: StoreContext }
}
