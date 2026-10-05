#!/usr/bin/env python3
"""Render-time policy assertions for the marimohub chart.

There is no live cluster available in this repository's CI/dev environment
(see the tool manifest in hack/tools), so `kubectl auth can-i --as=...`
cannot exercise the chart's RBAC against a real API server. This script is
the offline equivalent: it renders the chart with `helm template` and
parses the resulting Role/RoleBinding/NetworkPolicy/ServiceAccount objects
directly, asserting the *exact* verb/resource set and network edge for each
component rather than only checking that something is present. An exact-set
comparison catches an accidentally over-broad grant the same way it catches
an accidentally missing one.

Usage: verify_chart.py CHART_DIR VALUES_FILE [VALUES_FILE ...]
"""

from __future__ import annotations

import os
import subprocess
import sys

import yaml

RELEASE = "marimohub-test"

# Repository tooling is pinned under .bin/ (see hack/tools), not assumed to
# be on PATH; HELM lets the Makefile point this script at that exact binary
# without every caller needing to export PATH first.
HELM = os.environ.get("HELM", "helm")

# Offline `helm template`/`helm lint` fall back to Kubernetes v1.20.0 when no
# live cluster answers Capabilities.KubeVersion; this chart's Chart.yaml
# requires >=1.30, so every offline render must pin a compatible version
# explicitly rather than relying on a real API server to supply one.
KUBE_VERSION = "1.35.0"

# The operator's own RBAC sources, which the chart's operator Roles must
# mirror: role.yaml is generated from the kubebuilder markers in
# internal/controller/ by `make generate`; leader_election_role.yaml is
# maintained by hand.
OPERATOR_RBAC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "marimohub-operator", "config", "rbac"
)


