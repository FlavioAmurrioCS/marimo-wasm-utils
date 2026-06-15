from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from textwrap import dedent
from typing import Any
from urllib.parse import urlsplit

import typer
from python_metadata_parser import pep0723

PYTHONHOSTED_HOSTS = frozenset({"files.pythonhosted.org", "test-files.pythonhosted.org"})
PATCH_MARKER = "/* pypi-reroute v1 */"
DEFAULT_INDEX_BASE = "http://127.0.0.1:8000/pypi-index"


def _project_name(requirement: str) -> str:
    """PEP 503-normalized project name from a requirement line or ``name==version``."""
    head = re.split(r"[=<>!~ ;\[]", requirement, maxsplit=1)[0]
    return head.strip().lower().replace("_", "-")

JS_PATCH = r"""/* pypi-reroute v1 */
(function () {
  var g = (typeof self !== "undefined") ? self : globalThis;
  if (g.__PYPI_REROUTE__) return;
  g.__PYPI_REROUTE__ = true;

  var BASE = "__INDEX_BASE__".replace(/\/+$/, "");
  var HOSTS = {
    "files.pythonhosted.org": 1,
    "test-files.pythonhosted.org": 1,
    "pypi.org": 1
  };

  function targetHost(u) {
    try { return HOSTS[new URL(u, "http://x/").host] === 1; } catch (e) { return false; }
  }
  function normalize(name) {
    return name.toLowerCase().replace(/[-_.]+/g, "-");
  }
  function distFromWheel(basename) {
    var stem = basename.replace(/\.whl$/, "");
    var m = stem.match(/^(.*?)-\d/);
    return normalize(m ? m[1] : stem);
  }
  // Returns a local URL string for pure string-rewrite cases, else null.
  function rewriteSimple(u) {
    var url = new URL(u, "http://x/");
    var m = url.pathname.match(/^\/pypi\/(.+?)\/json\/?$/);
    if (m) return BASE + "/pypi/" + m[1] + "/json";
    if (url.pathname === "/simple" || url.pathname.indexOf("/simple/") === 0) {
      return BASE + url.pathname + url.search;
    }
    return null;
  }
  function isWheel(u) {
    return /\.whl(\?|$)/.test(new URL(u, "http://x/").pathname);
  }
  function jsonUrlForWheel(basename) {
    return BASE + "/pypi/" + distFromWheel(basename) + "/json";
  }
  function pickLocal(data, basename, jsonUrl) {
    if (!data) return null;
    // A Warehouse-style JSON API lists only the latest version under "urls"; older
    // versions live under "releases". Search both so any requested wheel resolves.
    var pools = [data.urls || []];
    var releases = data.releases || {};
    for (var k in releases) {
      if (Object.prototype.hasOwnProperty.call(releases, k)) pools.push(releases[k]);
    }
    for (var p = 0; p < pools.length; p++) {
      var files = pools[p] || [];
      for (var i = 0; i < files.length; i++) {
        if (files[i].filename === basename) {
          return new URL(files[i].url, jsonUrl).href;
        }
      }
    }
    return null;
  }

  // ---- fetch override (async) ----
  var origFetch = g.fetch ? g.fetch.bind(g) : null;
  if (origFetch) {
    g.fetch = function (input, init) {
      var u = (typeof input === "string") ? input
            : (input && input.url) ? input.url : String(input);
      if (!targetHost(u)) return origFetch(input, init);

      var simple = rewriteSimple(u);
      var localPromise;
      if (simple) {
        localPromise = Promise.resolve(simple);
      } else if (isWheel(u)) {
        var basename = new URL(u, "http://x/").pathname.split("/").pop();
        var jsonUrl = jsonUrlForWheel(basename);
        localPromise = origFetch(jsonUrl)
          .then(function (r) { return r.json(); })
          .then(function (data) {
            var local = pickLocal(data, basename, jsonUrl);
            if (!local) console.warn("[pypi-reroute] wheel not found locally:", basename);
            return local;
          })
          .catch(function (e) {
            console.warn("[pypi-reroute] json lookup failed:", basename, e);
            return null;
          });
      } else {
        return origFetch(input, init);
      }

      return localPromise.then(function (local) {
        if (!local) return origFetch(input, init);
        if (typeof input === "string") return origFetch(local, init);
        return origFetch(new Request(local, input), init);
      });
    };
  }

  // ---- XMLHttpRequest override (sync-safe) ----
  var XHR = g.XMLHttpRequest;
  if (XHR && XHR.prototype && XHR.prototype.open) {
    var origOpen = XHR.prototype.open;
    var rewriteXHRUrl = function (url) {
      if (!targetHost(url)) return url;
      var simple = rewriteSimple(url);
      if (simple) return simple;
      if (!isWheel(url)) return url;
      var basename = new URL(url, "http://x/").pathname.split("/").pop();
      var jsonUrl = jsonUrlForWheel(basename);
      try {
        var x = new XHR();
        origOpen.call(x, "GET", jsonUrl, false); // synchronous sub-request
        x.send();
        if (x.status >= 200 && x.status < 300) {
          var local = pickLocal(JSON.parse(x.responseText), basename, jsonUrl);
          if (local) return local;
        }
        console.warn("[pypi-reroute] xhr wheel not found locally:", basename);
      } catch (e) {
        console.warn("[pypi-reroute] xhr json lookup failed:", basename, e);
      }
      return url;
    };
    XHR.prototype.open = function (method, url) {
      var rest = Array.prototype.slice.call(arguments, 2);
      return origOpen.apply(this, [method, rewriteXHRUrl(url)].concat(rest));
    };
  }
})();
"""


