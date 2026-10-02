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
    "mongo",
    "redis",
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


def render(*overlays):
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
    environment = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
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

    def test_only_the_adk_workflow_services(self):
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

    def test_only_shared_and_admin_volumes(self):
        self.assertEqual(
            set(self.configuration["volumes"]),
            {"mongo-data", "mongo-config", "redis-data", "admin-mysql-data"},
        )


if __name__ == "__main__":
    unittest.main()