def helm_template(chart_dir: str, values_files: list[str]) -> list[dict]:
    cmd = [HELM, "template", RELEASE, chart_dir, "--kube-version", KUBE_VERSION]
    for values_file in values_files:
        cmd += ["-f", values_file]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def deep_merge(base: dict, override: dict) -> dict:
    """Helm's own values-merge strategy: nested dicts merge key-by-key, everything else is replaced."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def effective_values(chart_dir: str, values_files: list[str]) -> dict:
    """The exact values Helm would compute for this render, without needing a live cluster.

    `helm template` has no flag that dumps its merged values, so this
    reproduces Helm's own merge order directly: the chart's own
    `values.yaml` first, then each `-f` override file in argument order.
    """
    with open(os.path.join(chart_dir, "values.yaml")) as handle:
        merged = yaml.safe_load(handle)
    for values_file in values_files:
        with open(values_file) as handle:
            merged = deep_merge(merged, yaml.safe_load(handle))
    return merged


def index_objects(docs: list[dict]) -> dict[tuple[str, str, str | None], dict]:
    index = {}
    for doc in docs:
        key = (doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace"))
        index[key] = doc
    return index


def rule_map(role: dict) -> dict[tuple[str, str], set[str]]:
    """Flatten a Role's `rules` into {(apiGroup, resource): {verbs}}.

    Every rule in this chart names exactly one apiGroup and lists one or
    more resources under it with a shared verb set, so exploding by
    resource (not by rule index) is what makes an exact per-resource
    comparison possible regardless of how rules happen to be grouped.
    Verbs from every rule naming the same resource are unioned, because
    the API server grants their union: keeping only the last rule's verbs
    would let an earlier, broader rule pass an exact-set check unseen.
    """
    flattened: dict[tuple[str, str], set[str]] = {}
    for rule in role["rules"]:
        for api_group in rule.get("apiGroups", [""]):
            for resource in rule["resources"]:
                flattened.setdefault((api_group, resource), set()).update(rule["verbs"])
    return flattened


def load_role(path: str, name: str) -> dict:
    """The Role called `name` from a plain (non-template) RBAC manifest."""
    with open(path) as handle:
        for doc in yaml.safe_load_all(handle):
            if doc and doc.get("kind") == "Role" and doc["metadata"]["name"] == name:
                return doc
    raise LookupError(f"no Role named {name!r} in {path}")


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def eq(self, label: str, got, want) -> None:
        if got != want:
            self.failures.append(f"{label}: got {got!r}, want {want!r}")

    def true(self, label: str, condition: bool) -> None:
        if not condition:
            self.failures.append(label)


def check_rbac(index: dict, check: Check, ns: dict) -> None:
    operator_role = index[("Role", f"{RELEASE}-operator", ns["sessions"])]
    check.eq(
        "operator Role rule set",
        rule_map(operator_role),
        {
            ("", "events"): {"create", "patch"},
            ("", "pods"): {"create", "delete", "get", "list", "patch", "update", "watch"},
            ("", "services"): {"create", "delete", "get", "list", "patch", "update", "watch"},
            ("", "resourcequotas"): {"get", "list"},
            ("", "secrets"): {"get", "list", "watch"},
            ("marimohub.io", "marimosessions"): {"delete", "get", "list", "watch"},
            # Not finalizer use: OwnerReferencesPermissionEnforcement (on by
            # default in OpenShift) requires it before the operator may set
            # blockOwnerDeletion=true on the Pods/Services it creates. update
            # only, never get/patch/delete: the admission check asks for
            # nothing else.
            ("marimohub.io", "marimosessions/finalizers"): {"update"},
            ("marimohub.io", "marimosessions/status"): {"get", "patch", "update"},
        },
    )

    leader_role = index[("Role", f"{RELEASE}-operator-leader-election", ns["controller"])]
    check.eq(
        "operator leader-election Role rule set",
        rule_map(leader_role),
        {
            ("coordination.k8s.io", "leases"): {"get", "list", "watch", "create", "update", "patch", "delete"},
            # The LeaderElection Event leader election records on its Lease
            # in this namespace (core/v1 Events). create/patch only.
            ("", "events"): {"create", "patch"},
        },
    )

    # The chart must grant the operator exactly what the operator's own RBAC
    # sources declare. Pinning both sides catches a kubebuilder marker that
    # changes without the chart following, or a chart edit that the markers
    # never learn about.
    check.eq(
        "operator Role mirrors marimohub-operator/config/rbac/role.yaml",
        rule_map(operator_role),
        rule_map(load_role(os.path.join(OPERATOR_RBAC_DIR, "role.yaml"), "manager-role")),
    )
    check.eq(
        "operator leader-election Role mirrors marimohub-operator/config/rbac/leader_election_role.yaml",
        rule_map(leader_role),
        rule_map(load_role(os.path.join(OPERATOR_RBAC_DIR, "leader_election_role.yaml"), "leader-election-role")),
    )

    public_role = index[("Role", f"{RELEASE}-backend-public", ns["sessions"])]
    check.eq(
        "backend-public Role rule set",
        rule_map(public_role),
        {
            ("marimohub.io", "marimosessions"): {"create", "get", "list", "patch", "delete"},
            ("", "secrets"): {"create", "get"},
        },
    )

    internal_role = index[("Role", f"{RELEASE}-backend-internal", ns["sessions"])]
    check.eq(
        "backend-internal Role rule set",
        rule_map(internal_role),
        {
            ("marimohub.io", "marimosessions"): {"get"},
            ("", "secrets"): {"get"},
        },
    )

    # Cross-namespace RoleBinding subjects: the Role and the ServiceAccount
    # it binds must live in the namespaces the Namespace Boundaries table
    # requires, not just any two namespaces that happen to match.
    bindings = {
        (f"{RELEASE}-operator", ns["sessions"], ns["controller"], f"{RELEASE}-operator"),
        (f"{RELEASE}-operator-leader-election", ns["controller"], ns["controller"], f"{RELEASE}-operator"),
        (f"{RELEASE}-backend-public", ns["sessions"], ns["app"], f"{RELEASE}-backend-public"),
        (f"{RELEASE}-backend-internal", ns["sessions"], ns["app"], f"{RELEASE}-backend-internal"),
    }
    for name, role_ns, subject_ns, subject_name in bindings:
        binding = index[("RoleBinding", name, role_ns)]
        subject = binding["subjects"][0]
        check.eq(f"RoleBinding {name} subject namespace", subject["namespace"], subject_ns)
        check.eq(f"RoleBinding {name} subject name", subject["name"], subject_name)
        check.eq(f"RoleBinding {name} roleRef", binding["roleRef"]["name"], name)

    # No cluster-scoped RBAC anywhere: every application/operator permission
    # in this chart is a namespaced Role, never a ClusterRole.
    for kind in ("ClusterRole", "ClusterRoleBinding"):
        check.true(
            f"no {kind} is rendered",
            not any(k[0] == kind for k in index),
        )

    automount = {
        (f"{RELEASE}-frontend", ns["app"]): False,
        (f"{RELEASE}-backend-public", ns["app"]): True,
        (f"{RELEASE}-backend-internal", ns["app"]): True,
        (f"{RELEASE}-operator", ns["controller"]): True,
        (f"{RELEASE}-migration", ns["app"]): False,
        (f"{RELEASE}-reconcile-runtimes", ns["app"]): True,
        (f"{RELEASE}-purge-workspaces", ns["app"]): True,
        (f"{RELEASE}-uninstall-drain", ns["app"]): True,
    }
    for (name, sa_ns), want in automount.items():
        sa = index[("ServiceAccount", name, sa_ns)]
        check.eq(f"ServiceAccount {name} automountServiceAccountToken", sa.get("automountServiceAccountToken"), want)

    # Alembic only ever talks to the database (see templates/migration/): no
    # Role/RoleBinding named for it exists anywhere, in either namespace.
    check.true(
        "no migration Role/RoleBinding is rendered",
        not any(k[0] in ("Role", "RoleBinding") and k[1] == f"{RELEASE}-migration" for k in index),
    )

    reconcile_role = index[("Role", f"{RELEASE}-reconcile-runtimes", ns["sessions"])]
    check.eq(
        "reconcile-runtimes Role rule set",
        rule_map(reconcile_role),
        {("marimohub.io", "marimosessions"): {"get", "list", "delete"}},
    )

    purge_role = index[("Role", f"{RELEASE}-purge-workspaces", ns["sessions"])]
    check.eq(
        "purge-workspaces Role rule set",
        rule_map(purge_role),
        {("marimohub.io", "marimosessions"): {"get", "list", "delete"}},
    )

    drain_role = index[("Role", f"{RELEASE}-uninstall-drain", ns["sessions"])]
    check.eq(
        "uninstall-drain Role rule set",
        rule_map(drain_role),
        {
            ("marimohub.io", "marimosessions"): {"get", "list", "watch", "delete"},
            ("", "pods"): {"get", "list", "watch"},
            ("", "services"): {"get", "list", "watch"},
            ("", "secrets"): {"get", "list", "watch"},
        },
    )

    maintenance_bindings = {
        (f"{RELEASE}-reconcile-runtimes", ns["sessions"], ns["app"], f"{RELEASE}-reconcile-runtimes"),
        (f"{RELEASE}-purge-workspaces", ns["sessions"], ns["app"], f"{RELEASE}-purge-workspaces"),
        (f"{RELEASE}-uninstall-drain", ns["sessions"], ns["app"], f"{RELEASE}-uninstall-drain"),
    }
    for name, role_ns, subject_ns, subject_name in maintenance_bindings:
        binding = index[("RoleBinding", name, role_ns)]
        subject = binding["subjects"][0]
        check.eq(f"RoleBinding {name} subject namespace", subject["namespace"], subject_ns)
        check.eq(f"RoleBinding {name} subject name", subject["name"], subject_name)


def networkpolicy_edges(policy: dict, direction: str) -> list[dict]:
    return policy["spec"].get(direction, [])


def check_networkpolicy(index: dict, check: Check, ns: dict, network: dict) -> None:
    for namespace in (ns["app"], ns["controller"], ns["sessions"]):
        deny = index[("NetworkPolicy", f"{RELEASE}-default-deny", namespace)]
        check.eq(f"default-deny podSelector in {namespace}", deny["spec"]["podSelector"], {})
        check.eq(f"default-deny policyTypes in {namespace}", set(deny["spec"]["policyTypes"]), {"Ingress", "Egress"})

    runtime_policy = index[("NetworkPolicy", f"{RELEASE}-runtime", ns["sessions"])]
    ingress = networkpolicy_edges(runtime_policy, "ingress")
    check.eq("Runtime NetworkPolicy has exactly one ingress rule", len(ingress), 1)
    check.eq("Runtime ingress port", ingress[0]["ports"][0]["port"], 8080)

    egress = networkpolicy_edges(runtime_policy, "egress")
    egress_namespace_selectors = [
        rule["to"][0].get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
        for rule in egress
        if "to" in rule and "namespaceSelector" in rule["to"][0]
    ]
    check.true(
        "Runtime egress never targets namespaces.controller (no Kubernetes API path)",
        ns["controller"] not in egress_namespace_selectors,
    )
    check.true(
        "Runtime egress targets namespaces.app exactly once (the internal API only)",
        egress_namespace_selectors.count(ns["app"]) == 1,
    )

    internal_policy = index[("NetworkPolicy", f"{RELEASE}-backend-internal", ns["app"])]
    check.eq(
        "backend-internal NetworkPolicy has exactly one ingress rule (from namespaces.sessions only)",
        len(networkpolicy_edges(internal_policy, "ingress")),
        1,
    )
    ingress_source = networkpolicy_edges(internal_policy, "ingress")[0]["from"][0]
    check.eq(
        "backend-internal ingress source is namespaces.sessions",
        ingress_source["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
        ns["sessions"],
    )

    def egress_ports(policy: dict) -> set[int]:
        """Every port an egress policy allows, ignoring DNS's fixed 53/UDP+TCP pair."""
        ports: set[int] = set()
        for rule in networkpolicy_edges(policy, "egress"):
            for port_entry in rule.get("ports", []):
                if port_entry.get("port") != 53:
                    ports.add(port_entry["port"])
        return ports

    migration_policy = index[("NetworkPolicy", f"{RELEASE}-migration", ns["app"])]
    check.true(
        "migration NetworkPolicy has no ingress rule",
        not networkpolicy_edges(migration_policy, "ingress"),
    )
    check.true(
        "migration NetworkPolicy egress never targets the Kubernetes API port"
        f" ({network['kubernetesApi']['port']})",
        network["kubernetesApi"]["port"] not in egress_ports(migration_policy),
    )

    drain_policy = index[("NetworkPolicy", f"{RELEASE}-uninstall-drain", ns["app"])]
    check.true(
        "uninstall-drain NetworkPolicy egress never targets the database port"
        f" ({network['database']['port']})",
        network["database"]["port"] not in egress_ports(drain_policy),
    )
    if network["kubernetesApi"]["cidrs"]:
        check.true(
            "uninstall-drain NetworkPolicy egress targets the Kubernetes API port",
            network["kubernetesApi"]["port"] in egress_ports(drain_policy),
        )

    for component in ("reconcile-runtimes", "purge-workspaces"):
        maintenance_policy = index[("NetworkPolicy", f"{RELEASE}-{component}", ns["app"])]
        ports = egress_ports(maintenance_policy)
        if network["database"]["cidrs"]:
            check.true(f"{component} NetworkPolicy egress targets the database port", network["database"]["port"] in ports)
        if network["kubernetesApi"]["cidrs"]:
            check.true(
                f"{component} NetworkPolicy egress targets the Kubernetes API port",
                network["kubernetesApi"]["port"] in ports,
            )


