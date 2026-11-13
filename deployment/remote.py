import io
import json
import logging
import re
import shlex
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from fabric import Connection
from invoke.exceptions import CommandTimedOut, UnexpectedExit
from invoke.runners import Result
from paramiko import RejectPolicy

from .config import ROOT, Values, render_file


def q(value: object) -> str:
    """Shell-quote a value before including it in a remote command."""
    return shlex.quote(str(value))


class Remote:
    """Manage a locked application deployment on one SSH host."""

    def __init__(
        self,
        host: str,
        values: Values,
        supervisor: bool,
        health_url: str | None = None,
    ) -> None:
        """Configure the SSH connection and service details for a host."""
        self.connection = Connection(host, connect_timeout=20)
        self.connection.client.load_system_host_keys()
        self.connection.client.set_missing_host_key_policy(RejectPolicy())
        self.values = values
        self.supervisor = supervisor
        self.health_url = health_url
        self.base = values["deploy_to"]
        self.service = values["application_execute"]
        self.service_file = (
            f"/etc/supervisord.d/{self.service}.conf"
            if supervisor
            else f"/etc/init.d/{self.service}"
        )
        self.locked = False

    def run(self, command: str, **kwargs: Any) -> Result:
        """Run a remote command and log its context without exposing command output."""
        try:
            return self.connection.run(command, hide=True, timeout=120, **kwargs)
        except (UnexpectedExit, CommandTimedOut):
            # Surface the operation, but not service output that may contain secrets.
            logging.getLogger("camel-deploy").error(
                "Remote command failed on %s: %s", self.connection.host, command
            )
            raise

    def put(self, content: bytes, path: str) -> None:
        """Upload in-memory bytes to a remote path."""
        self.connection.put(io.BytesIO(content), remote=path)

    def exists(self, path: str) -> bool:
        """Return whether a remote path exists."""
        return self.run(f"test -e {q(path)}", warn=True).ok

    def lock(self) -> None:
        """Create the host's deployment lock and check service-manager conflicts."""
        # mkdir is also an atomic cross-job lock; never steal a stale lock automatically.
        self.run(f"mkdir -p {q(self.base + '/releases')} {q(self.base + '/shared')}")
        self.run(f"mkdir {q(self.base + '/.deploy-lock')}")
        self.locked = True
        if self.supervisor and self.exists(f"/etc/init.d/{self.service}"):
            raise ValueError(
                "Both service managers may be configured; migrate the SysV service explicitly first"
            )

    def close(self) -> None:
        """Release the deployment lock and close the SSH connection."""
        try:
            if self.locked:
                self.run(f"rmdir {q(self.base + '/.deploy-lock')}")
        finally:
            self.connection.close()

    def current(self) -> str | None:
        """Return the validated active release path, or None if none is active."""
        path = self.base + "/current"
        if not self.run(f"test -L {q(path)}", warn=True).ok:
            if self.exists(path):
                raise ValueError("Current deployment must be a symbolic link")
            return None
        result = self.run(f"readlink -f {q(path)}", warn=True)
        if not result.ok or not self.exists(path):
            raise ValueError("Current deployment is a broken symbolic link")
        target = result.stdout.strip()
        self.validate_release(target)
        return target

    def validate_release(self, path: str) -> None:
        """Ensure a release path is a direct child of this application's releases."""
        parent = str(PurePosixPath(path).parent)
        if parent != self.base + "/releases" or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", PurePosixPath(path).name
        ):
            raise ValueError(
                "Release path is outside this application's releases directory"
            )

    def prepare(
        self,
        bundle: Path,
        release: str,
        metadata: dict[str, str],
        shared: dict[str, bytes],
    ) -> dict[str, Any]:
        """Upload a release and stage its service, shared files, and rollback state."""
        self.validate_release(release)
        self.run(f"mkdir {q(release)} && mkdir -m 700 {q(release + '/.deployment')}")
        for path in sorted(bundle.rglob("*")):
            relative = path.relative_to(bundle).as_posix()
            remote = release + "/" + relative
            if path.is_dir():
                self.run(f"mkdir -p {q(remote)}")
            else:
                self.connection.put(str(path), remote=remote)
                self.run(
                    f"chmod {'750' if path.stat().st_mode & 0o111 else '640'} {q(remote)}"
                )
        previous = self.current()
        service_existed = self.exists(self.service_file)
        if service_existed:
            self.run(
                f"sudo -n cp -p {q(self.service_file)} {q(release + '/.deployment/service.previous')}"
            )
        shared_existed: dict[str, bool] = {}
        for name, content in shared.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
                raise ValueError("Invalid shared filename")
            shared_existed[name] = self.exists(self.base + "/shared/" + name)
            if shared_existed[name]:
                self.run(
                    f"cp -p {q(self.base + '/shared/' + name)} "
                    f"{q(release + '/.deployment/shared.previous.' + name)}"
                )
            self.put(content, release + "/.deployment/shared.next." + name)
            self.run(f"chmod 600 {q(release + '/.deployment/shared.next.' + name)}")
        state = dict(
            previous=previous,
            service_existed=service_existed,
            service_file=self.service_file,
            supervisor=self.supervisor,
            shared_existed=shared_existed,
            **metadata,
        )
        self.put(json.dumps(state).encode(), release + "/.deployment/state.json")
        self.run(f"chmod 600 {q(release + '/.deployment/state.json')}")
        self.run(f"sudo -n chgrp -R camel {q(release)} && chmod 750 {q(release)}")
        service_values = dict(self.values, release_path=release)
        template = (
            ROOT / "files" / ("camel_supervisord" if self.supervisor else "camel_init")
        )
        # Shell/INI service definitions run on Linux; never ship Windows line endings.
        text = (
            render_file(template, service_values).decode("utf-8").replace("\r\n", "\n")
        )
        self.put(text.encode(), release + "/.deployment/service.next")
        self.run(f"chmod 600 {q(release + '/.deployment/service.next')}")
        self.run(
            f"sudo -n install -d -o camel -g camel -m 775 "
            f"{q(self.values['camel_log_dir'] + '/' + self.service)}"
        )
        return state

    def switch(self, release: str) -> None:
        """Atomically point the current symlink at a validated release."""
        self.validate_release(release)
        temporary = self.base + "/.current-" + uuid.uuid4().hex
        self.run(
            f"ln -s {q(release)} {q(temporary)} && mv -Tf {q(temporary)} {q(self.base + '/current')}"
        )

    def activate(self, release: str) -> None:
        """Install staged files, switch releases, restart the service, and verify health."""
        state = self.load_state(release)
        for name in state["shared_existed"]:
            self.run(
                f"install -m 640 {q(release + '/.deployment/shared.next.' + name)} "
                f"{q(self.base + '/shared/' + name)}"
            )
            self.run(f"sudo -n chgrp camel {q(self.base + '/shared/' + name)}")
        mode = "644" if self.supervisor else "755"
        self.run(
            f"sudo -n install -m {mode} {q(release + '/.deployment/service.next')} "
            f"{q(self.service_file)}"
        )
        self.switch(release)
        if self.supervisor:
            self.run("sudo -n supervisorctl reread")
            self.run(f"sudo -n supervisorctl update {q(self.service)}")
            self.lifecycle("restart")
        else:
            self.run("sudo -n systemctl daemon-reload")
            self.lifecycle("restart")
        self.healthy()

    def lifecycle(self, action: str) -> None:
        """Apply a service lifecycle action, avoiding redundant known transitions."""
        if self.supervisor:
            status = self.run(
                f"sudo -n supervisorctl status {q(self.service)}", warn=True
            )
            running = bool(re.search(r"\bRUNNING\b", status.stdout))
            if action == "start" and running:
                return
            if action == "stop" and re.search(r"\b(STOPPED|EXITED)\b", status.stdout):
                return
            if action == "stop" and "ERROR (no such process)" in status.stdout:
                return
            self.run(f"sudo -n supervisorctl {action} {q(self.service)}")
        else:
            self.run(f"sudo -n /sbin/service {q(self.service)} {action}")

    def healthy(self) -> None:
        """Wait for the service and optional HTTP endpoint to become healthy."""
        for attempt in range(12):
            if self.supervisor:
                status = self.run(
                    f"sudo -n supervisorctl status {q(self.service)}", warn=True
                )
                okay = status.ok and bool(re.search(r"\bRUNNING\b", status.stdout))
            else:
                okay = self.run(
                    f"sudo -n /sbin/service {q(self.service)} status", warn=True
                ).ok
            if okay and self.health_url:
                okay = self.run(
                    "code=$(curl --fail --silent --show-error --max-time 5 "
                    f"--output /dev/null --write-out '%{{http_code}}' {q(self.health_url)}) "
                    '&& case "$code" in 2??) exit 0 ;; *) exit 1 ;; esac',
                    warn=True,
                ).ok
            if okay:
                return
            if attempt < 11:
                time.sleep(5)
        raise RuntimeError("Service did not become healthy within the startup window")

    def load_state(self, release: str) -> dict[str, Any]:
        """Load and validate rollback metadata for a release."""
        self.validate_release(release)
        state = json.loads(
            self.run(f"cat {q(release + '/.deployment/state.json')}").stdout
        )
        if (
            state["service_file"] != self.service_file
            or state["supervisor"] != self.supervisor
        ):
            raise ValueError("Rollback service manager does not match the release")
        if state["previous"]:
            self.validate_release(state["previous"])
            if not self.exists(state["previous"]):
                raise ValueError("Previous release no longer exists")
        return state

    def restore(self, release: str, state: dict[str, Any]) -> None:
        """Restore the prior service definition, shared files, and active release."""
        if state["service_existed"]:
            self.run(
                f"sudo -n cp -p {q(release + '/.deployment/service.previous')} {q(self.service_file)}"
            )
        else:
            if self.exists(self.service_file):
                self.lifecycle("stop")
            self.run(f"sudo -n rm -f {q(self.service_file)}")
        for name, existed in state.get("shared_existed", {}).items():
            destination = self.base + "/shared/" + name
            if existed:
                self.run(
                    f"cp -p {q(release + '/.deployment/shared.previous.' + name)} {q(destination)}"
                )
            else:
                self.run(f"rm -f {q(destination)}")
        if state["previous"]:
            self.switch(state["previous"])
        else:
            self.run(f"rm -f {q(self.base + '/current')}")
        if self.supervisor:
            self.run("sudo -n supervisorctl reread")
            self.run(f"sudo -n supervisorctl update {q(self.service)}")
        else:
            self.run("sudo -n systemctl daemon-reload")
        if state["service_existed"]:
            self.lifecycle("restart")
            self.healthy()

    def cleanup(self, keep: int) -> None:
        """Prune old managed releases while retaining current and rollback targets."""
        # Restrict pruning to releases created by this deployment tool.
        result = self.run(
            f"find {q(self.base + '/releases')} -mindepth 2 -maxdepth 2 "
            "-type d -name .deployment -printf '%h\\n'"
        )
        current = self.current()
        protected = {current}
        if current:
            protected.add(self.load_state(current)["previous"])
        releases = sorted(result.stdout.splitlines(), reverse=True)
        retained = set(releases[: max(keep, 2)]) | protected
        for release in releases:
            self.validate_release(release)
            if release not in retained:
                self.run(f"rm -rf -- {q(release)}")
