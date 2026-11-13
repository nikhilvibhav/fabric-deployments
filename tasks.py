"""Optional Invoke entrypoints; all options are also available via python -m deployment."""

from invoke import Context, task

from deployment.__main__ import main


def run(action: str, stage: str, roles: str, application: str | None) -> None:
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
    run("deploy", stage, roles, application)


@task
def plan(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    run("plan", stage, roles, application)


@task
def rollback(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    run("rollback", stage, roles, application)


@task
def start(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    run("start", stage, roles, application)


@task
def stop(
    context: Context, stage: str, roles: str, application: str | None = None
) -> None:
    run("stop", stage, roles, application)
