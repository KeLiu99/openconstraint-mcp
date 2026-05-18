# `install-runtime` (Linux x86_64) — Implementation Plan

> **Executor notes**
>
> - Drive task-by-task; tick `- [ ]` boxes as you go. Each task lists files, behavior, tests, and a verification command — write the code yourself from the behavior description, don't expect ready-to-paste snippets.
> - Preflight: run `just --list` to confirm recipes are present. Fall back to `uv run …` only if `just` is unavailable.
> - **Commits:** this plan does not prescribe commits. Commit at points that make sense to you (typically after each green task), follow the repo's plain-message convention, and re-run `just check` before any commit.
> - **Network policy:** this plan introduces the *only* network code path in the package. It runs solely inside the `install-runtime` CLI command. No other code path (stdio, check-runtime, list-solvers, MCP tool handlers, import-time init) may touch the network.
>
> **Project root:** `/home/bios8086/PycharmProjects/PythonProject/openconstraint-mcp`

---

## Context

The v0 skeleton ships `check-runtime`, `list-solvers`, and a placeholder `install-runtime` that prints "not yet implemented" and exits 1. README documents the workaround: point `OPENCONSTRAINT_MCP_RUNTIME_DIR` at an existing MiniZinc install.

This plan implements the real installer for **Linux x86_64 only**. Other platforms get a structured error pointing at the manual workaround; their installers are tracked separately. MiniZinc 2.9.7 is the pinned target; the bundle is `MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz` from the MiniZincIDE GitHub release, which extracts to a single top-level wrapper directory containing `bin/`, `lib/`, `share/`.

## Goal

`openconstraint-mcp install-runtime` performs, in order:

1. **Resolve install location** with precedence `--runtime-dir` flag > `OPENCONSTRAINT_MCP_RUNTIME_DIR` env > persisted install config > platformdirs default.
2. **Interactive prompt** when stdin is a TTY, no `--runtime-dir` was passed, and no `--yes`: display the proposed default and accept either Enter (use default) or a typed custom path.
3. **Marker-gated overwrite check.** A target directory may be overwritten **only** if it is empty, or if it is a previous managed install (detected by the presence of a parseable `<runtime_dir>/.openconstraint-runtime.json` marker). Any other non-empty directory — `$HOME`, `/tmp`, a project checkout, whatever — is refused **even with `--yes`**. The error names the path and tells the user to pick an empty directory or remove the contents themselves. `--yes` only authorizes overwriting *our own* prior installs, never arbitrary user data.
4. **Stream-download** the pinned bundle with a `rich.progress` UI; reject early on HTTP errors.
5. **Verify SHA256** of the downloaded archive against a pinned constant.
6. **Safely extract** into a *sibling* staging directory of the target (so the final rename is same-filesystem), using `tarfile.extractall(..., filter="data")` and stripping the single top-level wrapper. Write the `.openconstraint-runtime.json` marker into the staging tree before swap so the swapped-in runtime is self-identifying.
7. **Smoke-check** the staged binary by invoking `<staging>/bin/minizinc --version`; abort without touching the target on failure.
8. **Rollback-safe swap** into place: rename existing target aside to a sibling backup, rename staging into the target, then delete the backup. Restore the backup on any swap failure.
9. **Persist the chosen install location** to a small JSON config file so subsequent `check-runtime`, `list-solvers`, and MCP tool calls find the runtime without the user re-passing the path.

CLI surface:

- `--runtime-dir PATH` — explicit install location. Overrides env/config/default and suppresses the interactive prompt.
- `--yes` / `-y` — non-interactive: skip both the path prompt and the overwrite-confirmation prompt. Required for non-TTY runs into a non-empty target.

## Non-goals

- macOS, Windows, Linux ARM installers. (No bundles for these from upstream MiniZinc would be the same shape; each needs its own task.)
- Upgrade/migration from a prior bundled version. The `--yes` overwrite path is sufficient.
- A `--version` flag — version is pinned in the module; bumping is a code change.
- Telemetry of any kind, including "install succeeded" pings.
- Auto-install fallback on first `stdio` or `check-runtime` invocation.

## Architecture

```
cli  ─►  server  ─►  minizinc  ─►  runtime  ─►  schemas
   │                                  ▲
   └──►  runtime_install  ────────────┘
```

- New module `runtime_install` lives on its own branch reachable from `cli` only. It **does not** import `runtime`, `minizinc`, or `server` — it is a pure file-ops module that installs the bundle into whatever path the caller hands it. The CLI is responsible for resolving the target path and for persisting the chosen path after a successful install.
- The smoke check invokes the staged binary via `subprocess` directly, not via `minizinc.list_solvers()` (which would create a same-layer branch-cross).
- `runtime.py` gains a small config read/write surface (`read_install_config`, `write_install_config`). The CLI's `install-runtime` command calls `write_install_config` after `install_managed_runtime` returns. Read-side callers (`get_runtime_dir`, `check-runtime`, `list-solvers`, MCP handlers) consult the config via `read_install_config` transparently.
- `cli.py` imports `install_managed_runtime` **lazily inside the command function** so `httpx` and `rich.progress` are not loaded for `stdio`/`check-runtime`/`list-solvers`. A test enforces this.