def pod_spec_of(doc: dict) -> dict | None:
    """The Pod spec any of this chart's workload kinds ultimately runs, or `None` if not a workload.

    A CronJob nests its Pod spec two levels deeper than a Job/Deployment
    (`spec.jobTemplate.spec.template.spec`); every other kind this chart
    renders keeps `spec.template.spec` (Deployment, Job) or is bare `spec`
    (Pod), matching the shape `check_images`/`check_security_context`
    already expected before the migration Job and maintenance CronJobs
    existed for this chart to check.
    """
    kind = doc.get("kind")
    if kind == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    if kind in ("Deployment", "Job"):
        return doc["spec"]["template"]["spec"]
    if kind == "Pod":
        return doc["spec"]
    return None


def check_images(docs: list[dict], check: Check) -> None:
    for doc in docs:
        spec = pod_spec_of(doc)
        if spec is None:
            continue
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            image = container.get("image", "")
            check.true(
                f"{doc['metadata']['name']}/{container['name']} image is digest-pinned: {image}",
                "@sha256:" in image,
            )


def check_security_context(docs: list[dict], check: Check) -> None:
    for doc in docs:
        pod_spec = pod_spec_of(doc)
        if pod_spec is None:
            continue
        pod_sc = pod_spec.get("securityContext", {})
        check.true(f"{doc['metadata']['name']} pod runAsNonRoot", pod_sc.get("runAsNonRoot") is True)
        check.eq(
            f"{doc['metadata']['name']} pod seccompProfile",
            pod_sc.get("seccompProfile", {}).get("type"),
            "RuntimeDefault",
        )
        for container in pod_spec.get("containers", []):
            csc = container.get("securityContext", {})
            name = f"{doc['metadata']['name']}/{container['name']}"
            check.eq(f"{name} allowPrivilegeEscalation", csc.get("allowPrivilegeEscalation"), False)
            check.eq(f"{name} readOnlyRootFilesystem", csc.get("readOnlyRootFilesystem"), True)
            check.eq(f"{name} capabilities.drop", csc.get("capabilities", {}).get("drop"), ["ALL"])
            check.true(f"{name} declares resources", bool(container.get("resources")))


