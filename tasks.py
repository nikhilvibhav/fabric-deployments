"""Optional Invoke tasks wrapping the deployment CLI (``invoke deploy --stage sample-qa --roles app1``).

Each task forwards stage, roles and application only; use ``python -m deployment``
for the full set of options.
"""

from invoke import Context, task

from deployment.__main__ import main


def run(action: str, stage: str, roles: str, application: str | None) -> None:
    """Run a deployment CLI action and exit with status 1 if it fails."""
    args = [action, "--stage", stage, "--roles", roles]
    if application:
        args += ["--application", application]
    if main(args):
        from invoke.exceptions import Exit

        raise Exit(code=1)


@task
def deploy(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    """Deploy a new release to the hosts with the given roles."""
    run("deploy", stage, roles, application)


@task
def plan(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    """Print the deployment plan without connecting to any host."""
    run("plan", stage, roles, application)


@task
def rollback(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    """Switch the hosts back to their previous release."""
    run("rollback", stage, roles, application)


@task
def start(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    """Start the application service and wait until it is healthy."""
    run("start", stage, roles, application)


@task
def stop(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    """Stop the application service."""
    run("stop", stage, roles, application)
