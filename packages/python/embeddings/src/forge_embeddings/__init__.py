"""Model clients shared by the Forge embedding tasks.

A task gets the process-wide clients with ``model_clients(ctx)``
(:mod:`forge_embeddings.clients`), configured by ``HYBRID_EMBEDDING__*``,
``HYBRID_RERANK__*``, ``HYBRID_TOKENIZER__*`` and ``HYBRID_LLM__*``.
"""