def check_routes(index: dict, check: Check, ns: dict, values: dict) -> None:
    """Confirm whichever of Route (OpenShift)/Ingress (portable) rendered has the same-host,
    /api-and-/-path shape the Target Architecture requires, and never both at once.
    """
    has_route = any(k[0] == "Route" for k in index)
    has_ingress = any(k[0] == "Ingress" for k in index)
    check.true("Route and Ingress never both render for one profile", not (has_route and has_ingress))

    if has_route:
        api_route = index[("Route", f"{RELEASE}-api", ns["app"])]
        frontend_route = index[("Route", f"{RELEASE}-frontend", ns["app"])]
        check.eq("Route api path", api_route["spec"]["path"], "/api")
        check.eq("Route frontend path", frontend_route["spec"]["path"], "/")
        check.eq(
            "Route api and frontend share exactly one hostname",
            api_route["spec"]["host"],
            frontend_route["spec"]["host"],
        )
        for route in (api_route, frontend_route):
            check.eq(f"Route {route['metadata']['name']} TLS termination", route["spec"]["tls"]["termination"], "reencrypt")

    if has_ingress and values["ingress"]["enabled"]:
        ingress = index[("Ingress", f"{RELEASE}-ingress", ns["app"])]
        paths = {rule["path"] for rule in ingress["spec"]["rules"][0]["http"]["paths"]}
        check.eq("Ingress paths", paths, {"/api", "/"})
        check.true("Ingress has a tls entry", bool(ingress["spec"].get("tls")))


