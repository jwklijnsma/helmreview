# CI

## A job in each repository

[`examples/gitlab-ci.yml`](https://github.com/jwklijnsma/helmreview/blob/main/examples/gitlab-ci.yml)
has a job for your chart repo's `.gitlab-ci.yml`. It uses the container image,
which has helm, git and helmreview and runs as a non-root user. It needs a
runner that can reach your Ollama server; set `OLLAMA_HOST` as a CI/CD variable.

```yaml
helmreview:
  stage: test
  image:
    name: ghcr.io/jwklijnsma/helmreview:latest
    entrypoint: [""]
  variables:
    HELMREVIEW_MODEL: "qwen2.5-coder:14b"
    GIT_DEPTH: "0"            # diff mode needs history
  script:
    - |
      if [ -n "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" ]; then
        git fetch -q origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
        helmreview --diff "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" -f md,gitlab -o .helmreview
      else
        helmreview -f md,gitlab -o .helmreview
      fi
  artifacts:
    when: always
    paths: [.helmreview/report.md]
    reports:
      codequality: .helmreview/gl-code-quality-report.json
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
      when: never
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
```

In a merge request it reviews only the changed lines, and the findings show up
in GitLab's **Code Quality** widget. The job fails when a finding is at or above
`fail_on` (default `high`).

## One central job for all merge requests

The same file has a second job, `helmreview-merge-requests`. Put **only** that
job in one project (for example `platform/helmreview`) and add a pipeline
schedule there (**Build › Pipeline schedules**, e.g. `*/10 * * * *`). Each run
looks at the open merge requests of the watched projects, reviews new commits
and keeps one comment per merge request up to date. The other repositories need
no CI change.

CI/CD variables of that project:

| Variable | |
|---|---|
| `GITLAB_TOKEN` | Masked; group or project access token, scope `api`, on the watched projects |
| `OLLAMA_HOST` | Your Ollama server, reachable from the runner |

The job runs `helmreview watch-mrs --once` and keeps its checkouts in the CI
cache. Losing that cache is harmless: the comment remembers which commit was
reviewed. See [GitLab merge requests](../usage/merge-requests.md).

## A quick gate without the model

`helmreview --no-llm` runs only `helm lint` and the HR checks and needs no
Ollama. It takes seconds and is a good first step in any pipeline.
