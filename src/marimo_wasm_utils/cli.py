from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from textwrap import dedent

import typer
from python_metadata_parser import pep0723

JS_PATCH = """\
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

    def create_dumb_index(
        self, requirements_txt: str = "requirements.txt", output: str = "pypi-index"
    ) -> None:
        # delete the output dir if it exists
        import os
        import shutil

        if os.path.exists(output):
            shutil.rmtree(output)
        wheel_dir = os.path.join(output, "wheels")
        os.makedirs(wheel_dir, exist_ok=True)
        from pip._internal.cli.main import main as pip_main

        pip_main(["download", "-r", requirements_txt, "--dest", wheel_dir])
        wheels = os.listdir(wheel_dir)
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

    def run(self) -> None:
        notebooks = [os.path.join("notebooks", x) for x in os.listdir("notebooks")]
        self.extract_deps(notebooks)
        self.create_dumb_index()
        self.export_notebooks(notebooks)

    def patch_js(self, dist: str) -> None:
        # Go to each notebook listed in the index.html and patch the generated JS to include
        # the JS_PATCH
        ...

    def get_app(self) -> typer.Typer:
        app = typer.Typer()
        app.command()(self.extract_deps)
        app.command()(self.create_dumb_index)
        app.command()(self.export_notebooks)
        app.command()(self.run)
        return app


cli = CLI()
app = cli.get_app()

if __name__ == "__main__":
    # app()
    cli.create_dumb_index()
