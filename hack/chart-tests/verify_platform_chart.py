#!/usr/bin/env python3
"""Render-time assertions for the marimohub-platform chart.

Renders the chart offline with `helm template` and checks what it creates:
the namespaces and their Pod Security labels, the CRD and admission policy
(against the generated sources), the storage volumes and claims, the
generated Secrets and certificates, and the evaluation PostgreSQL. It then
renders values that the chart must refuse.

The contract test takes the app values the chart writes into the
marimohub-platform-values ConfigMap, renders charts/marimohub with them, runs
verify_chart.py on that render, and checks that every Secret and claim the
app render names is one this chart creates.

Usage: verify_platform_chart.py CHART_DIR VALUES_FILE [VALUES_FILE ...]
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile

import yaml

RELEASE = "marimohub-platform"
RELEASE_NAMESPACE = "marimohub-platform"
HELM = os.environ.get("HELM", "helm")
KUBE_VERSION = "1.35.0"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
CRD_SOURCE = os.path.join(REPO_ROOT, "deploy", "crd", "marimosession.yaml")
POLICY_SOURCE = os.path.join(REPO_ROOT, "marimohub-operator", "config", "policy", "marimosession_label_identity.yaml")
APP_CHART = os.path.join(REPO_ROOT, "charts", "marimohub")
OPENSHIFT_CI_HOST = "marimohub.apps.example.com"
KEEP = ("helm.sh/resource-policy", "keep")
RESERVED_LABEL_PREFIXES = ("network.openshift.io/policy-group", "policy-group.network.openshift.io/")

sys.path.insert(0, HERE)
import verify_chart  # noqa: E402 -- sibling module, found through sys.path above


class Check(verify_chart.Check):
    pass


def helm_template(chart_dir: str, values_files: list[str], extra: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [HELM, "template", RELEASE, chart_dir, "-n", RELEASE_NAMESPACE, "--kube-version", KUBE_VERSION]
    for values_file in values_files:
        cmd += ["-f", values_file]
    return subprocess.run(cmd + (extra or []), capture_output=True, text=True)


def render(chart_dir: str, values_files: list[str], extra: list[str] | None = None) -> list[dict]:
    result = helm_template(chart_dir, values_files, extra)
    if result.returncode != 0:
        raise SystemExit(f"verify_platform_chart: helm template failed for {values_files} {extra or []}:\n{result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def effective_values(chart_dir: str, values_files: list[str]) -> dict:
    return verify_chart.effective_values(chart_dir, values_files)


def index_objects(docs: list[dict]) -> dict:
    return verify_chart.index_objects(docs)


def has_keep(doc: dict) -> bool:
    return (doc["metadata"].get("annotations") or {}).get(KEEP[0]) == KEEP[1]


def load_yaml_docs(path: str) -> list[dict]:
    with open(path) as handle:
        return [doc for doc in yaml.safe_load_all(handle) if doc]


def openssl(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True)


def check_generated_copies(chart_dir: str, check: Check) -> None:
    """files/ holds byte-for-byte copies of the generated CRD and policy."""
    pairs = (
        (os.path.join(chart_dir, "files", "marimohub.io_marimosessions.yaml"), CRD_SOURCE),
        (os.path.join(chart_dir, "files", "marimosession_label_identity.yaml"), POLICY_SOURCE),
    )
    for copy, source in pairs:
        with open(copy, "rb") as a, open(source, "rb") as b:
            check.true(f"{os.path.relpath(copy, REPO_ROOT)} is a byte-for-byte copy of {os.path.relpath(source, REPO_ROOT)} (cp it)", a.read() == b.read())


def check_namespaces(index: dict, check: Check, values: dict) -> None:
    ns = values["namespaces"]
    want = {ns["app"], ns["controller"], ns["sessions"]}
    if values["postgresql"]["enabled"]:
        want.add(ns["database"])
    got = {key[1] for key in index if key[0] == "Namespace"}
    check.eq("Namespaces this chart creates", got, want)
    openshift = values["platform"] == "openshift"
    for name in got:
        namespace = index[("Namespace", name, None)]
        labels = namespace["metadata"].get("labels") or {}
        for mode in ("enforce", "audit", "warn"):
            check.eq(f"Namespace {name} pod-security {mode}", labels.get(f"pod-security.kubernetes.io/{mode}"), "restricted")
        check.eq(f"Namespace {name} pod-security enforce-version", labels.get("pod-security.kubernetes.io/enforce-version"), "latest")
        check.eq(
            f"Namespace {name} scc.podSecurityLabelSync",
            labels.get("security.openshift.io/scc.podSecurityLabelSync"),
            "false" if openshift else None,
        )
        reserved = [label for label in labels if label.startswith(RESERVED_LABEL_PREFIXES)]
        check.eq(f"Namespace {name} carries no OpenShift policy-group label", reserved, [])
        check.true(f"Namespace {name} is kept on uninstall", has_keep(namespace))


def strip_meta(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "metadata"}


def check_crd_and_policy(index: dict, check: Check, values: dict) -> None:
    source_crd = load_yaml_docs(CRD_SOURCE)[0]
    crds = [doc for key, doc in index.items() if key[0] == "CustomResourceDefinition"]
    if values["crd"]["enabled"]:
        check.eq("one CRD", len(crds), 1)
        if crds:
            check.eq("CRD name", crds[0]["metadata"]["name"], source_crd["metadata"]["name"])
            check.eq("CRD spec equals deploy/crd/marimosession.yaml", strip_meta(crds[0]), strip_meta(source_crd))
            check.true("CRD is kept on uninstall", has_keep(crds[0]))
    else:
        check.eq("no CRD with crd.enabled=false", crds, [])

    sources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in load_yaml_docs(POLICY_SOURCE)}
    for (kind, name), source in sources.items():
        rendered = index.get((kind, name, None))
        if not values["admissionPolicy"]["enabled"]:
            check.true(f"no {kind} with admissionPolicy.enabled=false", rendered is None)
            continue
        check.true(f"{kind} {name} renders", rendered is not None)
        if rendered is not None:
            check.eq(f"{kind} {name} equals the operator's policy", strip_meta(rendered), strip_meta(source))
            check.true(f"{kind} {name} is removed on uninstall (not kept)", not has_keep(rendered))


def check_storage(index: dict, check: Check, values: dict, app_values: dict) -> None:
    sessions = values["namespaces"]["sessions"]
    storage = values["storage"]
    openshift = values["platform"] == "openshift"
    volumes = []
    if storage["workspaces"]["enabled"]:
        volumes.append((storage["workspaces"]["claimName"], False))
    for share in app_values["runtime"]["sharedVolumes"]:
        volumes.append((share["existingClaim"], share["readOnly"]))
    pvs = {key[1] for key in index if key[0] == "PersistentVolume"}
    claims = {key[1] for key in index if key[0] == "PersistentVolumeClaim" and key[2] == sessions}
    check.eq("PersistentVolumes", pvs, {f"{sessions}-{claim}" for claim, _ in volumes})
    check.eq("PersistentVolumeClaims", claims, {claim for claim, _ in volumes})
    for claim_name, read_only in volumes:
        pv = index.get(("PersistentVolume", f"{sessions}-{claim_name}", None))
        claim = index.get(("PersistentVolumeClaim", claim_name, sessions))
        if pv is None or claim is None:
            continue
        spec = pv["spec"]
        check.eq(f"PV {claim_name} claimRef", spec["claimRef"], {"namespace": sessions, "name": claim_name})
        check.eq(f"claim {claim_name} volumeName", claim["spec"]["volumeName"], pv["metadata"]["name"])
        for name, obj in (("PV", spec), ("claim", claim["spec"])):
            check.eq(f"{name} {claim_name} storageClassName", obj.get("storageClassName"), "")
            check.eq(f"{name} {claim_name} accessModes", obj.get("accessModes"), ["ReadWriteMany"])
        check.eq(f"PV {claim_name} reclaim policy", spec.get("persistentVolumeReclaimPolicy"), "Retain")
        check.true(f"PV {claim_name} is kept on uninstall", has_keep(pv))
        check.true(f"claim {claim_name} is kept on uninstall", has_keep(claim))
        if storage["mode"] == "nfs":
            check.true(f"PV {claim_name} is an nfs volume", "nfs" in spec and "hostPath" not in spec)
            check.eq(f"PV {claim_name} has the ro mount option exactly when read-only", "ro" in (spec.get("mountOptions") or []), read_only)
        else:
            check.true(f"PV {claim_name} hostPath only on the portable profile", not openshift and "hostPath" in spec)


def check_share_defaults(chart_dir: str, check: Check) -> None:
    """An entry with only a name and a path gets the documented defaults."""
    overlay = {
        "storage": {
            "nfs": {"server": "nfs.example.internal"},
            "workspaces": {"path": "/exports/marimohub"},
            "shares": [{"name": "datasets", "path": "/exports/datasets"}],
        },
        "postgresql": {"enabled": True},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(overlay, handle)
    try:
        index = index_objects(render(chart_dir, [handle.name]))
    finally:
        os.unlink(handle.name)
    app_values = app_values_of(index, "marimohub")
    share = app_values["runtime"]["sharedVolumes"][0]
    check.eq(
        "share defaults",
        share,
        {"name": "datasets", "existingClaim": "marimohub-share-datasets", "mountPath": "/exports/datasets", "readOnly": True},
    )


def app_values_of(index: dict, app_namespace: str) -> dict:
    config_map = index[("ConfigMap", "marimohub-platform-values", app_namespace)]
    return yaml.safe_load(config_map["data"]["values.yaml"])


def secret_data(index: dict, name: str, namespace: str) -> dict | None:
    secret = index.get(("Secret", name, namespace))
    return None if secret is None else secret.get("data") or {}


def check_certificate(check: Check, label: str, data: dict, want_names: set[str]) -> None:
    """The leaf names exactly want_names and verifies against its ca.crt, including X.509 strict checks."""
    leaf = base64.b64decode(data["tls.crt"]).decode()
    ca = base64.b64decode(data["ca.crt"]).decode()
    result = openssl(["x509", "-noout", "-ext", "subjectAltName"], stdin=leaf)
    names = {
        entry.strip().removeprefix("DNS:")
        for line in result.stdout.splitlines()[1:]
        for entry in line.split(",")
        if entry.strip()
    }
    check.eq(f"{label} certificate DNS names", names, want_names)
    with tempfile.TemporaryDirectory() as work:
        ca_file = os.path.join(work, "ca.crt")
        leaf_file = os.path.join(work, "tls.crt")
        with open(ca_file, "w") as handle:
            handle.write(ca)
        with open(leaf_file, "w") as handle:
            handle.write(leaf)
        # -x509_strict applies the checks Python 3.13+ clients enable by default.
        verify = openssl(["verify", "-x509_strict", "-purpose", "sslserver", "-CAfile", ca_file, leaf_file])
        check.true(f"{label} certificate verifies against its CA (strict): {verify.stdout.strip()} {verify.stderr.strip()}", verify.returncode == 0)


def service_names(name: str, namespace: str) -> set[str]:
    return {name, f"{name}.{namespace}", f"{name}.{namespace}.svc", f"{name}.{namespace}.svc.cluster.local"}


def check_secrets(index: dict, check: Check, values: dict) -> None:
    ns = values["namespaces"]
    openshift = values["platform"] == "openshift"
    generated = [key for key in index if key[0] == "Secret"]
    for key in generated:
        check.true(f"Secret {key[2]}/{key[1]} is kept on uninstall", has_keep(index[key]))

    if not values["backendEnv"]["existingSecret"]:
        env = secret_data(index, values["backendEnv"]["name"], ns["app"])
        check.true("backend env Secret renders", env is not None)
        if env is not None:
            check.eq("backend env Secret keys", set(env), {"DATABASE_URL", "SECRET_KEY"})
            url = base64.b64decode(env["DATABASE_URL"]).decode()
            check.true(f"DATABASE_URL uses asyncpg: {url.split('@')[-1]}", url.startswith("postgresql+asyncpg://"))
            if values["postgresql"]["enabled"]:
                host = f"{values['postgresql']['name']}.{ns['database']}.svc:5432/marimohub"
                check.true("DATABASE_URL points at the evaluation database", url.endswith(f"@{host}"))
            check.true("SECRET_KEY is 64 characters", len(base64.b64decode(env["SECRET_KEY"])) == 64)

    if not values["database"]["ca"]["existingSecret"]:
        database_ca = secret_data(index, values["database"]["ca"]["name"], ns["app"])
        check.true("database CA Secret renders", database_ca is not None)
        if database_ca is not None:
            check.eq("database CA Secret keys", set(database_ca), {"ca.crt"})

    internal = secret_data(index, values["internalApiCA"]["name"], ns["sessions"])
    check.true("internal API CA Secret renders", internal is not None)
    if internal is not None:
        check.eq("internal API CA Secret keys", set(internal), {values["internalApiCA"]["key"]})

    platform_ca = secret_data(index, values["tls"]["caSecretName"], RELEASE_NAMESPACE)
    needs_ca = values["postgresql"]["enabled"] or not openshift
    check.eq("platform CA renders only when something needs it", platform_ca is not None, needs_ca)

    leaves = {}
    if not openshift:
        for component in ("frontend", "backend-public", "backend-internal"):
            name = f"{values['app']['releaseName']}-{component}"
            want = service_names(name, ns["app"])
            if component != "backend-internal":
                want |= set(values["tls"]["publicHosts"])
            leaves[(f"{name}-tls", ns["app"])] = want
        if internal is not None and platform_ca is not None:
            check.eq("portable internal API CA is the platform CA", internal[values["internalApiCA"]["key"]], platform_ca["tls.crt"])
    else:
        check.true(
            "no app Service certificates on openshift (the service CA issues them)",
            not any(key[1].endswith("-tls") and key[2] == ns["app"] for key in generated),
        )
    if values["postgresql"]["enabled"]:
        name = values["postgresql"]["name"]
        leaves[(f"{name}-tls", ns["database"])] = service_names(name, ns["database"])
        database = secret_data(index, name, ns["database"])
        check.eq("evaluation database Secret keys", set(database or {}), {"password", "postgres-password"})
    for (name, namespace), want in leaves.items():
        secret = index.get(("Secret", name, namespace))
        check.true(f"TLS Secret {namespace}/{name} renders", secret is not None)
        if secret is None:
            continue
        check.eq(f"TLS Secret {name} type", secret.get("type"), "kubernetes.io/tls")
        check.eq(f"TLS Secret {name} keys", set(secret["data"]), {"tls.crt", "tls.key", "ca.crt"})
        check.eq(f"TLS Secret {name} ca.crt is the platform CA", secret["data"]["ca.crt"], (platform_ca or {}).get("tls.crt"))
        check_certificate(check, name, secret["data"], want)


def check_postgresql(docs: list[dict], index: dict, check: Check, values: dict) -> None:
    if not values["postgresql"]["enabled"]:
        check.true("no StatefulSet without postgresql.enabled", not any(key[0] == "StatefulSet" for key in index))
        return
    ns = values["namespaces"]["database"]
    name = values["postgresql"]["name"]
    statefulset = index[("StatefulSet", name, ns)]
    pod = statefulset["spec"]["template"]
    security = pod["spec"].get("securityContext") or {}
    if values["platform"] == "openshift":
        for field in ("runAsUser", "runAsGroup", "fsGroup"):
            check.true(f"PostgreSQL sets no {field} on openshift (restricted-v2 assigns it)", field not in security)
    annotations = pod["metadata"].get("annotations") or {}
    for annotation in ("marimohub.io/config-sha256", "marimohub.io/tls-restart-key"):
        check.true(f"PostgreSQL Pod template carries {annotation}", bool(annotations.get(annotation)))
    # On a first install both templates must agree without seeing each
    # other, or the first helm upgrade restarts PostgreSQL for nothing.
    leaf = index.get(("Secret", f"{name}-tls", ns))
    if leaf is not None:
        check.eq(
            "PostgreSQL Pod restart key equals its certificate Secret's",
            annotations.get("marimohub.io/tls-restart-key"),
            (leaf["metadata"].get("annotations") or {}).get("marimohub.io/restart-key"),
        )
    pseudo = [doc for doc in docs if doc["kind"] == "StatefulSet"]
    for doc in pseudo:
        doc = {**doc, "kind": "Deployment"}
        verify_chart.check_security_context([doc], check)
        verify_chart.check_images([doc], check)
    policies = {key[1]: index[key] for key in index if key[0] == "NetworkPolicy" and key[2] == ns}
    check.eq("database NetworkPolicies", set(policies), {"default-deny", name, "platform-test"})
    deny = policies.get("default-deny")
    if deny:
        check.eq("database default-deny", (deny["spec"]["podSelector"], sorted(deny["spec"]["policyTypes"])), ({}, ["Egress", "Ingress"]))
    allow = policies.get(name)
    if allow:
        sources = allow["spec"]["ingress"][0]["from"]
        app = sources[0]
        check.eq("database ingress namespace", app["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": values["namespaces"]["app"]}})
        expressions = {expr["key"]: set(expr["values"]) for expr in app["podSelector"]["matchExpressions"]}
        check.eq(
            "database clients",
            expressions,
            {
                "app.kubernetes.io/instance": {values["app"]["releaseName"]},
                "app.kubernetes.io/component": {"backend-public", "backend-internal", "migration", "reconcile-runtimes", "purge-workspaces"},
            },
        )
        check.eq("database ingress from the test Pod", sources[1], {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "platform-test"}}})
        check.eq("database ingress port", allow["spec"]["ingress"][0]["ports"], [{"port": 5432, "protocol": "TCP"}])


def check_test_pods(docs: list[dict], check: Check, values: dict) -> None:
    pods = [doc for doc in docs if doc["kind"] == "Pod"]
    want = {f"{RELEASE}-storage-test"}
    if values["postgresql"]["enabled"]:
        want.add(f"{RELEASE}-database-test")
    check.eq("helm test Pods", {pod["metadata"]["name"] for pod in pods}, want)
    for pod in pods:
        check.eq(f"{pod['metadata']['name']} is a helm test hook", pod["metadata"]["annotations"].get("helm.sh/hook"), "test")
        if values["platform"] == "openshift":
            check.true(f"{pod['metadata']['name']} sets no runAsUser on openshift", "runAsUser" not in (pod["spec"].get("securityContext") or {}))
    verify_chart.check_security_context(pods, check)
    verify_chart.check_images(pods, check)


def check_image_pullers(index: dict, check: Check, values: dict) -> None:
    namespace = values["imageRegistry"]["internalPullerNamespace"]
    bindings = [key for key in index if key[0] == "RoleBinding"]
    if not namespace:
        check.eq("no image-puller RoleBinding", bindings, [])
        return
    binding = index[("RoleBinding", "marimohub-image-pullers", namespace)]
    check.eq("image-puller roleRef", binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "system:image-puller"})
    ns = values["namespaces"]
    check.eq(
        "image-puller subjects",
        {subject["name"] for subject in binding["subjects"]},
        {f"system:serviceaccounts:{ns[key]}" for key in ("app", "controller", "sessions")},
    )
    check.true("no ClusterRoleBinding", not any(key[0] in ("ClusterRole", "ClusterRoleBinding") for key in index))


def operator_flags(app_index: dict, release: str, controller_namespace: str) -> dict[str, str]:
    operator = app_index[("Deployment", f"{release}-operator", controller_namespace)]
    flags = {}
    for arg in operator["spec"]["template"]["spec"]["containers"][0]["args"]:
        key, _, value = arg.partition("=")
        flags[key] = value
    return flags


def check_contract(chart_dir: str, index: dict, check: Check, values: dict, app_values: dict) -> None:
    """The app chart, given the values this chart writes, names only Secrets and claims this chart creates."""
    openshift = values["platform"] == "openshift"
    release = values["app"]["releaseName"]
    profile = os.path.join(APP_CHART, "values-openshift.yaml" if openshift else "values-kind.yaml")
    # What an installer adds on top: the Route or Ingress host, and on an
    # offline render the API server endpoints the platform chart looks up live.
    overlay = {"routes": {"host": OPENSHIFT_CI_HOST}} if openshift else {"ingress": {"host": (values["tls"]["publicHosts"] or ["localhost"])[0]}}
    if "kubernetesApi" not in (app_values.get("network") or {}):
        overlay["network"] = {"kubernetesApi": {"cidrs": ["10.0.0.10/32"]}}
    with tempfile.TemporaryDirectory() as work:
        platform_values = os.path.join(work, "platform-values.yaml")
        installer_values = os.path.join(work, "installer-values.yaml")
        with open(platform_values, "w") as handle:
            yaml.safe_dump(app_values, handle)
        with open(installer_values, "w") as handle:
            yaml.safe_dump(overlay, handle)
        files = [profile, platform_values, installer_values]
        verify = subprocess.run(
            [sys.executable, os.path.join(HERE, "verify_chart.py"), APP_CHART, *files],
            capture_output=True,
            text=True,
            env={**os.environ, "HELM": HELM},
        )
        check.true(f"verify_chart.py passes on the app chart with the platform values:\n{verify.stdout}{verify.stderr}", verify.returncode == 0)
        # Rendered again under the release name the platform chart was told
        # about, since its database NetworkPolicy selects Pods by it.
        cmd = [HELM, "template", release, APP_CHART, "-n", values["namespaces"]["app"], "--kube-version", KUBE_VERSION]
        for values_file in files:
            cmd += ["-f", values_file]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            check.true(f"app chart renders with the platform values: {result.stderr}", False)
            return
        app_docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    app_index = index_objects(app_docs)
    ns = values["namespaces"]

    def platform_has(kind: str, name: str, namespace: str) -> bool:
        return (kind, name, namespace) in index

    for (kind, name, namespace), doc in app_index.items():
        spec = verify_chart.pod_spec_of(doc)
        if spec is None:
            continue
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            for source in container.get("envFrom", []):
                secret = source.get("secretRef", {}).get("name")
                if secret == app_values.get("backendPublic", {}).get("existingSecret") and not values["backendEnv"]["existingSecret"]:
                    check.true(f"{kind} {name} envFrom Secret {secret} is created by the platform chart", platform_has("Secret", secret, namespace))
        for volume in spec.get("volumes", []):
            secret = (volume.get("secret") or {}).get("secretName")
            if not secret:
                continue
            if volume["name"] == "db-ca" and not values["database"]["ca"]["existingSecret"]:
                check.true(f"{kind} {name} database CA Secret {secret} is created by the platform chart", platform_has("Secret", secret, namespace))
            if volume["name"] == "tls" and not openshift:
                check.true(f"{kind} {name} TLS Secret {secret} is created by the platform chart", platform_has("Secret", secret, namespace))

    if values["postgresql"]["enabled"]:
        # Both sides of the database edge: the app's egress peer selects the
        # PostgreSQL Pods, and the database's ingress rule selects the app's
        # clients by the labels they really carry.
        statefulset = index[("StatefulSet", values["postgresql"]["name"], ns["database"])]
        pod_labels = statefulset["spec"]["template"]["metadata"]["labels"]
        clients = {"backend-public", "backend-internal", "migration", "reconcile-runtimes", "purge-workspaces"}
        for component in sorted(clients):
            policy = app_index[("NetworkPolicy", f"{release}-{component}", ns["app"])]
            peers = [peer for rule in verify_chart.database_rules(policy, 5432) for peer in rule.get("to", [])]
            check.true(
                f"{component} database egress selects the PostgreSQL Pods",
                any(
                    peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == ns["database"]
                    and all(pod_labels.get(k) == v for k, v in peer.get("podSelector", {}).get("matchLabels", {}).items())
                    for peer in peers
                ),
            )
        for (kind, name, namespace), doc in app_index.items():
            spec = verify_chart.pod_spec_of(doc)
            if spec is None or namespace != ns["app"]:
                continue
            template = doc["spec"]["jobTemplate"]["spec"]["template"] if kind == "CronJob" else doc["spec"]["template"]
            labels = template["metadata"]["labels"]
            if labels.get("app.kubernetes.io/component") in clients:
                check.eq(f"{kind} {name} instance label matches app.releaseName", labels.get("app.kubernetes.io/instance"), release)

    flags = operator_flags(app_index, release, ns["controller"])
    ca_name = flags.get("--internal-api-ca-secret-name")
    check.true(f"operator --internal-api-ca-secret-name {ca_name} is created by the platform chart", platform_has("Secret", ca_name, ns["sessions"]))
    internal = secret_data(index, ca_name, ns["sessions"]) or {}
    check.true(f"operator --internal-api-ca-secret-key is a key of {ca_name}", flags.get("--internal-api-ca-secret-key") in internal)
    if values["storage"]["workspaces"]["enabled"]:
        claim = flags.get("--workspace-claim-name")
        check.true(f"operator --workspace-claim-name {claim} is a platform claim", platform_has("PersistentVolumeClaim", claim, ns["sessions"]))
    shared = yaml.safe_load(flags.get("--shared-volumes", "[]"))
    check.eq("operator --shared-volumes count", len(shared), len(values["storage"]["shares"]))
    for volume in shared:
        check.true(f"operator shared volume {volume['name']} claim {volume['claimName']} is a platform claim", platform_has("PersistentVolumeClaim", volume["claimName"], ns["sessions"]))


def check_refusals(chart_dir: str, values_files: list[str], check: Check, values: dict) -> None:
    """Values the chart must refuse, and near misses it must accept."""
    if values["platform"] == "openshift":
        refused = [
            (["--set", "storage.workspaces.path=/data1/nfs/marimohub"], "overlap"),
            (["--set", "storage.workspaces.path=/data1"], "overlap"),
            (["--set", "storage.workspaces.path=/data1/nfs/"], "overlap"),
            (["--set", "storage.mode=hostPath"], "hostPath is for single-node test clusters only"),
            (["--set", "postgresql.enabled=true", "--set", "database.host=", "--set", "postgresql.podSecurityContext.runAsUser=999"], "postgresql.podSecurityContext must stay empty on openshift"),
            (["--set-json", 'storage.shares=[{"name":"a","path":"/x"},{"name":"a","path":"/y"}]'], "is used twice"),
            (["--set-json", 'storage.shares=[{"name":"a","path":"/x","claimName":"marimohub-workspaces"}]'], "is used twice"),
            (["--set", "storage.nfs.server="], "server (or storage.nfs.server) is required"),
        ]
        accepted = [
            ["--set", "storage.workspaces.path=/data1/nfs2"],
            ["--set", "storage.workspaces.server=other-nfs.example.internal", "--set", "storage.workspaces.path=/data1/nfs/marimohub"],
        ]
        if values["postgresql"]["enabled"]:
            refused += [
                (["--set", "database.host=db.example.internal"], "not both"),
            ]
        else:
            refused += [
                (["--set", "database.host=", "--set", "database.passwordSecret.name="], "set postgresql.enabled (evaluation), backendEnv.existingSecret, or database.host and database.passwordSecret"),
                (["--set", "database.ca.bundle="], "an external database needs the CA"),
                (["--set", "postgresql.enabled=true"], "not both"),
            ]
            accepted += [
                ["--set", "backendEnv.existingSecret=my-env", "--set", "database.host=", "--set", "database.passwordSecret.name="],
            ]
    else:
        refused = [
            (["--set", "storage.shares[0].readOnly=false"], "only a read-only share may list mode deploy"),
            (["--set", "storage.workspaces.hostPath=/var/lib/marimohub/nfs/workspaces"], "overlap"),
            (["--set", "platform=other"], "platform"),
            (["--set", "storage.workspaces.path="], "storage.workspaces.path is required"),
        ]
        accepted = [
            ["--set", "storage.shares[0].readOnly=false", "--set-json", 'storage.shares[0].modes=["edit","run"]'],
        ]
    for extra, message in refused:
        result = helm_template(chart_dir, values_files, extra)
        check.true(
            f"render with {' '.join(extra)} fails with {message!r}: {result.stderr.strip()[:200]}",
            result.returncode != 0 and message in result.stderr,
        )
    for extra in accepted:
        result = helm_template(chart_dir, values_files, extra)
        check.true(f"render with {' '.join(extra)} succeeds: {result.stderr.strip()[:200]}", result.returncode == 0)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    chart_dir, values_files = sys.argv[1], sys.argv[2:]
    docs = render(chart_dir, values_files)
    index = index_objects(docs)
    values = effective_values(chart_dir, values_files)
    app_values = app_values_of(index, values["namespaces"]["app"])

    check = Check()
    check_generated_copies(chart_dir, check)
    check_namespaces(index, check, values)
    check_crd_and_policy(index, check, values)
    check_storage(index, check, values, app_values)
    check_share_defaults(chart_dir, check)
    check_secrets(index, check, values)
    check_postgresql(docs, index, check, values)
    check_test_pods(docs, check, values)
    check_image_pullers(index, check, values)
    check_contract(chart_dir, index, check, values, app_values)
    check_refusals(chart_dir, values_files, check, values)

    if check.failures:
        print(f"verify_platform_chart: {len(check.failures)} check(s) failed for {values_files}:", file=sys.stderr)
        for failure in check.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"verify_platform_chart: all checks passed for {values_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
