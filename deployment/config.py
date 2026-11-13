import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired, TypedDict

ROOT = Path(__file__).resolve().parents[1]
# [[name]] avoids Spring/logback ${...} and Camel {{...}}, both resolved at runtime.
PLACEHOLDER = re.compile(r"\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]")
PLACEHOLDER_BYTES = re.compile(PLACEHOLDER.pattern.encode())
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
DEFAULTS = {
    "java_bin": "/usr/bin/java",
    "camel_log_dir": "/var/log/camel",
    "jvm_options": "-Xmx128m -Xms128m -XX:+HeapDumpOnOutOfMemoryError "
    "-XX:HeapDumpPath=/var/log/camel",
    "server_port": "8080",
    "soap_api_port": "",
    "rest_api_port": "",
    "spring_profile": "default",
    "spring_source": "",
    "project_name": "",
    "project_description": "",
    "jdbc_driver_class_name": "",
    "jdbc_database_url": "",
    "jdbc_database_username": "",
    "jdbc_testWhileIdle": "",
    "jdbc_validationQuery": "",
    "jpa_show_sql": "",
    "jpa_hibernate_ddl_auto": "",
    "jpa_hibernate_naming_strategy": "",
    "jpa_properties_hibernate_dialect": "",
    "jpa_properties_datasource_maxwait": "",
    "jpa_properties_datasource_maxactive": "",
    "jpa_properties_datasource_test_on_borrow": "",
}
SECRET_KEYS = {"jdbc_database_password", "mongodb_password", "big_username"}

Values = dict[str, Any]


class Settings(TypedDict):
    """Stage-wide settings from an inventory file, with defaults filled in."""

    keep_releases: int
    use_supervisor: bool
    gelf_host: NotRequired[str]


def boolean(value: object) -> bool:
    """Parse a strict true/false option value."""
    if str(value).lower() not in {"true", "false"}:
        raise ValueError("Boolean parameters must be true or false")
    return str(value).lower() == "true"


def relative_path(value: str, label: str) -> str:
    """Validate and return a safe slash-separated relative path."""
    if not value or any(
        not SEGMENT.fullmatch(part) or part in {".", ".."} for part in value.split("/")
    ):
        raise ValueError(
            f"Invalid {label}: expected a relative artifact/application path"
        )
    return value


@dataclass(frozen=True)
class Host:
    """An inventory host and the deployment roles assigned to it."""

    address: str
    roles: tuple[str, ...]


def environment(stage: str, root: Path = ROOT) -> tuple[Settings, list[Host]]:
    """Load and validate stage settings and host inventory."""
    if not SEGMENT.fullmatch(stage) or stage in {".", ".."}:
        raise ValueError("Invalid stage")
    path = root / "deployment" / "inventories" / f"{stage}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Unknown stage: {stage}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid inventory {path.name}: {exc}") from None
    if not isinstance(document, dict) or set(document) != {"settings", "hosts"}:
        raise ValueError(f"Invalid inventory structure in {path.name}")
    supplied_settings = document["settings"]
    if (
        not isinstance(supplied_settings, dict)
        or set(supplied_settings) - {"gelf_host", "keep_releases", "use_supervisor"}
        or type(supplied_settings.get("keep_releases", 1)) is not int
        or supplied_settings.get("keep_releases", 1) < 1
        or not isinstance(supplied_settings.get("use_supervisor", False), bool)
        or (
            "gelf_host" in supplied_settings
            and not isinstance(supplied_settings["gelf_host"], str)
        )
    ):
        raise ValueError(f"Invalid environment settings in {path.name}")
    settings: Settings = {
        "keep_releases": 1,
        "use_supervisor": False,
        **supplied_settings,
    }
    hosts = []
    for item in document["hosts"] if isinstance(document["hosts"], list) else ():
        if (
            not isinstance(item, dict)
            or set(item) != {"address", "roles"}
            or not isinstance(item["address"], str)
            or not item["address"]
            or not isinstance(item["roles"], list)
            or not item["roles"]
            or any(
                not isinstance(role, str) or not SEGMENT.fullmatch(role)
                for role in item["roles"]
            )
        ):
            raise ValueError(f"Invalid host entry in {path.name}")
        hosts.append(Host(item["address"], tuple(item["roles"])))
    if not hosts:
        raise ValueError(f"No hosts configured for {stage}")
    return settings, hosts


def select_hosts(hosts: list[Host], roles: str) -> list[str]:
    """Return addresses for hosts matching the requested comma-separated roles."""
    wanted = set(roles.split(","))
    known = {role for host in hosts for role in host.roles}
    if not wanted or not wanted <= known:
        raise ValueError(f"Unknown roles: {', '.join(sorted(wanted - known))}")
    return [host.address for host in hosts if wanted.intersection(host.roles)]


