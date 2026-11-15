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
| Security | [Refuse to send Artifactory credentials over `http://`](#-security) | Small |
| Security | [Add secret scanning to CI](#-security) | Small |
| CI | [`permissions:`, SHA-pinned actions, Dependabot, `concurrency:`](#-cicd-workflow-githubworkflowstestsyaml) | Small |
| CI | [`pip-audit`, `shellcheck`, coverage, full-repo ruff, plan smoke test](#-cicd-workflow-githubworkflowstestsyaml) | Small each |
| Code | [Missing `ARTIFACTORY_PASSWORD` raises `KeyError`](#-python-code) | Small |
| Code | [Config rendered once per host; quadratic rollback lookup](#-python-code) | Small |
| Code | [`execute()` is 178 lines](#-python-code) | Medium |
| Code | [Health-check timing is not configurable per stage](#-python-code) | Small |
| Housekeeping | [README badge and project name use the old repository name](#-nice-to-have) | Trivial |

## 🔴 Security

1. ✅ **Fixed: `secrets verify` printed every decrypted secret.** It now
   prints names only. `verify --name X --compare` confirms a value matches
   without displaying it. `secrets check` lets developers validate secrets
   without a key, and it runs in pre-commit and CI.

2. ✅ **Removed: plaintext credentials and private keys were committed.**
   All configuration inherited from Capistrano (`files/<stage>/`, with
   plaintext passwords, a DSA private key and `.jks` keystores), the real
   inventories and secrets, and `lookup-hostnames.sh` were replaced by a
   sample environment. `scripts/rewrite-history.sh` removed them from Git
   history, and the rewritten history has been force-pushed.
   - ❌ **Rotate the credentials.** Old clones, forks and GitHub's cache of
     old commit hashes can still hold them.
   - ❌ **Add secret scanning to CI** (gitleaks or trufflehog) so new
     plaintext can't be committed. The sample test keys under
     `deployment/tests/fixtures/` are intentional and would need an
     allowlist entry.

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
   computes the JAR's SHA-256 itself (`deployment/artifacts.py:145`), so
   `--sha256` only needs a trusted value from outside the HTTP connection.

   Mitigations:
   - ✅ **Digests are logged for pinning.** Every deployment logs the JAR's
     SHA-256, and stable builds also log a ready-to-copy `--sha256` argument.
     Snapshot builds (QA, QT) are rebuilt under one version, so pinning them
     adds nothing. Preprod, prod and DR deploy the same stable build: pass
     the digest from the preprod job log as `--sha256` for prod and DR. A
     mismatch fails before any host changes and names both digests.
   - ❌ **Refuse to send credentials to an `http://` URL** unless explicitly
     allowed, or use anonymous read access or a read-only account scoped to
     the release repositories so leaked credentials can do little.
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
| ❌ | No `permissions:` block | Add `permissions: contents: read` at the top level (least privilege). |
| ❌ | Actions referenced by tag (`@v7`) | Pin `actions/checkout` and `astral-sh/setup-uv` to commit SHAs and let Dependabot bump them. This is a supply-chain risk for a repo that handles deploy secrets. |
| ❌ | No Dependabot | Add `.github/dependabot.yml` for the `uv` and `github-actions` ecosystems. |
| ❌ | No vulnerability scanning | Add a job running `uv export --no-dev --format requirements-txt > requirements.txt && uvx pip-audit -r requirements.txt`. |
| ❌ | No `concurrency:` group | Add one with `cancel-in-progress: true` for PR refs. |
| ❌ | Shell scripts aren't linted | Run `shellcheck` on `files/camel_init` (runs as root on targets) and `scripts/rewrite-history.sh`. |
| ❌ | Coverage isn't measured | Run `coverage run -m unittest` and upload the report or enforce a floor. |
| ❌ | Lint target misses `tasks.py` | CI runs `ruff check deployment` and `ruff format --check deployment`; use `.` for both. mypy already covers `tasks.py`. |
| ❌ | No CLI smoke test | Run `python -m deployment plan` for each sample stage and app. The sample environment makes this possible without real hosts. |

## 🟡 Python code

- ✅ **Fixed: `unseal` errors showed a traceback.** The `secrets` subcommand
  now catches `TypeError` (`deployment/__main__.py:359`).
- ❌ **Missing `ARTIFACTORY_PASSWORD` raises `KeyError`.** Setting
  `ARTIFACTORY_USERNAME` alone fails at `deployment/artifacts.py:59` and is
  reported with the generic "Inspect the target service…" hint, which points
  the operator the wrong way. Raise a `ValueError` naming the missing
  variable.
- ❌ **`config_files()` runs once per host** inside the deploy loop
  (`deployment/__main__.py:288`) with identical inputs. Render once before the
  loop.
- ❌ **The rollback lookup is quadratic:** it uses
  `next(item for item in states if item[0] is remote)`
  (`deployment/__main__.py:267`). Iterate over `states` directly.
- ❌ **`execute()` is 178 lines** (ruff PLR0912/PLR0915). Splitting it into
  `_plan`, `_lifecycle`, `_rollback` and `_deploy` would make the recovery path
  easier to review and test.
- ⚠️ **Health-check timing isn't configurable per stage.** It's now named
  (`HEALTH_ATTEMPTS = 12`, `HEALTH_INTERVAL = 5` in `deployment/remote.py`)
  and logged per attempt, but JVM services with slow startup may need longer
  than 60 s; make it an inventory setting.
- ✅ **Done: no hardcoded Artifactory.** The default repository URL (an
  internal IP) and Maven group are gone; each inventory sets
  `artifactory_url` and `artifact_group`, `--repository` or `--artifact-url`
  override them, and `plan`/`deploy` fail early if none is set.
- ✅ **Done: deployment progress is logged** (download, upload, restart, each
  health-check attempt, cleanup), with level names coloured on terminals or
  with `FORCE_COLOR`.

## 🟢 Nice to have

- ❌ **The README badge and project name use the old repository name.** The
  badge links to `nikhilvibhav/capistrano-migration`, but the remote is
  `nikhilvibhav/fabric-deployments`; `pyproject.toml` still says
  `name = "capistrano-migration"`.
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
| 3 | 🟡 MEDIUM | 9/10 | `valid_url()` permitted credentials, query strings and fragments; health-check URLs could leak tokens into CI logs through failed remote command messages. | ✅ Fixed: `valid_url()` rejects user info, queries and fragments (`deployment/artifacts.py:24`), and `--health-url` is validated with it (`deployment/__main__.py:207`). |
| 4 | 🟡 MEDIUM | 9/10 | Rekey rewrites stores in place before replacing the public key, so an interruption leaves mixed keys. | ✅ Fixed: see [Security #4](#-security). |
| 5 | 🟡 MEDIUM | 8/10 | `[[pca_api_key]]` isn't among the environment keys, so rendering fails unless it's supplied via a vars file or an encrypted secret. | ✅ Resolved: the apps that used it had it encrypted, and their configuration has since been removed. `secrets check` warns about any placeholder with no encrypted value. |

## Suggested order

1. Rotate every credential that was ever committed.
2. Harden the workflow: `permissions:`, SHA-pinned actions, Dependabot,
   `pip-audit`, secret scanning.
3. Refuse Artifactory credentials over HTTP (or switch to anonymous or
   read-only access), and pin prod and DR deployments to the preprod digest.
4. Small code fixes: the `ARTIFACTORY_PASSWORD` error, rendering config once,
   the rollback lookup, configurable health-check timing.
5. Split `execute()`, then the remaining tooling items (coverage, shellcheck,
   plan smoke test, badge and project name).
