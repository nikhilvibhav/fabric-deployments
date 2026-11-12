import hashlib
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from deployment import __main__ as cli
from deployment.artifacts import Repository
from deployment.config import (
    boolean,
    config_files,
    environment,
    render,
    select_hosts,
    variables,
)
from deployment.remote import Remote


class ConfigurationTests(unittest.TestCase):
    def test_all_existing_inventories_and_role_selection(self):
        counts = {
            "dr": 6,
            "lst": 1,
            "preprod": 8,
            "production": 7,
            "qa": 4,
            "qe": 2,
            "qs": 2,
            "qt": 4,
            "tst": 2,
            "vagrant": 1,
        }
        for stage, count in counts.items():
            with self.subTest(stage=stage):
                settings, hosts = environment(stage)
                self.assertEqual(len(hosts), count)
                self.assertEqual(
                    settings["use_supervisor"], stage in {"qa", "qe", "qt", "tst"}
                )
        _, hosts = environment("preprod")
        self.assertEqual(
            select_hosts(hosts, "cml1,cml2"),
            ["buildbot@10.90.131.134", "buildbot@10.90.131.135"],
        )
        with self.assertRaisesRegex(ValueError, "Unknown roles"):
            select_hosts(hosts, "app")

    def test_jenkins_aliases_nested_application_and_second_artifact(self):
        values = variables(
            "qa",
            "oba/oba-api",
            "1.2.3",
            True,
            {},
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
            variables("qa", "app", "latest", True, {}, environ={})

    def test_unsafe_parameters_and_boolean_typos_fail(self):
        for application in (
            "../app",
            "/app",
            "app;whoami",
            "app//nested",
            "app\\nested",
        ):
            with self.subTest(application=application), self.assertRaises(ValueError):
                variables("qa", application, "1", False, {}, environ={})
        with self.assertRaises(ValueError):
            boolean("flase")

    def test_render_is_literal_and_missing_secrets_are_not_defaulted(self):
        self.assertEqual(
            render("db=[[password]]", {"password": "x[[evil]]"}), "db=x[[evil]]"
        )
        with self.assertRaisesRegex(ValueError, "Missing template variable: password"):
            render("[[password]]", {})

    def test_actual_qa_configuration_and_shared_fallback(self):
        settings, _ = environment("qa")
        values = variables(
            "qa",
            "authentication-service",
            "1",
            False,
            settings,
            environ={},
            secret_values={"truststore_password": "not-a-p@ssw0rd"},
        )
        files = config_files("qa", "authentication-service", values)
        self.assertIn(b"server.port=8080", files["application.properties"])
        self.assertIn(
            b"<graylogHost>graylog.service</graylogHost>", files["logback.xml"]
        )
        self.assertIn("logback-access.xml", files)
        self.assertNotIn(b"[[", b"".join(files.values()))

    def test_flattened_config_collisions_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "files" / "qa" / "app"
            (source / "template").mkdir(parents=True)
            (source / "same.properties").write_text("a")
            (source / "template" / "same.properties").write_text("b")
            with self.assertRaisesRegex(ValueError, "collision"):
                config_files("qa", "app", {}, root)


class ArtifactTests(unittest.TestCase):
    def test_real_http_artifact_resolution_and_verified_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "app" / "1.0"
            version.mkdir(parents=True)
            content = b"test artifact content"
            (version / "app-1.0.jar").write_bytes(content)
            (version / "app-1.0-sources.jar").write_bytes(b"sources")

            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass

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
        repo = Repository()
        repo.open = MagicMock(
            return_value=io.BytesIO(b'<a href="a.jar">a</a><a href="b.jar">b</a>')
        )
        with self.assertRaisesRegex(ValueError, "found 2"):
            repo.resolve("https://repo", "app", "1", ".jar")

    def test_download_checksum(self):
        repo = Repository()
        repo.open = MagicMock(return_value=io.BytesIO(b"artifact"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                repo.download(
                    "https://repo/app.jar", Path(directory) / "app.jar", "bad"
                )

    def test_artifacts_are_resolved_as_jars(self):
        with tempfile.TemporaryDirectory() as directory:
            args = cli.parser().parse_args(
                [
                    "deploy",
                    "--stage",
                    "qa",
                    "--roles",
                    "cml1",
                    "--application",
                    "service",
                    "--version",
                    "1.0.0",
                ]
            )
            repo = Repository()
            repo.resolve = MagicMock(return_value="https://repo/service.jar")

            def download(url, destination, checksum=None):
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
                cli.DEFAULT_REPOSITORY + "/libs-release-local/com/rmg",
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
        args = cli.parser().parse_args(
            [
                "deploy",
                "--stage",
                "qa",
                "--roles",
                "cml1",
                "--application",
                "service",
                "--version",
                "1.0.0",
                "--artifact-url",
                "https://repo/service.zip",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must point to a JAR"):
                cli.prepare_bundle(
                    args,
                    {"application": "service", "application_name": "service"},
                    Path(directory),
                )


class RemoteTests(unittest.TestCase):
    def setUp(self):
        settings, _ = environment("qa")
        self.values = variables(
            "qa", "authentication-service", "1", False, settings, environ={}
        )
        self.remote = Remote("test@example.invalid", self.values, True)
        self.remote.run = MagicMock(return_value=SimpleNamespace(ok=True, stdout=""))
        self.remote.put = MagicMock()
        self.remote.exists = MagicMock(return_value=False)
        self.remote.current = MagicMock(return_value=None)
        self.release = self.values["deploy_to"] + "/releases/20260101000000-abc"

    def test_prepare_records_state_before_activation_and_renders_service(self):
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
        self.remote.load_state = MagicMock(return_value={"shared_existed": {}})
        self.remote.switch = MagicMock()
        self.remote.lifecycle = MagicMock()
        self.remote.healthy = MagicMock()
        self.remote.activate(self.release)
        self.remote.switch.assert_called_once_with(self.release)
        self.remote.lifecycle.assert_called_once_with("restart")
        self.remote.healthy.assert_called_once()

    def test_restore_reinstalls_previous_service_and_release(self):
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
        self.remote.run.return_value = SimpleNamespace(ok=False, stdout="app FATAL")
        with (
            patch("deployment.remote.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "healthy"),
        ):
            self.remote.healthy()
        self.assertEqual(self.remote.run.call_count, 12)

    def test_paths_outside_releases_cannot_be_pruned_or_rolled_back(self):
        for path in (
            "/etc",
            self.values["deploy_to"] + "/releases/../shared",
            self.values["deploy_to"] + "/releases/a/b",
        ):
            with self.assertRaises(ValueError):
                self.remote.validate_release(path)

    def test_stop_supervisor_is_idempotent_only_for_known_stopped_states(self):
        self.remote.run.return_value = SimpleNamespace(ok=False, stdout="app STOPPED")
        self.remote.lifecycle("stop")
        self.assertEqual(self.remote.run.call_count, 1)
        self.remote.run.reset_mock()
        self.remote.run.return_value = SimpleNamespace(ok=True, stdout="app RUNNING")
        self.remote.lifecycle("stop")
        self.assertEqual(self.remote.run.call_count, 2)


class OrchestrationTests(unittest.TestCase):
    def args(self, action="deploy"):
        return cli.parser().parse_args(
            [
                action,
                "--stage",
                "qa",
                "--roles",
                "cml1,cml2",
                "--application",
                "authentication-service",
                "--version",
                "1",
            ]
        )

    def test_offline_plan_never_opens_ssh_and_does_not_print_variables(self):
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
        self.assertNotIn("variables", plan)

    def test_release_type_is_inferred_from_snapshot_suffix(self):
        args = self.args("plan")
        args.version = "1.0.0-SNAPSHOT"
        with redirect_stdout(io.StringIO()) as output:
            cli.execute(args)
        self.assertEqual(json.loads(output.getvalue())["release_type"], "snapshot")

    def test_release_type_override_cannot_conflict_with_version(self):
        args = self.args("plan")
        args.release_type = "snapshot"
        with self.assertRaisesRegex(ValueError, "is stable"):
            cli.execute(args)

    def test_host_failure_restores_failed_and_previously_activated_hosts(self):
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
        ):

            def bundle(args, values, workspace):
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
        first, second = MagicMock(), MagicMock()
        second.lock.side_effect = RuntimeError("locked")
        with (
            patch("deployment.remote.Remote", side_effect=[first, second]),
            patch.object(cli, "prepare_bundle") as prepare,
        ):
            with self.assertRaisesRegex(RuntimeError, "locked"):
                cli.execute(self.args())
        prepare.assert_not_called()
        first.activate.assert_not_called()
        first.close.assert_called_once()

    def test_retention_failure_does_not_undo_successful_deployment(self):
        first, second = MagicMock(), MagicMock()
        first.cleanup.side_effect = RuntimeError("prune failed")
        with (
            patch("deployment.remote.Remote", side_effect=[first, second]),
            patch.object(cli, "prepare_bundle") as prepare,
        ):

            def bundle(args, values, workspace):
                path = workspace / "bundle"
                path.mkdir()
                return path, {}

            prepare.side_effect = bundle
            with self.assertRaisesRegex(RuntimeError, "prune failed"):
                cli.execute(self.args())
        first.restore.assert_not_called()
        second.restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