def check_high_availability(index: dict, check: Check, ns: dict) -> None:
    components = {
        "frontend": ns["app"],
        "backend-public": ns["app"],
        "backend-internal": ns["app"],
        "operator": ns["controller"],
    }
    for component, namespace in components.items():
        deployment = index[("Deployment", f"{RELEASE}-{component}", namespace)]
        replicas = deployment["spec"]["replicas"]
        pdb_key = ("PodDisruptionBudget", f"{RELEASE}-{component}", namespace)
        if replicas > 1:
            check.true(f"{component} PodDisruptionBudget renders (replicas={replicas})", pdb_key in index)
            check.true(
                f"{component} Deployment declares topologySpreadConstraints",
                bool(deployment["spec"]["template"]["spec"].get("topologySpreadConstraints")),
            )
            check.true(
                f"{component} Deployment declares podAntiAffinity",
                bool(
                    deployment["spec"]["template"]["spec"]
                    .get("affinity", {})
                    .get("podAntiAffinity")
                ),
            )
        else:
            check.true(f"{component} PodDisruptionBudget absent at replicas=1", pdb_key not in index)


def check_maintenance(index: dict, check: Check, ns: dict) -> None:
    for component in ("reconcile-runtimes", "purge-workspaces"):
        cronjob = index[("CronJob", f"{RELEASE}-{component}", ns["app"])]
        check.eq(f"{component} CronJob concurrencyPolicy", cronjob["spec"]["concurrencyPolicy"], "Forbid")
        check.true(f"{component} CronJob has a schedule", bool(cronjob["spec"]["schedule"]))

    migration_job = index[("Job", f"{RELEASE}-migration", ns["app"])]
    check.eq(
        "migration Job hook annotations",
        migration_job["metadata"]["annotations"].get("helm.sh/hook"),
        "pre-install,pre-upgrade",
    )
    check.eq("migration Job backoffLimit", migration_job["spec"]["backoffLimit"], 0)

    drain_job = index[("Job", f"{RELEASE}-uninstall-drain", ns["app"])]
    check.eq(
        "uninstall-drain Job hook annotations",
        drain_job["metadata"]["annotations"].get("helm.sh/hook"),
        "pre-delete",
    )
    check.eq("uninstall-drain Job backoffLimit", drain_job["spec"]["backoffLimit"], 0)


