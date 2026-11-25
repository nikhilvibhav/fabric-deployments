"""Tests for encrypted secret stores, the secrets CLI and their use during deployment."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from deployment import __main__ as cli
from deployment import secrets
from deployment.config import variables

FIXTURES = Path(__file__).parent / "fixtures"


def sample_key(test_case, stage):
    """Copy a committed sample test key to a 0600 temp file, as the CLI requires."""
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    path = Path(directory.name) / f"{stage}.private.key"
    shutil.copyfile(FIXTURES / f"{stage}.test-key", path)
    path.chmod(0o600)
    return path


class TemporaryStoreTestCase(unittest.TestCase):
    """Provide an isolated QA secret store with a generated key pair."""

    def setUp(self):
        """Create an isolated QA secret store and generate its key pair."""
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.key = self.root / "qa.private.key"
        secrets.keygen("qa", self.key, self.root)

    def tearDown(self):
        """Remove the temporary secret store and key files."""
        self.directory.cleanup()

    def store(self, application, name, value, stage="qa"):
        """Encrypt and persist one test secret for an application."""
        sealed = secrets.read_store(stage, application, self.root)
        sealed[name] = secrets.seal(
            secrets.load_public_key(stage, self.root), stage, application, name, value
        )
        secrets.write_store(stage, application, sealed, self.root)


class SecretStoreTests(TemporaryStoreTestCase):
    """Test encrypted secret storage, key management, and secret CLI operations."""

    def test_round_trip_keeps_names_readable_and_values_encrypted(self):
        """Keep secret names visible while encrypting values that decrypt correctly."""
        self.store("oba/oba-api", "jdbc_database_password", "p@ss=word\n")
        path = secrets.secrets_path("qa", "oba/oba-api", self.root)
        self.assertIn("jdbc_database_password", path.read_text())
        self.assertNotIn("p@ss", path.read_text())
        self.assertEqual(
            secrets.decrypt_all("qa", "oba/oba-api", self.key, self.root),
            {"jdbc_database_password": "p@ss=word\n"},
        )

    def test_contributor_can_add_values_without_private_key(self):
        """Allow contributors to add encrypted values using only the public key."""
        self.store("app", "first", "one")
        self.store("app", "second", "two")
        self.assertEqual(
            secrets.decrypt_all("qa", "app", self.key, self.root),
            {"first": "one", "second": "two"},
        )

    def test_another_stage_key_is_rejected(self):
        """Reject a private key that belongs to a different stage."""
        production_key = self.root / "production.private.key"
        secrets.keygen("production", production_key, self.root)
        self.store("app", "password", "value")
        with self.assertRaisesRegex(ValueError, "does not belong to stage qa"):
            secrets.decrypt_all("qa", "app", production_key, self.root)

    def test_ciphertext_cannot_be_moved_between_applications_or_names(self):
        """Reject ciphertext copied to a different application or secret name."""
        self.store("first", "password", "value")
        ciphertext = secrets.read_store("qa", "first", self.root)["password"]
        secrets.write_store("qa", "second", {"password": ciphertext}, self.root)
        secrets.write_store("qa", "first", {"other": ciphertext}, self.root)
        for application in ("second", "first"):
            with (
                self.subTest(application=application),
                self.assertRaisesRegex(
                    ValueError, "different stage, application or name"
                ),
            ):
                secrets.decrypt_all("qa", application, self.key, self.root)

    def test_missing_key_fails_when_secrets_exist(self):
        """Allow empty stores without a key but require one for encrypted values."""
        self.assertEqual(secrets.decrypt_all("qa", "app", None, self.root), {})
        self.store("app", "password", "value")
        with self.assertRaisesRegex(ValueError, "supply --secrets-key"):
            secrets.decrypt_all("qa", "app", None, self.root)

    def test_reserved_and_invalid_names_are_rejected(self):
        """Reject malformed secret names and names reserved for deployment values."""
        for name in ("jvm_options", "java_bin", "deploy_to", "bad-name", "1st"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                secrets.validate_name(name)

    def test_existing_keys_are_never_overwritten(self):
        """Ensure key generation never replaces an existing stage or key file."""
        with self.assertRaisesRegex(ValueError, "already has a key"):
            secrets.keygen("qa", self.root / "new.key", self.root)
        secrets.keygen("qe", self.root / "qe.key", self.root)
        with self.assertRaises(FileExistsError):
            secrets.keygen("qt", self.root / "qe.key", self.root)

    def test_rekey_preserves_values_and_retires_old_key(self):
        """Re-encrypt all stage values with a new key and reject the retired key."""
        self.store("app", "password", "value")
        self.store("oba/oba-api", "token", "abc")
        new_key = self.root / "qa-new.private.key"
        self.assertEqual(secrets.rekey("qa", self.key, new_key, self.root), 2)
        self.assertEqual(
            secrets.decrypt_all("qa", "oba/oba-api", new_key, self.root),
            {"token": "abc"},
        )
        with self.assertRaisesRegex(ValueError, "does not belong"):
            secrets.decrypt_all("qa", "app", self.key, self.root)

    def test_store_records_key_id_and_rekey_updates_it(self):
        """Stamp each store with its key's id so a rekey changes every store."""
        self.store("app", "password", "value")
        path = secrets.secrets_path("qa", "app", self.root)
        before = json.loads(path.read_text())["key_id"]
        self.assertEqual(
            before, secrets.key_id(secrets.load_public_key("qa", self.root))
        )
        secrets.rekey("qa", self.key, self.root / "qa-new.private.key", self.root)
        after = json.loads(path.read_text())["key_id"]
        self.assertEqual(
            after, secrets.key_id(secrets.load_public_key("qa", self.root))
        )

    def test_interrupted_rekey_is_detected_and_block_writes(self):
        """A rekey that stops before public.key changes leaves a detectable state."""
        self.store("app", "password", "value")
        public_key = secrets.public_key_path("qa", self.root)
        write_atomic = secrets.write_atomic

        def fail_on_public_key(path, text):
            """Simulate an interruption just before the new public key is written."""
            if Path(path) == public_key:
                raise OSError("simulated interruption")
            write_atomic(path, text)

        with (
            patch.object(secrets, "write_atomic", side_effect=fail_on_public_key),
            self.assertRaisesRegex(OSError, "simulated interruption"),
        ):
            secrets.rekey("qa", self.key, self.root / "qa.new.private.key", self.root)

        errors, _ = secrets.check("qa", "app", self.root)
        self.assertTrue(any("different key than public.key" in e for e in errors))
        with self.assertRaisesRegex(ValueError, "encrypted with a different key"):
            self.store("app", "other", "value")
        with self.assertRaisesRegex(ValueError, "encrypted with a different key"):
            secrets.decrypt_all("qa", "app", self.key, self.root)

    def test_rekey_refuses_an_existing_private_key_path_before_changing_files(self):
        """Fail before any work when the new private key path is already taken."""
        self.store("app", "password", "value")

        before = secrets.secrets_path("qa", "app", self.root).read_text()
        with self.assertRaises(FileExistsError):
            secrets.rekey("qa", self.key, self.key, self.root)
        self.assertEqual(
            secrets.secrets_path("qa", "app", self.root).read_text(), before
        )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_rekey_conflicts_with_a_secret_changed_on_another_branch(self):
        """A secret rotated on a branch must conflict with a rekey, not vanish."""

        def git(*args):
            """Run git in the temporary repository with a fixed identity."""
            return subprocess.run(  # noqa: S603 - fixed git arguments
                ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
                + list(args),
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )

        git("init", "-q")
        self.store("app", "password", "old")
        git("add", "secrets")
        git("commit", "-qm", "base")
        git("switch", "-qc", "rotate")
        self.store("app", "password", "new")
        git("commit", "-qam", "rotate password")
        git("switch", "-q", "-")
        secrets.rekey("qa", self.key, self.root / "qa-new.private.key", self.root)
        git("commit", "-qam", "rekey")

        merge = git("merge", "--no-edit", "rotate")
        self.assertNotEqual(merge.returncode, 0)
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout
        self.assertIn("secrets/qa/app.json", conflicted)

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_group_readable_private_key_is_rejected(self):
        """Reject private keys readable by group or other POSIX users."""
        self.key.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "chmod 600"):
            secrets.load_private_key(self.key, "qa", self.root)

    def test_cli_set_reads_value_from_stdin_and_list_shows_names_only(self):
        """Read a secret from standard input and list only its name afterward."""
        with (
            patch("sys.stdin", io.StringIO("top-secret\n")),
            redirect_stdout(io.StringIO()),
        ):
            secrets.run(
                [
                    "set",
                    "--stage",
                    "qa",
                    "--application",
                    "app",
                    "--name",
                    "password",
                    "--value-from",
                    "-",
                ],
                self.root,
            )
        with redirect_stdout(io.StringIO()) as listing:
            secrets.run(["list", "--stage", "qa", "--application", "app"], self.root)
        self.assertEqual(listing.getvalue(), "password\n")
        self.assertEqual(
            secrets.decrypt_all("qa", "app", self.key, self.root),
            {"password": "top-secret"},
        )
        self.assertNotIn(
            "top-secret",
            secrets.secrets_path("qa", "app", self.root).read_text(),
        )


