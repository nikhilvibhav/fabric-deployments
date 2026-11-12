# Python deployment review

Review scope: deployment CLI, artifact handling, remote service configuration,
secret rotation, template variables, and operational failure modes.

| # | Severity | File | Lines | Vulnerability | Confidence | Fixed |
| --- | ---------- | ------ | ------- | --------------- | ------------ | ------- |
| 1 | 🟠 HIGH | `deployment/__main__.py` | 21, 61-64 | The default Artifactory base URL uses HTTP, and artifact SHA-256 verification is optional. If the internal network path is not trusted, an attacker able to intercept downloads could replace a JAR before deployment. Prefer HTTPS and require a trusted expected digest for production releases. | 8/10 | NA |
| 2 | 🟡 MEDIUM | `files/camel_supervisord` | 19 | `sterr_logfile_backups` is misspelled; Supervisor expects `stderr_logfile_backups`. Supervisor may reject the generated configuration during reread/update, causing deployments on Supervisor-managed stages to roll back. Correct the option and validate the generated config with Supervisor before activation. | 8/10 | ✅ |
| 3 | 🟡 MEDIUM | `deployment/artifacts.py`, `deployment/remote.py` | `artifacts.py:13-19`; `remote.py:43-46` | `valid_url()` permits credentials, queries, and fragments. Health-check URLs use this validator, and remote command failures log the command, which includes the URL. A URL containing credentials or a query token could be exposed in CI logs. Validate health URLs with stricter rules or redact them from failure logs. | 9/10 | ❌ |
| 4 | 🟡 MEDIUM | `deployment/secrets.py` | 174-192 | Rekey writes each application's ciphertext store in place before replacing the stage public key. An interruption or write failure can leave stores encrypted to different keys while the public key remains old. Stage all outputs and use a recoverable transaction/backup-and-rollback procedure before switching keys. | 9/10 | ❌ |
| 5 | 🟡 MEDIUM | `deployment/config.py`, `files/qa/branch-finder-service/env.properties` | `config.py:116`; `env.properties:66` | `pca_api_key` is referenced by a template but is not in `environment_keys`; setting it as an environment variable does not populate the template value, so rendering fails with a missing-variable error unless supplied through a vars file or encrypted secret. Add it to the supported environment keys or explicitly document/enforce the supported source. | 8/10 | NA |

These are code-level findings; the HTTP download risk depends on the security of
the internal network path. The review did not execute deployments against
production hosts.
