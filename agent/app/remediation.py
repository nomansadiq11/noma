from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kubernetes import client
from kubernetes.client import ApiException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from .schemas import RemediateRequest, RemediateResponse


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

        body = {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": payload.container, "image": payload.image}]}
                }
            }
        }
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
                    "annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()}
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
