import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import secrets as secrets_cli
from .artifacts import Repository, valid_url
from .config import (
    Values,
    boolean,
    config_files,
    environment,
    select_hosts,
    shared_files,
    variables,
)
from .secrets import KEY_ENV, decrypt_all, read_store

LOG = logging.getLogger("camel-deploy")
DEFAULT_REPOSITORY = "http://10.90.135.56:8081/artifactory/simple"


def release_type_for_version(version: str, requested: str | None = None) -> str:
    """Infer a release channel from the version and reject conflicting overrides."""
    inferred = "snapshot" if version.endswith("-SNAPSHOT") else "stable"
    if requested is not None and requested != inferred:
        raise ValueError(
            f"Release type for version {version!r} is {inferred}; "
            f"--release-type {requested} conflicts with the version suffix"
        )
    return inferred


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
    result.add_argument("--repository", default=os.environ.get("artifactory_base_url"))
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
    release_type = release_type_for_version(args.version, args.release_type)
    base = args.repository or (
        DEFAULT_REPOSITORY
        + (
            "/libs-snapshot-local/com/rmg"
            if release_type == "snapshot"
            else "/libs-release-local/com/rmg"
        )
    )
    artifact_url = args.artifact_url or repository.resolve(
        base,
        os.environ.get("path_to_jar") or values["application"],
        args.version,
        ".jar",
    )
    if not urlsplit(artifact_url).path.lower().endswith(".jar"):
        raise ValueError("The artifact URL must point to a JAR file")
    artifact = workspace / "artifact"
    digest = repository.download(artifact_url, artifact, args.sha256)
    metadata = {"artifact_url": artifact_url, "sha256": digest}
    artifact.rename(bundle / (values["application_name"] + ".jar"))
    if (bundle / ".deployment").exists():
        raise ValueError("Archive contains reserved deployment paths")
    return bundle, metadata


def execute(args: argparse.Namespace) -> None:
    """Validate inputs, then plan or carry out the requested remote operation."""
    args.release_type = release_type_for_version(args.version, args.release_type)
    settings, hosts = environment(args.stage)
    selected = select_hosts(hosts, args.roles)
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
    if args.action == "plan":
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "application": args.application,
                    "hosts": selected,
                    "version": args.version,
                    "release_type": args.release_type,
                    "deploy_to": values["deploy_to"],
                    "service_manager": "supervisor"
                    if settings["use_supervisor"]
                    else "sysv",
                    "health_check": "HTTP + process"
                    if args.health_url
                    else "process only",
                    "secrets": {
                        "names": secret_names,
                        "decrypted": bool(decrypt and secret_names),
                    },
                    "templates": "[[name]] only",
                },
                indent=2,
            )
        )
        return
    from .remote import Remote

    remotes = []
    activated = []
    failure = None
    published = False
    try:
        # Acquire every selected host lock before publishing any release.
        for host in selected:
            remote = Remote(host, values, settings["use_supervisor"], args.health_url)
            remotes.append(remote)
            LOG.info("Acquiring deployment lock on %s", host)
            remote.lock()
        if args.action != "deploy":
            states = []
            if args.action == "rollback":
                for remote in remotes:
                    current = remote.current()
                    if not current:
                        raise ValueError("No current release to roll back")
                    state = remote.load_state(current)
                    if not state["previous"]:
                        raise ValueError("No previous release available")
                    states.append((remote, current, state))
            for remote in remotes:
                if args.action == "rollback":
                    _, current, state = next(
                        item for item in states if item[0] is remote
                    )
                    remote.restore(current, state)
                else:
                    remote.lifecycle(args.action)
                    if args.action == "start":
                        remote.healthy()
                LOG.info("%s succeeded on %s", args.action, remote.connection.host)
            return
        release_id = (
            datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        release = values["deploy_to"] + "/releases/" + release_id
        with tempfile.TemporaryDirectory(prefix="camel-deploy-") as temporary:
            workspace = Path(temporary)
            bundle, metadata = prepare_bundle(args, values, workspace)
            for remote in remotes:
                # Release paths are deliberately identical on every selected host.
                config = bundle / "config"
                config.mkdir(exist_ok=True)
                for name, content in config_files(
                    args.stage, args.application, dict(values, release_path=release)
                ).items():
                    (config / name).write_bytes(content)
                state = remote.prepare(
                    bundle,
                    release,
                    metadata,
                    shared_files(args.stage, args.application),
                )
                activated.append((remote, release, state))
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
            remote.cleanup(settings["keep_releases"])
        LOG.info("Deployment succeeded: %s", release_id)
    except Exception as exc:
        failure = exc
        # Restore all published hosts, including the one whose activation failed.
        for remote, release, state in reversed(activated):
            try:
                remote.restore(release, state)
                LOG.error("Restored previous deployment on %s", remote.connection.host)
            except Exception:
                LOG.error(
                    "ROLLBACK FAILED on %s; manual recovery required",
                    remote.connection.host,
                )
        if published:
            LOG.error(
                "Release was published and remains active; post-deployment cleanup failed"
            )
        raise
    finally:
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
        if close_errors and failure is None:
            raise RuntimeError(
                "Deployment locks could not be released; manual inspection required"
            )


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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
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
