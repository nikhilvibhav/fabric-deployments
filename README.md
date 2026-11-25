# Fabric/Invoke Deployments

[![Lint and Unit Tests](https://github.com/nikhilvibhav/fabric-deployments/actions/workflows/tests.yaml/badge.svg)](https://github.com/nikhilvibhav/fabric-deployments/actions/workflows/tests.yaml)

This Python CLI deploys JAR artifacts from Artifactory to existing hosts over SSH/SFTP using Fabric. It deploys configuration from this repository, but it does not build microservices or require Jenkins.

## Table of contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
  - [Controller and dependencies](#controller-and-dependencies)
  - [Target hosts and SSH access](#target-hosts-and-ssh-access)
- [Install](#install)
  - [Set up a Jenkins agent](#set-up-a-jenkins-agent)
  - [Set up a local environment](#set-up-a-local-environment)
- [Use the CLI](#use-the-cli)
  - [Plan a deployment](#plan-a-deployment)
  - [Deploy from Jenkins](#deploy-from-jenkins)
  - [Use Invoke](#use-invoke)
  - [Manage deployment lifecycle](#manage-deployment-lifecycle)
- [Configure deployments](#configure-deployments)
  - [Parameters and environment variables](#parameters-and-environment-variables)
  - [Inventories and application files](#inventories-and-application-files)
  - [Sample environment](#sample-environment)
  - [Templates and placeholders](#templates-and-placeholders)
  - [Artifacts](#artifacts)
- [Manage encrypted secrets](#manage-encrypted-secrets)
  - [How secret storage works](#how-secret-storage-works)
  - [Create and manage secrets](#create-and-manage-secrets)
  - [Check and verify secrets](#check-and-verify-secrets)
  - [Rekey a stage](#rekey-a-stage)
  - [Key handling and recovery](#key-handling-and-recovery)
- [Deployment behavior and recovery](#deployment-behavior-and-recovery)
- [Contribute and verify](#contribute-and-verify)
- [License](#license)

## Overview

The deployment CLI provides `plan`, `deploy`, `rollback`, `start`, and `stop` actions. It uses stage inventories to select hosts and roles, downloads artifacts on the controller, and deploys releases and rendered configuration to the selected hosts.

The CLI does not switch branches or fetch application source. The repository ships only a [sample environment](#sample-environment); add your own stages before deploying anywhere.

## Prerequisites

### Controller and dependencies

Use Python 3.11 or newer on a maintained Linux controller or container that can reach Artifactory and the deployment hosts. CentOS 7's system Python is not a suitable controller runtime. Do not replace the system Python; use a maintained Jenkins agent or container instead.

Dependencies are managed with [uv](https://docs.astral.sh/uv/). `pyproject.toml` declares them and `uv.lock` pins exact versions.

### Target hosts and SSH access

Target hosts do not require Python. They require:

- SSH/SFTP and a POSIX shell.
- GNU coreutils and `find`.
- Java, the `camel` user/group, and the existing service manager.
- `curl` on the target if using HTTP health checks.

The SSH account must be able to create and write the application's directory under `/opt/camel`, read existing shared files, and use noninteractive `sudo -n` for installing or restoring service definitions, changing release/shared group ownership to `camel`, creating log directories, and controlling the selected service manager. Provision this access separately and have operations review it; do not grant blanket passwordless sudo.

Provide Jenkins SSH credentials through an SSH agent or SSH configuration. Add **verified** host keys to the controller's `~/.ssh/known_hosts`. Unknown or changed host keys fail closed; host key checking is not disabled. Check SSH/network compatibility with CentOS 7 during the pilot. Do not enable obsolete algorithms globally to work around a failed connection.

## Install

### Set up a Jenkins agent

Install the CLI dependencies on the Jenkins **agent** running the deployment job—not on the Jenkins controller or target hosts.

Keep the virtual environment outside the checkout. The repository may be cloned under `/tmp/workspace`, which Jenkins may clean or replace. For example, use a persistent environment at `/opt/venvs/camel-deploy`, created by an administrator and owned by the Jenkins service account. Install Python packages as that account, not with `sudo pip`.

Install uv on the agent as the Jenkins account. After cloning the repository to `/tmp/workspace`, create the environment from the lockfile:

```sh
cd /tmp/workspace
UV_PROJECT_ENVIRONMENT=/opt/venvs/camel-deploy uv sync --locked --no-dev
```

Run this once per persistent agent and rerun it when `uv.lock` changes. For ephemeral agents, bake the environment into the image or create it during agent provisioning. A global install is possible on a dedicated, managed agent, but an isolated virtual environment avoids conflicts with other jobs and OS-managed Python packages.

The commands in this README use Linux shell syntax and assume the checkout is at `/tmp/workspace`.

### Set up a local environment

Use the same locked dependency sync from the repository root to create a local environment:

```sh
uv sync --locked
```

The project is run from the checkout with `python -m deployment`; it is not installed as a package.

## Use the CLI

### Plan a deployment

Start with an offline plan:

```sh
python -m deployment plan \
  --stage sample-qa \
  --roles app1 \
  --application hello-service \
  --version 1.0.0
```

The `app1` role selects the first host of the sample `sample-qa` stage. Planning validates configuration and templates without SSH or Artifactory access. It does not prove remote connectivity or artifact availability.

A plan can run without a secrets key. It lists secret names and validates templates using placeholders. Supplying a key also checks decryption.

### Deploy from Jenkins

Disable concurrent builds of the deployment job. Remote application locks also prevent overlapping deployments made through this CLI.

In a Jenkins job or another runner, invoke the CLI:

```sh
set -eu
set +x
. /opt/venvs/camel-deploy/bin/activate
cd /tmp/workspace
python -m deployment deploy \
  --stage sample-qa \
  --roles app1 \
  --application "$application" \
  --version "$version"
```

Store secrets as encrypted repository values, not as job parameters or command-line arguments. Use the job's actual existing parameters for service, version, and host roles. Do not install tools or modify target hosts automatically during deployment.

### Use Invoke

Optional Invoke entrypoints call the same implementation:

```sh
invoke plan --stage sample-qa --roles app1 --application hello-service
invoke deploy --stage sample-qa --roles app1 --application hello-service
invoke rollback --stage sample-qa --roles app1 --application hello-service
```

Invoke obtains artifact and version settings from the environment. For all CLI options, run:

```sh
python -m deployment --help
```

### Manage deployment lifecycle

The CLI supports `rollback`, `stop`, and `start`:

```sh
python -m deployment rollback --stage sample-qa --roles app1 --application hello-service
python -m deployment stop --stage sample-qa --roles app1 --application hello-service
python -m deployment start --stage sample-qa --roles app1 --application hello-service
```

For a second artifact, use the same `--version` and `--second-artifact true` for lifecycle and rollback commands.

## Configure deployments

### Parameters and environment variables

CLI options override environment variables where applicable. `application` is required either as an option or environment variable. The version defaults to `latest`.

| Input | Behavior |
| --- | --- |
| `application`, `version` | Nested application names such as `team/orders-api` are supported. |
| Release type | Inferred from `version`: only versions ending exactly in `-SNAPSHOT` use the snapshot repository; other versions, including `latest`, use the stable repository. If supplied, `--release-type` must agree. |
| `deploy_second_artifact` | Strict `true`/`false`; appends the explicit version to the installation directory and service name. `latest` is rejected for this mode. |
| `java_bin`, `jvm_options` | Existing Java parameters. `java_opts` is an alias if `jvm_options` is absent. One matching pair of wrapping quotes is removed from JVM options. |
| DB and template variables | Existing lowercase environment variable names are retained. Required secret values have no insecure default. Missing template variables fail before connecting. Prefer [encrypted secrets](#manage-encrypted-secrets) for sensitive values. A name supplied both as an encrypted secret and an environment variable is rejected. |
| `--secrets-key`, `CAMEL_SECRETS_KEY_FILE` | Path to the stage's private key. Required for `deploy` when the application has encrypted secrets. |
| `--vars-file` | JSON object for additional template values. Environment values override known existing parameters; CLI identity fields cannot be overridden by the file. Keep secret files outside Git with restricted permissions. |
| `--stage`, `--roles` | Required explicitly. Roles are comma-separated, such as `app1,app2`. Unknown roles fail rather than deploying to every host. |
| `--health-url`, `health_url` | Optional target-local HTTP(S) endpoint, for example `http://127.0.0.1:8080/actuator/health`. URLs must not contain credentials or query strings. |

Additional artifact-related environment variables are documented in [Artifacts](#artifacts).

### Inventories and application files

Environment inventories are JSON files under `deployment/inventories/`. They declare host addresses, roles, GELF settings, release retention, service manager, health-check timeout (`health_timeout` in seconds, 5–1800, default 60), and the Artifactory location (`artifactory_url`, `artifact_group`). Artifact downloads use the controller's standard `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` settings.

`.properties` and `.xml` files are flattened into each release's `config/` directory. If environment-specific application properties or logback files are missing, the shared files in `files/` are used. Flattened filename collisions fail instead of overwriting files.

Top-level `*.shared` files are copied by content into `shared/`, without the `.shared` suffix. Their previous contents are recorded for rollback. Keystores and other binary files are **not automatically uploaded**; provision them separately.

### Sample environment

The repository contains only sample data, so it can be cloned and tested without access to any real system:

| Path | Contents |
| --- | --- |
| `deployment/inventories/sample-qa.json` | Two hosts (`app1`, `app2`) using Supervisor, at documentation-only addresses (`192.0.2.0/24`). |
| `deployment/inventories/sample-prod.json` | Three hosts (`app1` to `app3`) using SysV, at documentation-only addresses (`198.51.100.0/24`). |
| `files/STAGE/hello-service/` | Its own `application.properties`, with an encrypted `[[api_token]]`. |
| `files/STAGE/orders-service/` | `orders.properties`; uses the shared `files/application.properties`, with an encrypted `[[jdbc_database_password]]`. |
| `secrets/STAGE/` | Each stage's `public.key` and encrypted values. |
| `deployment/tests/fixtures/STAGE.test-key` | **Test-only** private keys for the sample stages. Never use them, or this naming, for a real stage. |

Try it offline:

```sh
python -m deployment plan --stage sample-qa --roles app1 --application hello-service --version 1.0.0
cp deployment/tests/fixtures/sample-qa.test-key /tmp/sample-qa.key && chmod 600 /tmp/sample-qa.key
python -m deployment secrets verify --stage sample-qa --application hello-service --secrets-key /tmp/sample-qa.key
```

To add a real stage, create `deployment/inventories/STAGE.json` and `files/STAGE/APPLICATION/`, then run `secrets keygen` for it and keep its private key outside the repository. The sample stages can be deleted once real ones exist; update the tests that use them.

### Templates and placeholders

Deployment values use `[[name]]` placeholders, for example:

```properties
spring.datasource.password=[[jdbc_database_password]]
```

This syntax does not conflict with Spring/logback `${...}` or Camel `{{...}}` expressions; those are resolved at application runtime and left untouched. Placeholder names must be identifiers. Unknown names fail deployment, and substituted values are not rendered a second time. Files without placeholders are uploaded byte-for-byte. ERB syntax and `.erb` files are not supported.

### Artifacts

Artifact downloads happen on the controller, once per deployment, and are then uploaded to each selected host. There is no built-in repository. Each stage's inventory names its Artifactory, and the CLI picks the snapshot or release repository from the version:

```json
"settings": {
  "artifactory_url": "https://artifactory.example/artifactory",
  "artifact_group": "com/example"
}
```

With these settings, `1.0.0-SNAPSHOT` resolves under `https://artifactory.example/artifactory/libs-snapshot-local/com/example/APPLICATION/` and `1.0.0` under `.../libs-release-local/com/example/APPLICATION/`. `--repository` (or `artifactory_base_url`) overrides this with a complete repository URL, and `--artifact-url` skips resolution. `plan` and `deploy` fail before connecting if none of these is set.

Use HTTPS where possible. A plain `http://` repository or artifact URL exposes the JAR and any Artifactory credentials in transit, so it is refused unless the stage's inventory sets `"artifactory_allow_http": true`. Deployments to such a stage log a warning; pin the JAR with `--sha256` there.

When authentication is needed, set `ARTIFACTORY_TOKEN`, or both `ARTIFACTORY_USERNAME` and `ARTIFACTORY_PASSWORD`, through credentials binding. Credentials are restricted to one origin and redirects are rejected. URLs must not include credentials, query strings, or fragments.

For an explicit version, the resolver lists its artifact directory and requires exactly one matching JAR, excluding sources, javadoc, and tests. An existing version component in `path_to_jar` is not appended again. `latest` requires `maven-metadata.xml` to declare a release/latest version. The resolver does not choose between multiple timestamped snapshots or classifier artifacts based on HTML listing order; provide the exact URL instead.

Example with an exact artifact URL, SHA-256, and health check:

```sh
python -m deployment deploy \
  --stage sample-qa \
  --roles app1 \
  --application hello-service \
  --version 1.0.0 \
  --artifact-url https://artifacts.example/repository/hello-service-1.0.0.jar \
  --sha256 EXPECTED_SHA256 \
  --health-url http://127.0.0.1:8080/actuator/health
```

`artifact_url`, `artifact_sha256`, `path_to_jar`, and `artifactory_base_url` can also be supplied as environment variables. SHA-256 verification is optional; the actual digest and resolved URL are always logged and recorded in release metadata.

Snapshot builds (`-SNAPSHOT`) are rebuilt under the same version, so don't pin them. For a stable build, the job log of the first stage it reaches prints `Pin this build in later stages with: --sha256 DIGEST`. Pass that argument when deploying the same version to later stages; the deployment fails if Artifactory returns a different JAR.

## Manage encrypted secrets

### How secret storage works

Secrets are stored in Git encrypted per environment using libsodium sealed boxes via PyNaCl. No Vault, Jenkins credentials, or other service is required.

- `secrets/STAGE/public.key` encrypts values. It is committed; anyone can add or rotate a value with it but cannot read existing values.
- The stage's **private key** decrypts. Keep it outside Git on the deployment controller/operator system and supply it with `--secrets-key PATH` or `CAMEL_SECRETS_KEY_FILE`. Each stage has its own key; a `sample-qa` key cannot decrypt `sample-prod` secrets, and a mismatched key is rejected.
- `secrets/STAGE/APPLICATION.json` holds one sealed value per name. Names remain readable for review; values do not. Each value is bound to its stage, application, and name, so ciphertext cannot be copied elsewhere.
- Each store also records a `key_id`, a fingerprint of the public key that sealed its values. A store whose `key_id` doesn't match `public.key` is refused by `set`, `unset` and deploys, and reported by `check`.

Secret names are existing template variables, such as `jdbc_database_password`, `mongodb_password`, or any `[[name]]` used by an application's templates. Values are decrypted in memory and rendered only into release `config/` files. Names that would end up in service definitions, process arguments, or paths—including `jvm_options`, `java_bin`, and `deploy_to`—are refused.

### Create and manage secrets

Create a key pair for a new stage. Commit `secrets/STAGE/public.key`; keep the private key secure and outside the repository:

```sh
python -m deployment secrets keygen \
  --stage sample-qa \
  --private-key-out /secure/sample-qa.private.key
```

Add or rotate a value. The command prompts twice; alternatively, use `--value-from -` to read it from standard input:

```sh
python -m deployment secrets set \
  --stage sample-qa \
  --application hello-service \
  --name jdbc_database_password
```

List names without displaying values, or remove a value:

```sh
python -m deployment secrets list \
  --stage sample-qa \
  --application hello-service

python -m deployment secrets unset \
  --stage sample-qa \
  --application hello-service \
  --name jdbc_database_password
```

### Check and verify secrets

Developers with only the public key can run `check` without decrypting secrets. It reports:

- **Errors:** malformed or truncated ciphertext; a store encrypted with a different key than `public.key`; a secret name with no matching `[[name]]` placeholder; secrets for an application with no configuration directory; or a stage without `public.key`.
- **Warnings:** placeholders without encrypted values that must come from the environment or `--vars-file`; or sensitive-looking properties (password, secret, token, API key) that still contain plaintext in an application that uses encrypted secrets.

Run checks for all stages, or narrow them to a stage and application:

```sh
python -m deployment secrets check
python -m deployment secrets check --stage sample-qa --application hello-service
```

CI runs this check on every push and pull request. To run it before each commit, install the hook once:

```sh
uv tool install pre-commit
pre-commit install
```

Key holders can use `verify` to decrypt values. It prints names only. To compare a value interactively without displaying it, use `--compare`; this requires a terminal and cannot be scripted into CI logs:

```sh
export CAMEL_SECRETS_KEY_FILE=/secure/sample-qa.private.key

python -m deployment secrets verify \
  --stage sample-qa \
  --application hello-service

python -m deployment secrets verify \
  --stage sample-qa \
  --application hello-service \
  --name jdbc_database_password \
  --compare
```

### Rekey a stage

Run rekey from the repository checkout. Use a new private-key filename that does not already exist; the command will not overwrite a key. Keep it outside the repository and restrict access. Replace the stage and paths in this example as needed:

```sh
python -m deployment secrets rekey \
  --stage sample-qa \
  --old-key /secure/sample-qa.private.key \
  --private-key-out /secure/sample-qa-2026-11.private.key
```

Rekey decrypts, re-encrypts and verifies every application store in memory before writing anything. It then writes the new private key, rewrites every store, and replaces `public.key` last. If it is interrupted while writing, `check` reports the stores whose `key_id` no longer matches `public.key`. Restore the previous state with Git, delete the new private key, and run rekey again:

```sh
git checkout -- secrets/sample-qa
```

Verify the new key against each application before relying on it:

```sh
python -m deployment secrets verify \
  --stage sample-qa \
  --application hello-service \
  --secrets-key /secure/sample-qa-2026-11.private.key

python -m deployment secrets check --stage sample-qa
```

After verification, commit every change under the stage directory together. Never commit the private key:

```sh
git add secrets/sample-qa
git diff --cached --stat
```

Rekey changes every store's `key_id`, so a branch that changed secrets for the same stage will get a merge conflict instead of silently losing its change. To resolve it, take the rekeyed files (`git checkout --theirs secrets/sample-qa` when merging `main` into your branch) and repeat your `secrets set` or `unset` commands on top.

Keep the old private key securely if you may need to decrypt older commits. If the old key was exposed, rotate the actual secret values too; rekeying alone does not invalidate leaked credentials.

### Key handling and recovery

- Store private keys with mode `0600` (enforced on Linux), owned by the deploy account, on the controller only. Keep an offline backup for each stage; losing a key means re-entering every secret for that stage.
- Never commit private keys. `.gitignore` excludes `*.private.key` and any `*.key` under `secrets/` except `public.key`; check before committing.
- Do not run `set`, `unset`, or `rekey` concurrently for the same stage in the same checkout.
- If a private key leaks, rekeying alone is not enough: **rotate the actual secret values** (database passwords, etc.) too.
- Revoking someone’s ability to add secrets is not needed because encryption can be done with the public key. If someone who held a private key must be revoked, rekey and rotate the values.

Rendered config files remain plaintext on target hosts with mode `0640` and group `camel`. Existing plaintext credentials in `.properties` files can be replaced with `[[name]]` placeholders and encrypted values. Rotate anything already committed in plaintext because Git history retains it. Encrypted secrets hold text values only; binary files such as keystores and SSH or DSA private keys are not covered and must be provisioned on hosts separately.

## Deployment behavior and recovery

Deployments acquire per-application locks on every selected host, then deploy sequentially. Each release includes configuration and an audited artifact digest. The tool saves the previous `current` target and service definition, installs the new definition, atomically switches `current`, restarts the service, and checks its process state.

Each step is logged to standard error as `LEVEL: message`, one line per message, so URLs and digests can be copied from the job log. On a terminal the level name is coloured. CI output is plain by default; to colour it in Jenkins, install the [AnsiColor plugin](https://plugins.jenkins.io/ansicolor/), set `FORCE_COLOR=1`, and wrap the step:

```groovy
ansiColor('xterm') {
    withEnv(['FORCE_COLOR=1']) {
        sh 'uv run python -m deployment deploy ...'
    }
}
```

`NO_COLOR=1` turns colour off everywhere.

Health is checked every five seconds until the stage's `health_timeout` (default 60 seconds) runs out. If `--health-url` is supplied, HTTP readiness is also required. Without it, **process status alone is not application readiness**.

Each inventory chooses its service manager: `"use_supervisor": true` (as in `sample-qa`) uses Supervisor, otherwise the SysV script and `service`/systemd compatibility behavior are used (as in `sample-prod`). A Supervisor deployment refuses to proceed if a conflicting SysV definition remains. Resolve that operationally before deployment.

If deployment fails, previously activated hosts and the host whose activation failed are restored in reverse order, including saved service definitions and shared files. Rollback failure is reported explicitly and requires manual recovery. There is no load-balancer draining, distributed atomic cutover, or guarantee of zero downtime. If a controller is interrupted or killed, it may leave locks or partially activated releases; inspect each selected host rather than assuming rollback ran.

For a second artifact, use the same `--version` and `--second-artifact true` for lifecycle and rollback commands. Rollback uses the current release's saved state and can return to the preceding release. Rollback runs sequentially; if it fails midway, inspect all selected hosts.

Release retention prunes only releases created by this CLI. At least two are retained, and current/previous releases are protected. A pruning failure returns an error but leaves the successfully published release active.

The lock is at `/opt/camel/APPLICATION/.deploy-lock`, with the version suffix for second-artifact deployments. Release state and prior service definitions are under `releases/RELEASE_ID/.deployment/`. If a job is interrupted, first verify no deployment is still running; inspect state, `current`, and service definitions; recover the service as needed; then remove **only the specific empty lock directory**. Never delete the installation or releases directory to clear a lock. Treat release metadata, backups, and controller temporary files as sensitive.

## Contribute and verify

Run the test suite from the repository root:

```sh
python -m unittest discover -s deployment/tests -v
```

CI also lints, checks formatting, type-checks, shellchecks the rendered SysV init script, measures branch coverage, audits dependencies and scans every commit for secrets with [gitleaks](https://github.com/gitleaks/gitleaks). Run the same checks locally:

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run coverage run -m unittest discover -s deployment/tests && uv run coverage report
```

`coverage report` fails if branch coverage drops below `fail_under` in `pyproject.toml` (80%). If gitleaks is installed, `gitleaks git --config .gitleaks.toml .` runs the CI scan; `.gitleaks.toml` exempts only the sample test keys and sealed secret stores.

The tests cover template and inventory loading, artifact resolution, JAR validation, release preparation, and failure recovery using mocked remote connections. They do not deploy to real environments and do not replace a pilot deployment or operations review.

## License

[MIT](LICENSE) © 2026 Nikhil Vibhav
