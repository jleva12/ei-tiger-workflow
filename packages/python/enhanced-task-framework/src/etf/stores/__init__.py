"""Concrete ``StateStore`` implementations.

* :class:`etf.stores.memory.InMemoryStateStore` — a complete reference implementation for
  tests and single-process use.
* :class:`etf.stores.beanie_store.BeanieStateStore` — the default MongoDB-backed store
  (requires the optional ``beanie`` dependency).
"""
