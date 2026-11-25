"""Command-line entry point: ``python -m deployment <action> --stage ... --roles ...``.

Actions:
  plan      Validate inputs and templates offline and print a JSON summary.
  deploy    Lock every selected host, download and verify the JAR, render
            configuration, upload and activate the release on each host, then
            prune old releases. Hosts that were already activated are restored
            if any host fails.
  rollback  Restore each host to the release that preceded its current one.
  start/stop  Run the service lifecycle action on each selected host.

``python -m deployment secrets ...`` is passed to :mod:`deployment.secrets`.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlsplit

from . import secrets as secrets_cli
from .artifacts import Repository, valid_url
from .config import (
    Settings,
    Values,
    boolean,
    config_files,
    environment,
    select_hosts,
    shared_files,
    variables,
)
from .secrets import KEY_ENV, decrypt_all, read_store

if TYPE_CHECKING:
    from .remote import Remote

LOG = logging.getLogger("camel-deploy")
# ANSI colour codes for level names; messages stay uncoloured.
LEVEL_COLOURS = {
    logging.DEBUG: "2",
    logging.INFO: "34",
    logging.WARNING: "33",
    logging.ERROR: "31",
    logging.CRITICAL: "1;31",
}


class LevelColourFormatter(logging.Formatter):
    """Format records as 'LEVEL: message', optionally colouring the level name."""

    def __init__(self, colour: bool) -> None:
        """Choose whether level names are wrapped in ANSI colour codes."""
        super().__init__("%(levelname)s: %(message)s")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        """Colour only the level prefix so messages stay plain and copyable."""
        text = super().format(record)
        code = LEVEL_COLOURS.get(record.levelno)
        if not self.colour or not code:
            return text
        level = record.levelname
        return f"\033[{code}m{level}\033[0m{text[len(level) :]}"


def use_colour(stream: TextIO, environ: Mapping[str, str]) -> bool:
    """Colour terminals by default; NO_COLOR disables and FORCE_COLOR enables it."""
    if environ.get("NO_COLOR"):
        return False
    if environ.get("FORCE_COLOR", "0") != "0":
        return True
    return stream.isatty() and environ.get("TERM") != "dumb"


def release_type_for_version(version: str, requested: str | None = None) -> str:
    """Infer a release channel from the version and reject conflicting overrides."""
    inferred = "snapshot" if version.endswith("-SNAPSHOT") else "stable"
    if requested is not None and requested != inferred:
        raise ValueError(
            f"Release type for version {version!r} is {inferred}; "
            f"--release-type {requested} conflicts with the version suffix"
        )
    return inferred


def repository_base(
    repository: str | None, settings: Settings, release_type: str
) -> str | None:
    """Return --repository, or the stage's snapshot/release repository from its inventory."""
    if repository:
        return repository
    if "artifactory_url" not in settings:
        return None
    name = "libs-snapshot-local" if release_type == "snapshot" else "libs-release-local"
    parts = [settings["artifactory_url"].rstrip("/"), name]
    if "artifact_group" in settings:
        parts.append(settings["artifact_group"])
    return "/".join(parts)


def check_artifact_scheme(args: argparse.Namespace, settings: Settings) -> None:
    """Refuse plain-HTTP artifact URLs unless the stage's inventory allows them."""
    source = args.artifact_url or args.repository
    if urlsplit(source).scheme != "http":
        return
    if not settings.get("artifactory_allow_http", False):
        raise ValueError(
            f"{source} uses plain HTTP, which would expose the JAR and any "
            'Artifactory credentials in transit; set "artifactory_allow_http": true '
            f"in deployment/inventories/{args.stage}.json to allow it"
        )
    if args.action == "deploy":
        LOG.warning(
            "Downloading over plain HTTP, as allowed for %s; pass --sha256 to "
            "verify the JAR",
            args.stage,
        )


def parser() -> argparse.ArgumentParser:
    """Build the argument parser for deployment and lifecycle commands."""
    result = argparse.ArgumentParser(description="Fabric deployment CLI")
    result.add_argument(
        "action", choices=("plan", "deploy", "rollback", "start", "stop")
    )
    result.add_argument("--stage", required=True)
    result.add_argument(
        "--roles", required=True, help="Comma-separated inventory roles"
    )
    result.add_argument("--application", default=os.environ.get("application"))
    result.add_argument("--version", default=os.environ.get("version", "latest"))
    result.add_argument(
        "--second-artifact",
        default=os.environ.get("deploy_second_artifact", "false"),
        type=boolean,
    )
    result.add_argument(
        "--release-type",
        choices=("snapshot", "stable"),
        help="Optional consistency check; inferred from the version suffix",
    )
    result.add_argument("--vars-file")
    result.add_argument("--artifact-url", default=os.environ.get("artifact_url"))
    result.add_argument("--sha256", default=os.environ.get("artifact_sha256"))
    result.add_argument(
        "--repository",
        default=os.environ.get("artifactory_base_url"),
        help="Repository to resolve versions in (default: from the stage inventory)",
    )
    result.add_argument("--health-url", default=os.environ.get("health_url"))
    result.add_argument(
        "--secrets-key",
        default=os.environ.get(KEY_ENV),
        help=f"Stage private key file (default: ${KEY_ENV})",
    )
    return result


