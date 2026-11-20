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

## Automatic releases

`auto-release.yml` starts the release workflow on its own:

| When | What |
|---|---|
| One of your pull requests is merged into `main` | A release right away. Bot PRs (Renovate, Dependabot) don't start one |
| Every Monday at 05:23 UTC | A release of whatever was merged since the last one, such as automerged dependency updates |

Nothing happens when `main` has no commits since the last tag, so a quiet week
makes no release.

The bump comes from the labels on the pull requests merged since the last
release:

| Label | Bump |
|---|---|
| `release:major` | major |
| `release:minor` | minor |
| none | patch |

Label a pull request `no-release` to keep its merge from starting a release; it
still goes out with the next one. You can always start a release by hand as
above, and `auto-release` can also be started from the Actions tab.
