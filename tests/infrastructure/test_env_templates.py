"""The apps' .env templates take shared values from .env.common by reference."""

import unittest

from test_compose import ROOT, env_file

SHARED = ("FORGE_EMBEDDING_MODEL", "FORGE_EMBEDDING_DIMENSIONS", "FORGE_EMBEDDING_BASE_URL")

# Each app's embedding settings, and the shared value each must name: the one
# model and vector length every app embeds and searches with.
EMBEDDING_REFERENCES = {
    "apps/forge-admin-api/.env.example": {
        "FORGE_ADMIN_KNOWLEDGE_EMBEDDING__DOCUMENT_MODEL": "FORGE_EMBEDDING_MODEL",
        "FORGE_ADMIN_KNOWLEDGE_EMBEDDING__DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "FORGE_ADMIN_KNOWLEDGE_EMBEDDING__BASE_URL": "FORGE_EMBEDDING_BASE_URL",
        "FORGE_ADMIN_CODEGRAPH__EMBEDDING__MODEL": "FORGE_EMBEDDING_MODEL",
        "FORGE_ADMIN_CODEGRAPH__EMBEDDING__DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "FORGE_ADMIN_CODEGRAPH__EMBEDDING__BASE_URL": "FORGE_EMBEDDING_BASE_URL",
    },
    "apps/forge-async-worker/.env.example": {
        "HYBRID_EMBEDDING__DOCUMENT_MODEL": "FORGE_EMBEDDING_MODEL",
        "HYBRID_EMBEDDING__DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "HYBRID_EMBEDDING__BASE_URL": "FORGE_EMBEDDING_BASE_URL",
        "HYBRID_ADK_WORKFLOWS__CODEGRAPH__EMBEDDING__MODEL": "FORGE_EMBEDDING_MODEL",
        "HYBRID_ADK_WORKFLOWS__CODEGRAPH__EMBEDDING__DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "HYBRID_ADK_WORKFLOWS__CODEGRAPH__EMBEDDING__BASE_URL": "FORGE_EMBEDDING_BASE_URL",
    },
    "apps/forge-codegraph-worker/.env.example": {
        "CODEGRAPH_EMBEDDING_MODEL": "FORGE_EMBEDDING_MODEL",
        "CODEGRAPH_EMBEDDING_DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "CODEGRAPH_EMBEDDING_BASE_URL": "FORGE_EMBEDDING_BASE_URL",
    },
    "apps/forge-codegraph-mcp/.env.example": {
        "CODEGRAPH_EMBEDDING__MODEL": "FORGE_EMBEDDING_MODEL",
        "CODEGRAPH_EMBEDDING__DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "CODEGRAPH_EMBEDDING__BASE_URL": "FORGE_EMBEDDING_BASE_URL",
    },
}

# Every reader of the code graph: its template, and the prefix its code graph
# settings share; and the settings each names from .env.common.
CODEGRAPH_READERS = {
    "apps/forge-admin-api/.env.example": "FORGE_ADMIN_CODEGRAPH__",
    "apps/forge-async-worker/.env.example": "HYBRID_ADK_WORKFLOWS__CODEGRAPH__",
    "apps/forge-codegraph-mcp/.env.example": "CODEGRAPH_",
}
CODEGRAPH_SHARED = {
    "SPANNER__DATABASE": "FORGE_CODEGRAPH_SPANNER_DATABASE",
    "SPANNER__CURSOR_SIGNING_KEY": "FORGE_CODEGRAPH_CURSOR_SIGNING_KEY",
    "SEARCH__ENHANCE_QUERY": "FORGE_CODEGRAPH_SEARCH_ENHANCE_QUERY",
}


class EmbeddingTemplatesTest(unittest.TestCase):
    def test_env_common_defines_the_shared_embedding_model(self):
        common = env_file(ROOT / ".env.common.example")
        for name in SHARED:
            with self.subTest(name=name):
                self.assertIn(name, common)
        self.assertTrue(common["FORGE_EMBEDDING_MODEL"])
        self.assertGreater(int(common["FORGE_EMBEDDING_DIMENSIONS"]), 0)
        # One name per value: no app-specific copies left to drift.
        self.assertFalse([name for name in common if "EMBEDDING" in name and name not in SHARED])

    def test_every_app_names_the_shared_embedding_model(self):
        for path, settings in EMBEDDING_REFERENCES.items():
            template = env_file(ROOT / path)
            for setting, shared in settings.items():
                with self.subTest(template=path, setting=setting):
                    self.assertEqual(template.get(setting), "${" + shared + "}")


class CodeGraphTemplatesTest(unittest.TestCase):
    def test_every_reader_names_the_shared_code_graph_settings(self):
        # Natively, every reader of the code graph (packages/python/
        # codegraph-query) reads the same database with the same cursor key
        # and query enhancement, from .env.common, as make env sets them.
        common = env_file(ROOT / ".env.common.example")
        for name in CODEGRAPH_SHARED.values():
            with self.subTest(name=name):
                self.assertIn(name, common)
        self.assertNotIn("FORGE_CODEGRAPH_ADMISSION_TOKEN", common)
        for path, prefix in CODEGRAPH_READERS.items():
            template = env_file(ROOT / path)
            with self.subTest(template=path):
                for setting, shared in CODEGRAPH_SHARED.items():
                    self.assertEqual(template.get(prefix + setting), "${" + shared + "}")
                self.assertEqual(template.get(prefix + "EMBEDDING__API_KEY"), "${OPENAI_API_KEY}")
                # Nothing calls the worker: it serves only its probes.
                self.assertFalse([key for key in template if key.endswith(("CODEGRAPH_URL", "CODEGRAPH_TOKEN"))])

    def test_the_worker_only_ingests(self):
        worker = env_file(ROOT / "apps/forge-codegraph-worker/.env.example")
        self.assertNotIn("CODEGRAPH_ADMISSION_TOKEN", worker)
        self.assertNotIn("CODEGRAPH_SEARCH_ENHANCE_QUERY", worker)


if __name__ == "__main__":
    unittest.main()
