# Built-in rules

Run `helmreview rules` for the list. The HR checks are deterministic and fast.
They run on what each chart **renders** with `helm template` and its default
values, and on plain manifests, so they see the real objects.

| Rule | Title | What |
|---|---|---|
| HR000 | Chart does not render / YAML parse error | `helm template` fails with the default values, or a file is not valid YAML |
| HR001 | Hard-coded secret | In `values.yaml` or a Secret template; `{{ }}` placeholders are fine |
| HR002 | Privileged container | `privileged: true` |
| HR003 | Container not hardened (restricted profile) | Missing `allowPrivilegeEscalation: false`, `runAsNonRoot`, `readOnlyRootFilesystem`, `capabilities.drop: [ALL]`, seccomp (the *restricted* Pod Security Standard / OpenShift `restricted-v2`) |
| HR004 | Runs as root | `runAsUser: 0` |
| HR005 | Missing resource requests/limits | No CPU/memory requests or no memory limit |
| HR006 | Image tag `latest` or not pinned | |
| HR007 | Host network / PID / IPC | `hostNetwork`, `hostPID`, `hostIPC` |
| HR008 | `hostPath` volume | |
| HR009 | Missing readiness/liveness probe | |
| HR010 | Dangerous capability added | |
| HR011 | Over-broad RBAC | `*` in RBAC rules, bindings to cluster-admin |
| HR012 | Removed Kubernetes API version | `extensions/v1beta1`, `policy/v1beta1`, … |
| HR013 | Service exposed outside the cluster | LoadBalancer or NodePort Services |
| HR014 | Hard-coded namespace in a template | Use `.Release.Namespace` |
| HR015 | Private key committed | |
| HR016 | Chart apiVersion v1 | |
| HR017 | Dependency version not pinned | |
| HR018 | Dependency repository over plain HTTP | |
| HR019 | No `values.schema.json` | |
| HR020 | Ingress/Route without TLS | |
| HR021 | Credentials embedded in URL | `https://user:pass@…` |

Findings from rendered objects point at the template they came from (the line
is the first match for the key in that template). Helm test hooks are not held
to the hardening, resource and probe rules.

[`fix`](../usage/fix.md#hr-findings-need-a-human) never changes HR findings
itself: they touch values and defaults that are yours to decide, so the fix
report lists them as *needs a human*, with what to do.

## Other findings

- **helm lint** findings use the `helm-lint:` prefix, such as `helm-lint:icon`
  or `helm-lint:warning`.
- **Model** findings use the `llm:<rule>` prefix and carry a confidence score
  between 0 and 1. `min_confidence` drops the unsure ones.

## Switching rules off

```yaml
disabled_rules: [HR009, HR019, helm-lint:icon]
```

or `--disable HR009` on the command line (repeatable).

## Severities

`info` < `low` < `medium` < `high` < `critical`. `min_severity` hides findings
below a level, and `fail_on` sets the level at which the exit code becomes `1`.
