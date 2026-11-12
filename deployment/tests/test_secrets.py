import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from deployment import __main__ as cli
from deployment import secrets
from deployment.config import variables


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.key = self.root / "qa.private.key"
        secrets.keygen("qa", self.key, self.root)

    def tearDown(self):
        self.directory.cleanup()

    def store(self, application, name, value, stage="qa"):
        sealed = secrets.read_store(stage, application, self.root)
        sealed[name] = secrets.seal(
            secrets.load_public_key(stage, self.root), stage, application, name, value
        )
        secrets.write_store(stage, application, sealed, self.root)

    def test_round_trip_keeps_names_readable_and_values_encrypted(self):
        self.store("oba/oba-api", "jdbc_database_password", "p@ss=word\n")
        path = self.root / "secrets" / "qa" / "oba" / "oba-api.json"
        self.assertIn("jdbc_database_password", path.read_text())
        self.assertNotIn("p@ss", path.read_text())
        self.assertEqual(
            secrets.decrypt_all("qa", "oba/oba-api", self.key, self.root),
            {"jdbc_database_password": "p@ss=word\n"},
        )

    def test_contributor_can_add_values_without_private_key(self):
        self.store("app", "first", "one")
        self.store("app", "second", "two")
        self.assertEqual(
            secrets.decrypt_all("qa", "app", self.key, self.root),
            {"first": "one", "second": "two"},
        )

    def test_another_stage_key_is_rejected(self):
        production_key = self.root / "production.private.key"
        secrets.keygen("production", production_key, self.root)
        self.store("app", "password", "value")
        with self.assertRaisesRegex(ValueError, "does not belong to stage qa"):
            secrets.decrypt_all("qa", "app", production_key, self.root)

    def test_ciphertext_cannot_be_moved_between_applications_or_names(self):
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
        self.assertEqual(secrets.decrypt_all("qa", "app", None, self.root), {})
        self.store("app", "password", "value")
        with self.assertRaisesRegex(ValueError, "supply --secrets-key"):
            secrets.decrypt_all("qa", "app", None, self.root)

    def test_reserved_and_invalid_names_are_rejected(self):
        for name in ("jvm_options", "java_bin", "deploy_to", "bad-name", "1st"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                secrets.validate_name(name)

    def test_existing_keys_are_never_overwritten(self):
        with self.assertRaisesRegex(ValueError, "already has a key"):
            secrets.keygen("qa", self.root / "new.key", self.root)
        secrets.keygen("qe", self.root / "qe.key", self.root)
        with self.assertRaises(FileExistsError):
            secrets.keygen("qt", self.root / "qe.key", self.root)

    def test_rekey_preserves_values_and_retires_old_key(self):
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

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_group_readable_private_key_is_rejected(self):
        self.key.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "chmod 600"):
            secrets.load_private_key(self.key, "qa", self.root)

    def test_cli_set_reads_value_from_stdin_and_list_shows_names_only(self):
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
            "top-secret", (self.root / "secrets" / "qa" / "app.json").read_text()
        )


class CheckAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.key = self.root / "qa.private.key"
        secrets.keygen("qa", self.key, self.root)
        for name in ("application.properties", "logback.xml", "logback-access.xml"):
            self.write(Path("files") / name, "fallback\n")

    def tearDown(self):
        self.directory.cleanup()

    store = SecretStoreTests.store

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def config(self, text, application="app"):
        self.write(Path("files") / "qa" / application / "env.properties", text)

    def verify(self, *extra):
        argv = ["verify", "--stage", "qa", "--application", "app"]
        argv += ["--secrets-key", str(self.key), *extra]
        with redirect_stdout(io.StringIO()) as output:
            secrets.run(argv, self.root)
        return output.getvalue()

    def test_referenced_well_formed_secret_passes(self):
        self.config("db.password=[[db_password]]\n")
        self.store("app", "db_password", "value")
        self.assertEqual(secrets.check(root=self.root), ([], []))

    def test_unused_or_misnamed_secret_is_an_error(self):
        self.config("db.password=[[db_password]]\n")
        self.store("app", "db_pasword", "value")
        errors, _ = secrets.check(root=self.root)
        self.assertTrue(any("db_pasword is not used" in e for e in errors), errors)

    def test_secret_for_unknown_application_is_an_error(self):
        self.store("missing-app", "db_password", "value")
        errors, _ = secrets.check(root=self.root)
        self.assertTrue(any("Configuration directory is missing" in e for e in errors))

    def test_malformed_or_truncated_ciphertext_is_an_error(self):
        self.config("a=[[first]]\nb=[[second]]\n")
        secrets.write_store(
            "qa", "app", {"first": "not base64!", "second": "AAAA"}, self.root
        )
        errors, _ = secrets.check(root=self.root)
        self.assertTrue(any("Invalid base64 in first" in e for e in errors), errors)
        self.assertTrue(any("second is not a sealed value" in e for e in errors))

    def test_store_without_public_key_is_an_error(self):
        self.write(Path("secrets") / "qe" / "app.json", '{"version": 1, "values": {}}')
        errors, _ = secrets.check("qe", root=self.root)
        self.assertTrue(any("No public key for stage qe" in e for e in errors))

    def test_plaintext_and_environment_placeholders_are_warnings(self):
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
        self.store("missing-app", "db_password", "value")
        with (
            redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(ValueError, "found 1 error"),
        ):
            secrets.run(["check"], self.root)
        self.assertIn("ERROR qa/missing-app", output.getvalue())

    def test_repository_secrets_have_no_errors(self):
        errors, _ = secrets.check()
        self.assertEqual(errors, [])

    def test_verify_prints_names_never_values(self):
        self.store("app", "db_password", "top-secret")
        output = self.verify()
        self.assertIn("db_password: decrypted", output)
        self.assertNotIn("top-secret", output)

    def test_verify_compare_reports_match_without_printing_value(self):
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
        self.store("app", "db_password", "top-secret")
        with self.assertRaisesRegex(ValueError, "requires --name"):
            self.verify("--compare")
        with (
            patch("sys.stdin", MagicMock(isatty=lambda: False)),
            self.assertRaisesRegex(ValueError, "interactive terminal"),
        ):
            self.verify("--name", "db_password", "--compare")


class DeploymentIntegrationTests(unittest.TestCase):
    def test_environment_variable_conflicting_with_secret_fails(self):
        with self.assertRaisesRegex(ValueError, "both as an encrypted secret"):
            variables(
                "qa",
                "app",
                "1",
                False,
                {},
                environ={"jdbc_database_password": "old"},
                secret_values={"jdbc_database_password": "new"},
            )

    def args(self, action, key=None):
        argv = [
            action,
            "--stage",
            "qa",
            "--roles",
            "cml1",
            "--application",
            "camel-endpoint-test-tool",
            "--version",
            "1",
        ]
        return cli.parser().parse_args(argv + (["--secrets-key", key] if key else []))

    def test_plan_without_key_lists_names_and_never_decrypts(self):
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
        with (
            patch.object(cli, "read_store", return_value={"password": "x"}),
            patch.object(secrets, "read_store", return_value={"password": "x"}),
            patch("deployment.remote.Remote") as remote,
            self.assertRaisesRegex(ValueError, "supply --secrets-key"),
            patch("os.environ", {}),
        ):
            cli.execute(self.args("deploy"))
        remote.assert_not_called()

    def test_decrypted_secret_is_rendered_into_uploaded_configuration(self):
        captured = {}
        remote = MagicMock()

        def prepare(bundle, release, metadata, shared):
            captured.update(
                {path.name: path.read_text() for path in (bundle / "config").iterdir()}
            )

        remote.prepare.side_effect = prepare

        def bundle(args, values, workspace):
            path = workspace / "bundle"
            path.mkdir()
            return path, {}

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


if __name__ == "__main__":
    unittest.main()
