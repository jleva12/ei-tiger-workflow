"""Organizations' knowledge bases: named sets of documents their chat agents
search.

- :mod:`.storage`: the documents bucket (S3) the uploads are stored in.
- :mod:`.queue`: the async worker's documents queue, which parses, chunks and
  embeds them into MongoDB Atlas (``forge_task_documents``), and removes them.
- :mod:`.search`: the hybrid search (BM25 and vector) over what it embedded.
- :mod:`.access`: finding a knowledge base a caller may read or manage.
- :mod:`.tools`: knowledge bases as chat agents' tools.

The routes are ``api/routes/knowledge_bases.py``, ``knowledge_documents.py``
and ``knowledge_collections.py``.
"""
