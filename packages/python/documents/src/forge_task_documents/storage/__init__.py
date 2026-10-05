"""Storage backends. Each one implements ``protocols.StorageBackend``.

To add a database (e.g. Postgres + pgvector + tsvector):
  1. implement DocumentStore, ChunkStore and SearchBackend (fuse with
     ``retrieval.fusion.rrf_fuse`` if the DB has no native fusion),
  2. bundle them in a class with ``ensure_schema`` / ``close``,
  3. register a factory: ``register_storage_backend("postgres", factory)``,
  4. run tests/test_storage_contract.py against it.
"""