def prepare_bundle(
    args: argparse.Namespace, values: Values, workspace: Path
) -> tuple[Path, dict[str, str]]:
    """Download and verify the application artifact into a release bundle."""
    bundle = workspace / "bundle"
    bundle.mkdir()
    repository = Repository()
    if not args.artifact_url and not args.repository:
        raise ValueError("Pass --artifact-url or --repository")
    artifact_url = args.artifact_url or repository.resolve(
        args.repository,
        os.environ.get("path_to_jar") or values["application"],
        args.version,
        ".jar",
    )
    if not urlsplit(artifact_url).path.lower().endswith(".jar"):
        raise ValueError("The artifact URL must point to a JAR file")
    artifact = workspace / "artifact"
    LOG.info("Downloading artifact %s", artifact_url)
    digest = repository.download(artifact_url, artifact, args.sha256)
    if args.sha256:
        LOG.info("Artifact matches the expected SHA-256")
    metadata = {"artifact_url": artifact_url, "sha256": digest}
    artifact.rename(bundle / (values["application_name"] + ".jar"))
    if (bundle / ".deployment").exists():
        raise ValueError("Archive contains reserved deployment paths")
    return bundle, metadata


@dataclass(frozen=True)
class Context:
    """Validated inputs shared by every action."""

    args: argparse.Namespace
    settings: Settings
    hosts: list[str]
    values: Values
    secret_names: list[str]
    decrypted: bool


def prepare_context(args: argparse.Namespace) -> Context:
    """Validate inputs and build deployment variables before any connection."""
    args.release_type = release_type_for_version(args.version, args.release_type)
    settings, hosts = environment(args.stage)
    selected = select_hosts(hosts, args.roles)
    args.repository = repository_base(args.repository, settings, args.release_type)
    if args.action in {"plan", "deploy"}:
        if not (args.artifact_url or args.repository):
            raise ValueError(
                f"No artifact repository for {args.stage}: set artifactory_url in "
                f"deployment/inventories/{args.stage}.json, or pass --repository "
                "or --artifact-url"
            )
        check_artifact_scheme(args, settings)
    secret_names = sorted(read_store(args.stage, args.application))
    # Plan without a key stays offline-safe; deploy always requires decryption.
    decrypt = args.action == "deploy" or (args.action == "plan" and args.secrets_key)
    secret_values = (
        decrypt_all(args.stage, args.application, args.secrets_key) if decrypt else {}
    )
    values = variables(
        args.stage,
        args.application,
        args.version,
        args.second_artifact,
        settings,
        args.vars_file,
        secret_values=secret_values,
    )
    if secret_names and args.action == "plan" and not decrypt:
        # Placeholders let templates be checked without the key; never deployed.
        values.update(
            {name: "<encrypted>" for name in secret_names if name not in values}
        )
    if args.health_url:
        valid_url(args.health_url)
    if args.action in {"plan", "deploy"}:
        # Validate every selected template before making any remote change.
        config_values = dict(
            values, release_path=values["deploy_to"] + "/releases/validation"
        )
        config_files(args.stage, args.application, config_values)
    return Context(args, settings, selected, values, secret_names, bool(decrypt))


def print_plan(context: Context) -> None:
    """Print the offline plan as JSON, without deployment variables."""
    args = context.args
    plan = {
        "stage": args.stage,
        "application": args.application,
        "hosts": context.hosts,
        "version": args.version,
        "release_type": args.release_type,
        "artifact": args.artifact_url or args.repository,
        "deploy_to": context.values["deploy_to"],
        "service_manager": "supervisor"
        if context.settings["use_supervisor"]
        else "sysv",
        "health_check": "HTTP + process" if args.health_url else "process only",
        "secrets": {
            "names": context.secret_names,
            "decrypted": context.decrypted and bool(context.secret_names),
        },
        "templates": "[[name]] only",
    }
    print(json.dumps(plan, indent=2))


@contextmanager
def locked_remotes(context: Context) -> Iterator[list["Remote"]]:
    """Lock every selected host before yielding; always release in reverse order."""
    from .remote import Remote

    remotes: list[Remote] = []
    failed = False
    try:
        for host in context.hosts:
            remote = Remote(
                host,
                context.values,
                context.settings["use_supervisor"],
                context.args.health_url,
                context.settings["health_timeout"],
            )
            remotes.append(remote)
            LOG.info("Acquiring deployment lock on %s", host)
            remote.lock()
        yield remotes
    except Exception:
        failed = True
        raise
    finally:
        release_locks(remotes, raise_on_error=not failed)


