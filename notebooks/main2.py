# /// script
# dependencies = [
#     "lambda-dev-server==0.0.8",
#     "marimo",
# ]
# requires-python = ">=3.12"
# ///

import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell
def _() -> None:
    import lambda_dev_server

    print(lambda_dev_server)


@app.cell
def _() -> None:
    return


if __name__ == "__main__":
    app.run()
