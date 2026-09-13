# Releasing (maintainers)

`.github/workflows/release.yml` uses PyPI Trusted Publishing, so no PyPI token is
stored in GitHub. A manual workflow run publishes only to TestPyPI; publishing to
PyPI requires a version tag and approval of the protected `pypi` environment.

A Trusted Publisher is bound to an exact owner, repository, workflow filename, and
environment — here `Openconstraint`, `openconstraint-mcp`, `release.yml`, and
`pypi`/`testpypi`. Changing any of them requires re-registering the publisher.

## One-time setup

1. Create the GitHub environments `testpypi` and `pypi`. Require the maintainer as a
   reviewer for `pypi`. A solo maintainer must leave **Prevent self-review**
   disabled, and should uncheck **Allow administrators to bypass** so the approval
   applies to admins too. Restrict `pypi` deployments to tags matching `v*`, and
   `testpypi` deployments to the `master` branch — the TestPyPI job runs from a
   manual dispatch, so a tag rule there would reject every rehearsal.
2. Create and verify separate accounts on [TestPyPI](https://test.pypi.org/) and
   [PyPI](https://pypi.org/), enable 2FA, and store recovery codes safely.
3. On each account's **Publishing** page, add a pending GitHub Trusted Publisher with:
   project `openconstraint-mcp`, owner `Openconstraint`, repository
   `openconstraint-mcp`, workflow `release.yml`, and environment `testpypi` or `pypi`
   respectively. Do not create an API token.

## TestPyPI rehearsal

1. Run `just check` and `just build` locally.
2. After the release workflow is on the default branch, open GitHub **Actions →
   Release → Run workflow** and run it from that branch. This path can publish only
   to TestPyPI.
3. Approve the `testpypi` deployment if that environment has a required reviewer.
4. Smoke-test the uploaded package (replace the version after the first rehearsal):

   ```bash
   uv run --isolated --no-project \
     --with "openconstraint-mcp==0.1.0" \
     --index https://pypi.org/simple/ \
     --default-index https://test.pypi.org/simple/ \
     openconstraint-mcp --help
   ```

   `--index` outranks `--default-index`, so dependencies (pydantic, httpx, ...)
   resolve from PyPI; only the unreleased `openconstraint-mcp` version — absent
   from PyPI — falls through to TestPyPI. A bare `--index test.pypi.org` would
   make TestPyPI's stale/alpha releases of common dependency names (e.g.
   `pydantic` only goes up to `1.5a1` there) win resolution and break the smoke
   test.

TestPyPI never overwrites a release. Increment the version before repeating a
rehearsal whose version is already present there. TestPyPI and PyPI are separate, so
using `0.1.0` on TestPyPI does not prevent publishing `0.1.0` to PyPI.

## PyPI release

After the rehearsal and default-branch CI are green, verify that the version in
`pyproject.toml` is the intended release, then create and push only that tag:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

Approve the waiting `pypi` deployment in GitHub Actions. Without both the tag push
and that approval, the workflow cannot publish to PyPI.
