# Project review: suggested improvements

Review date: 2026-11-15 (first review 2026-11-12)

Scope: the deployment CLI (`deployment/`, ~1,800 lines of Python including
tests), the CI workflow, the sample configuration (`files/`), and encrypted
secrets (`secrets/`). This file also absorbs the earlier `SECURITY-REVIEW.md`;
its findings are merged into the sections below, and their current status is in
[Earlier security review](#earlier-security-review).

The repository now holds only a sample environment (`sample-qa`,
`sample-prod`). Configuration, inventories and secrets inherited from
Capistrano were removed, and the Git history was rewritten to start at the first
Fabric commit without them.

The code itself is carefully engineered: locks fail closed, host keys must be
known, rollback runs in reverse order, and templates render in a single pass.
The remaining risks are mostly in the CI setup and in how Artifactory is
reached.

## Still open

| Area | Item | Effort |
| --- | --- | --- |
| Security | [Rotate the credentials that used to be committed](#-security) | Operational |
| CI | [`shellcheck` for `files/camel_init`](#-cicd-workflow-githubworkflowstestsyaml) | Small |

## 🔴 Security

1. ✅ **Fixed: `secrets verify` printed every decrypted secret.** It now
   prints names only. `verify --name X --compare` confirms a value matches
   without displaying it. `secrets check` lets developers validate secrets
   without a key, and it runs in pre-commit and CI.

2. ✅ **Removed: plaintext credentials and private keys were committed.**
   All configuration inherited from Capistrano (`files/<stage>/`, with
   plaintext passwords, a DSA private key and `.jks` keystores), the real
   inventories and secrets, and `lookup-hostnames.sh` were replaced by a
   sample environment. Git history was rewritten to start at the first Fabric
   commit without them, and force-pushed.
   - ❌ **Rotate the credentials.** Old clones, forks and GitHub's cache of
     old commit hashes can still hold them.
   - ✅ **Secret scanning:** the `secret-scan` CI job runs gitleaks over every
     commit. `.gitleaks.toml` exempts only the sample test keys and sealed
     secret stores.

3. ⚠️ **Artifacts and Artifactory credentials can travel over plain HTTP**
   (MEDIUM, accepted risk; earlier review #1 rated it HIGH). The repository
   no longer hardcodes an Artifactory address: each stage's inventory sets
   `artifactory_url`, and the samples use HTTPS. The real Artifactory is still
   HTTP-only, though, and SHA-256 verification is optional. Someone able to
   intercept traffic between the controller and Artifactory could:
   - replace a JAR before it is deployed;
   - read the Bearer/Basic header, which `Repository` attaches regardless of
     scheme.

   **Context:** Artifactory is 2.x on private servers in Azure, reachable
   only on the internal network. It serves no SHA-256 checksums, and
   switching it to HTTPS isn't straightforward. An attacker would need a
   foothold inside the virtual network first, which is why this is
   downgraded rather than closed.

   Artifactory's own checksums don't matter for verification: the CLI
   computes the JAR's SHA-256 itself (`deployment/artifacts.py:150`), so
   `--sha256` only needs a trusted value from outside the HTTP connection.

   Mitigations:
   - ✅ **Digests are logged for pinning.** Every deployment logs the JAR's
     SHA-256, and stable builds also log a ready-to-copy `--sha256` argument.
     Snapshot builds (QA, QT) are rebuilt under one version, so pinning them
     adds nothing. Preprod, prod and DR deploy the same stable build: pass
     the digest from the preprod job log as `--sha256` for prod and DR. A
     mismatch fails before any host changes and names both digests.
   - ✅ **Plain HTTP is opt-in per stage.** `http://` repository and artifact
     URLs are refused unless the stage's inventory sets
     `"artifactory_allow_http": true`, and deployments to such stages log a
     warning. Also consider anonymous read access or a read-only account so
     credentials sent over HTTP can do little.
   - **Restrict the Artifactory port** with an Azure network security group
     so only the deployment controller's subnet can reach it.
   - **Optional HTTPS:** put a TLS reverse proxy (nginx, or an internal
     Azure Application Gateway) in front of Artifactory. This works with any
     Artifactory version.

4. ✅ **Fixed: Rekey wasn't atomic** (MEDIUM, earlier review #4).
   `rekey` (`deployment/secrets.py`) decrypts, re-encrypts and verifies every
   store in memory before writing, and replaces `public.key` last. Each store
   records a `key_id` (public-key fingerprint):
   - An interrupted rekey leaves mismatched stores, which `check`, `set` and
     deploys refuse; `git checkout -- secrets/<stage>` restores them.
   - Rekey rewrites every store, so it conflicts in Git with any branch that
     changed secrets instead of silently losing the change.

## 🟠 CI/CD workflow (`.github/workflows/tests.yaml`)

| Status | Issue | Suggestion |
| --- | --- | --- |
| ✅ | CI triggered on `main` while the default branch was `chore/fabric-deployment` | `main` is now the default and only branch. |
| ✅ | Ruff was unpinned | Ruff and mypy are dev dependencies pinned in `uv.lock`; CI runs them with `uv run --locked`. |
| ✅ | Only direct dependencies were pinned | `uv.lock` pins every dependency with SHA-256 hashes, and `--locked` fails if it is out of date. |
| ✅ | No `permissions:` block | The workflow has `permissions: contents: read`. |
| ✅ | Actions referenced by tag (`@v7`) | Actions are pinned to commit SHAs, with the version in a comment. |
| ✅ | No Dependabot | `.github/dependabot.yml` updates the `uv` and `github-actions` ecosystems weekly, grouped into one PR each. |
| ✅ | No vulnerability scanning | The `audit` job runs `pip-audit` on the runtime dependencies exported from `uv.lock`. |
| ✅ | No `concurrency:` group | Superseded runs of the same branch or PR are cancelled. |
| ✅ | Lint target missed `tasks.py` | Ruff checks and format-checks the whole repository. |
| ✅ | No CLI smoke test | The `secrets` job runs an offline `plan` for every application under `files/`, using each stage's first role. |
| ✅ | Coverage wasn't measured | Tests run under `coverage` with branch coverage; the report goes to the job summary and CI fails below 85% (`fail_under` in `pyproject.toml`). |
| ✅ | No secret scanning | The `secret-scan` job runs a checksum-verified gitleaks over the full history. |
| ❌ | `files/camel_init` isn't linted | Run `shellcheck` on it (it runs as root on targets). It contains `[[name]]` placeholders, so render it first or exclude those lines. |

## 🟡 Python code

- ✅ **Fixed: `unseal` errors showed a traceback.** The `secrets` subcommand
  now catches `TypeError` (`deployment/__main__.py:449`).
- ✅ **Fixed: missing `ARTIFACTORY_PASSWORD` raised `KeyError`.** It is now a
  `ValueError` naming the missing variable.
- ✅ **Fixed: `config_files()` ran once per host.** Configuration and shared
  files are rendered once and the same bundle is uploaded to every host.
- ✅ **Fixed: the rollback lookup was quadratic.** Rollback checks every host
  first, then restores them in one pass over the collected states.
- ✅ **Fixed: health-check timing wasn't configurable.** Each inventory can
  set `health_timeout` (seconds, 5–1800, default 60); checks run every 5 s.
- ✅ **Fixed: `execute()` was 182 lines.** It now prepares a `Context`, then
  either prints the plan or runs `deploy`, `rollback` or `lifecycle` inside
  `locked_remotes()`, which owns acquiring and releasing host locks. Tests
  pin down the lock, restore and unlock order first.
- ✅ **Done: complexity limits are enforced** (ruff `C901`, `PLR0912`,
  `PLR0915`) with no exceptions. `variables()`, `check_store()` and the
  `secrets` command dispatcher were split to meet them, after tests pinned
  down their behaviour.
- ✅ **Done: no hardcoded Artifactory.** The default repository URL (an
  internal IP) and Maven group are gone; each inventory sets
  `artifactory_url` and `artifact_group`, `--repository` or `--artifact-url`
  override them, and `plan`/`deploy` fail early if none is set.
- ✅ **Done: deployment progress is logged** (download, upload, restart, each
  health-check attempt, cleanup), with level names coloured on terminals or
  with `FORCE_COLOR`.

## 🟢 Nice to have

- ✅ **Fixed: the README badge and project name** now use
  `fabric-deployments`, matching the repository.
- ✅ **Done: `pyproject.toml`** holds project metadata,
  `requires-python = ">=3.11"`, locked dependencies, dev dependencies and the
  ruff and mypy config. The project runs with `python -m deployment`
  (`package = false`), so there is no console script.
- ✅ **Done: broader ruff rules.** `B`, `S` (bandit), `UP` and `I` are
  enabled on top of the defaults. Their three hits were false positives, each
  suppressed with a reason.
- ✅ **Done: type hints and mypy.** Every function signature outside the tests
  is annotated, `disallow_untyped_defs` is enabled for those modules, and mypy
  runs in CI.
- ✅ **Done: a sample environment** replaces real data, so the repository can
  be cloned and fully tested without access to any real system.

## Earlier security review

These findings come from the earlier `SECURITY-REVIEW.md`. It covered the
deployment CLI, artifact handling, remote service configuration, secret
rotation, template variables and operational failure modes. That review made
no deployments against production hosts. Each finding was rechecked against
the code on 2026-11-15.

| # | Severity | Confidence | Finding | Status |
| --- | --- | --- | --- | --- |
| 1 | 🟠 HIGH | 8/10 | Default Artifactory URL is HTTP and SHA-256 verification is optional, so a JAR could be replaced in transit. | ⚠️ Accepted risk, downgraded to MEDIUM. There is no default URL any more, but the real Artifactory is HTTP-only. Mitigations in [Security #3](#-security). |
| 2 | 🟡 MEDIUM | 8/10 | `sterr_logfile_backups` misspelled in `files/camel_supervisord`, so Supervisor could reject the config and trigger a rollback. | ✅ Fixed: `files/camel_supervisord:19` reads `stderr_logfile_backups`. |
| 3 | 🟡 MEDIUM | 9/10 | `valid_url()` permitted credentials, query strings and fragments; health-check URLs could leak tokens into CI logs through failed remote command messages. | ✅ Fixed: `valid_url()` rejects user info, queries and fragments (`deployment/artifacts.py:24`), and `--health-url` is validated with it (`deployment/__main__.py:245`). |
| 4 | 🟡 MEDIUM | 9/10 | Rekey rewrites stores in place before replacing the public key, so an interruption leaves mixed keys. | ✅ Fixed: see [Security #4](#-security). |
| 5 | 🟡 MEDIUM | 8/10 | `[[pca_api_key]]` isn't among the environment keys, so rendering fails unless it's supplied via a vars file or an encrypted secret. | ✅ Resolved: the apps that used it had it encrypted, and their configuration has since been removed. `secrets check` warns about any placeholder with no encrypted value. |

## Suggested order

1. Rotate every credential that was ever committed.
2. Set `"artifactory_allow_http": true` only on stages that need it, use
   anonymous or read-only Artifactory access there, and pin prod and DR
   deployments to the preprod digest.
3. Shellcheck `files/camel_init`, and raise the coverage floor as
   `remote.py` (64%) gains tests.
