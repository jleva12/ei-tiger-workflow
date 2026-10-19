/**
 * Keeps a controlled `value` and the document in step without echoes:
 *
 * - an edit reaches `onChange` only when the Markdown actually changed;
 * - the parent handing back what `onChange` gave it changes nothing;
 * - a genuinely new `value` replaces the document, and wins over an edit
 *   still waiting in the debounce.
 */
export class MarkdownSync {
  /** The Markdown the parent knows: what we last emitted or received. */
  private known: string
  private timer: ReturnType<typeof setTimeout> | null = null
  private read: () => string
  private emit: (markdown: string) => void
  private delay: () => number

  constructor(options: {
    initial: string
    /** Serializes the document now. */
    read: () => string
    emit: (markdown: string) => void
    /** Milliseconds to wait after an edit; 0 emits at once. */
    delay: () => number
  }) {
    this.known = options.initial
    this.read = options.read
    this.emit = options.emit
    this.delay = options.delay
  }

  /** The document changed through an edit or the controller. */
  changed() {
    const delay = this.delay()
    if (delay <= 0) {
      this.flush()
      return
    }
    if (this.timer) clearTimeout(this.timer)
    this.timer = setTimeout(() => this.flush(), delay)
  }

  /** Emits a waiting change now (on blur and unmount). */
  flush() {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    const markdown = this.read()
    if (markdown === this.known) return
    this.known = markdown
    this.emit(markdown)
  }

  /**
   * A `value` arrived from the parent. Returns whether the document must be
   * replaced with it: not for our own echo, nor when it already says that.
   */
  received(value: string): boolean {
    if (value === this.known) return false
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.known = value
    return this.read() !== value
  }

  dispose() {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
  }
}