class CheckAndVerifyTests(TemporaryStoreTestCase):
    """Test keyless store validation and private-key secret verification."""

    def setUp(self):
        """Create an isolated QA store and fallback configuration templates."""
        super().setUp()
        for name in ("application.properties", "logback.xml", "logback-access.xml"):
            self.write(Path("files") / name, "fallback\n")

    def write(self, relative, text):
        """Write text beneath the temporary repository root."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def config(self, text, application="app"):
        """Write an application-specific QA properties template."""
        self.write(Path("files") / "qa" / application / "env.properties", text)

    def verify(self, *extra):
        """Run the verify command and return its standard output."""
        argv = ["verify", "--stage", "qa", "--application", "app"]
        argv += ["--secrets-key", str(self.key), *extra]
        with redirect_stdout(io.StringIO()) as output:
            secrets.run(argv, self.root)
        return output.getvalue()

    def cli(self, *argv):
        """Run a secrets subcommand against the temporary root and return stdout."""
        with redirect_stdout(io.StringIO()) as output:
            secrets.run(list(argv), self.root)
        return output.getvalue()

    def test_cli_keygen_and_rekey_say_what_to_commit(self):
        """Tell the operator which files to commit after keygen and rekey."""
        qe_key = self.root / "qe.private.key"
        self.assertEqual(
            self.cli("keygen", "--stage", "qe", "--private-key-out", str(qe_key)),
            "Created secrets/qe/public.key; commit it.\n"
            f"Store {qe_key} securely and back it up offline.\n",
        )
        self.store("app", "db_password", "value")
        output = self.cli(
            "rekey",
            "--stage",
            "qa",
            "--old-key",
            str(self.key),
            "--private-key-out",
            str(self.root / "qa-new.private.key"),
        )
        self.assertTrue(
            output.startswith(
                "Re-encrypted 1 application file(s) for qa. "
                "Commit all changes under secrets/qa together."
            ),
            output,
        )

    def test_cli_unset_removes_a_name_and_rejects_unknown_ones(self):
        """Remove only an existing name, leaving the others encrypted."""
        self.store("app", "first", "1")
        self.store("app", "second", "2")
        self.assertEqual(
            self.cli(
                "unset", "--stage", "qa", "--application", "app", "--name", "first"
            ),
            "Removed first from qa/app\n",
        )
        self.assertEqual(list(secrets.read_store("qa", "app", self.root)), ["second"])
        with self.assertRaisesRegex(ValueError, "first is not set"):
            self.cli(
                "unset", "--stage", "qa", "--application", "app", "--name", "first"
            )

    def test_cli_check_prints_warnings_and_requires_stage_with_application(self):
        """Print each warning, then a passing summary; --application needs --stage."""
        self.config("db.password=[[db_password]]\napi.key=[[api_key]]\n")
        self.store("app", "db_password", "value")
        self.assertEqual(
            self.cli("check", "--stage", "qa"),
            "WARNING qa/app: [[api_key]] is not an encrypted secret; "
            "it must come from the environment or --vars-file\n"
            "Secrets check passed with 1 warning(s)\n",
        )
        with self.assertRaisesRegex(ValueError, "--application requires --stage"):
            self.cli("check", "--application", "app")

    def test_application_without_secrets_is_an_error_when_checked_by_name(self):
        """Report an application checked by name that has no encrypted store."""
        self.config("db.password=[[db_password]]\n")
        errors, _ = secrets.check("qa", "app", self.root)
        self.assertIn("qa/app: no encrypted secrets", errors)

    def test_cli_verify_needs_a_key_and_a_known_name(self):
        """Fail verification without a key or for a name that isn't stored."""
        self.store("app", "db_password", "value")
        with (
            patch.dict(os.environ, {secrets.KEY_ENV: ""}),
            self.assertRaisesRegex(ValueError, "Supply --secrets-key"),
        ):
            self.cli("verify", "--stage", "qa", "--application", "app")
        with self.assertRaisesRegex(ValueError, "missing is not set"):
            self.verify("--name", "missing")

    def test_referenced_well_formed_secret_passes(self):
        """Accept a valid encrypted secret referenced by an application template."""
        self.config("db.password=[[db_password]]\n")
        self.store("app", "db_password", "value")
        self.assertEqual(secrets.check(root=self.root), ([], []))

    def test_unused_or_misnamed_secret_is_an_error(self):
        """Report encrypted values whose names are absent from templates."""
        self.config("db.password=[[db_password]]\n")
        self.store("app", "db_pasword", "value")
        errors, _ = secrets.check(root=self.root)
        self.assertTrue(any("db_pasword is not used" in e for e in errors), errors)

    def test_secret_for_unknown_application_is_an_error(self):
        """Report stores whose application configuration directory is missing."""
        self.store("missing-app", "db_password", "value")
        errors, _ = secrets.check(root=self.root)
        self.assertTrue(any("Configuration directory is missing" in e for e in errors))

    def test_malformed_or_truncated_ciphertext_is_an_error(self):
        """Report malformed base64 and ciphertext too short to be a sealed value."""
        self.config("a=[[first]]\nb=[[second]]\n")
        secrets.write_store(
            "qa", "app", {"first": "not base64!", "second": "AAAA"}, self.root
        )
        errors, _ = secrets.check(root=self.root)
        self.assertTrue(any("Invalid base64 in first" in e for e in errors), errors)
        self.assertTrue(any("second is not a sealed value" in e for e in errors))

    def test_store_without_public_key_is_an_error(self):
        """Report a stage store that has no corresponding public key."""
        self.write(Path("secrets") / "qe" / "app.json", '{"version": 1, "values": {}}')
        errors, _ = secrets.check("qe", root=self.root)
        self.assertTrue(any("No public key for stage qe" in e for e in errors))

    def test_plaintext_and_environment_placeholders_are_warnings(self):
        """Warn about literal sensitive properties and unmanaged placeholders."""
        self.config(
            "db.password=[[db_password]]\n"
            "admin.password=hunter2\n"
            "auth.base-url-token=https://auth/token\n"
            "spring.password=${SPRING_PASSWORD}\n"
            "jdbc.password=[[jdbc_database_password]]\n"
        )
        self.store("app", "db_password", "value")
        errors, warnings = secrets.check(root=self.root)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("plaintext value for admin.password", warnings[1])
        self.assertIn("[[jdbc_database_password]] is not an encrypted", warnings[0])

    def test_cli_check_fails_on_errors(self):
        """Print validation errors and fail the check command when errors exist."""
        self.store("missing-app", "db_password", "value")
        with (
            redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(ValueError, "found 1 error"),
        ):
            secrets.run(["check"], self.root)
        self.assertIn("ERROR qa/missing-app", output.getvalue())

    def test_repository_secrets_have_no_errors(self):
        """Ensure committed repository secret stores pass validation."""
        errors, _ = secrets.check()
        self.assertEqual(errors, [])

    def test_verify_prints_names_never_values(self):
        """Confirm successful verification reports names without exposing values."""
        self.store("app", "db_password", "top-secret")
        output = self.verify()
        self.assertIn("db_password: decrypted", output)
        self.assertNotIn("top-secret", output)

    def test_verify_compare_reports_match_without_printing_value(self):
        """Compare a secret interactively without printing its contents."""
        self.store("app", "db_password", "top-secret")
        with (
            patch("sys.stdin", MagicMock(isatty=lambda: True)),
            patch("getpass.getpass", return_value="top-secret"),
        ):
            output = self.verify("--name", "db_password", "--compare")
        self.assertIn("matches the expected value", output)
        self.assertNotIn("top-secret", output)
        with (
            patch("sys.stdin", MagicMock(isatty=lambda: True)),
            patch("getpass.getpass", return_value="wrong"),
            self.assertRaisesRegex(ValueError, "does not match"),
        ):
            self.verify("--name", "db_password", "--compare")

    def test_verify_compare_requires_terminal_and_name(self):
        """Require a secret name and interactive terminal for compare mode."""
        self.store("app", "db_password", "top-secret")
        with self.assertRaisesRegex(ValueError, "requires --name"):
            self.verify("--compare")
        with (
            patch("sys.stdin", MagicMock(isatty=lambda: False)),
            self.assertRaisesRegex(ValueError, "interactive terminal"),
        ):
            self.verify("--name", "db_password", "--compare")