## Tech stack

- Python 3.12 target. `httpx` (streaming download), `rich.progress` (UI), `tarfile` with `filter="data"` (CVE-2007-4559-safe extraction), `hashlib` (SHA256), `tempfile.TemporaryDirectory` (download scratch), `subprocess` (smoke check).
- All already declared in `pyproject.toml`; nothing new to add.

## Safety & privacy invariants

- **No network outside `install_managed_runtime`.** Enforced by lazy-import in `cli.py` and a regression test (`test_cli_module_does_not_import_httpx_eagerly`).
- **Pinned SHA256.** Computed once and stored as a module constant. A swapped release asset or compromised mirror is rejected.
- **Safe extraction.** Always `filter="data"` — no hand-rolled traversal validators.
- **Rollback-safe install.** Staging lives in `<runtime_dir>.parent / ".<name>.staging.<pid>"` so the swap renames are same-filesystem. A prior target is renamed aside to `".<name>.backup.<pid>"` before the new tree moves in; backup restoration is attempted on any swap failure. A test injects a `Path.rename` failure mid-swap and asserts the prior runtime is restored.
- **No silent overwrite of user data.** A target is overwriteable only if it is empty *or* carries our `<runtime_dir>/.openconstraint-runtime.json` marker (a small JSON written by the installer, parsed on next run to confirm it's our prior install). Any other non-empty directory is refused regardless of `--yes` — this prevents a fat-finger like `--runtime-dir $HOME --yes` from nuking the home directory. The interactive confirm + `--yes` only authorize replacing a *prior managed install*, never arbitrary user data.
- **Config file = config, not data.** Lives under `<platformdirs user_config_dir>/install.json` (which is already `~/.config/openconstraint-mcp/install.json` on Linux — `PlatformDirs("openconstraint-mcp", "openconstraint-mcp").user_config_dir` already includes the app segment; **do not** append another `openconstraint-mcp/`). Single Pydantic-validated field `runtime_dir: str`. Read failures (missing, malformed, schema mismatch) silently return `None` so a corrupted config never blocks `check-runtime` — the next `install-runtime` rewrites it.

## File structure

| File | Action | Responsibility |
| ---- | ------ | -------------- |
| `src/openconstraint_mcp/runtime_install.py` | Create | Constants, error type, download, extract, smoke-check, orchestrator. |
| `src/openconstraint_mcp/runtime.py` | Modify | Add `_config_path`, `read_install_config`, `write_install_config`; extend `get_runtime_dir` precedence to env > config > default. |
| `src/openconstraint_mcp/schemas.py` | Modify | Add `InstallConfig(BaseModel)` with `runtime_dir: str`. |
| `src/openconstraint_mcp/cli.py` | Modify | Replace `install-runtime` placeholder with the real command: lazy installer import, `--runtime-dir`/`--yes` flags, interactive prompt, config write on success. |
| `tests/conftest.py` | Modify | Add `fake_minizinc_tarball` and `isolated_config_dir` fixtures. |
| `tests/test_runtime_install.py` | Create | Unit tests for the installer (see Task tests). |
| `tests/test_runtime.py` | Modify | Tests for config read/write and `get_runtime_dir` precedence. |
| `tests/test_cli.py` | Modify | Replace placeholder test with new install-runtime CLI tests (mocked installer + prompt + config write); add lazy-import regression test. |
| `README.md` | Modify | Document new flags, prompt behavior, config persistence; fix the existing default-path mention. |

`pyproject.toml` is **not** modified.

---

## Module-level interface (signatures only)

These pin the contracts that tasks build against; later tasks reference them by name.

```python
# runtime_install.py
MINIZINC_VERSION: str = "2.9.7"
_ARCHIVE_SHA256: str  # filled in by Task 6

class RuntimeInstallError(RuntimeError): ...

def install_managed_runtime(
    runtime_dir: Path,
    *,
    yes: bool = False,
    console: Console | None = None,
) -> Path: ...

# Public helpers (also called from the CLI, so they are *not* underscore-prefixed):
def check_supported_platform() -> None: ...                    # raises RuntimeInstallError on unsupported
def is_managed_runtime_dir(path: Path) -> bool: ...            # True iff path/.openconstraint-runtime.json parses with managed_by=="openconstraint-mcp"

# Marker filename and shape (inline check, no separate model needed):
MANAGED_RUNTIME_MARKER: str = ".openconstraint-runtime.json"
# Contents written by the installer:
#   { "managed_by": "openconstraint-mcp", "minizinc_version": "<version>" }

# Private helpers (named here because tests monkeypatch them):
def _download_archive(url: str, dest: Path, expected_sha256: str, console: Console) -> None: ...
def _extract_bundle(archive: Path, dest: Path) -> None: ...
def _smoke_check_binary(binary: Path) -> None: ...
def _write_runtime_marker(runtime_dir: Path) -> None: ...      # writes MANAGED_RUNTIME_MARKER with current version
```

```python
# runtime.py additions
def _config_path() -> Path: ...                              # platformdirs user_config_dir
def read_install_config() -> InstallConfig | None: ...       # None on missing/malformed
def write_install_config(runtime_dir: Path) -> None: ...
def get_runtime_dir() -> Path: ...                           # env > config > platformdirs default
```

```python
# schemas.py addition
class InstallConfig(BaseModel):
    runtime_dir: str  # validated non-empty + absolute by a field_validator (Task 7)
```

---

## Task list

Each task is TDD: write tests describing the behavior, run them red, implement, run them green, then verify with the listed command.

### Task 1: Module skeleton

**Files:** create `src/openconstraint_mcp/runtime_install.py`, create `tests/test_runtime_install.py`.

- [ ] Add empty module exporting `MINIZINC_VERSION = "2.9.7"`, `RuntimeInstallError(RuntimeError)`, and a `install_managed_runtime` stub that raises `NotImplementedError`.
- [ ] Tests: assert the three names exist, `RuntimeInstallError` is a `RuntimeError` subclass, `MINIZINC_VERSION == "2.9.7"`.
- [ ] Verify: `just pytest tests/test_runtime_install.py -v` → green.

### Task 2: `fake_minizinc_tarball` fixture

**Files:** modify `tests/conftest.py`.

- [ ] Add a fixture that builds a `tar.gz` mirroring the real bundle layout: a single top-level wrapper directory (`MiniZincIDE-2.9.7-bundle-linux-x86_64/`) containing `bin/minizinc` (executable shell stub that prints `minizinc 2.9.7 (fake)`) and `share/README`. The wrapper dir is required so Task 3's prefix-strip is exercised.
- [ ] Sanity test: open the fixture archive and assert there's exactly one top-level directory and at least one member ending in `/bin/minizinc`.
- [ ] Verify: `just pytest tests/test_runtime_install.py -v` → green.

### Task 3: Safe extraction with prefix-strip

**Files:** modify `runtime_install.py`, modify `tests/test_runtime_install.py`.

Behavior of `_extract_bundle(archive, dest)`:

- Extract `archive` into a `_extract` sub-directory of `dest` using `tarfile.extractall(..., filter="data")` (the upstream-blessed CVE-2007-4559 defense).
- Refuse if extraction yields more than one top-level entry or if that entry is not a directory.
- Move the contents of the single top-level wrapper up so they live directly under `dest`; remove the now-empty wrapper and `_extract` dir.

Tests:

- Happy path: extract the fake fixture into a tmp dir; `dest/bin/minizinc` exists and `os.access(..., X_OK)` is true.
- Path traversal — three cases, each in its own malicious tar built in the test:
  - Member with `..` segments resolving outside the destination.
  - Member with an absolute path (`/etc/passwd`).
  - Symlink member with an absolute target.
  All three must raise `RuntimeInstallError`.
- Two top-level directories: must raise `RuntimeInstallError`.

Verify: `just pytest tests/test_runtime_install.py -v` → green.

### Task 4: Streaming download + SHA256 verify

**Files:** modify `runtime_install.py`, modify `tests/test_runtime_install.py`.

Behavior of `_download_archive(url, dest, expected_sha256, console)`:

- Open a context-managed `httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))` and call `client.stream("GET", url)` for the response body. (Use the `Client(...).stream(...)` form, **not** top-level `httpx.stream(...)`, because the tests patch `httpx.Client`; patching the top-level helper would silently bypass the mock.)
- Update a `rich.progress` bar (with `DownloadColumn`, `TransferSpeedColumn`, `TimeRemainingColumn`) from `content-length` if present.
- Compute SHA256 incrementally as bytes arrive.
- On HTTP non-2xx, on any `httpx.HTTPError`, or on SHA256 mismatch: delete `dest` and raise `RuntimeInstallError` with a clear message. A partial or tampered file must never be left on disk.

Tests (no real network):

- Patch `httpx.Client` to return a client bound to an `httpx.MockTransport`. **Capture the real `httpx.Client` before patching** so the factory can construct an actual client without recursing into itself.
- Successful download with matching SHA256: bytes land at `dest`.
- SHA256 mismatch: raises with `"checksum"` in the message; `dest` does not exist after.
- HTTP 404: raises with `"download"` in the message.

Verify: `just pytest tests/test_runtime_install.py -v` → green.

### Task 5: Orchestrator `install_managed_runtime`

**Files:** modify `runtime_install.py`, modify `tests/test_runtime_install.py`.

Behavior, in order:

1. Default `console` to a quiet `Console` if `None`.
2. `check_supported_platform()` (public, no underscore — see "Module-level interface" above): raise `RuntimeInstallError` on anything other than `sys.platform == "linux"` and `platform.machine() == "x86_64"`. Message must mention "Linux x86_64" and point at the manual env-var workaround.
3. Resolve `runtime_dir = runtime_dir.resolve()`. If it exists but is not a directory: raise `RuntimeInstallError("target exists but is not a directory: <path>")` before any download. If it exists, is a directory, and is non-empty: call `is_managed_runtime_dir(runtime_dir)` (read + validate-parse `<runtime_dir>/.openconstraint-runtime.json`; True iff the file exists, is valid JSON, and contains `"managed_by": "openconstraint-mcp"`).
   - **Unmanaged non-empty target:** raise `RuntimeInstallError` regardless of `yes`. The message must name the path, explain that this directory does not look like a prior managed install, and tell the user to pick an empty directory or remove the contents themselves. This is the safety net for `--runtime-dir $HOME --yes` mistakes.
   - **Managed non-empty target + `yes is False`:** raise (`"refusing to overwrite non-empty runtime directory"`) as before.
   - **Managed non-empty target + `yes is True`:** proceed (this is a legitimate re-install of our own runtime).
4. Compute `staging = parent / f".{name}.staging.{pid}"` and `backup = parent / f".{name}.backup.{pid}"` as siblings of `runtime_dir`. Create `parent` if missing. If a stale `staging.*` (any pid) exists, remove it. If any stale `backup.*` (any pid) exists from a prior kill (`SIGKILL`/power loss) or a failed post-success cleanup, raise with a message that branches on the current state of `runtime_dir`:
   - **If `runtime_dir` is missing** (install was interrupted mid-swap): "Recover by running `mv <backup> <runtime_dir>` to restore the prior runtime, then re-run `install-runtime`." Restoring the backup is safe — there is nothing at the target.
   - **If `runtime_dir` exists** (the new runtime is already in place; the backup is leftover from a cleanup failure on the previous successful install): "Remove it with `rm -rf <backup>`." Do **not** suggest `mv` here — that would clobber a healthy install with the prior runtime.

   In both branches, name the backup path explicitly and refuse to proceed. Refusing prevents a second install from either clobbering the user's only remaining copy of the prior runtime or compounding the cleanup leftover.
5. **Phase 1 (no target mutation):** in an OS `TemporaryDirectory`, call `_download_archive` → `_extract_bundle` into `staging` → `_smoke_check_binary` on `staging/bin/minizinc` → `_write_runtime_marker(staging)` so the swapped-in directory is self-identifying for the *next* install run's `is_managed_runtime_dir` check. Catch `BaseException` (not just `Exception`) so a Ctrl-C during the download or extract removes `staging` before propagating `KeyboardInterrupt`.
6. **Phase 2 (rollback-safe swap).** Track two state flags initialised `False`: `moved_aside` (set after the prior runtime is renamed to backup) and `target_swapped` (set after the staging tree is renamed into place). Wrap the swap in a `try` that catches `BaseException`:
   1. If `runtime_dir` exists: `runtime_dir.rename(backup)`, then `moved_aside = True`.
   2. `staging.rename(runtime_dir)`, then `target_swapped = True`.
   In the `except BaseException` handler: always remove `staging` if it still exists (`ignore_errors=True`). Then **only if `not target_swapped`** restore the backup: if `moved_aside and backup.exists()`, attempt `backup.rename(runtime_dir)`; on failure raise `RuntimeInstallError` whose message names the backup path and the exact `mv <backup> <runtime_dir>` recovery command. If `target_swapped` is already `True`, do **not** touch backup — the new runtime is in place and restoring would destroy a successful install. Re-raise the original exception in all error cases.

   After the `try/except` (success path falls through): if `backup` still exists, attempt `shutil.rmtree(backup)`. If that raises `OSError`, **do not** propagate it (the install itself succeeded) but **do** print a yellow warning naming the backup path and suggesting the manual `rm -rf <backup>` so the user is not left with hidden disk usage. Doing the backup cleanup **outside** the try keeps a stray rmtree failure from poisoning an otherwise-successful install. Guarding it with `backup.exists()` means a fresh install (no prior runtime, so backup was never created) does not crash trying to remove a non-existent path.

   The `BaseException` handler makes Ctrl-C during the rename window restorable; `SIGKILL`/`SIGTERM` are uncatchable and may leave a `.<name>.backup.<pid>` sibling, which the next install run detects at step 4 (stale-backup check) and refuses with a clear recovery message.
7. Print a green success line; return the resolved `runtime_dir`.

Behavior of `_smoke_check_binary(binary)`:

- Verify `binary.is_file()` and `os.access(binary, X_OK)`; raise otherwise.
- Run `[binary, "--version"]` with `capture_output=True, text=True, timeout=30, check=True`.
- Raise `RuntimeInstallError` (wrapping the original) on `CalledProcessError`, `TimeoutExpired`, or `OSError`, and on stdout that does not contain `"minizinc"` (case-insensitive).

Tests:

- Use a `stub_linux_x86_64` fixture that monkeypatches `sys.platform = "linux"` and `platform.machine` → `"x86_64"`.
- Use a `stub_download_with_fixture` fixture that monkeypatches `_download_archive` to `shutil.copy(fake_minizinc_tarball, dest)`.
- Happy path into a missing target: returns `runtime_dir.resolve()`, `bin/minizinc` exists and is executable.
- Unsupported platform (darwin): raises with `"Linux x86_64"`.
- Unsupported arch (linux/aarch64): raises with `"Linux x86_64"`.
- **Unmanaged non-empty target refuses even with `yes=True`:** create a target dir containing `unrelated.txt` (no marker file). Call installer with `yes=True`. Raises `RuntimeInstallError` whose message names the path and tells the user to pick an empty directory; `unrelated.txt` is still on disk; `_download_archive` was not called.
- Managed non-empty target without `yes`: create a target dir with a valid `.openconstraint-runtime.json` marker (use `_write_runtime_marker` directly, or write the JSON by hand in the test). Raises with `"not empty"`; the prior marker is still on disk.
- Managed non-empty target with `yes=True`: same setup; succeeds; new `bin/minizinc` exists; the prior marker file from the old install is gone (the swap replaced the entire directory); a *new* marker is present.
- **Target is a regular file:** create a file at the target path before invoking the installer; raises `RuntimeInstallError` matching `"not a directory"` *before* any download is attempted (verifiable by asserting `_download_archive` was not called — leave the monkeypatched download recorded via a list).
- **Stale backup, runtime missing → mv message:** create `<parent>/.runtime.backup.99999`; do **not** create `runtime_dir`. Call installer with `yes=True`. Raises `RuntimeInstallError` whose message contains the backup path and the literal `mv <backup> <runtime_dir>` recovery command (not `rm`); `_download_archive` was not called; the stale backup is untouched.
- **Stale backup, runtime present → rm message:** create both `<parent>/.runtime.backup.99999` and a managed `runtime_dir` (with marker). Call installer with `yes=True`. Raises `RuntimeInstallError` whose message contains the backup path and the literal `rm -rf <backup>` instruction (not `mv`); `_download_archive` was not called; both the stale backup and the healthy runtime are untouched.
- **Marker is written on success:** happy-path install; after the call returns, assert `<runtime_dir>/.openconstraint-runtime.json` exists, parses as JSON, and contains `managed_by == "openconstraint-mcp"` and `minizinc_version == MINIZINC_VERSION`.
- Smoke-check failure (monkeypatch `_smoke_check_binary` to raise): no `bin/minizinc` ends up under `runtime_dir`.
- **Rename-failure rollback:** prior target with a marker file; monkeypatch `Path.rename` to fail on its second call (the staging→target rename). After the orchestrator raises, the prior target dir exists with the marker file intact and there are no leftover `.<name>.staging.*` / `.<name>.backup.*` siblings.

Verify: `just pytest tests/test_runtime_install.py -v` → green.

### Task 6: Pin the real SHA256

**Files:** modify `runtime_install.py`.

- [ ] One-time: run `curl -fsSL --retry 3 https://github.com/MiniZinc/MiniZincIDE/releases/download/2.9.7/MiniZincIDE-2.9.7-bundle-linux-x86_64.tgz -o /tmp/minizinc-2.9.7.tgz && sha256sum /tmp/minizinc-2.9.7.tgz` and capture the 64-char hex digest. The `-f` flag is critical: without it, a GitHub HTML error page would be silently hashed and the wrong digest pinned.
- [ ] Replace `_ARCHIVE_SHA256 = ""` with the pinned value. Add a one-line comment naming the archive so future bumps know what to recompute.
- [ ] Verify: `just pytest tests/test_runtime_install.py -v` → still green (no test depends on the literal).

### Task 7: Persist install location in `runtime.py`

**Files:** modify `schemas.py`, modify `runtime.py`, modify `tests/conftest.py`, modify `tests/test_runtime.py`.

- [ ] Add `InstallConfig(BaseModel)` to `schemas.py` with `runtime_dir: str` **and a `field_validator("runtime_dir")` that rejects empty strings and any string whose `Path(...).is_absolute()` is `False`** — raise `ValueError` in those cases so Pydantic reports a `ValidationError`. The writer always passes `runtime_dir.resolve()` (absolute), so this validator only fires on a hand-edited or corrupted config file; `read_install_config` swallows the `ValidationError` and returns `None`, falling back to the platformdirs default rather than silently honouring `""` or `"./runtime"`.
- [ ] Add `_config_path() -> Path` that returns `Path(PlatformDirs("openconstraint-mcp", "openconstraint-mcp").user_config_dir) / "install.json"` — the platformdirs call already yields `~/.config/openconstraint-mcp` on Linux, so do not append an extra `openconstraint-mcp/` segment (matches the doubling bug called out in the safety section). Add `read_install_config() -> InstallConfig | None` and `write_install_config(runtime_dir: Path) -> None` to `runtime.py`. Read swallows `OSError`, `JSONDecodeError`, and `ValidationError` by returning `None` so a corrupted config never blocks read-side commands. Write creates the parent dir, serializes via `model_dump_json(indent=2)`.
- [ ] Extend `get_runtime_dir()` precedence to **env > config > platformdirs default**. Keep the existing default branch unchanged.
- [ ] Add an `isolated_config_dir` fixture to `conftest.py` that monkeypatches `runtime._config_path` to a tmp file **and** `monkeypatch.delenv("OPENCONSTRAINT_MCP_RUNTIME_DIR", raising=False)`, so config tests are hermetic against a developer's shell env.

Tests (append to `tests/test_runtime.py`):

- `read_install_config()` returns `None` when the file is absent, when contents are not JSON, when JSON does not match the schema, when `runtime_dir` is the empty string, and when `runtime_dir` is a relative path. (The first three are read-failure cases; the last two exercise the new validator.)
- Write-then-read round-trips a `Path` (resolved).
- `get_runtime_dir()` returns the env path when env is set even though config is also set (env wins).
- `get_runtime_dir()` returns the config path when env is unset.
- `get_runtime_dir()` returns the platformdirs default (`…/minizinc`) when both env and config are unset.

Existing `test_runtime.py` tests continue to pass because they use `fake_runtime_dir`, which sets the env var.

Verify: `just pytest tests/test_runtime.py -v` → all green.

### Task 8: Wire installer into the CLI

**Files:** modify `cli.py`, modify `tests/test_cli.py`.

Behavior of the new `install-runtime` command:

- Typer signature: `--runtime-dir` (optional `Path`) and `--yes`/`-y` (bool, default false).
- **Lazy-import** `install_managed_runtime`, `RuntimeInstallError`, `check_supported_platform`, **and `is_managed_runtime_dir`** (all public) from `runtime_install` *inside* the command body, and `get_runtime_dir`/`write_install_config` from `runtime`. This keeps `httpx` and `rich.progress` out of `stdio` / `check-runtime` / `list-solvers` cold paths.
- **Platform check first.** Immediately after the lazy import, call `check_supported_platform()`; on `RuntimeInstallError`, print the message in red and `raise typer.Exit(1)`. This runs **before** any path resolution or interactive prompt so macOS/Windows/Linux-ARM users are not asked to pick an install location only to be told the platform is unsupported.
- Resolve `target`:
  1. If `--runtime-dir` was passed: use it; skip the path prompt.
  2. Else compute `default = get_runtime_dir()`. If stdin is a TTY and `--yes` was not passed: show the default and accept either Enter (default) or a typed path. Else: use `default` directly.
- **Overwrite-confirm step.** First, if `target` exists but is not a directory (regular file, symlink to a file, etc.), print a clear error and `raise typer.Exit(1)` — do not prompt, do not proceed. Then, if `target` exists, is a directory, and is non-empty, call `is_managed_runtime_dir(target)`:
  - **Unmanaged non-empty target** → print a clear error: target is not empty and does not look like a prior managed install; user must pick an empty directory or remove the contents themselves. `raise typer.Exit(1)`. Do **not** prompt, do **not** accept `--yes` here. (`install_managed_runtime` would also refuse, but the CLI message lands before any download is even attempted.)
  - Managed non-empty target + `--yes` was passed → set `effective_yes = True` and proceed.
  - Managed non-empty target + stdin is a TTY + no `--yes` → prompt `"<target> is a prior managed runtime. Overwrite? [y/N]"`. On `y`/`yes`: set `effective_yes = True`. On anything else: print "aborted, nothing was changed" and `raise typer.Exit(0)` (clean exit, not an error).
  - Managed non-empty target + stdin not a TTY + no `--yes` → print a clear error naming the target and the `--yes` flag, `raise typer.Exit(1)`.
  - Otherwise (target absent or empty directory) → `effective_yes = yes` (whatever the user passed).
- Call `install_managed_runtime(target, yes=effective_yes, console=_console)`. On `RuntimeInstallError`: print a red error line, `raise typer.Exit(1)`.
- On success: `write_install_config(installed)` and print a green "Runtime installed at …" line.

Tests (replace the existing `test_install_runtime_is_placeholder`):

**Test-file-scoped autouse fixture (critical — without this, every test below exits 1 on macOS/Windows/Linux-ARM dev machines).** Add an `autouse=True` fixture in `tests/test_cli.py` that monkeypatches `openconstraint_mcp.runtime_install.check_supported_platform` to a no-op (return `None`). The unsupported-platform test below overrides this inside its body by re-monkeypatching `check_supported_platform` to raise — this works because pytest's `monkeypatch` honors the last assignment within a test.

- With `--runtime-dir <tmp> --yes`: monkeypatch `install_managed_runtime` to create `bin/minizinc` and return the path; CLI exits 0, the printed path matches `tmp`, and `read_install_config()` (via an `isolated_config_dir` fixture) returns a record pointing at `tmp`.
- Without `--runtime-dir`, with `--yes`: installer is called with `get_runtime_dir()` as target.
- Without `--runtime-dir`, no `--yes`, TTY simulated: monkeypatch `click.termui.visible_prompt_func` (or `typer.prompt`) to return a custom path; installer is called with that custom path.
- **Overwrite confirm — accepted:** non-empty target, no `--yes`, TTY simulated, prompt-mock returns `"y"`. Installer is called with `yes=True`; CLI exits 0.
- **Overwrite confirm — declined:** non-empty target, no `--yes`, TTY simulated, prompt-mock returns `"n"` (or empty string). Installer is **not** called; CLI exits 0 with an "aborted" message; the prior target contents are still on disk.
- **Overwrite refused in non-TTY:** *managed* non-empty target, no `--yes`, stdin not a TTY (monkeypatch `sys.stdin.isatty` to return `False`). Installer is not called; CLI exits 1; the error names `--yes`.
- **Unmanaged non-empty target refused with `--yes`:** target dir contains an unrelated file (no marker); pass `--yes`. CLI exits 1 with a message telling the user to pick an empty dir; installer was not called; unrelated file is still on disk; no prompt was shown.
- **Target is a regular file:** create a file at the target path; CLI exits 1; the message names "not a directory"; the installer was not called and the user was not prompted.
- **Unsupported platform rejected before any prompt:** inside the test body, re-monkeypatch `check_supported_platform` (public) to raise `RuntimeInstallError("…Linux x86_64…")`, overriding the autouse no-op fixture; assert the CLI exits 1, the error mentions "Linux x86_64", and `typer.prompt` was not called and `install_managed_runtime` was not called. (Record both as monkeypatched no-ops with call counters.)
- Installer raises `RuntimeInstallError("simulated failure")`: CLI exits 1 and prints the message.
- `test_cli_module_does_not_import_httpx_eagerly`: clear `httpx` and CLI-related modules from `sys.modules`, re-import `openconstraint_mcp.cli`, assert `"httpx" not in sys.modules`.

Verify: `just pytest tests/test_cli.py -v` → all green.

### Task 9: README

**Files:** modify `README.md`.

- [ ] Replace the `install-runtime` bullet (currently "placeholder in v0") with: behaviour summary, the two flags, the interactive-prompt behaviour, the note that this is the only network-using command. **Spell out the safety boundary on `--yes`**: it skips the path prompt (so the install goes to the env/config/default path without asking) and skips the overwrite-confirmation prompt **for a prior managed runtime only**. It does **not** force overwrite of an unmanaged non-empty directory — pointing `--runtime-dir` at `$HOME`, `/tmp`, or any directory we did not previously install into is refused regardless of `--yes`. The marker file `.openconstraint-runtime.json` is what makes a directory eligible for overwrite. Recommend `--runtime-dir <path>` as the explicit form when humans want to be sure where the install lands.
- [ ] Add a new "Installing the managed runtime" subsection under "Managed runtime" covering: the default path (`<platformdirs user_data_dir>/minizinc` — **no extra app-name segment**; current code is `Path(dirs.user_data_dir) / "minizinc"`), how `--runtime-dir` interacts with the persisted config (subsequent commands automatically pick up the chosen path), how to revert (delete the config file or set the env var).
- [ ] Update the "v0 limitations" section: drop "managed runtime is not yet auto-downloaded", drop "`httpx` … is not imported anywhere in v0", replace with "automated installer is Linux x86_64 only" and "the only code path that touches the network is the `install-runtime` CLI command".
- [ ] **Add a "Licensing & upstream sources" section** (AGENTS.md requires surfacing third-party licenses for anything bundled). Cover: (a) the managed runtime is **fetched** from the official MiniZincIDE GitHub release at install time — we do not redistribute MiniZinc in this git repo; (b) the upstream bundle includes MiniZinc itself and bundled solvers (Gecode, Chuffed, OR-Tools CP-SAT, COIN-BC, etc.) — link to https://www.minizinc.org/ for the upstream license index; (c) license files for the bundled components live inside the installed runtime directory (`<runtime_dir>/share/minizinc/...` and adjacent) and stay there untouched after install; (d) for users who want a single document, point at the MiniZinc release page where upstream surfaces per-solver licenses.
- [ ] **Verify the path strings against the current `get_runtime_dir()` implementation in `runtime.py`** (don't pin a line number — it'll drift). The previous draft doubled `openconstraint-mcp` in the path; the correct shape is `<platformdirs user_data_dir>/minizinc`.

Verify: read the rendered README sections; no code to test.

### Task 10: Final `just check` + manual smoke test

- [ ] `just check` — lint + typecheck + tests green.
- [ ] Manual integration smoke (Linux x86_64 only, not part of `just check`):
  - `just cli install-runtime --runtime-dir /tmp/oc-mcp-real --yes` → prints download progress, ends with "Installed MiniZinc 2.9.7 at /tmp/oc-mcp-real".
  - `just cli check-runtime` → reports `Runtime installed at /tmp/oc-mcp-real/bin/minizinc`. (No env var needed — the install wrote the config in Task 8.)
  - `just cli list-solvers` → prints a non-empty solver table.
- [ ] If `list-solvers` fails with a shared-library error, add a minimal `env=` to the `subprocess.run` call in `minizinc.list_solvers` that prepends `<runtime_dir>/lib` to `LD_LIBRARY_PATH`. **Do not add this shim preemptively** — evidence-driven only.

## Acceptance criteria

- `just check` green.
- On Linux x86_64, a fresh `install-runtime --runtime-dir <path> --yes` produces `<path>/bin/minizinc` that answers `--version`; `check-runtime` and `list-solvers` work afterwards with no env-var fiddling.
- On macOS / Windows / Linux ARM, `install-runtime` exits 1 with a clear message naming "Linux x86_64" and the env-var workaround. No half-installed state.
- A handled install failure (smoke-check failure, rename failure, Ctrl-C / `KeyboardInterrupt`) leaves either the prior runtime intact or the target absent, with no leftover `.<name>.staging.*` siblings. Backup siblings are never left behind by a handled failure; on a *successful* install they are removed, and if that removal raises `OSError` the install still succeeds but a yellow warning names the leftover backup so the user can remove it manually.
- An unmanaged non-empty target (a directory we did not previously install into, identified by the absence of a parseable `.openconstraint-runtime.json` marker) is refused both at the CLI overwrite-confirm step and inside `install_managed_runtime`, regardless of `--yes`. `--runtime-dir $HOME --yes` (or any analogous mistake) cannot destroy user data.
- A successful install leaves `<runtime_dir>/.openconstraint-runtime.json` containing `managed_by == "openconstraint-mcp"` and `minizinc_version == MINIZINC_VERSION`, so the next install run can recognise the directory as overwriteable.
- An unhandled kill (`SIGKILL` / `SIGTERM`, or a power loss) between the two Phase-2 renames may leave a `.<name>.backup.<pid>` sibling. The next `install-runtime` invocation detects this at the stale-backup check and refuses to proceed with a message naming the backup path and the manual `mv` command to restore.
- Importing `openconstraint_mcp.cli` does not import `httpx` (regression test).
- README documents the flags, the persisted config, and the default path correctly against `runtime.py`.
- No new telemetry, no new hidden network calls, no new global mutable state.

## Known risks

- **`tarfile.extractall(..., filter="data")` rejection list may include members the real bundle relies on** (e.g. symlinks pointing inside the wrapper dir but expressed as absolute paths). Mitigation: Task 10's manual integration smoke is the first place this would surface (the unit tests in Task 3/5 use a synthetic fixture); the data filter does permit safe relative symlinks, which is what MiniZinc's `lib/` ships.
- **Cross-filesystem rename.** Phase 2 renames assume `staging`, `backup`, and `runtime_dir` are on the same filesystem (they're siblings, so they are unless a bind mount intervenes). If a user picks `--runtime-dir` on a different mount than its parent's other entries, the rename could still fail; the rollback path handles this by leaving the prior runtime intact.
- **SHA256 drift on upstream republish.** If MiniZinc re-uploads the 2.9.7 asset with a different digest, Task 6's pin will reject the new asset and the installer will fail loudly until the constant is rebumped. This is the desired behavior, but worth noting for the next person to bump the version.
