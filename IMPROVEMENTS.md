# Project review: suggested improvements

Review date: 2026-11-12

Scope: the deployment CLI (`deployment/`, ~1,100 lines of Python), the CI
workflow, the configuration tree (`files/`), and encrypted secrets
(`secrets/`). This file also absorbs the earlier `SECURITY-REVIEW.md`; its
findings are merged into the sections below, and their current status is in
[Earlier security review](#earlier-security-review).

The code itself is carefully engineered: locks fail closed, host keys must be
known, rollback runs in reverse order, and templates render in a single pass.
The largest risks are in the repository's contents and the CI setup rather
than the Python logic.

## 🔴 Security

1. ✅ **Fixed: `secrets verify` printed every decrypted secret.** It now
   prints names only. `verify --name X --compare` confirms a value matches
   without displaying it. The new `secrets check` lets developers validate
   secrets without a key, and it runs in pre-commit and CI.

2. **Plaintext credentials and private keys are committed.** Only
   `secrets/qa/` has been migrated to encrypted values. Production, preprod and
   DR have only a `public.key`, while their configs still hold plaintext
   values: 18 `spring.datasource.password` entries plus `am.admin.password`,
   `salesforce.password` and others in `files/production` alone. A
   `-----BEGIN DSA PRIVATE KEY-----` file (`kognitio-privateKey`) is committed
   for 7 environments, along with `.jks` keystores.
   - Move these values to `secrets/<stage>/*.json` with `[[name]]`
     placeholders.
   - **Rotate the credentials.** Git history retains the old values.
   - Add a secret-scanning step to CI (gitleaks or trufflehog) so new
     plaintext can't be committed.
   - `secrets check` already warns about two plaintext values in QA apps
     that otherwise use encrypted secrets: `am.admin.password`
     (`qa/oba-account-service`) and `rest.api.security.encoded_password`
     (`qa/tplms-finder-service`).

3. **Artifacts and Artifactory credentials travel over plain HTTP** (HIGH,
   earlier review #1). `DEFAULT_REPOSITORY` (`deployment/__main__.py:25`) is
   `http://`, and SHA-256 verification is optional. Anyone who can intercept
   the internal network path could:
   - replace a JAR before it is deployed;
   - read the Bearer/Basic header that `Repository` attaches regardless of
     scheme.

   Fixes:
   - Make HTTPS the default.
   - Refuse to send credentials to an `http://` URL.
   - Require a trusted `--sha256` for production releases.

   How serious this is depends on whether the internal network path is trusted.

4. ✅ **Fixed: Rekey wasn't atomic** (MEDIUM, earlier review #4).
   `rekey` (`deployment/secrets.py`) now decrypts, re-encrypts and verifies
   every store in memory before writing, and replaces `public.key` last.
   Each store records a `key_id` (public-key fingerprint):
   - An interrupted rekey leaves mismatched stores, which `check`, `set` and
     deploys refuse; `git checkout -- secrets/<stage>` restores them.
   - Rekey rewrites every store, so it conflicts in Git with any branch that
     changed secrets. An earlier generation-directory design let such
     changes be silently lost or reverted on merge.

## 🟠 CI/CD workflow (`.github/workflows/tests.yaml`)

| Issue | Suggestion |
| --- | --- |
| CI only triggers on `main`, but `origin/HEAD` points to `chore/fabric-deployment` | Confirm which branch PRs target. The README badge (no `?branch=`) also reports on the default branch. |
| No `permissions:` block | Add `permissions: contents: read` at the top level (least privilege). |
| Actions referenced by tag (`@v7`) | Pin to commit SHAs and let Dependabot bump them. This is a supply-chain risk for a repo that handles deploy secrets. |
| `pip install ruff` is unpinned | Pin it (`requirements-dev.txt` or a `pyproject` dependency group) so a new ruff release can't break CI without a code change. |
| Only direct dependencies are pinned | `paramiko`, `cryptography`, `bcrypt` and others float. Generate a lockfile with hashes (`uv pip compile --generate-hashes` or `pip-compile`) and install with `--require-hashes`. This matters for a tool that holds SSH and decryption keys. |
| No vulnerability scanning | Add `pip-audit -r deployment/requirements.txt`. |
| No `concurrency:` group | Add one with `cancel-in-progress: true` for PR refs. |
| No Dependabot | Add `.github/dependabot.yml` for `pip` and `github-actions`. |
| Shell scripts aren't linted | Run `shellcheck` on `lookup-hostnames.sh` and `files/camel_init` (which runs as root on targets). |
| Coverage isn't measured | Run `coverage run -m unittest` and upload the report or enforce a floor. |
| Lint target misses `tasks.py` | Run `ruff check .` instead of `ruff check deployment`. |

Also consider a CI job that runs `python -m deployment plan` for each
inventory/role: a cheap offline smoke test of the CLI wiring. The template
test already covers rendering.

## 🟡 Python code

- **Inconsistent error types:**
  - ✅ Fixed: `unseal` raises `TypeError`, which the `secrets` subcommand now
    catches (`deployment/__main__.py:267`) instead of showing a traceback.
  - Setting `ARTIFACTORY_USERNAME` without `ARTIFACTORY_PASSWORD` causes a
    `KeyError` (`deployment/artifacts.py:40`). It's reported with the generic
    "Inspect the target service…" message, which points the operator the
    wrong way. Raise `ValueError` for the missing password.
- **`config_files()` runs once per host** inside the deploy loop
  (`deployment/__main__.py:207`) with identical inputs. Render once before the
  loop.
- **The rollback branch is quadratic:** it uses
  `next(item for item in states if item[0] is remote)`
  (`deployment/__main__.py:186`). Iterate over `states` directly.
- **`execute()` is about 150 lines** (ruff PLR0912/PLR0915). Splitting it into
  `_plan`, `_lifecycle`, `_rollback` and `_deploy` would make the recovery path
  easier to review and test.
- **Health-check timing is hardcoded** (12 attempts × 5 s in `Remote.healthy`).
  JVM services with slow startup may need longer; make it an inventory
  setting.

## 🟢 Nice to have

- **Add a `pyproject.toml`** to hold project metadata,
  `requires-python = ">=3.11"`, the ruff config, dev dependencies, and a
  `camel-deploy` console script. It replaces the bare `ruff.toml` and
  `requirements.txt`.
- **Broaden ruff rules.** Running `E,F,B,UP,S,SIM,I,PL,RUF` produced 44 hits,
  none serious. Adding `B`, `S` (bandit), `UP` and `I` is cheap, and `S`
  catches regressions in a security-sensitive tool.
- **Add type hints and run mypy.** Fabric/paramiko call sites and the
  inventory dicts would benefit most.
- **`lookup-hostnames.sh` hardcodes a username and the inventory IPs.** Read
  the IPs from `deployment/inventories/*.json` so the two can't drift, and
  take the user as a parameter.

## Earlier security review

These findings come from the earlier `SECURITY-REVIEW.md`. It covered the
deployment CLI, artifact handling, remote service configuration, secret
rotation, template variables and operational failure modes. That review made
no deployments against production hosts. Each finding was rechecked against
the code on 2026-11-12.

| # | Severity | Confidence | Finding | Status |
| --- | --- | --- | --- | --- |
| 1 | 🟠 HIGH | 8/10 | Default Artifactory URL is HTTP and SHA-256 verification is optional, so a JAR could be replaced in transit. | ❌ Open: see [Security #3](#-security). |
| 2 | 🟡 MEDIUM | 8/10 | `sterr_logfile_backups` misspelled in `files/camel_supervisord`, so Supervisor could reject the config and trigger a rollback. | ✅ Fixed: `files/camel_supervisord:19` now reads `stderr_logfile_backups`. |
| 3 | 🟡 MEDIUM | 9/10 | `valid_url()` permitted credentials, query strings and fragments; health-check URLs could leak tokens into CI logs through failed remote command messages. | ✅ Fixed (previously marked ❌): `valid_url()` now rejects user info, queries and fragments (`deployment/artifacts.py:17`), and `--health-url` is validated with it (`deployment/__main__.py:127`). |
| 4 | 🟡 MEDIUM | 9/10 | Rekey rewrites stores in place before replacing the public key, so an interruption leaves mixed keys. | ✅ Fixed: see [Security #4](#-security). |
| 5 | 🟡 MEDIUM | 8/10 | `[[pca_api_key]]` isn't among the environment keys, so rendering fails unless it's supplied via a vars file or an encrypted secret. | ✅ Resolved: all six QA apps that use it (`branch-finder-service`, `enquiry-office-api`, `inflight-redelivery-service`, `pfw-depot-finder`, `postoffice-lat-long-loader`, `tplms-finder-service`) have it encrypted. `secrets check` now warns about any placeholder with no encrypted value. It still can't be set as an environment variable, which is fine while it stays encrypted. |

## Suggested order

1. ✅ Stop `secrets verify` from printing values (done).
2. ✅ Migrate the remaining plaintext credentials to encrypted secrets and rotate
   them; remove committed private keys.
3. Use HTTPS for Artifactory, refuse credentials over HTTP, and require
   digests for production.
4. Harden the workflow: permissions, SHA pins, pinned ruff, pip-audit,
   gitleaks, Dependabot.
5. Add a hashed lockfile for the controller's dependencies.
6. ✅ Make rekey atomic (done); then the remaining code and tooling items.