@dataclass
class CLI:
    def extract_deps(self, files: list[str], output: str = "requirements.txt") -> None:
        # delete requirements.txt if it exists
        if os.path.exists(output):
            os.remove(output)
        deps: set[str] = set()
        for file in files:
            with open(file) as f:
                script = f.read()
                metadata = pep0723.read(script)
                if metadata is not None:
                    deps.update(metadata["dependencies"])
        with open(output, "w") as f:
            for dep in sorted(deps):
                if dep.startswith("marimo"):
                    continue
                f.write(f"{dep}\n")

    def _regen_index(self, output: str = "pypi-index") -> None:
        """(Re)generate the dumb_pypi index over every wheel in ``output/wheels``."""
        wheel_dir = os.path.join(output, "wheels")
        os.makedirs(wheel_dir, exist_ok=True)
        wheels = [w for w in os.listdir(wheel_dir) if w.endswith(".whl")]
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            for wheel in wheels:
                f.write(f"{wheel}\n")
            f.flush()
            from dumb_pypi.main import (  # pyright: ignore[reportMissingTypeStubs]
                main as dumb_pypi_main,
            )

            dumb_pypi_main(
                [
                    "--package-list",
                    f.name,
                    "--output-dir",
                    output,
                    "--packages-url",
                    "../../wheels/",
                ]
            )

    def create_dumb_index(
        self, requirements_txt: str = "requirements.txt", output: str = "pypi-index"
    ) -> None:
        wheel_dir = os.path.join(output, "wheels")
        os.makedirs(wheel_dir, exist_ok=True)
        from pip._internal.cli.main import main as pip_main

        # Fetch pure ``py3-none-any`` wheels for the Pyodide target (CPython 3.12),
        # regardless of this build machine's platform. ``--implementation py --abi none``
        # excludes platform/cpython wheels (e.g. black ships both), so the downloaded
        # filenames match what the WASM runtime requests.
        pip_main(
            [
                "download",
                "-r",
                requirements_txt,
                "--dest",
                wheel_dir,
                "--only-binary=:all:",
                "--implementation",
                "py",
                "--abi",
                "none",
                "--platform",
                "any",
                "--python-version",
                "3.12",
            ]
        )
        self._regen_index(output)

    def _lockfile_url(self) -> str:
        """Build the lockfile URL the marimo WASM runtime fetches at boot."""
        from marimo._pyodide.pyodide_constraints import (  # pyright: ignore[reportMissingTypeStubs]
            PYODIDE_VERSION,
        )
        from marimo._version import (  # pyright: ignore[reportMissingTypeStubs]
            __version__ as marimo_version,
        )

        return (
            "https://wasm.marimo.app/pyodide-lock.json"
            f"?v={marimo_version}&pyodide=v{PYODIDE_VERSION}"
        )

    def download_lockfile_wheels(
        self,
        output: str = "pypi-index",
        lockfile_url: str = "",
    ) -> None:
        """Pip-download marimo's bootstrap wheels (from the configured mirror) into the index.

        The marimo WASM runtime boots a set of packages whose ``file_name`` in
        ``pyodide-lock.json`` is an absolute ``*.pythonhosted.org`` URL (marimo_base plus
        its pure-Python deps: markdown, pymdown-extensions, narwhals, ...). Build machines
        behind a corporate firewall can't reach ``files.pythonhosted.org`` directly, but
        ``pip`` is configured to fetch from an internal mirror. So we extract each package's
        ``name==version`` from the lockfile and ``pip download`` those exact pins from the
        mirror instead of hitting pythonhosted.

        ``--no-deps`` is essential: the lockfile pins the *pyodide-patched* versions, which
        deliberately violate the packages' own metadata ranges (e.g. marimo-base 0.23.9
        declares ``pymdown-extensions>=10.21.2`` but the lockfile -- and the browser --
        use 10.8.1). Letting pip resolve dependencies would fail with a version conflict,
        so we download exactly the pinned wheels and nothing else. The notebook deps' own
        dependency closure is handled separately by :meth:`create_dumb_index`.

        Integrity holds: a pinned ``name==version`` wheel from the mirror is byte-identical
        to the lockfile's pythonhosted file (PyPI files are immutable), so Pyodide's
        lockfile-sha256 check still passes. Packages bundled in the Pyodide CDN
        (micropip/msgspec/packaging/...) have a non-URL ``file_name`` and are skipped.

        The default lockfile URL matches what the WASM runtime actually fetches:
        ``?v={marimo}&pyodide=v{PYODIDE_VERSION}``. The ``&pyodide=`` param matters --
        without it wasm.marimo.app serves a *different* set of pinned versions (e.g.
        markdown 3.10.2 vs 3.6), which would not match the wheels the browser requests.
        """
        if not lockfile_url:
            lockfile_url = self._lockfile_url()

        # wasm.marimo.app is behind Cloudflare, which 403s the default urllib UA.
        req = urllib.request.Request(  # noqa: S310
            lockfile_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            lock = json.loads(resp.read())

        specs = self._lockfile_specifiers(lock)
        if not specs:
            return

        wheel_dir = os.path.join(output, "wheels")
        os.makedirs(wheel_dir, exist_ok=True)
        from pip._internal.cli.main import main as pip_main

        # ``--no-deps``: download exactly these pinned pyodide wheels (see docstring).
        # Pure ``py3-none-any`` wheels for the Pyodide target, like create_dumb_index.
        pip_main(
            [
                "download",
                *specs,
                "--no-deps",
                "--dest",
                wheel_dir,
                "--only-binary=:all:",
                "--implementation",
                "py",
                "--abi",
                "none",
                "--platform",
                "any",
                "--python-version",
                "3.12",
            ]
        )
        self._regen_index(output)

    @staticmethod
    def _lockfile_specifiers(lock: dict[str, Any]) -> list[str]:
        """``name==version`` for every lockfile package hosted on pythonhosted (de-duped)."""
        specs: list[str] = []
        seen: set[str] = set()
        packages: dict[str, Any] = lock.get("packages", {})
        for info in packages.values():
            file_name: str = info.get("file_name", "")
            if not file_name.startswith("http"):
                continue
            if urlsplit(file_name).hostname not in PYTHONHOSTED_HOSTS:
                continue
            # Derive name/version from the wheel basename, not the lockfile ``name`` field
            # (which is wrong for marimo-base: it says "marimo" but the wheel is
            # marimo_base-<v>-py3-none-any.whl).
            basename = file_name.rsplit("/", 1)[-1]
            name, _, rest = basename.partition("-")
            version = rest.partition("-")[0]
            if not (name and version) or _project_name(name) in seen:
                continue
            seen.add(_project_name(name))
            specs.append(f"{name}=={version}")
        return specs

    def export_notebooks(self, files: list[str], output: str = "marimo") -> None:
        import os
        import shutil

        if os.path.exists(output):
            shutil.rmtree(output)

        for file in files:
            basename = os.path.basename(file)
            cmd_export = [
                "export",
                "html-wasm",
                f"--output={output}/{basename.replace('.py', '.html')}",
                "--mode=edit",
                # "--watch",
                # "--show-code",
                # "--include-cloudflare",
                "--sandbox",
                "--force",
                # "--execute",
                file,
                # "--help"
            ]
            subprocess.run(["marimo", *cmd_export], check=True)  # noqa: S603, S607

        index_html_content = dedent("""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Marimo Notebooks</title>
        </head>
        <body>
            <h1>Marimo Notebooks</h1>
            <ul>
                {}
            </ul>
        </body>
        </html>
        """).format(
            "\n".join(
                f'<li><a href="{os.path.basename(file).replace(".py", ".html")}">{os.path.basename(file).replace(".py", "")}</a></li>'  # noqa: E501
                for file in files
            )
        )

        with open(os.path.join(output, "index.html"), "w") as f:
            f.write(index_html_content)

    def run(self, output: str = "build") -> None:
        os.makedirs(output, exist_ok=True)

        requirements_txt = os.path.join(output, "requirements.txt")
        pypi_index = os.path.join(output, "pypi-index")
        notebooks = [os.path.join("notebooks", x) for x in os.listdir("notebooks")]
        self.extract_deps(notebooks, output=requirements_txt)

        marimo_base_deps = ()
        with open(requirements_txt, "a") as f:
            f.writelines(f"{dep}\n" for dep in marimo_base_deps)

        # Notebook deps (with their dependency closure) come from pip/the mirror...
        self.create_dumb_index(requirements_txt=requirements_txt, output=pypi_index)
        # ...and marimo's pinned bootstrap wheels are pip-downloaded with --no-deps.
        self.download_lockfile_wheels(output=pypi_index)
        self.export_notebooks(notebooks, output=os.path.join(output, "marimo"))
        self.patch_js(dist=os.path.join(output, "marimo"))

    def patch_js(self, dist: str = "build/marimo", index_base: str = DEFAULT_INDEX_BASE) -> None:
        """Patch an exported build in place so wheel/index traffic hits the local index.

        Prepends a fetch+XMLHttpRequest reroute shim to every Pyodide worker chunk
        (``assets/*worker*.js``) and injects it into every notebook HTML page (those
        containing ``<marimo-wasm``, i.e. not the static listing ``index.html``).
        Idempotent: files already containing the marker are skipped.
        """
        shim = JS_PATCH.replace("__INDEX_BASE__", index_base.rstrip("/"))

        # Worker chunks: prepend the shim so it runs before Pyodide loads.
        for worker in sorted(glob.glob(os.path.join(dist, "assets", "*worker*.js"))):
            with open(worker, encoding="utf-8") as f:
                content = f.read()
            if PATCH_MARKER in content:
                print(f"skip (already patched): {worker}")
                continue
            with open(worker, "w", encoding="utf-8") as f:
                f.write(shim + "\n" + content)
            print(f"patched worker: {worker}")

        # Notebook HTML pages: inject a <script> right after <head>.
        head_re = re.compile(r"<head[^>]*>", re.IGNORECASE)
        script_tag = f"\n    <script>\n{shim}\n    </script>"

        def inject_after_head(match: re.Match[str]) -> str:
            return match.group(0) + script_tag

        for html in sorted(glob.glob(os.path.join(dist, "*.html"))):
            with open(html, encoding="utf-8") as f:
                content = f.read()
            if "<marimo-wasm" not in content:
                continue  # static listing page, not a notebook
            if PATCH_MARKER in content:
                print(f"skip (already patched): {html}")
                continue
            new_content, n = head_re.subn(inject_after_head, content, count=1)
            if n == 0:
                print(f"warning: no <head> found, skipping: {html}")
                continue
            with open(html, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"patched html: {html}")

    def get_app(self) -> typer.Typer:
        app = typer.Typer()
        app.command()(self.extract_deps)
        app.command()(self.create_dumb_index)
        app.command()(self.download_lockfile_wheels)
        app.command()(self.export_notebooks)
        app.command()(self.patch_js)
        app.command()(self.run)
        return app


cli = CLI()
app = cli.get_app()

if __name__ == "__main__":
    # app()
    cli.run()
