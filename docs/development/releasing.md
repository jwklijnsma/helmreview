# Releasing

Releases run on GitHub. In the repository: **Actions › release › Run
workflow**, pick `patch`, `minor` or `major` (or type an exact version), and
start it. Or from a terminal with the [GitHub CLI](https://cli.github.com):

```bash
make release                    # 0.1.0 -> 0.1.1, then follows the run
make release BUMP=minor         # 0.1.0 -> 0.2.0   (or BUMP=major)
make release VERSION=1.0.0      # an exact version
make release-dry-run            # rehearse: test and build everything, publish nothing
```

## What the workflow does

1. Checks the new version is higher and not released yet, and runs the tests.
2. Bumps `__version__` in `src/helmreview/__init__.py` (the only place the
   version lives), commits `release: vX.Y.Z` to `main`, and pushes it together
   with an annotated `vX.Y.Z` tag.
3. Builds the wheel and sdist and creates the GitHub release, with install lines
   and notes generated from the merged PRs.
4. Builds and smoke-tests the Docker image and publishes
   `ghcr.io/jwklijnsma/helmreview:X.Y.Z`, `:X.Y` and `:latest`.

A dry run does all of this without the push, the release or the image push, and
shows the release notes it would use in the run summary. If you ever push a
`vX.Y.Z` tag by hand, it still gets a GitHub release and an image.