def variables(
    stage: str,
    application: str,
    version: str,
    second: bool,
    settings: Settings,
    vars_file: str | None = None,
    environ: Mapping[str, str] | None = None,
    secret_values: dict[str, str] | None = None,
) -> Values:
    """Merge defaults, file, environment, and secret values into deployment variables."""
    environ = os.environ if environ is None else environ
    values = dict(DEFAULTS)
    values.update({key: settings[key] for key in ("gelf_host",) if key in settings})
    if vars_file:
        supplied = json.loads(Path(vars_file).read_text())
        if not isinstance(supplied, dict) or any(
            not isinstance(value, (str, int, float, bool))
            for value in supplied.values()
        ):
            raise ValueError(
                "Variables file must contain a JSON object of scalar values"
            )
        values.update(supplied)
    environment_keys = set(DEFAULTS) | SECRET_KEYS | {"gelf_host"}
    for key in environment_keys:
        if key in environ:
            values[key] = environ[key]
    for key, value in (secret_values or {}).items():
        # Refuse ambiguity rather than letting a stale job variable shadow a rotated secret.
        if key in environment_keys and key in environ:
            raise ValueError(
                f"{key} is set both as an encrypted secret and an environment variable"
            )
        values[key] = value
    if "java_opts" in environ and "jvm_options" not in environ:
        values["jvm_options"] = environ["java_opts"]
    options = str(values["jvm_options"])
    if len(options) >= 2 and options[0] == options[-1] and options[0] in "'\"":
        options = options[1:-1]
    values["jvm_options"] = options
    relative_path(application, "application")
    if not SEGMENT.fullmatch(version) or version in {".", ".."}:
        raise ValueError("Invalid version")
    if second and version == "latest":
        raise ValueError("A second artifact requires an explicit version")
    values.update(
        stage=stage,
        application=application,
        version=version,
        application_name=PurePosixPath(application).name,
        application_execute=PurePosixPath(application).name
        + (f"-{version}" if second else ""),
        deploy_to=f"/opt/camel/{application}" + (f"-{version}" if second else ""),
    )
    for key in ("java_bin", "camel_log_dir"):
        if not re.fullmatch(r"/[A-Za-z0-9_/.-]+", str(values[key])) or ".." in str(
            values[key]
        ).split("/"):
            raise ValueError(f"{key} must be an absolute path without whitespace")
    if any(character in options for character in "\r\n"):
        raise ValueError("JVM options cannot contain line breaks")
    return values


def render(text: str, values: Mapping[str, Any]) -> str:
    """Substitute deployment placeholders once, failing for missing values."""

    def replace(match: re.Match[str]) -> str:
        """Resolve one placeholder from the supplied variable mapping."""
        key = match.group(1)
        if key not in values:
            raise ValueError(f"Missing template variable: {key}")
        return str(values[key])

    return PLACEHOLDER.sub(replace, text)


def utf8(data: bytes, path: Path) -> str:
    """Decode renderable template bytes as UTF-8 with a path-aware error."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            f"{path.name} is not valid UTF-8 and cannot be rendered"
        ) from None


def render_file(path: Path, values: Mapping[str, Any]) -> bytes:
    """Render a template file while preserving untouched files byte-for-byte."""
    data = path.read_bytes()
    if not PLACEHOLDER_BYTES.search(data):
        return data
    return render(utf8(data, path), values).encode("utf-8")


def config_files(
    stage: str, application: str, values: Mapping[str, Any], root: Path = ROOT
) -> dict[str, bytes]:
    """Collect, flatten, and render configuration files for an application."""
    source = root / "files" / stage / application
    if not source.is_dir():
        raise ValueError(f"Configuration directory is missing: {stage}/{application}")
    files = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".erb":
            raise ValueError(f"{path.name} has an unsupported template suffix")
        if path.suffix not in {".properties", ".xml", ".yaml", ".yml"}:
            continue
        name = path.name
        if name in files:
            raise ValueError(f"Flattened configuration filename collision: {name}")
        files[name] = render_file(path, values)
    for name in ("application.properties", "logback.xml", "logback-access.xml"):
        if name not in files:
            files[name] = render_file(root / "files" / name, values)
    return files


class _Recorder(dict[str, str]):
    """Renders every placeholder as empty while recording its name."""

    def __init__(self) -> None:
        """Initialize an empty variable-name recorder."""
        super().__init__()
        self.names: set[str] = set()

    def __contains__(self, key: object) -> bool:
        """Treat every name as available so rendering can enumerate placeholders."""
        return True

    def __getitem__(self, key: str) -> str:
        """Record a requested placeholder and return its blank replacement."""
        self.names.add(key)
        return ""


def template_names(
    stage: str, application: str, root: Path = ROOT
) -> tuple[set[str], dict[str, bytes]]:
    """Placeholder names and blank-rendered files, selected exactly as deploy does."""
    recorder = _Recorder()
    files = config_files(stage, application, recorder, root)
    return recorder.names, files


def shared_files(stage: str, application: str, root: Path = ROOT) -> dict[str, bytes]:
    """Read application-level shared files that must persist across releases."""
    source = root / "files" / stage / application
    return {path.stem: path.read_bytes() for path in sorted(source.glob("*.shared"))}
