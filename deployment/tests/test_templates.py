import re
import tempfile
import unittest
from pathlib import Path

from deployment.config import ROOT, config_files, render, render_file


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TemplateTests(unittest.TestCase):
    def test_placeholders_are_single_pass_and_preserve_runtime_syntax(self):
        text = "a=[[x]] b={{camel}} c=${spring} d=[[:alpha:]] e=[[ -f f ]]"
        self.assertEqual(
            render(text, {"x": "[[y]]"}),
            "a=[[y]] b={{camel}} c=${spring} d=[[:alpha:]] e=[[ -f f ]]",
        )
        with self.assertRaisesRegex(ValueError, "Missing template variable: x"):
            render("[[x]]", {})

    def test_render_file_preserves_raw_bytes_and_rejects_obsolete_suffix(self):
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
        self.assertGreater(len(applications), 50)
        successful = 0
        for relative in applications:
            stage, application = relative.parts[0], "/".join(relative.parts[1:])
            with self.subTest(stage=stage, application=application):
                try:
                    files = config_files(stage, application, values)
                except ValueError as exc:
                    self.assertIn(
                        "Flattened configuration filename collision", str(exc)
                    )
                    continue
                successful += 1
                for name, content in files.items():
                    self.assertNotIn(b"[[", content, name)
        self.assertGreater(successful, 50)
        for name in ("camel_init", "camel_supervisord"):
            content = render_file(ROOT / "files" / name, values)
            self.assertNotIn(b"[[", content)


if __name__ == "__main__":
    unittest.main()
