"""End-to-end verification that a patched marimo WASM export makes zero requests to
pythonhosted/pypi.org and resolves every wheel + index lookup against the local index.

Serves ``build/`` from a single origin (so the notebook and ``/pypi-index`` share an
origin, no CORS needed) and drives headless Chromium via Playwright, whose request events
DO capture web-worker traffic (CDP/agent-browser does not).

Run:  .venv/bin/python tests/verify_reroute.py
"""

from __future__ import annotations

import functools
import http.server
import re
import socketserver
import threading
from urllib.parse import urlsplit

from playwright.sync_api import Browser
from playwright.sync_api import sync_playwright

BUILD_DIR = "build"
PORT = 8000
BAD_HOSTS = {"files.pythonhosted.org", "test-files.pythonhosted.org", "pypi.org"}

# notebook page -> a dependency wheel whose local fetch proves the notebook booted
NOTEBOOKS = {
    "marimo/main.html": "python_metadata_parser",
    "marimo/main2.html": "lambda_dev_server",
}

BOOT_TIMEOUT_MS = 180_000


def serve(directory: str, port: int) -> socketserver.TCPServer | None:
    """Start a static server for ``directory`` on ``port``; reuse one already running."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=directory
    )
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    except OSError:
        print(f"port {port} already in use; reusing existing server")
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def check_notebook(
    browser: Browser, page_path: str, dep: str, *, abort_bad: bool
) -> None:
    label = f"{page_path} (abort_bad={abort_bad})"
    print(f"\n=== {label} ===")
    context = browser.new_context()

    if abort_bad:
        context.route(
            re.compile(r"pythonhosted\.org|pypi\.org"), lambda r: r.abort()
        )

    requests: list[str] = []
    context.on("request", lambda r: requests.append(r.url))

    page = context.new_page()
    url = f"http://127.0.0.1:{PORT}/{page_path}"
    page.goto(url)

    # "Booted" == the notebook's own dependency wheel was fetched from the local index.
    dep_wheel = re.compile(rf"/pypi-index/wheels/{re.escape(dep)}-.*\.whl")
    try:
        page.wait_for_event(
            "requestfinished",
            predicate=lambda r: dep_wheel.search(r.url) is not None,
            timeout=BOOT_TIMEOUT_MS,
        )
    except Exception as err:
        # The request may already have completed before we started waiting.
        if not any(dep_wheel.search(u) for u in requests):
            context.close()
            msg = f"{label}: notebook never fetched {dep} wheel locally"
            raise AssertionError(msg) from err

    page.wait_for_timeout(1500)  # let any trailing requests land

    bad = sorted({u for u in requests if urlsplit(u).hostname in BAD_HOSTS})
    index_json = [u for u in requests if "/pypi-index/pypi/" in u and u.endswith("/json")]
    index_wheels = [u for u in requests if "/pypi-index/wheels/" in u]

    print(f"  total requests:      {len(requests)}")
    print(f"  pythonhosted/pypi:   {len(bad)}")
    print(f"  pypi-index json:     {len(index_json)}")
    print(f"  pypi-index wheels:   {len(index_wheels)}")
    if bad:
        for u in bad:
            print(f"    BAD -> {u}")

    context.close()

    assert not bad, f"{label}: saw {len(bad)} requests to pythonhosted/pypi.org"
    assert index_wheels, f"{label}: no wheels fetched from local index"
    if not abort_bad:
        assert index_json, f"{label}: no JSON API lookups against local index"
    print(f"  PASS: {label}")


def main() -> None:
    httpd = serve(BUILD_DIR, PORT)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for page_path, dep in NOTEBOOKS.items():
                check_notebook(browser, page_path, dep, abort_bad=False)
            # Stronger guarantee: physically block the bad hosts; must still boot.
            for page_path, dep in NOTEBOOKS.items():
                check_notebook(browser, page_path, dep, abort_bad=True)
            browser.close()
    finally:
        if httpd is not None:
            httpd.shutdown()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
