## Summary

<!-- What does this change, and why? Link any issue it resolves (e.g. "Fixes #12"). -->

## Deployment impact

<!-- Delete the ones that don't apply. -->

- New or changed CLI option, environment variable or inventory setting:
- Change to the remote release layout, service definitions or rollback state:
- None: internal change only.

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` passes
- [ ] `uv run coverage run -m unittest discover -s deployment/tests && uv run coverage report` passes
- [ ] `uv run python -m deployment secrets check` passes
- [ ] Tests cover the change, using the sample stages rather than real environments
- [ ] No plaintext secrets, private keys, credentials or internal hostnames/IPs are added
- [ ] `README.md` (and `IMPROVEMENTS.md`, if relevant) is updated
