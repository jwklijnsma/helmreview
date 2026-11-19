# Reports

Reports go to `<repo>/.helmreview/` by default (`-o` to change it). Add that
folder to `.gitignore`.

## Formats

Pick them with `-f` (default `md,json`):

| `-f` | File | For |
|---|---|---|
| `md` | `report.md` | Reading; also attached to CI jobs |
| `json` | `report.json` | Scripts, and the input for [`fix`](../usage/fix.md) |
| `sarif` | `report.sarif` | GitHub code scanning and other SARIF viewers |
| `gitlab` | `gl-code-quality-report.json` | GitLab's Code Quality widget in merge requests (see [CI](../deploy/ci.md)) |

Every finding has a severity, a rule, a file and line, a message and, where
possible, a suggested fix. Model findings use the `llm:` prefix and carry a
confidence score. The report language follows `language` (`en` or `nl`).

## Other files in .helmreview/

| File | Written by | |
|---|---|---|
| `queue.json` | `scan`, `fix` | Status of every file: pending, running, done, cached, failed |
| `scan-cache.json` | `scan` | Model answers per file; only changed files go back to the model |
| `fix-cache.json` | `fix` | Model answers and timeouts for fixes |
| `fix-report.md` | `fix` | What was fixed, declined, failed or needs a human |
| `fix.patch` | `fix` | The diff, also with `--dry-run` |

Delete the folder, or pass `--no-cache`, to start fresh.
