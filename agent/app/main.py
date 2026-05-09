from __future__ import annotations

from datetime import datetime, timezone
import os
import re
from typing import Any

from fastapi import FastAPI
from kubernetes import client, config
from kubernetes.client import ApiException
from pydantic import BaseModel, Field
from urllib3.exceptions import MaxRetryError, NewConnectionError

app = FastAPI(title="noma agent", version="0.1.0")


class DiagnoseRequest(BaseModel):
    message: str = Field(min_length=1)
    cluster: str | None = None
    namespace: str | None = None


class DiagnoseResponse(BaseModel):
    answer: str
    evidence: list[dict[str, Any]]


class RemediateRequest(BaseModel):
    action_type: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    deployment: str = Field(min_length=1)
    container: str | None = None
    image: str | None = None
    reason: str | None = None


class RemediateResponse(BaseModel):
    status: str
    message: str
    evidence: list[dict[str, Any]]


def _is_service_list_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False

    list_patterns = [
        r"\blist\b.*\bservices?\b",
        r"\bshow\b.*\bservices?\b",
        r"\bwhat\s+are\s+the\s+services?\b",
        r"\bwhat\s+services?\b",
        r"\ball\s+services?\b",
    ]

    diagnostic_terms = [
        "error",
        "failed",
        "failing",
        "issue",
        "problem",
        "down",
        "crash",
        "restart",
        "not working",
        "unhealthy",
        "timeout",
    ]

    has_list_phrase = any(re.search(pattern, text) for pattern in list_patterns)
    has_diagnostic_signal = any(term in text for term in diagnostic_terms)
    return has_list_phrase and not has_diagnostic_signal


def _wants_all_namespaces(message: str) -> bool:
    text = (message or "").strip().lower()
    return "all namespaces" in text or "across namespaces" in text or "all services" in text


def _is_error_check_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    indicators = [
        "error",
        "failed",
        "failing",
        "issue",
        "problem",
        "down",
        "crash",
        "restart",
        "not working",
        "unhealthy",
        "timeout",
        "image",
    ]
    return any(word in text for word in indicators)


def _is_true(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def apply_kube_overrides() -> None:
    # For local Docker + kind, kubeconfig server usually points to 127.0.0.1 on the host.
    # From inside a container that address is not reachable, so allow an override.
    override_server = os.getenv("KUBE_API_SERVER")
    skip_tls_verify = _is_true(os.getenv("KUBE_SKIP_TLS_VERIFY"))

    if not override_server and not skip_tls_verify:
        return

    cfg = client.Configuration.get_default_copy()
    if override_server:
        cfg.host = override_server
    if skip_tls_verify:
        cfg.verify_ssl = False
        cfg.assert_hostname = False
    client.Configuration.set_default(cfg)


def load_kube_context() -> None:
    kubeconfig = os.getenv("KUBECONFIG")
    context = os.getenv("KUBE_CONTEXT")
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig, context=context)
    else:
        config.load_incluster_config()
    apply_kube_overrides()


@app.on_event("startup")
def startup() -> None:
    load_kube_context()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/verify/{namespace}/{deployment}")
def verify_deployment(namespace: str, deployment: str) -> dict[str, Any]:
    """Check whether a deployment is healthy after remediation."""
    apps_api = client.AppsV1Api()
    try:
        dep = apps_api.read_namespaced_deployment(name=deployment, namespace=namespace)
    except ApiException as exc:
        return {"healthy": False, "reason": f"deployment not found: {exc.reason}"}
    except (MaxRetryError, NewConnectionError, OSError) as exc:
        return {"healthy": False, "reason": f"cluster unreachable: {exc}"}

    desired = dep.spec.replicas or 0
    available = dep.status.available_replicas or 0
    ready = dep.status.ready_replicas or 0
    unavailable = dep.status.unavailable_replicas or 0
    healthy = available >= desired and unavailable == 0 and desired > 0
    return {
        "healthy": healthy,
        "desired": desired,
        "available": available,
        "ready": ready,
        "unavailable": unavailable,
    }


