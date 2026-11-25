"""Tests for ``[[name]]`` template rendering and configuration file selection."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from deployment.config import (
    ROOT,
    config_files,
    environment,
    render,
    render_file,
    variables,
)


def write(path, data):
    """Create parent directories and write bytes to a test fixture path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TemplateTests(unittest.TestCase):
    """Test safe placeholder rendering and configuration file selection."""

    def test_placeholders_are_single_pass_and_preserve_runtime_syntax(self):
        """Substitute only deployment placeholders and preserve runtime syntax."""
        text = "a=[[x]] b={{camel}} c=${spring} d=[[:alpha:]] e=[[ -f f ]]"
        self.assertEqual(
            render(text, {"x": "[[y]]"}),
            "a=[[y]] b={{camel}} c=${spring} d=[[:alpha:]] e=[[ -f f ]]",
        )
        with self.assertRaisesRegex(ValueError, "Missing template variable: x"):
            render("[[x]]", {})

    def test_render_file_preserves_raw_bytes_and_rejects_obsolete_suffix(self):
        """Preserve binary files and reject obsolete ERB-style template suffixes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "a.properties"
            write(path, b"p=[[x]]\r\nq=\xc3\xa9\r\n")
            self.assertEqual(render_file(path, {"x": "v"}), "p=v\r\nq=é\r\n".encode())
            write(path, b"\xff\xfe binary")
            self.assertEqual(render_file(path, {}), b"\xff\xfe binary")
            write(root / "files" / "qa" / "app" / "old.properties.erb", b"literal")
            for name in ("application.properties", "logback.xml", "logback-access.xml"):
                write(root / "files" / name, b"fallback")
            with self.assertRaisesRegex(ValueError, "unsupported template suffix"):
                config_files("qa", "app", {}, root)

    def test_service_configuration_includes_xml_and_renders_placeholders(self):
        """Render selected properties and XML configuration files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "files" / "qa" / "app"
            write(source / "settings.properties", b"password=[[secret]]\r\n")
            write(source / "logging.xml", b"<host>[[log_host]]</host>")
            for name in ("application.properties", "logback.xml", "logback-access.xml"):
                write(root / "files" / name, b"fallback")
            files = config_files("qa", "app", {"secret": "s", "log_host": "h"}, root)
            self.assertEqual(files["settings.properties"], b"password=s\r\n")
            self.assertEqual(files["logging.xml"], b"<host>h</host>")

    def test_all_repository_configuration_and_service_templates_render(self):
        """Check repository templates for unresolved deployment placeholders."""
        values = {}
        for path in (ROOT / "files").rglob("*"):
            if path.is_file():
                for name in re.findall(
                    rb"\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]", path.read_bytes()
                ):
                    values[name.decode()] = f"value-{name.decode()}"
        applications = sorted(
            {
                path.parent.relative_to(ROOT / "files")
                for path in (ROOT / "files").rglob("*")
                if path.is_file()
                and path.suffix in {".properties", ".xml"}
                and len(path.relative_to(ROOT / "files").parts) >= 3
            }
        )
        self.assertTrue(applications, "no stage configuration found under files/")
        for relative in applications:
            stage, application = relative.parts[0], "/".join(relative.parts[1:])
            with self.subTest(stage=stage, application=application):
                files = config_files(stage, application, values)
                for name, content in files.items():
                    self.assertNotIn(b"[[", content, name)
        for name in ("camel_init", "camel_supervisord"):
            content = render_file(ROOT / "files" / name, values)
            self.assertNotIn(b"[[", content)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is a dev dependency")
    def test_rendered_sysv_init_script_passes_shellcheck(self):
        """Lint camel_init as rendered for every SysV sample application."""
        shellcheck = shutil.which("shellcheck") or "shellcheck"
        cases = [
            (stage, application, second)
            for stage in ("sample-prod",)
            for application in ("hello-service", "orders-service")
            for second in (False, True)
        ]
        for stage, application, second in cases:
            with (
                self.subTest(stage=stage, application=application, second=second),
                tempfile.TemporaryDirectory() as directory,
            ):
                settings, _ = environment(stage)
                self.assertFalse(settings["use_supervisor"])
                values = variables(
                    stage, application, "1.0.0", second, settings, environ={}
                )
                values["release_path"] = values["deploy_to"] + "/releases/1-abc"
                script = Path(directory) / "camel_init"
                script.write_bytes(render_file(ROOT / "files" / "camel_init", values))
                result = subprocess.run(  # noqa: S603 - fixed arguments
                    [shellcheck, str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
