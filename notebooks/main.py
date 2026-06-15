# /// script
# dependencies = [
#     "marimo",
#     "python-metadata-parser==0.1.3",
# ]
# requires-python = ">=3.12"
# ///

import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell
def _() -> None:
    import python_metadata_parser

    print(python_metadata_parser)


@app.cell
def _() -> None:
    return


if __name__ == "__main__":
    app.run()
