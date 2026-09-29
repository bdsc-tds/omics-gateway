# Contributing

Contributions are welcome, and they are greatly appreciated! Every little bit
helps, and credit will always be given.

## Types of Contributions

### Report Bugs

If you are reporting a bug, please include:

* Your operating system name and version
* Any details about your local setup that might be helpful in troubleshooting
* Detailed steps to reproduce the bug

### Fix Bugs

Look through the GitHub issues for bugs. Anything tagged with "bug" and "help
wanted" is open to whoever wants to implement it.

### Implement Features

Look through the GitHub issues for features. Anything tagged with "enhancement"
and "help wanted" is open to whoever wants to implement it.

### Write Documentation

You can never have enough documentation! Please feel free to contribute to any
part of the documentation, such as the official docs, docstrings, or even
on the web in blog posts, articles, and such.

### Submit Feedback

If you are proposing a feature:

* Explain in detail how it would work
* Keep the scope as narrow as possible, to make it easier to implement
* Remember that this is a volunteer-driven project, and that contributions
  are welcome :)

## Get Started!

Ready to contribute? Here's how to set up `cellxgene-gateway` for local development.

1. Clone `cellxgene-gateway` locally:

    ```console
    $ git clone https://github.com/bdsc-tds/cellxgene-gateway.git
    ```

2. Create the `cellxgateway` conda environment and install the repository into it (see the [README](README.md#installing-from-a-fresh-clone)):

    ```console
    $ ./deploy/setup.sh
    ```

3. Use `git` (or similar) to create a branch for local development and make your changes:

    ```console
    $ git checkout -b name-of-your-bugfix-or-feature
    ```

4. When you're done making changes, check that they pass the tests and the ruff checks described in the README's [Development](README.md#development) section

5. Commit your changes and open a pull request

## Pull Request Guidelines

Before you submit a pull request, check that it meets these guidelines:

1. The pull request should include additional tests if appropriate
2. If the pull request adds functionality, the docs should be updated
3. The pull request should work in the `cellxgateway` environment defined by `deploy/cellxgateway_env.yaml`

## Code of Conduct

Please note that the `cellxgene-gateway` project is released with a
[Code of Conduct](CONDUCT.md). By contributing to this project you agree to abide by its terms.