@app.post("/diagnose", response_model=DiagnoseResponse)
def diagnose(payload: DiagnoseRequest) -> DiagnoseResponse:
    namespace = payload.namespace or os.getenv("KUBE_NAMESPACE", "default")
    evidence: list[dict[str, Any]] = []
    service_list_intent = _is_service_list_intent(payload.message)
    all_namespaces = _wants_all_namespaces(payload.message)
    error_check_intent = _is_error_check_intent(payload.message)

    core = client.CoreV1Api()
    apps = client.AppsV1Api()

    try:
        if service_list_intent and all_namespaces:
            services = core.list_service_for_all_namespaces()
        else:
            services = core.list_namespaced_service(namespace=namespace)

        endpoints = core.list_namespaced_endpoints(namespace=namespace)

        pods = core.list_namespaced_pod(namespace=namespace)
        evidence.append({"tool": "get_pods", "namespace": namespace, "count": len(pods.items)})

        deployments = apps.list_namespaced_deployment(namespace=namespace)
        evidence.append({"tool": "get_deployments", "namespace": namespace, "count": len(deployments.items)})

        evidence.append(
            {
                "tool": "get_services",
                "scope": "all_namespaces" if (service_list_intent and all_namespaces) else "namespace",
                "namespace": None if (service_list_intent and all_namespaces) else namespace,
                "count": len(services.items),
            }
        )
        evidence.append({"tool": "get_endpoints", "namespace": namespace, "count": len(endpoints.items)})
    except (ApiException, MaxRetryError, NewConnectionError, OSError) as exc:
        answer = (
            "I could not reach the Kubernetes API from the agent container. "
            "Check KUBE_API_SERVER and KUBE_SKIP_TLS_VERIFY in your local .env."
        )
        evidence.append({"tool": "kubernetes_connectivity", "namespace": namespace, "error": str(exc)})
        return DiagnoseResponse(answer=answer, evidence=evidence)

    pod_details = []
    for pod in pods.items[:5]:
        pod_details.append(
            {
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "restarts": sum(container.restart_count or 0 for container in (pod.status.container_statuses or [])),
            }
        )

    service_details = []
    for svc in services.items[:25]:
        ports = [p.port for p in (svc.spec.ports or [])]
        service_details.append(
            {
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "type": svc.spec.type,
                "ports": ports,
            }
        )

    evidence.append({"tool": "service_summary", "items": service_details})

    endpoint_map: dict[str, int] = {}
    for ep in endpoints.items:
        total_addresses = 0
        for subset in ep.subsets or []:
            total_addresses += len(subset.addresses or [])
        endpoint_map[ep.metadata.name] = total_addresses

    evidence.append({"tool": "endpoint_summary", "items": endpoint_map})

    deployment_issues: list[dict[str, Any]] = []
    for dep in deployments.items:
        unavailable = dep.status.unavailable_replicas or 0
        available = dep.status.available_replicas or 0
        desired = dep.spec.replicas or 0
        if unavailable > 0 or available < desired:
            deployment_issues.append(
                {
                    "name": dep.metadata.name,
                    "namespace": dep.metadata.namespace,
                    "desired": desired,
                    "available": available,
                    "unavailable": unavailable,
                }
            )

    pod_issues: list[dict[str, Any]] = []
    for pod in pods.items[:40]:
        for status in (pod.status.container_statuses or []):
            waiting_reason = (status.state.waiting.reason if status.state and status.state.waiting else None)
            if waiting_reason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}:
                pod_issues.append(
                    {
                        "pod": pod.metadata.name,
                        "container": status.name,
                        "reason": waiting_reason,
                        "restarts": status.restart_count or 0,
                    }
                )
            elif (status.restart_count or 0) >= 3:
                pod_issues.append(
                    {
                        "pod": pod.metadata.name,
                        "container": status.name,
                        "reason": "HighRestarts",
                        "restarts": status.restart_count or 0,
                    }
                )

    service_issues: list[dict[str, Any]] = []
    for svc in services.items:
        if svc.metadata.namespace != namespace:
            continue
        ready_backends = endpoint_map.get(svc.metadata.name, 0)
        if ready_backends == 0:
            service_issues.append(
                {
                    "service": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "issue": "NoReadyEndpoints",
                }
            )

    evidence.append({"tool": "deployment_issues", "items": deployment_issues})
    evidence.append({"tool": "pod_issues", "items": pod_issues})
    evidence.append({"tool": "service_issues", "items": service_issues})

    if service_list_intent:
        if service_details:
            listed = ", ".join([f"{item['namespace']}/{item['name']}" for item in service_details])
            answer = (
                f"I found {len(services.items)} service(s) "
                f"{'across all namespaces' if all_namespaces else f'in namespace {namespace}'}: {listed}."
            )
        else:
            answer = (
                "No services were found "
                f"{'across all namespaces' if all_namespaces else f'in namespace {namespace}'}."
            )
        return DiagnoseResponse(answer=answer, evidence=evidence)

    if error_check_intent or deployment_issues or pod_issues or service_issues:
        problem_parts: list[str] = []
        if deployment_issues:
            problem_parts.append(f"deployment issues={len(deployment_issues)}")
        if pod_issues:
            problem_parts.append(f"pod issues={len(pod_issues)}")
        if service_issues:
            problem_parts.append(f"service endpoint issues={len(service_issues)}")
        if not problem_parts:
            problem_parts.append("no obvious runtime failures detected")

        answer = (
            f"Service diagnostic for namespace {namespace}: {', '.join(problem_parts)}. "
            "Review evidence for exact failing pod/container reason (for example ImagePullBackOff or CrashLoopBackOff)."
        )
        return DiagnoseResponse(answer=answer, evidence=evidence)

    if not pods.items:
        answer = f"No pods were found in namespace {namespace}. Check whether the namespace is correct or whether workloads have been deployed yet."
    else:
        answer = (
            f"I found {len(pods.items)} pod(s) and {len(deployments.items)} deployment(s) in namespace {namespace}. "
            f"Current pod summary: {pod_details}. This is a read-only diagnostic slice and will become more accurate once logs, events, and metrics are added."
        )

    evidence.append({"tool": "pod_summary", "items": pod_details})
    return DiagnoseResponse(answer=answer, evidence=evidence)