def release_locks(remotes: list["Remote"], raise_on_error: bool) -> None:
    """Close every connection; fail only if nothing else has already failed."""
    close_errors = []
    for remote in reversed(remotes):
        try:
            remote.close()
        except Exception as exc:
            LOG.error(
                "Could not release lock/close connection on %s: %s",
                remote.connection.host,
                type(exc).__name__,
            )
            close_errors.append(exc)
    if close_errors and raise_on_error:
        raise RuntimeError(
            "Deployment locks could not be released; manual inspection required"
        )


def rollback(context: Context, remotes: list["Remote"]) -> None:
    """Return every host to its previous release, checking all hosts first."""
    states = []
    for remote in remotes:
        current = remote.current()
        if not current:
            raise ValueError("No current release to roll back")
        state = remote.load_state(current)
        if not state["previous"]:
            raise ValueError("No previous release available")
        states.append((remote, current, state))
    for remote, current, state in states:
        remote.restore(current, state)
        LOG.info("rollback succeeded on %s", remote.connection.host)


def lifecycle(context: Context, remotes: list["Remote"]) -> None:
    """Start or stop the service on every host; starting also waits for health."""
    action = context.args.action
    for remote in remotes:
        remote.lifecycle(action)
        if action == "start":
            remote.healthy()
        LOG.info("%s succeeded on %s", action, remote.connection.host)


def build_bundle(
    context: Context, workspace: Path, release: str
) -> tuple[Path, dict[str, str], dict[str, bytes]]:
    """Download the artifact and render configuration once for every host."""
    args = context.args
    bundle, metadata = prepare_bundle(args, context.values, workspace)
    # Release paths are deliberately identical on every selected host, so
    # configuration is rendered once for all of them.
    config = bundle / "config"
    config.mkdir(exist_ok=True)
    for name, content in config_files(
        args.stage, args.application, dict(context.values, release_path=release)
    ).items():
        (config / name).write_bytes(content)
    return bundle, metadata, shared_files(args.stage, args.application)


def restore_activated(
    activated: list[tuple["Remote", dict[str, Any]]], release: str
) -> None:
    """Undo activated hosts newest first, continuing past hosts that fail."""
    for remote, state in reversed(activated):
        try:
            remote.restore(release, state)
            LOG.error("Restored previous deployment on %s", remote.connection.host)
        except Exception:
            LOG.error(
                "ROLLBACK FAILED on %s; manual recovery required",
                remote.connection.host,
            )


def deploy(context: Context, remotes: list["Remote"]) -> None:
    """Publish a new release host by host, restoring every host if one fails."""
    args = context.args
    release_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    release = context.values["deploy_to"] + "/releases/" + release_id
    activated: list[tuple[Remote, dict[str, Any]]] = []
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="camel-deploy-") as temporary:
            bundle, metadata, shared = build_bundle(context, Path(temporary), release)
            for remote in remotes:
                LOG.info("Uploading %s to %s", release_id, remote.connection.host)
                state = remote.prepare(bundle, release, metadata, shared)
                activated.append((remote, state))
                LOG.info("Publishing %s on %s", release_id, remote.connection.host)
                remote.activate(release)
        # Publishing is committed; pruning failure must not undo a healthy deployment.
        activated.clear()
        published = True
        LOG.info(
            "Published artifact %s with SHA-256 %s",
            metadata["artifact_url"],
            metadata["sha256"],
        )
        if args.release_type == "stable":
            LOG.info(
                "Pin this build in later stages with: --sha256 %s", metadata["sha256"]
            )
        for remote in remotes:
            remote.cleanup(context.settings["keep_releases"])
        LOG.info("Deployment succeeded: %s", release_id)
    except Exception:
        # Restore all published hosts, including the one whose activation failed.
        restore_activated(activated, release)
        if published:
            LOG.error(
                "Release was published and remains active; post-deployment cleanup failed"
            )
        raise


ACTIONS: dict[str, Callable[[Context, list["Remote"]], None]] = {
    "deploy": deploy,
    "rollback": rollback,
    "start": lifecycle,
    "stop": lifecycle,
}


def execute(args: argparse.Namespace) -> None:
    """Validate inputs, then plan or carry out the requested remote operation."""
    context = prepare_context(args)
    if args.action == "plan":
        print_plan(context)
        return
    with locked_remotes(context) as remotes:
        ACTIONS[args.action](context, remotes)


def main(argv: list[str] | None = None) -> int:
    """Dispatch the CLI command and convert expected failures to exit statuses."""
    argv = sys.argv[1:] if argv is None else argv
    commands = {"secrets": secrets_cli.run}
    if argv[:1] and argv[0] in commands:
        try:
            commands[argv[0]](argv[1:])
        except (TypeError, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0
    args = parser().parse_args(argv)
    if not args.application:
        parser().error(
            "--application or the application environment variable is required"
        )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LevelColourFormatter(use_colour(sys.stderr, os.environ)))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        execute(args)
    except Exception as exc:
        # Command failures may contain application output. Do not echo secrets in CI logs.
        LOG.error(
            "Deployment failed (%s). %s",
            type(exc).__name__,
            str(exc)
            if isinstance(exc, (ValueError, OSError, RuntimeError))
            else "Inspect the target service, deployment state and messages above.",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
