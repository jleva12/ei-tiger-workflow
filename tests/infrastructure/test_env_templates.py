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
    },
    "apps/forge-async-worker/.env.example": {
        "HYBRID_EMBEDDING__DOCUMENT_MODEL": "FORGE_EMBEDDING_MODEL",
        "HYBRID_EMBEDDING__DIMENSIONS": "FORGE_EMBEDDING_DIMENSIONS",
        "HYBRID_EMBEDDING__BASE_URL": "FORGE_EMBEDDING_BASE_URL",
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
    def test_graph_knowledge_bases_reach_the_native_worker_with_its_token(self):
        # Natively, the admin API and the workflows worker call the code
        # graph worker where make worker listens, with the token make env
        # generates into .env.common.
        worker = env_file(ROOT / "apps/forge-codegraph-worker/.env.example")
        address = worker["CODEGRAPH_HEALTH_ADDR"]
        callers = {
            "apps/forge-admin-api/.env.example": "FORGE_ADMIN_CODEGRAPH",
            "apps/forge-async-worker/.env.example": "HYBRID_ADK_WORKFLOWS__CODEGRAPH",
        }
        for path, prefix in callers.items():
            template = env_file(ROOT / path)
            with self.subTest(template=path):
                self.assertEqual(template[f"{prefix}_URL"], f"http://{address}")
                self.assertEqual(
                    template[f"{prefix}_TOKEN"], "${FORGE_CODEGRAPH_ADMISSION_TOKEN}"
                )
                self.assertEqual(
                    worker["CODEGRAPH_ADMISSION_TOKEN"], "${FORGE_CODEGRAPH_ADMISSION_TOKEN}"
                )


if __name__ == "__main__":
    unittest.main()
