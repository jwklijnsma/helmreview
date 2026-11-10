"""Deterministic Helm and Kubernetes checks (rule IDs HR0xx).

They run on what a chart actually produces: `helm template` with the chart's default values,
mapped back to the template file each object came from. Plain manifests outside charts get
the same object checks with exact line numbers. values.yaml, Chart.yaml and the template
sources get their own checks (hard-coded secrets, pinned dependencies, ...).

Every check is cheap and needs no model, so these findings are reliable and make good
context for the model pass.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from . import lint
from .discover import chart_name
from .models import Finding, Severity, Unit

SECRET_KEY_RE = re.compile(
    r"(^|_|-|[a-z](?=[A-Z]))(pass(word|wd|phrase)?|Pass(word|wd|phrase)?|pwd|secret|Secret|"
    r"token|Token|api_?key|apiKey|ApiKey|private_?key|privateKey|client_?secret|clientSecret|"
    r"credentials?|Credentials?|auth_?key|access_?key|accessKey|secret_?key|secretKey)($|_|-|[A-Z])")
NOT_SECRET_RE = re.compile(
    r"(file|path|dir|length|len|policy|enabled|min|max|age|expire|expiry|algorithm|url|name|"
    r"user|username|id|field|regex|prompt|ref|key_?ref|secret_?ref|secretName|secret_?name|"
    r"existing\w*|mount\w*|mode|type|format|header|env|from|create|annotations?|labels?|"
    r"keys?|provider|issuer|duration|ttl|rotation\w*)$", re.I)
PLACEHOLDER_RE = re.compile(r"^(<.*>|\*+|x+|todo|tbd|none|null|n/?a|undefined|changeme|"
                            r"change-me|change_me|replace-?me|example|dummy|.*\{\{.*\}\}.*|"
                            r"\$\{?\w+\}?|true|false)$", re.I)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
URL_CREDS_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@{}\"']+:[^/\s@{}\"']+@", re.I)
LITERAL_NS_RE = re.compile(r"^(\s*)namespace:\s*[\"']?([a-z0-9]([-a-z0-9]*[a-z0-9])?)[\"']?\s*(#.*)?$")

WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod",
             "DeploymentConfig", "ReplicationController"}
LONG_RUNNING = WORKLOADS - {"Job", "CronJob", "Pod"}
DANGEROUS_CAPS = {"ALL", "SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH",
                  "NET_RAW", "SYS_RAWIO", "BPF", "PERFMON"}
# apiVersion/kind -> the Kubernetes release that removed it
REMOVED_APIS = {
    ("extensions/v1beta1", None): "1.16/1.22",
    ("apps/v1beta1", None): "1.16", ("apps/v1beta2", None): "1.16",
    ("networking.k8s.io/v1beta1", None): "1.22",
    ("rbac.authorization.k8s.io/v1beta1", None): "1.22",
    ("rbac.authorization.k8s.io/v1alpha1", None): "1.22",
    ("apiextensions.k8s.io/v1beta1", None): "1.22",
    ("admissionregistration.k8s.io/v1beta1", None): "1.22",
    ("scheduling.k8s.io/v1beta1", None): "1.22",
    ("storage.k8s.io/v1beta1", "CSIDriver"): "1.22",
    ("storage.k8s.io/v1beta1", "StorageClass"): "1.22",
    ("storage.k8s.io/v1beta1", "VolumeAttachment"): "1.22",
    ("storage.k8s.io/v1beta1", "CSIStorageCapacity"): "1.27",
    ("certificates.k8s.io/v1beta1", None): "1.22",
    ("coordination.k8s.io/v1beta1", None): "1.22",
    ("batch/v1beta1", "CronJob"): "1.25",
    ("policy/v1beta1", "PodDisruptionBudget"): "1.25",
    ("policy/v1beta1", "PodSecurityPolicy"): "1.25",
    ("discovery.k8s.io/v1beta1", None): "1.25",
    ("events.k8s.io/v1beta1", None): "1.25",
    ("autoscaling/v2beta1", None): "1.25", ("autoscaling/v2beta2", None): "1.26",
    ("flowcontrol.apiserver.k8s.io/v1beta1", None): "1.26",
    ("flowcontrol.apiserver.k8s.io/v1beta2", None): "1.29",
}
RULE_DOCS = {
    "HR000": "Chart does not render / YAML parse error",
    "HR001": "Hard-coded secret",
    "HR002": "Privileged container",
    "HR003": "Container not hardened (restricted profile)",
    "HR004": "Runs as root",
    "HR005": "Missing resource requests/limits",
    "HR006": "Image tag latest or not pinned",
    "HR007": "Host network / PID / IPC",
    "HR008": "hostPath volume",
    "HR009": "Missing readiness/liveness probe",
    "HR010": "Dangerous capability added",
    "HR011": "Over-broad RBAC",
    "HR012": "Removed Kubernetes API version",
    "HR013": "Service exposed outside the cluster",
    "HR014": "Hard-coded namespace in a template",
    "HR015": "Private key committed",
    "HR016": "Chart apiVersion v1",
    "HR017": "Dependency version not pinned",
    "HR018": "Dependency repository over plain HTTP",
    "HR019": "No values.schema.json",
    "HR020": "Ingress/Route without TLS",
    "HR021": "Credentials embedded in URL",
}


class LineLoader(yaml.SafeLoader):
    """SafeLoader that remembers the line of every mapping and its keys."""


def _construct_mapping(loader, node, deep=False):
    mapping, key_lines = {}, {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            key = str(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
        key_lines[key] = key_node.start_mark.line + 1
    mapping["__line__"] = node.start_mark.line + 1
    mapping["__keylines__"] = key_lines
    return mapping


LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
LineLoader.add_multi_constructor("!", lambda l, suffix, n: (
    l.construct_scalar(n) if isinstance(n, yaml.ScalarNode) else None))


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not (isinstance(k, str) and k.startswith("__"))}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _mask(value: str) -> str:
    value = str(value)
    return (value[:2] + "***") if len(value) > 4 else "***"


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in {"true", "yes", "1", "on"}


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key)) and not NOT_SECRET_RE.search(key)


def is_plaintext_secret(value) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    return bool(v) and len(v) >= 4 and not PLACEHOLDER_RE.match(v)


def pod_spec(kind: str, obj: dict) -> dict | None:
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else None
    if spec is None:
        return None
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    tpl = spec.get("template") if isinstance(spec, dict) else None
    ps = tpl.get("spec") if isinstance(tpl, dict) else None
    return ps if isinstance(ps, dict) else None


class Locator:
    """Where to point a finding. For a plain manifest: the exact line of the object or key.
    For a rendered chart object: the first line in the template that mentions the key."""

    def __init__(self, text: str, exact: bool):
        self.lines = text.splitlines()
        self.exact = exact

    def find(self, *patterns: str, default: int | None = None) -> int | None:
        for pat in patterns:
            rx = re.compile(pat)
            for i, line in enumerate(self.lines, 1):
                if rx.search(line):
                    return i
        return default


class StaticChecker:
    def __init__(self, root: Path, disabled: list[str] | None = None):
        self.root = root
        self.disabled = set(disabled or [])
        self.findings: list[Finding] = []
        self.errors: list[str] = []
        self.notes: list[str] = []
        self._seen: set[tuple] = set()

    def add(self, rule: str, sev: Severity, file: str, line: int | None, msg: str,
            category: str = "security", suggestion: str = ""):
        if rule in self.disabled:
            return
        key = (rule, file, line, msg)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(Finding(rule=rule, severity=sev, file=file, line=line, message=msg,
                                     source="static", category=category, suggestion=suggestion))

    # ------------------------------------------------------------------ units
    def check_unit(self, unit: Unit, scope: set[str] | None = None):
        if unit.kind == "chart":
            self.check_chart(unit, scope)
        else:
            for rel in unit.files:
                if scope is None or rel in scope:
                    self.check_manifest_file(rel)

    def check_chart(self, unit: Unit, scope: set[str] | None):
        chart_rel = unit.path
        prefix = f"{chart_rel}/" if chart_rel else ""
        name = chart_name(self.root, chart_rel)
        files = [f for f in unit.files if scope is None or f in scope]
        for rel in files:
            inner = rel[len(prefix):]
            text = self._read(rel)
            if text is None:
                continue
            self._check_text(rel, text)
            if inner in ("Chart.yaml", "Chart.yml"):
                self.check_chart_yaml(rel, text)
            elif re.match(r"^(values[\w.-]*|ci/[\w.-]+)\.ya?ml$", inner):
                self.check_values(rel, text)
            elif inner.startswith("templates/"):
                self.check_template_source(rel, text)
        if not any((self.root / prefix / n).is_file() for n in ("values.schema.json",)) \
                and (self.root / prefix / "values.yaml").is_file() \
                and (scope is None or f"{prefix}values.yaml" in scope):
            self.add("HR019", Severity.INFO, f"{prefix}values.yaml", None,
                     "The chart has no values.schema.json, so typos and wrong types in values "
                     "are not caught at install time.", "best-practice",
                     "Add a values.schema.json (helm-schema or helm-values-schema-json can generate one).")

        rendered = lint.render(self.root, chart_rel, name)
        if rendered.note:
            self.notes.append(f"{unit.name}: {rendered.note}")
        if rendered.error:
            if rendered.error.startswith("helm not found"):
                self.errors.append("helm not found on PATH; rendered-manifest checks skipped")
                return
            file = rendered.error_file if rendered.error_file and (
                self.root / rendered.error_file).is_file() else f"{prefix}Chart.yaml"
            if scope is None or file in scope:
                self.add("HR000", Severity.HIGH, file, rendered.error_line,
                         f"The chart does not render with its default values: {rendered.error}",
                         "reliability", "Fix the template, or give the values it needs a default.")
            return
        for rel, piece in rendered.docs:
            if scope is not None and rel not in scope:
                continue
            try:
                docs = list(yaml.safe_load_all(piece))
            except yaml.YAMLError as exc:
                self.add("HR000", Severity.HIGH, rel, None,
                         f"Renders to invalid YAML: {str(exc).splitlines()[0]}", "reliability")
                continue
            src = self._read(rel) or ""
            loc = Locator(src, exact=False)
            for doc in docs:
                if isinstance(doc, dict) and doc.get("kind"):
                    self.check_object(rel, doc, loc, rendered=True)

    def check_manifest_file(self, rel: str):
        text = self._read(rel)
        if text is None:
            return
        self._check_text(rel, text)
        try:
            docs = list(yaml.load_all(text, Loader=LineLoader))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            self.add("HR000", Severity.HIGH, rel, mark.line + 1 if mark else None,
                     f"YAML parse error: {str(exc).splitlines()[0]}", "reliability")
            return
        loc = Locator(text, exact=True)
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind"):
                self.check_object(rel, doc, loc, rendered=False)

    def _read(self, rel: str) -> str | None:
        try:
            return (self.root / rel).read_text(errors="replace")
        except OSError as exc:
            self.errors.append(f"{rel}: {exc}")
            return None

    # ------------------------------------------------------------- text checks
    def _check_text(self, rel: str, text: str):
        for i, line in enumerate(text.splitlines(), 1):
            if PRIVATE_KEY_RE.search(line):
                self.add("HR015", Severity.CRITICAL, rel, i,
                         "A private key is committed to the repository.", "security",
                         "Remove it, rotate it, and load it from an existing Secret "
                         "(e.g. External Secrets, Sealed Secrets or Vault).")
            if URL_CREDS_RE.search(line) and "{{" not in line:
                self.add("HR021", Severity.HIGH, rel, i,
                         "A URL contains a username and password.", "security",
                         "Put the credentials in a Secret and reference it.")

    def check_values(self, rel: str, text: str):
        try:
            data = yaml.load(text, Loader=LineLoader)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            self.add("HR000", Severity.HIGH, rel, mark.line + 1 if mark else None,
                     f"YAML parse error: {str(exc).splitlines()[0]}", "reliability")
            return
        self._walk_secrets(rel, data, [])

    def _walk_secrets(self, rel: str, node, path: list[str]):
        if isinstance(node, dict):
            lines = node.get("__keylines__", {})
            for k, v in node.items():
                if not isinstance(k, str) or k.startswith("__"):
                    continue
                if is_secret_key(k) and is_plaintext_secret(v):
                    self.add("HR001", Severity.HIGH, rel, lines.get(k),
                             f"Hard-coded secret in values: {'.'.join(path + [k])} = {_mask(v)}",
                             "security",
                             "Leave it empty and reference an existing Secret "
                             "(existingSecret / secretKeyRef), or use External/Sealed Secrets.")
                else:
                    self._walk_secrets(rel, v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self._walk_secrets(rel, v, path + [str(i)])

    def check_template_source(self, rel: str, text: str):
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            m = LITERAL_NS_RE.match(line)
            if m and "{{" not in line:
                self.add("HR014", Severity.LOW, rel, i,
                         f"Namespace '{m.group(2)}' is hard-coded; the chart ignores "
                         "`helm install --namespace` / the Argo CD destination namespace.",
                         "best-practice", "Use {{ .Release.Namespace }} (or a value that defaults to it).")
            # key: "literal" in a Secret template
            kv = re.match(r"^\s*([A-Za-z_][\w.-]*):\s*[\"']?([^\"'{}\s#][^\"'{}#]*?)[\"']?\s*$", line)
            if kv and is_secret_key(kv.group(1)) and is_plaintext_secret(kv.group(2)) \
                    and re.search(r"^\s*(stringData|data):", text, re.M):
                self.add("HR001", Severity.HIGH, rel, i,
                         f"Hard-coded secret in a template: {kv.group(1)} = {_mask(kv.group(2))}",
                         "security", "Take it from values or an existing Secret instead.")

    def check_chart_yaml(self, rel: str, text: str):
        try:
            data = yaml.load(text, Loader=LineLoader) or {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            self.add("HR000", Severity.HIGH, rel, mark.line + 1 if mark else None,
                     f"YAML parse error: {str(exc).splitlines()[0]}", "reliability")
            return
        if not isinstance(data, dict):
            return
        lines = data.get("__keylines__", {})
        if str(data.get("apiVersion", "")).strip() == "v1":
            self.add("HR016", Severity.LOW, rel, lines.get("apiVersion"),
                     "apiVersion v1 is the Helm 2 chart format.", "best-practice",
                     "Use apiVersion: v2 and move requirements.yaml into Chart.yaml dependencies.")
        deps = data.get("dependencies") or []
        if isinstance(deps, list):
            for d in deps:
                if not isinstance(d, dict):
                    continue
                dl = d.get("__keylines__", {})
                ver = str(d.get("version", "")).strip()
                name = d.get("name", "?")
                if not ver or ver in ("*", "x") or re.search(r"[*xX^~><|]|\s-\s", ver):
                    self.add("HR017", Severity.LOW, rel, dl.get("version", d.get("__line__")),
                             f"Dependency '{name}' is not pinned to one version ({ver or 'no version'}); "
                             "builds are not reproducible.", "reliability",
                             "Pin an exact version and let Renovate raise updates.")
                repo = str(d.get("repository", ""))
                if repo.startswith("http://"):
                    self.add("HR018", Severity.MEDIUM, rel, dl.get("repository"),
                             f"Dependency '{name}' comes from a plain-HTTP repository.", "security",
                             "Use https:// or an oci:// registry.")

    # ----------------------------------------------------------- object checks
    def check_object(self, rel: str, obj: dict, loc: Locator, rendered: bool):
        kind = str(obj.get("kind", ""))
        api = str(obj.get("apiVersion", ""))
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        name = meta.get("name", "?")
        what = f"{kind} {name}"
        exact = not rendered

        def line_of(node, key=None, *patterns, default=None):
            if exact and isinstance(node, dict):
                if key and key in node.get("__keylines__", {}):
                    return node["__keylines__"][key]
                return node.get("__line__", default)
            return loc.find(*patterns, default=default)

        obj_line = line_of(obj, "kind", r"^\s*kind:\s*" + re.escape(kind) + r"\b", default=None)

        removed = REMOVED_APIS.get((api, kind)) or REMOVED_APIS.get((api, None))
        if removed:
            self.add("HR012", Severity.HIGH, rel, line_of(obj, "apiVersion", r"^\s*apiVersion:", default=obj_line),
                     f"{what} uses {api}, removed in Kubernetes {removed}.", "reliability",
                     "Move to the current API version (see `kubectl convert` / pluto).")

        if kind in ("Role", "ClusterRole"):
            for rule in obj.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                wild = [k for k in ("verbs", "resources", "apiGroups")
                        if "*" in [str(x) for x in (rule.get(k) or [])]]
                if wild and set(wild) & {"verbs", "resources"}:
                    self.add("HR011", Severity.HIGH, rel, line_of(rule, None, r"['\"]?\*['\"]?", r"^\s*rules:",
                                                                  default=obj_line),
                             f"{what} grants '*' on {', '.join(wild)}.", "security",
                             "List only the resources and verbs the workload needs.")
        if kind in ("ClusterRoleBinding", "RoleBinding"):
            ref = obj.get("roleRef") if isinstance(obj.get("roleRef"), dict) else {}
            if ref.get("name") == "cluster-admin":
                self.add("HR011", Severity.HIGH, rel, line_of(ref, "name", r"cluster-admin", default=obj_line),
                         f"{what} binds cluster-admin.", "security",
                         "Bind a role with only the permissions the workload needs.")

        if kind == "Service":
            spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
            if spec.get("type") in ("LoadBalancer", "NodePort"):
                self.add("HR013", Severity.INFO, rel, line_of(spec, "type", r"^\s*type:", default=obj_line),
                         f"{what} is of type {spec.get('type')} and reachable from outside the cluster.",
                         "security", "Use ClusterIP behind an Ingress/Route unless this is intended.")
        if kind == "Ingress":
            spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
            if spec.get("rules") and not spec.get("tls"):
                self.add("HR020", Severity.LOW, rel, line_of(spec, "rules", r"^\s*rules:", default=obj_line),
                         f"{what} has no tls section; traffic is plain HTTP unless something in front terminates TLS.",
                         "security", "Add spec.tls (cert-manager can issue the certificate).")
        if kind == "Route":
            spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
            if spec and not spec.get("tls"):
                self.add("HR020", Severity.LOW, rel, line_of(spec, "to", r"^\s*spec:", default=obj_line),
                         f"{what} has no tls section, so it is served over plain HTTP.",
                         "security", "Add tls: {termination: edge, insecureEdgeTerminationPolicy: Redirect}.")

        if kind in WORKLOADS:
            ps = pod_spec(kind, obj)
            if ps is not None:
                hook = str((meta.get("annotations") or {}).get("helm.sh/hook", "")) \
                    if isinstance(meta.get("annotations"), dict) else ""
                self._check_pod(rel, kind, what, ps, line_of, obj_line, "test" in hook)

    def _check_pod(self, rel, kind, what, ps, line_of, obj_line, test_hook=False):
        psc = ps.get("securityContext") if isinstance(ps.get("securityContext"), dict) else {}
        for key, label in (("hostNetwork", "host network"), ("hostPID", "host PID namespace"),
                           ("hostIPC", "host IPC namespace")):
            if _truthy(ps.get(key)):
                self.add("HR007", Severity.HIGH, rel, line_of(ps, key, rf"^\s*{key}:", default=obj_line),
                         f"{what} uses the {label}.", "security",
                         f"Remove {key} unless the workload really needs it (and document why).")
        for vol in ps.get("volumes") or []:
            if isinstance(vol, dict) and vol.get("hostPath") is not None:
                hp = vol["hostPath"] if isinstance(vol["hostPath"], dict) else {}
                self.add("HR008", Severity.MEDIUM, rel, line_of(vol, "hostPath", r"^\s*hostPath:", default=obj_line),
                         f"{what} mounts hostPath {hp.get('path', '?')} (volume {vol.get('name', '?')}).",
                         "security", "Use a PersistentVolumeClaim, ConfigMap or emptyDir instead.")
        pod_nonroot = _truthy(psc.get("runAsNonRoot"))
        pod_uid = psc.get("runAsUser")
        pod_seccomp = (psc.get("seccompProfile") or {}).get("type") if isinstance(psc.get("seccompProfile"), dict) else None

        containers = [(c, False) for c in ps.get("containers") or [] if isinstance(c, dict)]
        containers += [(c, True) for c in ps.get("initContainers") or [] if isinstance(c, dict)]
        for c, init in containers:
            cname = f"{what}, {'init ' if init else ''}container {c.get('name', '?')}"
            csc = c.get("securityContext") if isinstance(c.get("securityContext"), dict) else {}
            cline = line_of(c, "name", r"^\s*(containers|initContainers):", default=obj_line)
            sc_line = line_of(csc, None, r"^\s*securityContext:", default=cline) if csc else cline

            if _truthy(csc.get("privileged")):
                self.add("HR002", Severity.CRITICAL, rel, line_of(csc, "privileged", r"^\s*privileged:", default=sc_line),
                         f"{cname} runs privileged: full access to the node.", "security",
                         "Drop privileged and add only the capabilities it needs.")
            uid = csc.get("runAsUser", pod_uid)
            if str(uid) == "0":
                self.add("HR004", Severity.HIGH, rel, line_of(csc if "runAsUser" in csc else ps, "runAsUser",
                                                              r"^\s*runAsUser:\s*0", default=sc_line),
                         f"{cname} runs as root (runAsUser: 0).", "security",
                         "Run as a non-root UID (and leave the UID to OpenShift's SCC there).")
            caps = csc.get("capabilities") if isinstance(csc.get("capabilities"), dict) else {}
            added = {str(x).upper() for x in caps.get("add") or []}
            bad = sorted(added & DANGEROUS_CAPS)
            if bad:
                self.add("HR010", Severity.HIGH, rel, line_of(caps, "add", r"^\s*add:", default=sc_line),
                         f"{cname} adds capabilities {', '.join(bad)}.", "security",
                         "Add only the specific capability needed, or none.")
            if not test_hook:
                missing = []
                if csc.get("allowPrivilegeEscalation") is not False and not _truthy(csc.get("privileged")):
                    missing.append("allowPrivilegeEscalation: false")
                if not (pod_nonroot or _truthy(csc.get("runAsNonRoot"))) and str(uid) != "0" \
                        and uid in (None, ""):
                    missing.append("runAsNonRoot: true")
                if not _truthy(csc.get("readOnlyRootFilesystem")):
                    missing.append("readOnlyRootFilesystem: true")
                if "ALL" not in {str(x).upper() for x in caps.get("drop") or []}:
                    missing.append("capabilities.drop: [ALL]")
                c_seccomp = (csc.get("seccompProfile") or {}).get("type") \
                    if isinstance(csc.get("seccompProfile"), dict) else None
                if (c_seccomp or pod_seccomp) not in ("RuntimeDefault", "Localhost"):
                    missing.append("seccompProfile: RuntimeDefault")
                if missing:
                    sev = Severity.MEDIUM if missing[0].startswith("allowPrivilegeEscalation") else Severity.LOW
                    self.add("HR003", sev, rel, sc_line,
                             f"{cname} is not hardened; missing {', '.join(missing)}.", "security",
                             "Set these in the container securityContext (the 'restricted' Pod "
                             "Security Standard / OpenShift restricted-v2 SCC expects them).")
            image = str(c.get("image") or "")
            img_line = line_of(c, "image", r"^\s*image:", default=cline)
            if image and "@sha256:" not in image:
                last = image.rsplit("/", 1)[-1]
                tag = last.split(":", 1)[1] if ":" in last else ""
                if not tag or tag == "latest":
                    self.add("HR006", Severity.MEDIUM, rel, img_line,
                             f"{cname} uses image {image!r} ({'no tag' if not tag else 'latest'}); "
                             "what runs changes without a chart change.", "reliability",
                             "Pin a version tag (default it to .Chart.AppVersion) or a digest.")
            res = c.get("resources") if isinstance(c.get("resources"), dict) else {}
            req = res.get("requests") if isinstance(res.get("requests"), dict) else {}
            lim = res.get("limits") if isinstance(res.get("limits"), dict) else {}
            missing = [n for n, ok in (("requests.cpu", req.get("cpu")),
                                       ("requests.memory", req.get("memory")),
                                       ("limits.memory", lim.get("memory"))) if not ok]
            if missing and not test_hook:
                self.add("HR005", Severity.LOW, rel,
                         line_of(c, "resources", r"^\s*resources:", default=cline),
                         f"{cname} has no {', '.join(missing)}.", "reliability",
                         "Set requests (scheduling) and a memory limit (stops one pod starving the node).")
            if not init and not test_hook and kind in LONG_RUNNING:
                probes = [p for p in ("readinessProbe", "livenessProbe") if not c.get(p)]
                if probes:
                    self.add("HR009", Severity.LOW, rel, cline,
                             f"{cname} has no {' or '.join(probes)}.", "reliability",
                             "Add a readinessProbe (traffic) and a livenessProbe (restarts).")


def run_static(root: Path, units: list[Unit], files: list[str] | None = None,
               disabled: list[str] | None = None) -> tuple[list[Finding], list[str], list[str]]:
    """Run every check on the units. `files` limits the findings to those files.
    Returns (findings, errors, notes)."""
    checker = StaticChecker(root, disabled)
    scope = set(files) if files is not None else None
    for unit in units:
        try:
            checker.check_unit(unit, scope)
        except Exception as exc:     # one broken chart must not stop the others
            checker.errors.append(f"{unit.name}: static checks failed: {type(exc).__name__}: {exc}")
    return checker.findings, checker.errors, checker.notes