def check_monitoring(index: dict, check: Check, ns: dict, values: dict) -> None:
    if values["monitoring"]["serviceMonitor"]["enabled"]:
        service_monitor = index[("ServiceMonitor", f"{RELEASE}-operator", ns["controller"])]
        endpoint = service_monitor["spec"]["endpoints"][0]
        check.eq("ServiceMonitor endpoint port", endpoint["port"], "metrics")
        if values["monitoring"]["tls"]["enabled"]:
            check.eq("ServiceMonitor endpoint scheme", endpoint.get("scheme"), "https")
    else:
        check.true(
            "no ServiceMonitor renders when monitoring.serviceMonitor.enabled is false",
            not any(k[0] == "ServiceMonitor" for k in index),
        )


def check_quota(index: dict, check: Check, ns: dict, values: dict) -> None:
    quota = index[("ResourceQuota", f"{RELEASE}-runtime", ns["sessions"])]
    check.eq("ResourceQuota hard values", quota["spec"]["hard"], values["runtime"]["quota"]["hard"])

    limit_range = index[("LimitRange", f"{RELEASE}-runtime", ns["sessions"])]
    limit = limit_range["spec"]["limits"][0]
    check.eq("LimitRange max", limit["max"], values["runtime"]["limitRange"]["max"])
    check.eq("LimitRange default (limits)", limit["default"], values["runtime"]["resources"]["limits"])
    check.eq(
        "LimitRange defaultRequest (requests)", limit["defaultRequest"], values["runtime"]["resources"]["requests"]
    )


def check_runtime_image_flavors(index: dict, check: Check, ns: dict, values: dict) -> None:
    images = values["runtime"]["images"]
    check.true("runtime.images has at least the ubi and ubuntu flavors", {"ubi", "ubuntu"} <= set(images))
    for flavor, image in images.items():
        check.true(f"runtime.images.{flavor} is digest-pinned", "@sha256:" in image)

    default_flavor = values["runtime"]["defaultImageFlavor"]
    resolved_image = images[default_flavor]
    backend_public = index[("Deployment", f"{RELEASE}-backend-public", ns["app"])]
    env = {
        item["name"]: item.get("value")
        for item in backend_public["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    check.eq(
        "backend-public SESSION_RUNTIME_IMAGE resolves the configured default flavor",
        env.get("SESSION_RUNTIME_IMAGE"),
        resolved_image,
    )


def check_tls_certificates(index: dict, check: Check, ns: dict, values: dict) -> None:
    """Every TLS-enabled component's Deployment mounts the exact existing Secret its own values name."""
    components = {
        "frontend": (ns["app"], values["frontend"]),
        "backend-public": (ns["app"], values["backendPublic"]),
        "backend-internal": (ns["app"], values["backendInternal"]),
    }
    for component, (namespace, component_values) in components.items():
        if not component_values["tls"]["enabled"]:
            continue
        deployment = index[("Deployment", f"{RELEASE}-{component}", namespace)]
        volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
        check.eq(
            f"{component} tls volume secretName",
            volumes["tls"]["secret"]["secretName"],
            component_values["tls"]["secretName"],
        )
        if values["openshift"]["serviceServingCerts"]["enabled"]:
            service = index[("Service", f"{RELEASE}-{component}", namespace)]
            check.eq(
                f"{component} Service serving-cert annotation",
                service["metadata"].get("annotations", {}).get("service.beta.openshift.io/serving-cert-secret-name"),
                component_values["tls"]["secretName"],
            )


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    chart_dir, values_files = sys.argv[1], sys.argv[2:]

    docs = helm_template(chart_dir, values_files)
    index = index_objects(docs)
    values = effective_values(chart_dir, values_files)
    ns = {
        "app": "marimohub",
        "controller": "marimohub-controller",
        "sessions": "marimohub-sessions",
    }

    check = Check()
    check_rbac(index, check, ns)
    check_networkpolicy(index, check, ns, values["network"])
    check_images(docs, check)
    check_security_context(docs, check)
    check_routes(index, check, ns, values)
    check_high_availability(index, check, ns)
    check_maintenance(index, check, ns)
    check_monitoring(index, check, ns, values)
    check_quota(index, check, ns, values)
    check_runtime_image_flavors(index, check, ns, values)
    check_tls_certificates(index, check, ns, values)

    if check.failures:
        print(f"verify_chart: {len(check.failures)} check(s) failed for {values_files}:", file=sys.stderr)
        for failure in check.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"verify_chart: all checks passed for {values_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
