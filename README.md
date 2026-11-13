# Fabric/Invoke deployments

[![Lint and Unit Tests](https://github.com/nikhilvibhav/capistrano-migration/actions/workflows/tests.yaml/badge.svg)](https://github.com/nikhilvibhav/capistrano-migration/actions/workflows/tests.yaml)

The Python deployment CLI uses SSH/SFTP through Fabric to deploy JAR artifacts
from Artifactory together with configuration from this repository. It does not
build microservices and does not require Jenkins.

## Runtime and host prerequisites

- Use Python 3.11 or newer on a maintained Linux controller or container with
  network access to Artifactory and the deployment hosts. CentOS 7's system
  Python is **not** a suitable controller runtime. Do not replace its system
  Python; use a maintained Jenkins agent/container instead.
- Target hosts do **not** require Python. They require SSH/SFTP, a POSIX shell,
  GNU coreutils/find, Java, the `camel` user/group, and their existing service
  manager. HTTP health checks additionally require `curl` on the target.
- The SSH account must be able to create/write the application's directory under
  `/opt/camel` and read existing shared files. Provision this access separately.
  It also needs noninteractive `sudo -n` permissions for installing/restoring
  service definitions, changing release/shared group ownership to `camel`,
  creating log directories, and controlling the chosen service manager.
  Have operations review these permissions; do not grant blanket passwordless sudo.
- Provide Jenkins SSH credentials through an SSH agent or SSH configuration.
  Prepopulate the controller's `~/.ssh/known_hosts` with **verified** host keys.
  Unknown or changed keys fail closed; host key checking is not disabled.
  Network/SSH compatibility with CentOS 7 must be checked in the pilot; do not
  enable obsolete algorithms globally to work around a failed connection.

## One-time Jenkins agent setup

Install the CLI dependencies once on the Jenkins **agent** that runs the
deployment job, not on the Jenkins controller or target hosts. Keep the virtual
environment outside the checkout: this repository is cloned under
`/tmp/workspace`, which Jenkins may clean or replace. For example, create a
persistent environment at `/opt/venvs/camel-deploy` as the Jenkins service
account. Have an administrator create that directory and grant the Jenkins
account ownership if needed; install Python packages as that account, not with
`sudo pip`.

Dependencies are managed with [uv](https://docs.astral.sh/uv/): `pyproject.toml`
declares them and `uv.lock` pins exact versions. Install uv on the agent as the
Jenkins account, then, after the repository has been cloned to `/tmp/workspace`,
create the venv from the lockfile:

```sh
cd /tmp/workspace
UV_PROJECT_ENVIRONMENT=/opt/venvs/camel-deploy uv sync --locked --no-dev
```

Do this once per persistent agent, and rerun it when `uv.lock` changes. If the agent is ephemeral, bake the
environment into its image or create it during agent provisioning. A global
install is possible on a dedicated, managed agent, but an isolated venv avoids
conflicts with other jobs and OS-managed Python packages.

The commands below use Linux shell syntax and assume the repository checkout is
at `/tmp/workspace`.

## QA pilot and Jenkins freestyle build step

The deployment CLI does not switch branches or fetch application source.
Disable concurrent builds of the deployment job; remote application locks also
prevent overlapping deployments from this CLI.

First inspect an offline plan:

```sh
python -m deployment plan \
  --stage qa --roles cml1 --application authentication-service --version 1.0.0
```

`cml1` selects the existing QA host role; verify that it is the intended placement
before the pilot. The plan validates configuration/templates without SSH or
Artifactory access; it does not prove remote connectivity or artifact availability.

In a QA Jenkins job or another runner, invoke the CLI:

```sh
set -eu
set +x
. /opt/venvs/camel-deploy/bin/activate
cd /tmp/workspace
python -m deployment deploy \
  --stage qa --roles cml1 \
  --application "$application" --version "$version"
```

Do not install tools or modify target hosts automatically during a deployment.
Store secrets as encrypted values in this repository (see
[Encrypted secrets](#encrypted-secrets)), not as job parameters or arguments.
Use the actual existing job parameters for service, version and host roles.
Only after confirming SSH/sudo access, artifact selection, service health, and
rollback on this pilot should other jobs be changed.

Optional Invoke entrypoints call the same implementation:

```sh
invoke plan --stage qa --roles cml1 --application authentication-service
invoke deploy --stage qa --roles cml1 --application authentication-service
invoke rollback --stage qa --roles cml1 --application authentication-service
```

Invoke obtains artifact/version settings from the environment. Use
`python -m deployment --help` for the full CLI options.

## Existing parameters and inventories

| Input | Behavior |
| --- | --- |
| `application`, `version` | CLI options override environment variables; version defaults to `latest`. Nested application names such as `oba/oba-api` are supported. |
| Release type | Inferred from `version`: only a version ending exactly in `-SNAPSHOT` uses the snapshot repository; all other versions (including `latest`) use the stable repository. `--release-type`, if supplied, must agree with the inferred value. |
| `deploy_second_artifact` | Strict `true`/`false`; appends the explicit version to both installation directory and service name. `latest` is rejected for this mode. |
| `java_bin`, `jvm_options` | Existing Java parameters; `java_opts` is an alias when `jvm_options` is absent. One matching pair of wrapping quotes is removed from JVM options. |
| DB and template variables | Existing lowercase environment variable names are retained. Required secret values have no insecure default. Missing template variables fail before connecting. Prefer [encrypted secrets](#encrypted-secrets) for sensitive values; a name set both as an encrypted secret and an environment variable is rejected. |
| `--secrets-key` / `CAMEL_SECRETS_KEY_FILE` | Path to the stage's private key file. Required for `deploy` when the application has encrypted secrets. |
| `--vars-file` | JSON object for additional template values. Environment values override known existing parameters; CLI identity fields cannot be overridden by the file. Keep secret files outside Git with restricted permissions. |
| `--stage`, `--roles` | Required explicitly; roles are comma-separated (`cml1,cml2`, `app`, etc.). Unknown roles fail instead of deploying to every host. |
| `--health-url` / `health_url` | Optional target-local HTTP(S) endpoint, e.g. `http://127.0.0.1:8080/actuator/health`. No embedded credentials/query strings. |

Environment inventories are JSON files under `deployment/inventories/`. They
declare host addresses, roles, GELF settings, release retention and service
manager. Downloads use the controller's standard `HTTP_PROXY`, `HTTPS_PROXY`
and `NO_PROXY` settings.

`.properties` and `.xml` files are flattened into release `config/`, including
the 33 existing service XML files that were not previously part of deployment.
Application properties/logback files fall back to the shared files in `files/`
when an environment-specific one is absent. Flattened filename collisions are
rejected rather than overwritten. Top-level `*.shared` files are copied by
content into `shared/`, without their `.shared` suffix, and their previous
contents are recorded for rollback. Keystores and other binary files are
**not automatically uploaded**; provision them separately.

## Templates and placeholders

Values are substituted with `[[name]]` placeholders, e.g.
`spring.datasource.password=[[jdbc_database_password]]`. This syntax doesn't
clash with Spring/logback `${...}` or Camel `{{...}}`, which are resolved by the
application at runtime and left untouched. Names must be identifiers; unknown
names fail the deployment; substituted values are never re-rendered. Files
without placeholders are uploaded byte-for-byte. ERB syntax and `.erb` files
are not supported.

## Artifacts

Downloads occur on the controller, once per deployment, then are uploaded to
each selected host. The existing internal Artifactory base remains the default;
prefer a canonical HTTPS endpoint via `--repository`/`artifactory_base_url`.
Set `ARTIFACTORY_TOKEN`, or both `ARTIFACTORY_USERNAME` and
`ARTIFACTORY_PASSWORD`, through credentials binding when authentication is needed.
Credentials are restricted to one origin and redirects are rejected. URLs must
not contain embedded credentials, query strings, or fragments.

For a supplied version, the resolver lists its artifact directory and requires
one matching JAR (excluding sources/javadoc/tests).
An existing version component in `path_to_jar` is not appended again.
`latest` requires `maven-metadata.xml` declaring a release/latest version.
Multiple timestamped snapshots or classifier artifacts are never selected by
HTML listing order: give the exact URL instead.

```sh
python -m deployment deploy --stage qa --roles cml1 \
  --application authentication-service --version 1.0.0 \
  --artifact-url https://artifacts.example/repository/authentication-service-1.0.0.jar \
  --sha256 EXPECTED_SHA256 \
  --health-url http://127.0.0.1:8080/actuator/health
```

`artifact_url`, `artifact_sha256`, `path_to_jar` and `artifactory_base_url` can
also be supplied as environment variables. SHA-256 verification is optional;
the actual digest and resolved URL are always recorded in release metadata.
Use immutable exact versions/URLs plus expected digests for controlled promotion.

## Encrypted secrets

Secrets live in Git, encrypted per environment with public-key cryptography
(libsodium sealed boxes via PyNaCl). No Vault, Jenkins credentials, or other
service is required, so this works with any future CI/CD tool.

- `secrets/STAGE/CURRENT` (committed) selects the active secrets generation.
  Stages without this file still use the legacy layout until their first rekey.
- `secrets/STAGE/.generations/GENERATION/` contains that generation's
  `public.key` and application JSON stores. The public key (committed) encrypts;
  anyone can add or rotate a value with it but cannot read existing values.
- The stage **private key** decrypts. It is held only by the deployment
  controller/operators, outside Git, and is supplied via `--secrets-key PATH`
  or `CAMEL_SECRETS_KEY_FILE`. Each stage has its own key: a QA key cannot
  decrypt production secrets, and a mismatched key is rejected.
- Each application JSON store (committed inside the active generation) holds
  one sealed value per name. Names stay readable for review; values do not.
  Each value is bound to its stage, application and name, so ciphertext cannot
  be copied elsewhere. `set` and `unset` modify stores in the active generation.

Names are the existing template variables, e.g. `jdbc_database_password`,
`mongodb_password`, or any `[[name]]` used by the application's templates.
Values are decrypted in memory and rendered into the release
`config/` files only. Names that end up in service definitions, process
arguments or paths (`jvm_options`, `java_bin`, `deploy_to`, etc.) are refused.

```sh
# One-time per stage (operator): creates a public key; commit it.
python -m deployment secrets keygen --stage qa --private-key-out /secure/qa.private.key

# Add or rotate a value (anyone; prompts twice, or use --value-from - for stdin).
python -m deployment secrets set --stage qa --application authentication-service \
  --name jdbc_database_password
python -m deployment secrets list  --stage qa --application authentication-service
python -m deployment secrets unset --stage qa --application authentication-service \
  --name jdbc_database_password

# Check names and file structure against templates (anyone; no key needed).
python -m deployment secrets check

# Check every value decrypts (key holder), then deploy.
export CAMEL_SECRETS_KEY_FILE=/secure/qa.private.key
python -m deployment secrets verify --stage qa --application authentication-service
python -m deployment deploy --stage qa --roles cml1 --application authentication-service
```

### Checking secrets before you push

Developers hold only the public key and cannot decrypt, so they validate
secrets with `secrets check`. It needs no key and reports:

- **Errors** (fail the check): malformed or truncated ciphertext, a secret
  name with no matching `[[name]]` placeholder (e.g. a typo), secrets for an
  application with no configuration directory, or a stage without `public.key`.
- **Warnings**: a placeholder that has no encrypted value and must come from
  the environment or `--vars-file`, or a sensitive-looking property (password,
  secret, token, API key) that still has a plaintext value in an application
  that uses encrypted secrets.

Narrow it with `--stage` and `--application`. CI runs it on every push and PR.
To run it automatically before each commit, install the hook once:

```sh
uv tool install pre-commit
pre-commit install
```

Key holders use `secrets verify`, which decrypts every value but prints only
names. To confirm a stored value is the one you expect without displaying it,
use `--compare`. It prompts for the expected value and reports only whether it
matches. It requires an interactive terminal, so it cannot be scripted into CI
logs.

```sh
python -m deployment secrets verify --stage qa --application authentication-service \
  --name jdbc_database_password --compare
```

`plan` works without the key: it lists secret names and validates templates
using placeholders. With the key, it also checks decryption. `deploy` fails
before connecting to any host when secrets exist and no valid key is supplied.

### Key handling

- Store private keys with mode `0600` (enforced on Linux), owned by the deploy
  account, on the controller only. Keep an offline backup per stage; losing a
  key means re-entering every secret for that stage.
- Never commit private keys. `.gitignore` excludes `*.private.key` and any
  `*.key` under `secrets/` except `public.key`, but check before committing.
- Rotate a key with `secrets rekey --stage qa --old-key OLD --private-key-out NEW`.
  It decrypts every active application store, writes and verifies a complete new
  generation, then atomically switches `CURRENT`. The old generation remains
  available as a recovery copy. Commit `CURRENT` and the new generation
  together, then destroy the old private key only after verifying the new one.
  Do not run `set`, `unset`, or another `rekey` for the same stage concurrently.
  When rekeying a legacy-layout stage, its old root-level files remain inactive
  after the switch; `CURRENT` determines which files are active.
- Git history keeps old ciphertext. If a private key leaks, rekeying is not
  enough: **rotate the actual secret values** (database passwords, etc.) too.
- Revoking a person's ability to *add* secrets is not needed (encryption-only).
  Revoking someone who held a private key requires rekeying and rotating values.

Rendered config files remain plaintext on target hosts (mode `0640`, group
`camel`). Passwords already committed in plaintext `.properties` files can be
replaced with `[[name]]` placeholders plus encrypted values. Encrypted secrets
hold text values only; binary files such as keystores and `kognitio-privateKey`
are not covered and must be provisioned on hosts separately. Rotate anything
already committed in plaintext, because Git history retains it.

## Publication, recovery, and lifecycle

Deployments acquire per-application locks on every selected host, then deploy
sequentially. Each release gets configuration and an audited artifact digest;
the tool saves the previous `current` target and service definition, installs
the new definition, atomically switches `current`, restarts the service, and
checks its process state. If `--health-url` is supplied, HTTP readiness is also
required, with up to twelve attempts at five-second intervals. Without it,
**process status alone is not application readiness**.

QA/QE/QT/TST retain Supervisor; other environments retain the existing SysV
script and `service`/systemd compatibility behavior. This is not a service-manager
migration. A Supervisor deployment refuses to proceed if a conflicting SysV
definition remains; resolve that operationally before deploying.

On failure, previously activated hosts and the failed activation host are
restored in reverse order, including saved service definitions and shared files.
Rollback failure is reported explicitly and requires manual recovery.
There is no load-balancer draining, distributed atomic cutover, or guarantee of
zero downtime. Interrupted/killed controllers may leave locks or partially
activated releases; inspect each selected host rather than assuming rollback ran.

```sh
python -m deployment rollback --stage qa --roles cml1 --application authentication-service
python -m deployment stop --stage qa --roles cml1 --application authentication-service
python -m deployment start --stage qa --roles cml1 --application authentication-service
```

For a second artifact, use the same `--version` and `--second-artifact true`
for lifecycle and rollback commands. Rollback uses the current release's saved
state and can return to the preceding release. Rollback runs sequentially; if
it fails midway, inspect all selected hosts.

Retention only prunes releases created by this CLI. At least two are retained,
and current/previous releases are protected. A pruning failure returns an error
but leaves the successfully published release active.

Lock location: `/opt/camel/APPLICATION/.deploy-lock` (with the version suffix
for second-artifact deployments). Release state and prior service definitions
are under `releases/RELEASE_ID/.deployment/`. If a job is interrupted, verify
that no deployment is still running, inspect state/current/service definitions,
recover the service as needed, then remove **only the specific empty lock
directory**. Never delete the installation or releases directory to clear a lock.
Treat release metadata/backups and controller temporary files as sensitive.

## Local verification

```sh
python -m unittest discover -s deployment/tests -v
```

These tests exercise template/inventory loading, artifact resolution,
JAR validation, release preparation, and failure recovery using mocked remote
connections. They do not deploy to the internal environments or replace the QA
pilot and operations review.