@app.post("/remediate", response_model=RemediateResponse)
def remediate(payload: RemediateRequest) -> RemediateResponse:
    allowed_actions = {"set_deployment_image", "rollout_restart_deployment"}
    if payload.action_type not in allowed_actions:
        return RemediateResponse(
            status="rejected",
            message=f"Action {payload.action_type} is not allowed.",
            evidence=[{"tool": "remediation_guard", "allowed_actions": sorted(allowed_actions)}],
        )

    apps = client.AppsV1Api()
    evidence: list[dict[str, Any]] = []

    try:
        deployment = apps.read_namespaced_deployment(name=payload.deployment, namespace=payload.namespace)
    except ApiException as exc:
        return RemediateResponse(
            status="failed",
            message=f"Could not read deployment {payload.namespace}/{payload.deployment}.",
            evidence=[{"tool": "read_deployment", "error": str(exc)}],
        )

    if payload.action_type == "set_deployment_image":
        if not payload.container or not payload.image:
            return RemediateResponse(
                status="rejected",
                message="set_deployment_image requires container and image fields.",
                evidence=[],
            )

        updated = False
        for c in deployment.spec.template.spec.containers:
            if c.name == payload.container:
                c.image = payload.image
                updated = True
                break

        if not updated:
            return RemediateResponse(
                status="failed",
                message=f"Container {payload.container} not found in deployment {payload.deployment}.",
                evidence=[{"tool": "set_image", "container": payload.container}],
            )

        body = {"spec": {"template": {"spec": {"containers": [{"name": payload.container, "image": payload.image}]}}}}
        try:
            apps.patch_namespaced_deployment(name=payload.deployment, namespace=payload.namespace, body=body)
        except ApiException as exc:
            return RemediateResponse(
                status="failed",
                message="Failed to patch deployment image.",
                evidence=[{"tool": "patch_deployment_image", "error": str(exc)}],
            )

        evidence.append(
            {
                "tool": "set_deployment_image",
                "namespace": payload.namespace,
                "deployment": payload.deployment,
                "container": payload.container,
                "image": payload.image,
            }
        )
        return RemediateResponse(
            status="applied",
            message=(
                f"Updated image for {payload.namespace}/{payload.deployment} container {payload.container} "
                f"to {payload.image}."
            ),
            evidence=evidence,
        )

    restart_patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    try:
        apps.patch_namespaced_deployment(name=payload.deployment, namespace=payload.namespace, body=restart_patch)
    except ApiException as exc:
        return RemediateResponse(
            status="failed",
            message="Failed to trigger rollout restart.",
            evidence=[{"tool": "rollout_restart", "error": str(exc)}],
        )

    evidence.append(
        {
            "tool": "rollout_restart_deployment",
            "namespace": payload.namespace,
            "deployment": payload.deployment,
        }
    )
    return RemediateResponse(
        status="applied",
        message=f"Triggered rollout restart for deployment {payload.namespace}/{payload.deployment}.",
        evidence=evidence,
    )
