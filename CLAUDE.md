# CLAUDE.md

Guidance for working on **marimo-wasm-utils**. Read this first.

## What this project does

Tooling to build [marimo](https://marimo.io) `html-wasm` notebook exports that run Python
(Pyodide, in the browser) **fully against a local/offline PyPI index** instead of the public
internet. After building + patching, loading a notebook produces **zero** network requests
to `files.pythonhosted.org`, `test-files.pythonhosted.org`, or `pypi.org` — every wheel and
package-index lookup is rewritten to a local index (default
`http://127.0.0.1:8000/pypi-index`).

Use cases: airgapped / corporate deployment (where PyPI is blocked but an internal mirror
exists), reproducible offline notebooks.

It does this **without a service worker** — see "How the reroute works" below.

## Repo layout

| Path | Purpose |
| --- | --- |
| `src/marimo_wasm_utils/cli.py` | **All the logic.** A `typer` CLI built from a `@dataclass CLI`. Also holds `JS_PATCH`, the runtime reroute shim. |
| `notebooks/*.py` | marimo notebooks (`main.py`, `main2.py`) with PEP 723 inline `# /// script` deps. |
| `tests/scripts_test.py` | Smoke test: runs each `[project.scripts]` entry point with `--help`. |
| `tests/verify_reroute.py` | End-to-end Playwright harness (zero-pythonhosted assertion). |
| `build/` | **Generated.** `build/marimo/` (patched export), `build/pypi-index/` (local index), `build/requirements.txt`. |
| `pypi-index/` (root) | A previously generated index (sample/scratch). |

Entry point: `marimo-wasm-utils = marimo_wasm_utils.cli:app` (pyproject `[project.scripts]`).

## CLI commands & the `run()` pipeline

Subcommands (kebab-case): `extract-deps`, `create-dumb-index`, `download-lockfile-wheels`,
`export-notebooks`, `patch-js`, `run`.

`run()` does the whole build, in order:

1. **`extract_deps`** — read PEP 723 deps from `notebooks/*.py` → `build/requirements.txt`
   (skips `marimo*`).
2. **`create_dumb_index`** — `pip download` the notebook deps **and their dependency
   closure** into `build/pypi-index/wheels/`, then generate the index with
   [`dumb_pypi`](https://github.com/chriskuehl/dumb-pypi).
3. **`download_lockfile_wheels`** — `pip download --no-deps` marimo's pinned **bootstrap
   wheels** (from the lockfile) into the same index, then regenerate. See gotchas below.
4. **`export_notebooks`** — `marimo export html-wasm` each notebook into `build/marimo/`
   (plus a static listing `index.html`).
5. **`patch_js`** — inject the reroute shim into the export (idempotent).

pip is invoked with `--only-binary=:all: --implementation py --abi none --platform any
--python-version 3.12` so it fetches the **pure `py3-none-any` wheels for the Pyodide
target** (CPython 3.12), regardless of the build machine's platform — otherwise a package
that also ships platform wheels (e.g. `black`) would download the wrong file and its
filename wouldn't match what the runtime requests.

## How the reroute works (the core trick)

`JS_PATCH` in `cli.py` is a guarded IIFE shim (idempotency marker `/* pypi-reroute v1 */`,
`globalThis.__PYPI_REROUTE__`). `patch_js` injects it into:

- **every notebook HTML page** — those containing `<marimo-wasm` (the static listing
  `index.html` has no such marker and is left alone), as a `<script>` right after `<head>`;
- **every `assets/*worker*.js`** — prepended, because Pyodide runs in **web workers** that
  do **not** inherit main-thread monkeypatches, and marimo emits two worker chunks
  (`worker-*.js` kernel + `save-worker-*.js`) that both boot Pyodide and fetch wheels.

The shim overrides `fetch` and `XMLHttpRequest.prototype.open`:

- Intercepts only `files.pythonhosted.org`, `test-files.pythonhosted.org`, `pypi.org`.
  **jsDelivr (`cdn.jsdelivr.net`) and `wasm.marimo.app` pass through untouched** — they
  serve the Pyodide runtime, bundled wheels, and the lockfile.
- `pypi.org/pypi/{name}/json` and `/simple/...` → string-rewritten to the local index.
- Wheel URLs → resolved via the local Warehouse-style JSON API
  (`{BASE}/pypi/{name}/json`): it searches both `urls` **and** `releases` (dumb_pypi lists
  only the latest version under `urls`) and resolves the entry's **relative** `url`
  (`../../wheels/…`) against the JSON request URL.
- XHR wheel resolution is done with a **synchronous** sub-XHR at `open()` time, since
  `pyodide-http` uses sync XHR in workers. The primary wheel path is `fetch`; XHR is
  belt-and-suspenders.

The index base is configurable: `patch_js(dist, index_base=...)` (default
`http://127.0.0.1:8000/pypi-index`).

## Bootstrap-wheel / lockfile gotchas

These are non-obvious and easy to re-break:

- **The lockfile URL must include the pyodide param.** The WASM runtime fetches
  `https://wasm.marimo.app/pyodide-lock.json?v={marimo}&pyodide=v{PYODIDE_VERSION}`.
  Without `&pyodide=...` the server returns a *different* pinned set (e.g. markdown 3.10.2
  vs the runtime's 3.6). `_lockfile_url()` builds it from
  `marimo._pyodide.pyodide_constraints.PYODIDE_VERSION` + `marimo._version.__version__`.
- **`download_lockfile_wheels` must use `pip --no-deps`.** The lockfile pins
  *pyodide-patched* versions that violate the packages' own metadata — e.g. `marimo-base
  0.23.9` declares `pymdown-extensions>=10.21.2` but the lockfile (and the browser) use
  `10.8.1`. A resolved install fails with `ResolutionImpossible`, so we download exactly the
  pinned wheels and nothing else. (Notebook deps' closure is handled separately by
  `create_dumb_index`, which *does* resolve.)
- **Integrity is preserved by byte-identity.** Pyodide checks each bootstrap wheel's sha256
  against the lockfile. PyPI/mirror files are immutable, so a pinned `name==version` wheel
  matches. **Never alter the lockfile or disable integrity checks.**
- **`marimo-base` lives on test-pypi**, and its lockfile `name` field says `"marimo"` while
  the actual wheel is `marimo_base-0.23.9-py3-none-any.whl`. Derive the pip specifier from
  the **wheel basename**, not the `name` field (see `_lockfile_specifiers`).
- **`wasm.marimo.app` is behind Cloudflare** and 403s the default urllib User-Agent →
  lockfile requests send `User-Agent: Mozilla/5.0`.

## Working on a different machine

- **Toolchain:** `mise.toml` pins python 3.12, `uv`, `ruff`, `pre-commit`. Env: this is a
  uv project (`uv.lock`); use `uv sync` (the `dev` group pulls `cli` + `test` + `types`).
  The CLI needs the `cli` optional-deps (`marimo`, `dumb-pypi`, `pip`, `typer-slim`,
  `python-metadata-parser`).
- **Lint/format:** `ruff check` / `ruff format` (config in `pyproject.toml`: `select=ALL`,
  line-length 100, single-line isort). Keep it clean — CI/pre-commit enforces it.
- **Types:** `mypy` strict (`hatch run types:check`); several other checkers are configured
  but optional.
- **Tests:** `pytest`. Note `tests/scripts_test.py` invokes the `marimo-wasm-utils` console
  script **by bare name**, so the venv's `bin/` must be on `PATH` (run via
  `PATH="$PWD/.venv/bin:$PATH" pytest`, or `uv run pytest`).
- **Browser verification** needs Chromium: `playwright install chromium` (`playwright` is in
  the `test` group).

## Corporate / airgapped notes

- `files.pythonhosted.org` is often **blocked**. Configure pip for the internal mirror
  (`PIP_INDEX_URL` / `pip.conf`); the build then routes both notebook deps and marimo's
  bootstrap wheels through the mirror. No direct pythonhosted access is needed at build time.
- **`wasm.marimo.app` must be reachable at build time** to read the lockfile (same host the
  notebook uses at runtime, so usually allowed). It serves the Pyodide runtime + lockfile,
  not wheels — leave it un-rerouted.
- The mirror **must serve `marimo-base==0.23.9` byte-identically** (it's a test-pypi build;
  confirm your mirror proxies it).
- **CORS:** serve the whole `build/` dir from a single origin
  (`python3 -m http.server 8000` from inside `build/`) so the notebook
  (`/marimo/main.html`) and the index (`/pypi-index/...`) share an origin — no CORS config
  needed, and the default `index_base` already matches. If served cross-origin, the index
  must return permissive `Access-Control-Allow-Origin` for both the JSON API and wheels.

## Verify end-to-end

```bash
playwright install chromium               # once
PIP_EXTRA_INDEX_URL=https://test.pypi.org/simple/ marimo-wasm-utils run   # build (test.pypi simulates a mirror that has marimo-base)
( cd build && python3 -m http.server 8000 ) &                            # serve one origin
.venv/bin/python tests/verify_reroute.py                                 # assert zero pythonhosted
```

`tests/verify_reroute.py` uses **Playwright + headless Chromium**, whose request events
**do** capture web-worker traffic (CDP / agent-browser do **not** — use Playwright). It
loads each notebook, waits for boot, and asserts **zero** requests to
pythonhosted/pypi.org while wheels + JSON come from `/pypi-index/...` — including a stronger
pass that hard-blocks those hosts (`context.route(..., abort)`) and confirms the notebook
still boots.