class DeploymentIntegrationTests(unittest.TestCase):
    """Test interactions between encrypted secrets and deployment orchestration."""

    def test_environment_variable_conflicting_with_secret_fails(self):
        """Reject ambiguous values supplied both as secrets and environment vars."""
        with self.assertRaisesRegex(ValueError, "both as an encrypted secret"):
            variables(
                "qa",
                "app",
                "1",
                False,
                {"keep_releases": 1, "use_supervisor": False, "health_timeout": 60},
                environ={"jdbc_database_password": "old"},
                secret_values={"jdbc_database_password": "new"},
            )

    def args(self, action, key=None):
        """Build sample-qa CLI arguments for deployment integration tests."""
        argv = [
            action,
            "--stage",
            "sample-qa",
            "--roles",
            "app1",
            "--application",
            "orders-service",
            "--version",
            "1",
        ]
        return cli.parser().parse_args(argv + (["--secrets-key", key] if key else []))

    def test_plan_without_key_lists_names_and_never_decrypts(self):
        """Plan without a key by listing secret names without decrypting them."""
        with (
            patch.dict(os.environ, {secrets.KEY_ENV: ""}),
            patch.object(
                cli, "read_store", return_value={"jdbc_database_password": "x"}
            ),
            patch.object(cli, "decrypt_all") as decrypt,
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.execute(self.args("plan"))
        decrypt.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(
            plan["secrets"], {"names": ["jdbc_database_password"], "decrypted": False}
        )

    def test_deploy_without_key_fails_before_connecting(self):
        """Require a decryption key before creating any remote connection."""
        with (
            patch.object(cli, "read_store", return_value={"password": "x"}),
            patch.object(
                secrets,
                "read_store_file",
                return_value={"version": 2, "key_id": "x", "values": {"password": "x"}},
            ),
            patch("deployment.remote.Remote") as remote,
            self.assertRaisesRegex(ValueError, "supply --secrets-key"),
            patch("os.environ", {}),
        ):
            cli.execute(self.args("deploy"))
        remote.assert_not_called()

    def test_decrypted_secret_is_rendered_into_uploaded_configuration(self):
        """Render a decrypted value into configuration before remote preparation."""
        captured = {}
        remote = MagicMock()

        def prepare(bundle, release, metadata, shared):
            """Capture generated configuration files passed to the remote."""
            captured.update(
                {path.name: path.read_text() for path in (bundle / "config").iterdir()}
            )

        remote.prepare.side_effect = prepare

        def bundle(args, values, workspace):
            """Create the minimal bundle needed by the integration test."""
            path = workspace / "bundle"
            path.mkdir()
            return path, {"artifact_url": "http://repo/app.jar", "sha256": "ab12"}

        with (
            patch.object(
                cli, "read_store", return_value={"jdbc_database_password": "x"}
            ),
            patch.object(
                cli, "decrypt_all", return_value={"jdbc_database_password": "s3cret"}
            ),
            patch.object(cli, "prepare_bundle", side_effect=bundle),
            patch("deployment.remote.Remote", return_value=remote),
        ):
            cli.execute(self.args("deploy", "qa.key"))
        self.assertIn(
            "spring.datasource.password=s3cret", captured["application.properties"]
        )


class SampleEnvironmentTests(unittest.TestCase):
    """Use the committed sample stages and test keys without mocking secrets."""

    def test_sample_secrets_decrypt_with_their_test_keys(self):
        """Decrypt every sample store and reject the other stage's key."""
        expected = {
            "hello-service": {"api_token": "{stage}-api-token"},
            "orders-service": {"jdbc_database_password": "{stage}-db-password"},
        }
        for stage in ("sample-qa", "sample-prod"):
            key = sample_key(self, stage)
            for application, values in expected.items():
                with self.subTest(stage=stage, application=application):
                    self.assertEqual(
                        secrets.decrypt_all(stage, application, key),
                        {
                            name: value.format(stage=stage)
                            for name, value in values.items()
                        },
                    )
        with self.assertRaisesRegex(ValueError, "does not belong"):
            secrets.decrypt_all(
                "sample-prod", "hello-service", sample_key(self, "sample-qa")
            )

    def test_sample_deploy_renders_decrypted_secret_into_configuration(self):
        """Deploy sample-prod with its test key, faking only SSH and the download."""
        captured = {}
        remote = MagicMock()

        def prepare(bundle, release, metadata, shared):
            """Capture generated configuration files passed to the remote."""
            captured.update(
                {path.name: path.read_text() for path in (bundle / "config").iterdir()}
            )

        remote.prepare.side_effect = prepare

        def bundle(args, values, workspace):
            """Create the minimal bundle needed by the sample deployment."""
            path = workspace / "bundle"
            path.mkdir()
            return path, {"artifact_url": "http://repo/app.jar", "sha256": "ab12"}

        argv = [
            "deploy",
            "--stage",
            "sample-prod",
            "--roles",
            "app1",
            "--application",
            "hello-service",
            "--version",
            "2.0.0",
            "--secrets-key",
            str(sample_key(self, "sample-prod")),
        ]
        with (
            patch.object(cli, "prepare_bundle", side_effect=bundle),
            patch("deployment.remote.Remote", return_value=remote),
        ):
            cli.execute(cli.parser().parse_args(argv))
        properties = captured["application.properties"]
        self.assertIn("greeting.api.token=sample-prod-api-token", properties)
        self.assertIn("greeting.message=Hello from sample-prod (2.0.0)", properties)


if __name__ == "__main__":
    unittest.main()
