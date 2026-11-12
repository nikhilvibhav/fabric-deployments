"""Optional Invoke entrypoints; all options are also available via python -m deployment."""

from invoke import task

from deployment.__main__ import main


def run(action, stage, roles, application):
    args = [action, "--stage", stage, "--roles", roles]
    if application:
        args += ["--application", application]
    if main(args):
        from invoke.exceptions import Exit

        raise Exit(code=1)


@task
def deploy(context, stage, roles, application=None):
    run("deploy", stage, roles, application)


@task
def plan(context, stage, roles, application=None):
    run("plan", stage, roles, application)


@task
def rollback(context, stage, roles, application=None):
    run("rollback", stage, roles, application)


@task
def start(context, stage, roles, application=None):
    run("start", stage, roles, application)


@task
def stop(context, stage, roles, application=None):
    run("stop", stage, roles, application)
