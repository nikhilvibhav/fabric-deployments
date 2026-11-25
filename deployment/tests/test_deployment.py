"""Tests for configuration loading, artifact handling, remote operations and orchestration."""

import hashlib
import io
import json
import logging
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

from deployment import __main__ as cli
from deployment.artifacts import Repository
from deployment.config import (
    Host,
    Settings,
    boolean,
    config_files,
    environment,
    render,
    select_hosts,
    variables,
)
from deployment.remote import Remote


class ConfigurationTests(unittest.TestCase):
    """Exercise inventory loading, deployment variables, and template rendering."""

    def test_all_existing_inventories_and_role_selection(self):
        """Check repository inventories and role-based host selection."""
        counts = {"sample-qa": 2, "sample-prod": 3}
        for stage, count in counts.items():
            with self.subTest(stage=stage):
                settings, hosts = environment(stage)
                self.assertEqual(len(hosts), count)
                self.assertEqual(settings["use_supervisor"], stage == "sample-qa")
        _, hosts = environment("sample-prod")
        self.assertEqual(
            select_hosts(hosts, "app1,app2"),
            ["deploy@198.51.100.10", "deploy@198.51.100.11"],
        )
        with self.assertRaisesRegex(ValueError, "Unknown roles"):
            select_hosts(hosts, "app9")

    def test_repository_comes_from_inventory_unless_overridden(self):
        """Build the snapshot/release repository from inventory settings."""
        settings: Settings = {
            "keep_releases": 1,
            "use_supervisor": False,
            "health_timeout": 60,
            "artifactory_url": "https://artifactory.example/artifactory/",
            "artifact_group": "com/example",
        }
        self.assertEqual(
            cli.repository_base(None, settings, "snapshot"),
            "https://artifactory.example/artifactory/libs-snapshot-local/com/example",
        )
        self.assertEqual(
            cli.repository_base(None, settings, "stable"),
            "https://artifactory.example/artifactory/libs-release-local/com/example",
        )
        self.assertEqual(
            cli.repository_base("https://other/repo", settings, "stable"),
            "https://other/repo",
        )
        del settings["artifact_group"]
        self.assertEqual(
            cli.repository_base(None, settings, "stable"),
            "https://artifactory.example/artifactory/libs-release-local",
        )
        self.assertIsNone(
            cli.repository_base(
                None,
                {"keep_releases": 1, "use_supervisor": False, "health_timeout": 60},
                "stable",
            )
        )

    def test_inventory_health_timeout_defaults_and_bounds(self):
        """Default health_timeout to 60 s and reject values outside 5-1800."""
        settings, _ = environment("sample-qa")
        self.assertEqual(settings["health_timeout"], 60)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "deployment" / "inventories" / "stage.json"
            path.parent.mkdir(parents=True)
            for timeout in (4, 1801, "60", True):
                with self.subTest(timeout=timeout):
                    path.write_text(
                        json.dumps(
                            {
                                "settings": {"health_timeout": timeout},
                                "hosts": [{"address": "h", "roles": ["app1"]}],
                            }
                        )
                    )
                    with self.assertRaises(ValueError):
                        environment("stage", root)

    def test_inventory_artifact_group_must_be_a_relative_path(self):
        """Reject an artifact group that could escape the repository path."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "deployment" / "inventories" / "stage.json"
            path.parent.mkdir(parents=True)
            for group, valid in (("com/example", True), ("../etc", False), (7, False)):
                with self.subTest(group=group):
                    path.write_text(
                        json.dumps(
                            {
                                "settings": {"artifact_group": group},
                                "hosts": [{"address": "h", "roles": ["app1"]}],
                            }
                        )
                    )
                    if valid:
                        environment("stage", root)
                    else:
                        with self.assertRaises(ValueError):
                            environment("stage", root)

    def test_jenkins_aliases_nested_application_and_second_artifact(self):
        """Verify legacy environment aliases and versioned application paths."""
        values = variables(
            "qa",
            "oba/oba-api",
            "1.2.3",
            True,
            {"keep_releases": 1, "use_supervisor": False, "health_timeout": 60},
            environ={
                "java_opts": "'-Xmx256m -Xms128m'",
                "jdbc_database_password": "secret",
            },
        )
        self.assertEqual(values["application_name"], "oba-api")
        self.assertEqual(values["application_execute"], "oba-api-1.2.3")
        self.assertEqual(values["deploy_to"], "/opt/camel/oba/oba-api-1.2.3")
        self.assertEqual(values["jvm_options"], "-Xmx256m -Xms128m")
        self.assertEqual(values["jdbc_database_password"], "secret")
        with self.assertRaisesRegex(ValueError, "explicit version"):
            variables(
                "qa",
                "app",
                "latest",
                True,
                {"keep_releases": 1, "use_supervisor": False, "health_timeout": 60},
                environ={},
            )

    SETTINGS: ClassVar[Settings] = {
        "keep_releases": 1,
        "use_supervisor": False,
        "health_timeout": 60,
        "gelf_host": "logs.inventory.example",
    }

    def test_vars_file_supplies_scalars_and_rejects_nested_values(self):
        """Apply scalar values from a vars file and reject anything else."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vars.json"
            path.write_text(json.dumps({"server_port": 9090, "custom": "x"}))
            values = variables(
                "qa", "app", "1", False, self.SETTINGS, str(path), environ={}
            )
            self.assertEqual((values["server_port"], values["custom"]), (9090, "x"))
            for document in ([1, 2], {"nested": {"a": 1}}):
                with self.subTest(document=document):
                    path.write_text(json.dumps(document))
                    with self.assertRaisesRegex(ValueError, "JSON object of scalar"):
                        variables(
                            "qa",
                            "app",
                            "1",
                            False,
                            self.SETTINGS,
                            str(path),
                            environ={},
                        )

    def test_environment_overrides_inventory_and_defaults(self):
        """Prefer environment values; java_opts applies only without jvm_options."""
        values = variables(
            "qa",
            "app",
            "1",
            False,
            self.SETTINGS,
            environ={
                "gelf_host": "logs.env.example",
                "server_port": "9090",
                "java_opts": '"-Xmx1g"',
            },
        )
        self.assertEqual(values["gelf_host"], "logs.env.example")
        self.assertEqual(values["server_port"], "9090")
        self.assertEqual(values["jvm_options"], "-Xmx1g")
        values = variables(
            "qa",
            "app",
            "1",
            False,
            self.SETTINGS,
            environ={"java_opts": "-Xmx1g", "jvm_options": "-Xmx2g"},
        )
        self.assertEqual(values["jvm_options"], "-Xmx2g")
        self.assertEqual(
            variables("qa", "app", "1", False, self.SETTINGS, environ={})["gelf_host"],
            "logs.inventory.example",
        )

    def test_invalid_versions_paths_and_jvm_options_are_rejected(self):
        """Reject unsafe versions, Java/log paths and multi-line JVM options."""
        cases = [
            ({}, "1/2", "Invalid version"),
            ({}, "..", "Invalid version"),
            ({"java_bin": "bin/java"}, "1", "java_bin must be an absolute path"),
            ({"java_bin": "/opt/../java"}, "1", "java_bin must be an absolute path"),
            ({"camel_log_dir": "/var/log x"}, "1", "camel_log_dir must be an absolute"),
            ({"jvm_options": "-Xmx1g\n-Xms1g"}, "1", "line breaks"),
        ]
        for environ, version, message in cases:
            with (
                self.subTest(environ=environ, version=version),
                self.assertRaisesRegex(ValueError, message),
            ):
                variables("qa", "app", version, False, self.SETTINGS, environ=environ)

    def test_unsafe_parameters_and_boolean_typos_fail(self):
        """Reject unsafe paths and malformed boolean values."""
        for application in (
            "../app",
            "/app",
            "app;whoami",
            "app//nested",
            "app\\nested",
        ):
            with self.subTest(application=application), self.assertRaises(ValueError):
                variables(
                    "qa",
                    application,
                    "1",
                    False,
                    {"keep_releases": 1, "use_supervisor": False, "health_timeout": 60},
                    environ={},
                )
        with self.assertRaises(ValueError):
            boolean("flase")

    def test_render_is_literal_and_missing_secrets_are_not_defaulted(self):
        """Ensure replacement is single-pass and missing variables remain errors."""
        self.assertEqual(
            render("db=[[password]]", {"password": "x[[evil]]"}), "db=x[[evil]]"
        )
        with self.assertRaisesRegex(ValueError, "Missing template variable: password"):
            render("[[password]]", {})

    def test_sample_configuration_and_shared_fallback(self):
        """Render sample apps: one with its own properties, one using the fallback."""
        settings, _ = environment("sample-qa")
        values = variables(
            "sample-qa",
            "hello-service",
            "1",
            False,
            settings,
            environ={},
            secret_values={"api_token": "not-a-t0ken"},
        )
        files = config_files("sample-qa", "hello-service", values)
        self.assertIn(b"server.port=8080", files["application.properties"])
        self.assertIn(
            b"greeting.api.token=not-a-t0ken", files["application.properties"]
        )
        self.assertIn(
            b"<graylogHost>logs.sample-qa.example</graylogHost>", files["logback.xml"]
        )
        self.assertIn("logback-access.xml", files)
        self.assertNotIn(b"[[", b"".join(files.values()))
        values = variables(
            "sample-qa",
            "orders-service",
            "1",
            False,
            settings,
            environ={},
            secret_values={"jdbc_database_password": "not-a-p@ssw0rd"},
        )
        files = config_files(
            "sample-qa", "orders-service", dict(values, release_path="/r")
        )
        self.assertIn(
            b"spring.datasource.password=not-a-p@ssw0rd",
            files["application.properties"],
        )
        self.assertIn(b"orders.queue.name=orders-sample-qa", files["orders.properties"])
        self.assertNotIn(b"[[", b"".join(files.values()))

    def test_flattened_config_collisions_fail(self):
        """Reject configuration files that collide after directory flattening."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "files" / "qa" / "app"
            (source / "template").mkdir(parents=True)
            (source / "same.properties").write_text("a")
            (source / "template" / "same.properties").write_text("b")
            with self.assertRaisesRegex(ValueError, "collision"):
                config_files("qa", "app", {}, root)


class ArtifactTests(unittest.TestCase):
    """Exercise artifact resolution, transport, and checksum validation."""

    def test_real_http_artifact_resolution_and_verified_download(self):
        """Resolve and download a local test artifact while verifying its digest."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "app" / "1.0"
            version.mkdir(parents=True)
            content = b"test artifact content"
            (version / "app-1.0.jar").write_bytes(content)
            (version / "app-1.0-sources.jar").write_bytes(b"sources")

            class Handler(SimpleHTTPRequestHandler):
                """Serve the temporary repository without emitting request logs."""

                def log_message(self, *args):
                    """Suppress routine HTTP server request logging."""

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), partial(Handler, directory=directory)
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with patch.dict(
                    "os.environ",
                    {
                        "NO_PROXY": "127.0.0.1",
                        "ARTIFACTORY_TOKEN": "",
                        "ARTIFACTORY_USERNAME": "",
                    },
                ):
                    repository = Repository()
                    url = repository.resolve(base, "app", "1.0", ".jar")
                    destination = root / "download.jar"
                    expected = hashlib.sha256(content).hexdigest()
                    actual = repository.download(url, destination, expected)
                self.assertEqual(actual, expected)
                self.assertEqual(destination.read_bytes(), content)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

    def test_snapshot_resolution_ignores_classifiers_and_external_links(self):
        """Select the snapshot artifact while ignoring classifiers and unsafe links."""
        repo = Repository()
        repo.open = MagicMock(
            return_value=io.BytesIO(b"""
          <a href="app-1-SNAPSHOT.jar">app</a>
          <a href="app-1-SNAPSHOT-sources.jar">sources</a>
          <a href="https://elsewhere/app.jar">external</a>
          <a href="../other.jar">parent</a>
        """)
        )
        self.assertEqual(
            repo.resolve("https://repo/base", "app", "1-SNAPSHOT", ".jar"),
            "https://repo/base/app/1-SNAPSHOT/app-1-SNAPSHOT.jar",
        )

    def test_latest_uses_maven_metadata_and_existing_version_path_is_not_duplicated(
        self,
    ):
        """Resolve latest via Maven metadata and avoid repeating a version path."""
        repo = Repository()
        repo.open = MagicMock(
            side_effect=[
                io.BytesIO(
                    b"<metadata><versioning><release>2.0</release></versioning></metadata>"
                ),
                io.BytesIO(b'<a href="app-2.0.jar">app</a>'),
            ]
        )
        self.assertEqual(
            repo.resolve("https://repo/base", "app", "latest", ".jar"),
            "https://repo/base/app/2.0/app-2.0.jar",
        )
        repo.open = MagicMock(return_value=io.BytesIO(b'<a href="app.jar">app</a>'))
        repo.resolve("https://repo/base", "app/2.0", "2.0", ".jar")
        repo.open.assert_called_once_with("https://repo/base/app/2.0/")

    def test_ambiguous_artifacts_fail_instead_of_guessing(self):
        """Require a unique artifact rather than guessing between candidates."""
        repo = Repository()
        repo.open = MagicMock(
            return_value=io.BytesIO(b'<a href="a.jar">a</a><a href="b.jar">b</a>')
        )
        with self.assertRaisesRegex(ValueError, "found 2"):
            repo.resolve("https://repo", "app", "1", ".jar")

    def test_username_without_password_is_a_clear_error(self):
        """Name the missing variable instead of failing with a KeyError."""
        environ = {"ARTIFACTORY_USERNAME": "deploy", "ARTIFACTORY_PASSWORD": ""}
        with (
            patch.dict("os.environ", environ),
            self.assertRaisesRegex(ValueError, "ARTIFACTORY_PASSWORD is not"),
        ):
            Repository()

    def test_download_checksum(self):
        """Reject a mismatched download, naming both digests so the cause is clear."""
        repo = Repository()
        repo.open = MagicMock(return_value=io.BytesIO(b"artifact"))
        actual = hashlib.sha256(b"artifact").hexdigest()
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(
                ValueError, f"SHA-256 mismatch: expected bad, downloaded {actual}"
            ),
        ):
            repo.download("https://repo/app.jar", Path(directory) / "app.jar", "BAD")

    def test_artifacts_are_resolved_as_jars(self):
        """Build a bundle from the resolved JAR and preserve its URL and digest."""
        with tempfile.TemporaryDirectory() as directory:
            args = cli.parser().parse_args(
                [
                    "deploy",
                    "--stage",
                    "sample-qa",
                    "--roles",
                    "app1",
                    "--application",
                    "service",
                    "--version",
                    "1.0.0",
                    "--repository",
                    "https://repo/libs-release-local/com/example",
                ]
            )
            repo = Repository()
            repo.resolve = MagicMock(return_value="https://repo/service.jar")

            def download(url, destination, checksum=None):
                """Write fake artifact bytes and return a deterministic digest."""
                destination.write_bytes(b"jar")
                return "digest"

            repo.download = MagicMock(side_effect=download)
            with patch("deployment.__main__.Repository", return_value=repo):
                bundle, state = cli.prepare_bundle(
                    args,
                    {"application": "service", "application_name": "service"},
                    Path(directory),
                )
            repo.resolve.assert_called_once_with(
                "https://repo/libs-release-local/com/example",
                "service",
                "1.0.0",
                ".jar",
            )
            repo.download.assert_called_once()
            self.assertTrue((bundle / "service.jar").is_file())
            self.assertEqual(
                state,
                {
                    "artifact_url": "https://repo/service.jar",
                    "sha256": "digest",
                },
            )

    def test_non_jar_artifact_urls_are_rejected(self):
        """Reject artifact URLs that do not identify JAR files."""
        args = cli.parser().parse_args(
            [
                "deploy",
                "--stage",
                "sample-qa",
                "--roles",
                "app1",
                "--application",
                "service",
                "--version",
                "1.0.0",
                "--artifact-url",
                "https://repo/service.zip",
            ]
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "must point to a JAR"),
        ):
            cli.prepare_bundle(
                args,
                {"application": "service", "application_name": "service"},
                Path(directory),
            )


