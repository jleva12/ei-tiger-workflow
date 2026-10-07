"""Validate the rendered deployment without starting containers or exposing secrets."""

import json
import os
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]

# Every service of the stack, with the admin seed's tools profile.
SERVICES = {
    "web",
    "admin",
    "admin-mysql",
    "admin-seed",
    "async-worker-adk-workflows",
    "async-worker-documents",
    "codegraph-mcp",
    "knowledge-s3",
    "mongo",
    "redis",
    "worker",
    "worker-spanner",
}
# The services that connect to the shared MongoDB (the admin API's ADK
# workflow documents), and the setting each connects with.
MONGO_CONNECTIONS = {
    "admin": "FORGE_ADMIN_MONGO_URI",
}
# The services that use the async worker's SAQ queue.
QUEUE_CONNECTIONS = {
    "async-worker-adk-workflows": "HYBRID_REDIS_URL",
}


def env_file(path):
    """A dotenv file's assignments, comments left out."""
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def render(*overlays, environment=None):
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env.compose.example",
        "--env-file",
        ".env.common.example",
        "-f",
        "compose.yaml",
    ]
    for overlay in overlays:
        command.extend(["-f", overlay])
    command.extend(
        ["--profile", "*", "config", "--no-env-resolution", "--format", "json"]
    )
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        **(environment or {}),
    }
    result = subprocess.run(
        command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise AssertionError("Compose configuration failed: " + result.stderr)
    return json.loads(result.stdout)


class SharedInfrastructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configuration = render()
        cls.services = cls.configuration["services"]

    def test_services(self):
        self.assertEqual(set(self.services), SERVICES)

    def test_single_mongo_and_redis(self):
        databases = {
            name
            for name, service in self.services.items()
            if service.get("image", "").startswith(("mongo", "redis:"))
        }
        self.assertEqual(databases, {"mongo", "redis"})
        self.assertEqual(
            self.services["mongo"]["image"], "mongodb/mongodb-atlas-local:8.3.9"
        )
        self.assertEqual(
            {v["target"] for v in self.services["mongo"]["volumes"]},
            {"/data/db", "/data/configdb"},
        )

    def test_applications_share_authenticated_mongo(self):
        root = self.services["mongo"]["environment"]
        for service, setting in MONGO_CONNECTIONS.items():
            with self.subTest(service=service):
                connection = urlsplit(self.services[service]["environment"][setting])
                self.assertEqual(connection.hostname, "mongo")
                self.assertEqual(connection.port, 27017)
                self.assertEqual(
                    connection.username, root["MONGODB_INITDB_ROOT_USERNAME"]
                )
                self.assertEqual(
                    connection.password, root["MONGODB_INITDB_ROOT_PASSWORD"]
                )
                self.assertIn("directConnection=true", connection.query)

    def test_admin_submits_runs_to_the_worker_queue(self):
        # The admin API's setting for the async worker's Redis, whatever it
        # is named: FORGE_ADMIN_<...>REDIS_URL.
        admin = self.services["admin"]["environment"]
        settings = [
            key
            for key in admin
            if key.startswith("FORGE_ADMIN_") and key.endswith("REDIS_URL")
        ]
        self.assertEqual(len(settings), 1, settings)
        connections = {("admin", settings[0]), *QUEUE_CONNECTIONS.items()}
        for service, key in connections:
            with self.subTest(service=service):
                uri = urlsplit(self.services[service]["environment"][key])
                self.assertEqual((uri.hostname, uri.port, uri.path), ("redis", 6379, "/0"))

    def test_the_worker_needs_no_mongodb(self):
        self.assertNotIn("mongo", self.services["async-worker-adk-workflows"]["depends_on"])

    def test_adk_workflow_runs_and_their_sessions_are_in_the_admin_mysql(self):
        worker = self.services["async-worker-adk-workflows"]
        admin = self.services["admin"]["environment"]
        database = urlsplit(worker["environment"]["HYBRID_ADK_WORKFLOWS__SESSION_DATABASE_URL"])
        self.assertEqual(database.scheme, "mysql+aiomysql")
        self.assertEqual((database.hostname, database.port), ("admin-mysql", 3306))
        self.assertEqual(database.username, admin["FORGE_ADMIN_MYSQL_USER"])
        self.assertEqual(database.password, admin["FORGE_ADMIN_MYSQL_PASSWORD"])
        self.assertEqual(database.path, "/" + admin["FORGE_ADMIN_MYSQL_DATABASE"])
        self.assertEqual(
            worker["depends_on"]["admin-mysql"]["condition"], "service_healthy"
        )
        self.assertIn("--queues=adk_workflows", worker["command"])

    def test_redis_persistence_and_no_eviction(self):
        redis = self.services["redis"]
        self.assertEqual(
            redis["command"],
            ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"],
        )
        self.assertEqual(redis["volumes"][0]["source"], "redis-data")
        self.assertEqual(redis["volumes"][0]["target"], "/data")

    def test_dependencies(self):
        for service in MONGO_CONNECTIONS:
            with self.subTest(service=service):
                self.assertEqual(
                    self.services[service]["depends_on"]["mongo"]["condition"],
                    "service_healthy",
                )
        for service in QUEUE_CONNECTIONS:
            with self.subTest(service=service):
                self.assertEqual(
                    self.services[service]["depends_on"]["redis"]["condition"],
                    "service_healthy",
                )
        for service in ("admin", "admin-seed"):
            with self.subTest(service=service):
                self.assertEqual(
                    self.services[service]["depends_on"]["admin-mysql"]["condition"],
                    "service_healthy",
                )
        self.assertEqual(self.services["admin-seed"]["profiles"], ["tools"])

    def test_only_loopback_ports(self):
        for name, service in self.services.items():
            for port in service.get("ports", []):
                with self.subTest(service=name, port=port["target"]):
                    self.assertEqual(port["host_ip"], "127.0.0.1")

    def test_no_two_services_publish_one_port(self):
        published = [
            (port["published"], name)
            for name, service in self.services.items()
            for port in service.get("ports", [])
        ]
        ports = [port for port, _ in published]
        self.assertEqual(len(ports), len(set(ports)), sorted(published))

    def test_volumes(self):
        self.assertEqual(
            set(self.configuration["volumes"]),
            {
                "mongo-data",
                "mongo-config",
                "redis-data",
                "admin-mysql-data",
                "knowledge-s3-data",
                "worker-scratch",
            },
        )

    def test_the_code_graph_worker_writes_to_the_emulator(self):
        worker = self.services["worker"]
        environment = worker["environment"]
        self.assertEqual(environment["SPANNER_EMULATOR_HOST"], "worker-spanner:9010")
        self.assertEqual(environment["CODEGRAPH_SPANNER_AUTO_PROVISION"], "true")
        self.assertIn("worker-spanner", worker["depends_on"])
        self.assertEqual(
            {port["published"] for port in self.services["worker-spanner"]["ports"]},
            {"19030", "19040"},
        )
        self.assertEqual(worker["ports"][0]["published"], "18090")

    def test_the_code_graph_worker_claims_its_jobs_from_the_admin_mysql(self):
        worker = self.services["worker"]
        admin = self.services["admin"]["environment"]
        # A go-sql-driver DSN: user:password@tcp(host:port)/database.
        dsn = worker["environment"]["CODEGRAPH_JOBS_MYSQL_DSN"]
        credentials, _, rest = dsn.partition("@tcp(")
        address, _, database = rest.partition(")/")
        self.assertEqual(
            credentials,
            f"{admin['FORGE_ADMIN_MYSQL_USER']}:{admin['FORGE_ADMIN_MYSQL_PASSWORD']}",
        )
        self.assertEqual(address, "admin-mysql:3306")
        self.assertEqual(database, admin["FORGE_ADMIN_MYSQL_DATABASE"])
        self.assertEqual(
            worker["depends_on"]["admin-mysql"]["condition"], "service_healthy"
        )

    def test_the_code_graph_worker_takes_shared_settings_from_env_common(self):
        environment = self.services["worker"]["environment"]
        # The container has no .env.common: the values its .env references
        # come from Compose, which reads .env.common.
        self.assertEqual(environment["FORGE_ENV_COMMON_FILE"], "")
        self.assertEqual(
            environment["CODEGRAPH_EMBEDDING_MODEL"], "text-embedding-3-large"
        )
        self.assertEqual(environment["CODEGRAPH_EMBEDDING_DIMENSIONS"], "1024")
        for setting in (
            "CODEGRAPH_ADMISSION_TOKEN",
            "CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY",
            "CODEGRAPH_GITHUB_TOKEN",
            "OPENAI_API_KEY",
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, environment)
        self.assertFalse(
            [key for key in environment if key.startswith("CODEGRAPH_GITHUB_TOKENS_")]
        )

    def test_every_app_embeds_with_the_shared_model(self):
        # Knowledge bases (written by the documents worker, searched by the
        # admin API and the workflows worker's knowledge tools) and the code
        # graph (written by its worker, searched by its MCP server) embed with
        # the one model and length in .env.common, or searches match nothing.
        shared = env_file(ROOT / ".env.common.example")
        # Each service's settings prefix, and its model setting's name.
        consumers = {
            "admin": ("FORGE_ADMIN_KNOWLEDGE_EMBEDDING__", "DOCUMENT_MODEL"),
            "async-worker-documents": ("HYBRID_EMBEDDING__", "DOCUMENT_MODEL"),
            "async-worker-adk-workflows": ("HYBRID_EMBEDDING__", "DOCUMENT_MODEL"),
            "worker": ("CODEGRAPH_EMBEDDING_", "MODEL"),
            "codegraph-mcp": ("CODEGRAPH_EMBEDDING__", "MODEL"),
        }
        for service, (prefix, model) in consumers.items():
            environment = self.services[service]["environment"]
            with self.subTest(service=service):
                self.assertEqual(environment[prefix + model], shared["FORGE_EMBEDDING_MODEL"])
                self.assertEqual(
                    environment[prefix + "DIMENSIONS"], shared["FORGE_EMBEDDING_DIMENSIONS"]
                )
                self.assertEqual(
                    environment[prefix + "BASE_URL"], shared["FORGE_EMBEDDING_BASE_URL"]
                )

    def test_the_code_graph_mcp_server_reads_the_workers_graph(self):
        mcp = self.services["codegraph-mcp"]
        environment = mcp["environment"]
        worker = self.services["worker"]["environment"]
        self.assertEqual(environment["SPANNER_EMULATOR_HOST"], "worker-spanner:9010")
        self.assertEqual(
            environment["CODEGRAPH_SPANNER__DATABASE"], worker["CODEGRAPH_SPANNER_DATABASE"]
        )
        self.assertEqual(environment["CODEGRAPH_SPANNER__SCOPE"], worker["CODEGRAPH_SCOPE"])
        # The worker creates the emulator's database before it is healthy.
        self.assertEqual(mcp["depends_on"]["worker"]["condition"], "service_healthy")
        # Questions are embedded as the worker embeds the graph.
        for mine, theirs in (
            ("CODEGRAPH_SPANNER__CURSOR_SIGNING_KEY", "CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY"),
            ("CODEGRAPH_EMBEDDING__MODEL", "CODEGRAPH_EMBEDDING_MODEL"),
            ("CODEGRAPH_EMBEDDING__DIMENSIONS", "CODEGRAPH_EMBEDDING_DIMENSIONS"),
            ("CODEGRAPH_EMBEDDING__API_KEY", "OPENAI_API_KEY"),
        ):
            with self.subTest(setting=mine):
                self.assertEqual(environment[mine], worker[theirs])
        self.assertEqual(environment["FORGE_ENV_COMMON_FILE"], "")
        self.assertEqual(mcp["ports"][0]["published"], "18203")

    def test_the_admin_offers_the_code_explorer_at_its_address_on_the_stack(self):
        # The code explorer the admin API offers ready-made is the code graph
        # MCP server, at its container port on the stack's network.
        url = self.services["admin"]["environment"]["FORGE_ADMIN_CODEGRAPH_MCP_URL"]
        port = self.services["codegraph-mcp"]["ports"][0]["target"]
        self.assertEqual(url, f"http://codegraph-mcp:{port}/mcp")

    def test_graph_knowledge_bases_are_searched_on_the_workers_api(self):
        # The admin API (graph knowledge bases, the code graph explorer) and
        # the workflows worker (their knowledge base tools) call the code
        # graph worker's API at its container port, with its token.
        worker = self.services["worker"]["environment"]
        port = worker["CODEGRAPH_HEALTH_ADDR"].rpartition(":")[2]
        callers = {
            "admin": "FORGE_ADMIN_CODEGRAPH",
            "async-worker-adk-workflows": "HYBRID_ADK_WORKFLOWS__CODEGRAPH",
        }
        for service, prefix in callers.items():
            environment = self.services[service]["environment"]
            with self.subTest(service=service):
                self.assertEqual(environment[f"{prefix}_URL"], f"http://worker:{port}")
                self.assertEqual(
                    environment[f"{prefix}_TOKEN"], worker["CODEGRAPH_ADMISSION_TOKEN"]
                )

    def test_the_code_graph_mcp_server_has_the_admin_check_its_callers(self):
        # Each caller's credential goes to the admin API on the stack's
        # network, at its prefix and container port.
        admin = self.services["admin"]
        url = self.services["codegraph-mcp"]["environment"]["CODEGRAPH_MCP__AUTH__ADMIN_URL"]
        port = admin["environment"]["FORGE_ADMIN_PORT"]
        self.assertEqual(url, f"http://admin:{port}/api/v1")
        self.assertEqual(admin["ports"][0]["target"], int(port))

    def test_cloud_spanner_overlay_moves_only_the_worker_and_its_reader(self):
        database = "projects/p/instances/i/databases/codegraph"
        cloud = render(
            "compose.cloud.yaml",
            environment={"WORKER_CLOUD_SPANNER_DATABASE": database},
        )["services"]
        environment = cloud["worker"]["environment"]
        self.assertEqual(environment["SPANNER_EMULATOR_HOST"], "")
        self.assertEqual(environment["CODEGRAPH_SPANNER_DATABASE"], database)
        self.assertEqual(environment["CODEGRAPH_SPANNER_AUTO_PROVISION"], "false")
        self.assertNotIn("worker-spanner", cloud["worker"].get("depends_on", {}))
        mcp = cloud["codegraph-mcp"]["environment"]
        self.assertEqual(mcp["SPANNER_EMULATOR_HOST"], "")
        self.assertEqual(mcp["CODEGRAPH_SPANNER__DATABASE"], database)
        self.assertEqual(
            mcp["GOOGLE_APPLICATION_CREDENTIALS"],
            environment["GOOGLE_APPLICATION_CREDENTIALS"],
        )
        for name, service in self.services.items():
            if name not in ("worker", "codegraph-mcp"):
                with self.subTest(service=name):
                    self.assertEqual(cloud[name], service)


if __name__ == "__main__":
    unittest.main()