class RemoteTests(unittest.TestCase):
    """Verify remote release preparation, activation, restoration, and health checks."""

    def setUp(self):
        """Create a mocked remote with representative QA deployment settings."""
        settings, _ = environment("sample-qa")
        self.values = variables(
            "sample-qa", "hello-service", "1", False, settings, environ={}
        )
        # Any: these tests replace Remote methods with mocks.
        self.remote: Any = Remote("test@example.invalid", self.values, True)
        self.remote.run = MagicMock(return_value=SimpleNamespace(ok=True, stdout=""))
        self.remote.put = MagicMock()
        self.remote.exists = MagicMock(return_value=False)
        self.remote.current = MagicMock(return_value=None)
        self.release = self.values["deploy_to"] + "/releases/20260101000000-abc"

    def test_prepare_records_state_before_activation_and_renders_service(self):
        """Stage a release and record rollback state before activation."""
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "config").mkdir()
            (bundle / "config" / "app.properties").write_text("secret=value")
            self.remote.connection.put = MagicMock()
            state = self.remote.prepare(bundle, self.release, {}, {})
        self.assertIsNone(state["previous"])
        texts = [call.args[0] for call in self.remote.put.call_args_list]
        service = next(text for text in texts if text.startswith(b"[program:"))
        self.assertIn(self.release.encode(), service)
        self.assertNotIn(b"<%=", service)
        self.assertFalse(
            any(
                "supervisorctl" in call.args[0]
                for call in self.remote.run.call_args_list
            )
        )

    def test_activate_switches_then_restarts_and_checks_health(self):
        """Switch to the release before restarting and checking the service."""
        self.remote.load_state = MagicMock(return_value={"shared_existed": {}})
        self.remote.switch = MagicMock()
        self.remote.lifecycle = MagicMock()
        self.remote.healthy = MagicMock()
        self.remote.activate(self.release)
        self.remote.switch.assert_called_once_with(self.release)
        self.remote.lifecycle.assert_called_once_with("restart")
        self.remote.healthy.assert_called_once()

    def test_restore_reinstalls_previous_service_and_release(self):
        """Restore the prior service file and release after a failed deployment."""
        previous = self.values["deploy_to"] + "/releases/previous"
        self.remote.switch = MagicMock()
        self.remote.lifecycle = MagicMock()
        self.remote.healthy = MagicMock()
        self.remote.restore(
            self.release,
            {
                "previous": previous,
                "service_existed": True,
                "shared_existed": {},
            },
        )
        self.remote.switch.assert_called_once_with(previous)
        self.remote.lifecycle.assert_called_once_with("restart")
        self.assertTrue(
            any(
                "service.previous" in call.args[0]
                for call in self.remote.run.call_args_list
            )
        )

    def test_shared_content_is_backed_up_and_restored(self):
        """Back up existing shared content and restore it during rollback."""
        self.remote.exists.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            state = self.remote.prepare(
                Path(directory), self.release, {}, {"settings": b"real contents"}
            )
        self.assertTrue(state["shared_existed"]["settings"])
        self.remote.put.assert_any_call(
            b"real contents", self.release + "/.deployment/shared.next.settings"
        )
        self.remote.lifecycle = MagicMock()
        self.remote.healthy = MagicMock()
        self.remote.restore(self.release, state)
        self.assertTrue(
            any(
                "shared.previous.settings" in call.args[0]
                for call in self.remote.run.call_args_list
            )
        )

    def test_first_deployment_restore_removes_new_service_and_current(self):
        """Remove service and current-link state when rolling back the first release."""
        self.remote.exists.return_value = True
        self.remote.lifecycle = MagicMock()
        self.remote.restore(
            self.release,
            {
                "previous": None,
                "service_existed": False,
                "shared_existed": {},
            },
        )
        self.remote.lifecycle.assert_called_once_with("stop")
        commands = [call.args[0] for call in self.remote.run.call_args_list]
        self.assertIn("rm -f " + self.values["deploy_to"] + "/current", commands)

    def test_http_readiness_checks_successful_status_not_just_curl_exit(self):
        """Require a successful HTTP status code in addition to curl success."""
        self.remote.health_url = "http://localhost:8080/health"
        self.remote.run.side_effect = [
            SimpleNamespace(ok=True, stdout="app RUNNING"),
            SimpleNamespace(ok=True, stdout=""),
        ]
        self.remote.healthy()
        command = self.remote.run.call_args.args[0]
        self.assertIn('case "$code" in 2??)', command)
        self.assertIn("--max-time 5", command)

    def test_health_failure_is_not_reported_as_success(self):
        """Fail after the bounded readiness retry window is exhausted."""
        self.remote.run.return_value = SimpleNamespace(ok=False, stdout="app FATAL")
        with (
            patch("deployment.remote.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "healthy"),
        ):
            self.remote.healthy()
        self.assertEqual(self.remote.run.call_count, 12)

    def test_health_timeout_sets_the_number_of_checks(self):
        """Check every 5 s until the stage's health_timeout, rounding up."""
        remote = Remote("test@example.invalid", self.values, True, health_timeout=12)
        remote.run = MagicMock(
            return_value=SimpleNamespace(ok=False, stdout="app FATAL")
        )
        with (
            patch("deployment.remote.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "healthy"),
        ):
            remote.healthy()
        self.assertEqual(remote.run.call_count, 3)

    def test_restart_and_health_progress_are_logged(self):
        """Log the restart, each failed readiness attempt, and the healthy result."""
        host = self.remote.connection.host
        self.remote.run.side_effect = [
            SimpleNamespace(ok=True, stdout="app RUNNING"),
            SimpleNamespace(ok=True, stdout=""),
            SimpleNamespace(ok=False, stdout="app STARTING"),
            SimpleNamespace(ok=True, stdout="app RUNNING"),
        ]
        with (
            patch("deployment.remote.time.sleep"),
            self.assertLogs("camel-deploy") as logs,
        ):
            self.remote.lifecycle("restart")
            self.remote.healthy()
        service = self.remote.service
        self.assertEqual(
            [line.removeprefix("INFO:camel-deploy:") for line in logs.output],
            [
                f"Running restart of {service} on {host}",
                f"Checking health of {service} on {host} (process only; up to 60s)",
                f"{service} not healthy yet on {host} (attempt 1/12); retrying in 5s",
                f"{service} is healthy on {host}",
            ],
        )

    def test_paths_outside_releases_cannot_be_pruned_or_rolled_back(self):
        """Reject release paths outside the application's direct release directory."""
        for path in (
            "/etc",
            self.values["deploy_to"] + "/releases/../shared",
            self.values["deploy_to"] + "/releases/a/b",
        ):
            with self.assertRaises(ValueError):
                self.remote.validate_release(path)

    def test_stop_supervisor_is_idempotent_only_for_known_stopped_states(self):
        """Skip stop only for known stopped states and issue it for running services."""
        self.remote.run.return_value = SimpleNamespace(ok=False, stdout="app STOPPED")
        self.remote.lifecycle("stop")
        self.assertEqual(self.remote.run.call_count, 1)
        self.remote.run.reset_mock()
        self.remote.run.return_value = SimpleNamespace(ok=True, stdout="app RUNNING")
        self.remote.lifecycle("stop")
        self.assertEqual(self.remote.run.call_count, 2)


class OrchestrationTests(unittest.TestCase):
    """Verify command planning and multi-host deployment failure handling."""

    def args(self, action="deploy"):
        """Build standard QA CLI arguments for an orchestration action."""
        return cli.parser().parse_args(
            [
                action,
                "--stage",
                "sample-qa",
                "--roles",
                "app1,app2",
                "--application",
                "hello-service",
                "--version",
                "1",
            ]
        )

    def deploy_with_fake_bundle(self, args, *remotes):
        """Run a deployment with mocked hosts and a fake downloaded artifact."""
        with (
            patch("deployment.remote.Remote", side_effect=list(remotes)),
            patch.object(cli, "prepare_bundle") as prepare,
            patch.object(cli, "decrypt_all", return_value={"api_token": "test-value"}),
        ):

            def bundle(args, values, workspace):
                """Create a minimal fake bundle for the orchestration test."""
                path = workspace / "bundle"
                path.mkdir()
                return path, {"artifact_url": "http://repo/app.jar", "sha256": "ab12"}

            prepare.side_effect = bundle
            cli.execute(args)

    def test_stable_deployment_logs_digest_to_pin_later_stages(self):
        """Log the published digest and how to pin it for a stable build."""
        with self.assertLogs("camel-deploy") as logs:
            self.deploy_with_fake_bundle(self.args(), MagicMock(), MagicMock())
        self.assertIn(
            "INFO:camel-deploy:Published artifact http://repo/app.jar with SHA-256 ab12",
            logs.output,
        )
        self.assertIn(
            "INFO:camel-deploy:Pin this build in later stages with: --sha256 ab12",
            logs.output,
        )

    def test_snapshot_deployment_logs_digest_without_pin_hint(self):
        """Skip the pinning hint for snapshots, which are rebuilt under one version."""
        args = self.args()
        args.version = "1.0.0-SNAPSHOT"
        with self.assertLogs("camel-deploy") as logs:
            self.deploy_with_fake_bundle(args, MagicMock(), MagicMock())
        self.assertTrue(any("SHA-256 ab12" in line for line in logs.output))
        self.assertFalse(any("--sha256" in line for line in logs.output))

    def test_log_format_colours_only_the_level_name(self):
        """Colour the level prefix when enabled and leave the message untouched."""
        record = logging.LogRecord(
            "camel-deploy",
            logging.ERROR,
            __file__,
            1,
            "Pin with: --sha256 %s",
            ("a" * 64,),
            None,
        )
        message = "Pin with: --sha256 " + "a" * 64
        self.assertEqual(
            cli.LevelColourFormatter(colour=False).format(record), "ERROR: " + message
        )
        self.assertEqual(
            cli.LevelColourFormatter(colour=True).format(record),
            "\033[31mERROR\033[0m: " + message,
        )

    def test_colour_follows_terminal_and_environment_overrides(self):
        """Use colour on terminals, honour FORCE_COLOR and NO_COLOR, skip dumb ones."""
        terminal, pipe = MagicMock(), MagicMock()
        terminal.isatty.return_value = True
        pipe.isatty.return_value = False
        self.assertTrue(cli.use_colour(terminal, {}))
        self.assertFalse(cli.use_colour(terminal, {"TERM": "dumb"}))
        self.assertFalse(cli.use_colour(terminal, {"NO_COLOR": "1"}))
        self.assertFalse(cli.use_colour(pipe, {}))
        self.assertFalse(cli.use_colour(pipe, {"FORCE_COLOR": "0"}))
        self.assertTrue(cli.use_colour(pipe, {"FORCE_COLOR": "1"}))
        self.assertFalse(cli.use_colour(pipe, {"FORCE_COLOR": "1", "NO_COLOR": "1"}))

    def test_offline_plan_never_opens_ssh_and_does_not_print_variables(self):
        """Keep planning offline and omit the full deployment variable set."""
        with (
            patch("deployment.remote.Remote") as remote,
            redirect_stdout(io.StringIO()) as output,
        ):
            plan_args = self.args("plan")
            cli.execute(plan_args)
        remote.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(len(plan["hosts"]), 2)
        self.assertEqual(plan["release_type"], "stable")
        self.assertEqual(
            plan["artifact"],
            "https://artifactory.example/artifactory/libs-release-local/com/example",
        )
        self.assertNotIn("variables", plan)

    def test_plan_fails_without_an_artifact_repository(self):
        """Fail before connecting when no inventory, option or URL names a repository."""
        settings = {"keep_releases": 1, "use_supervisor": False, "health_timeout": 60}
        hosts = [Host("deploy@192.0.2.10", ("app1", "app2"))]
        with (
            patch.object(cli, "environment", return_value=(settings, hosts)),
            patch.dict("os.environ", {"artifactory_base_url": "", "artifact_url": ""}),
            patch("deployment.remote.Remote") as remote,
            self.assertRaisesRegex(ValueError, "set artifactory_url in"),
        ):
            cli.execute(self.args("plan"))
        remote.assert_not_called()

    def test_plain_http_artifactory_needs_the_stage_setting(self):
        """Refuse http:// artifact sources unless the inventory allows them."""
        settings: Settings = {
            "keep_releases": 1,
            "use_supervisor": False,
            "health_timeout": 60,
            "artifactory_url": "http://artifactory.internal.example:8081/artifactory",
            "gelf_host": "logs.example",
        }
        hosts = [Host("deploy@192.0.2.10", ("app1", "app2"))]
        with (
            patch.object(cli, "environment", return_value=(settings, hosts)),
            patch.dict("os.environ", {"artifactory_base_url": "", "artifact_url": ""}),
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaisesRegex(ValueError, "artifactory_allow_http"):
                cli.execute(self.args("plan"))
            settings["artifactory_allow_http"] = True
            cli.execute(self.args("plan"))
        self.assertTrue(json.loads(output.getvalue())["artifact"].startswith("http://"))

    def test_plain_http_artifact_url_is_refused_and_allowed_deploy_warns(self):
        """Apply the stage setting to --artifact-url and warn when deploying."""
        args = self.args()
        args.artifact_url = "http://artifactory.internal.example/app-1.jar"
        settings: Settings = {
            "keep_releases": 1,
            "use_supervisor": False,
            "health_timeout": 60,
        }
        with self.assertRaisesRegex(ValueError, "plain HTTP"):
            cli.check_artifact_scheme(args, settings)
        settings["artifactory_allow_http"] = True
        with self.assertLogs("camel-deploy", "WARNING") as logs:
            cli.check_artifact_scheme(args, settings)
        self.assertIn("Downloading over plain HTTP", logs.output[0])

    def test_inventory_allow_http_must_be_boolean(self):
        """Reject a non-boolean artifactory_allow_http setting."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "deployment" / "inventories" / "stage.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "settings": {"artifactory_allow_http": "yes"},
                        "hosts": [{"address": "h", "roles": ["app1"]}],
                    }
                )
            )
            with self.assertRaises(ValueError):
                environment("stage", root)

    def test_release_type_is_inferred_from_snapshot_suffix(self):
        """Infer the snapshot release channel from the version suffix."""
        args = self.args("plan")
        args.version = "1.0.0-SNAPSHOT"
        with redirect_stdout(io.StringIO()) as output:
            cli.execute(args)
        self.assertEqual(json.loads(output.getvalue())["release_type"], "snapshot")

    def test_release_type_override_cannot_conflict_with_version(self):
        """Reject an explicit release channel that conflicts with the version."""
        args = self.args("plan")
        args.release_type = "snapshot"
        with self.assertRaisesRegex(ValueError, "is stable"):
            cli.execute(args)

    def test_host_failure_restores_failed_and_previously_activated_hosts(self):
        """Roll back every activated host when a later host fails activation."""
        first, second = MagicMock(), MagicMock()
        second.activate.side_effect = RuntimeError("startup failed")
        events = []
        for number, remote in enumerate((first, second)):
            remote.connection.host = f"host-{number}"
            remote.restore.side_effect = lambda *args, number=number: events.append(
                number
            )
        with (
            patch("deployment.remote.Remote", side_effect=[first, second]),
            patch.object(cli, "prepare_bundle") as prepare,
            patch.object(cli, "decrypt_all", return_value={"api_token": "test-value"}),
        ):

            def bundle(args, values, workspace):
                """Create a minimal fake bundle for the orchestration test."""
                path = workspace / "bundle"
                path.mkdir()
                return path, {}

            prepare.side_effect = bundle
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                cli.execute(self.args())
        self.assertEqual(events, [1, 0])
        first.close.assert_called_once()
        second.close.assert_called_once()
        first.cleanup.assert_not_called()

    def test_lock_failure_publishes_nothing_and_releases_acquired_locks(self):
        """Avoid publishing on lock failure and release earlier acquired locks."""
        first, second = MagicMock(), MagicMock()
        second.lock.side_effect = RuntimeError("locked")
        with (
            patch("deployment.remote.Remote", side_effect=[first, second]),
            patch.object(cli, "prepare_bundle") as prepare,
            patch.object(cli, "decrypt_all", return_value={"api_token": "test-value"}),
            self.assertRaisesRegex(RuntimeError, "locked"),
        ):
            cli.execute(self.args())
        prepare.assert_not_called()
        first.activate.assert_not_called()
        first.close.assert_called_once()

    def test_configuration_is_rendered_once_for_all_hosts(self):
        """Render configuration once and upload the same bundle to every host."""
        first, second = MagicMock(), MagicMock()
        with patch.object(cli, "config_files", wraps=cli.config_files) as render:
            self.deploy_with_fake_bundle(self.args(), first, second)
        # One call validates templates before connecting; one renders the release.
        self.assertEqual(render.call_count, 2)
        self.assertEqual(
            first.prepare.call_args.args[0], second.prepare.call_args.args[0]
        )

    def test_rollback_checks_every_host_before_restoring_any(self):
        """Restore nothing if any selected host has no previous release."""
        first, second = MagicMock(), MagicMock()
        for remote, previous in ((first, "/opt/r/1"), (second, None)):
            remote.current.return_value = "/opt/r/2"
            remote.load_state.return_value = {"previous": previous}
        with (
            patch("deployment.remote.Remote", side_effect=[first, second]),
            patch.object(cli, "decrypt_all", return_value={"api_token": "x"}),
            self.assertRaisesRegex(ValueError, "No previous release"),
        ):
            cli.execute(self.args("rollback"))
        first.restore.assert_not_called()
        second.restore.assert_not_called()

    def three_hosts(self, action="deploy"):
        """Return sample-prod arguments for three hosts and their recorded remotes."""
        args = self.args(action)
        args.stage, args.roles = "sample-prod", "app1,app2,app3"
        timeline = MagicMock()
        remotes = []
        for number in range(3):
            remote = MagicMock()
            remote.connection.host = f"host{number}"
            timeline.attach_mock(remote, f"host{number}")
            remotes.append(remote)
        return args, remotes, timeline

    @staticmethod
    def events(timeline, *methods):
        """List 'hostN.method' for recorded calls to the given methods, in order."""
        return [
            name
            for name, _, _ in timeline.mock_calls
            if name.rpartition(".")[2] in methods
        ]

    def test_deploy_locks_every_host_before_changing_any_then_unlocks_in_reverse(self):
        """Lock all hosts, publish host by host, prune, then close in reverse."""
        args, remotes, timeline = self.three_hosts()
        self.deploy_with_fake_bundle(args, *remotes)
        self.assertEqual(
            self.events(timeline, "lock", "prepare", "activate", "cleanup", "close"),
            [
                "host0.lock",
                "host1.lock",
                "host2.lock",
                "host0.prepare",
                "host0.activate",
                "host1.prepare",
                "host1.activate",
                "host2.prepare",
                "host2.activate",
                "host0.cleanup",
                "host1.cleanup",
                "host2.cleanup",
                "host2.close",
                "host1.close",
                "host0.close",
            ],
        )

    def test_failed_activation_restores_in_reverse_before_unlocking(self):
        """Restore the failed and earlier hosts newest first, then release locks."""
        args, remotes, timeline = self.three_hosts()
        remotes[2].activate.side_effect = RuntimeError("startup failed")
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.deploy_with_fake_bundle(args, *remotes)
        self.assertEqual(
            self.events(timeline, "restore", "close", "cleanup"),
            [
                "host2.restore",
                "host1.restore",
                "host0.restore",
                "host2.close",
                "host1.close",
                "host0.close",
            ],
        )

    def test_failed_restore_is_reported_and_other_hosts_are_still_restored(self):
        """Keep restoring after one restore fails, and log manual recovery."""
        args, remotes, timeline = self.three_hosts()
        remotes[2].activate.side_effect = RuntimeError("startup failed")
        remotes[1].restore.side_effect = OSError("ssh dropped")
        with (
            self.assertLogs("camel-deploy", "ERROR") as logs,
            self.assertRaisesRegex(RuntimeError, "startup failed"),
        ):
            self.deploy_with_fake_bundle(args, *remotes)
        self.assertEqual(
            self.events(timeline, "restore"),
            ["host2.restore", "host1.restore", "host0.restore"],
        )
        self.assertIn(
            "ERROR:camel-deploy:ROLLBACK FAILED on host1; manual recovery required",
            logs.output,
        )

    def test_unreleased_lock_after_success_fails_the_command(self):
        """Fail a successful deployment if any lock can't be released."""
        args, remotes, timeline = self.three_hosts()
        remotes[1].close.side_effect = OSError("ssh dropped")
        with self.assertRaisesRegex(RuntimeError, "locks could not be released"):
            self.deploy_with_fake_bundle(args, *remotes)
        self.assertEqual(
            self.events(timeline, "close"),
            ["host2.close", "host1.close", "host0.close"],
        )

    def test_unreleased_lock_does_not_mask_the_original_failure(self):
        """Report the deployment failure, not the close failure that follows it."""
        args, remotes, _ = self.three_hosts()
        remotes[0].activate.side_effect = RuntimeError("startup failed")
        remotes[2].close.side_effect = OSError("ssh dropped")
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.deploy_with_fake_bundle(args, *remotes)

    def test_rollback_restores_every_host_to_its_previous_release(self):
        """Restore each host from its current release's saved state."""
        args, remotes, timeline = self.three_hosts("rollback")
        for number, remote in enumerate(remotes):
            remote.current.return_value = f"/opt/r/{number}-new"
            remote.load_state.return_value = {"previous": f"/opt/r/{number}-old"}
        with self.assertLogs("camel-deploy") as logs:
            self.deploy_with_fake_bundle(args, *remotes)
        for number, remote in enumerate(remotes):
            remote.restore.assert_called_once_with(
                f"/opt/r/{number}-new", {"previous": f"/opt/r/{number}-old"}
            )
            self.assertIn(
                f"INFO:camel-deploy:rollback succeeded on host{number}", logs.output
            )
        self.assertEqual(
            self.events(timeline, "restore", "close")[-3:],
            ["host2.close", "host1.close", "host0.close"],
        )

    def test_start_checks_health_and_stop_does_not(self):
        """Run the lifecycle action on every host; only start waits for health."""
        for action, health_checks in (("start", 1), ("stop", 0)):
            with self.subTest(action=action):
                args, remotes, _ = self.three_hosts(action)
                self.deploy_with_fake_bundle(args, *remotes)
                for remote in remotes:
                    remote.lifecycle.assert_called_once_with(action)
                    self.assertEqual(remote.healthy.call_count, health_checks)
                    remote.prepare.assert_not_called()

    def test_retention_failure_does_not_undo_successful_deployment(self):
        """Keep a healthy published release active if pruning later fails."""
        first, second = MagicMock(), MagicMock()
        first.cleanup.side_effect = RuntimeError("prune failed")
        with self.assertRaisesRegex(RuntimeError, "prune failed"):
            self.deploy_with_fake_bundle(self.args(), first, second)
        first.restore.assert_not_called()
        second.restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
