from __future__ import annotations

import os
from typing import Any

from kubernetes import client
from kubernetes.client import ApiException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from .intents import is_error_check_intent, is_service_list_intent, wants_all_namespaces
from .schemas import DiagnoseRequest, DiagnoseResponse


def _get_pod_logs(
    core: client.CoreV1Api,
    namespace: str,
    pod_name: str,
    container_name: str,
    previous: bool = False,
) -> str | None:
    """Fetch logs from a specific container. Returns None if not found."""
    try:
        logs = core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container_name,
            previous=previous,
            tail_lines=50,
        )
        return logs
    except ApiException:
        return None


def _analyze_pod_events(core: client.CoreV1Api, namespace: str, pod_name: str) -> dict[str, Any]:
    """Analyze pod events and group by reason."""
    events_by_reason: dict[str, Any] = {}
    try:
        events = core.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        for event in events.items:
            reason = event.reason or "Unknown"
            if reason not in events_by_reason:
                events_by_reason[reason] = {
                    "count": 0,
                    "messages": [],
                    "first_timestamp": None,
                    "last_timestamp": None,
                }
            events_by_reason[reason]["count"] = event.count or 1
            events_by_reason[reason]["messages"].append(event.message or "")
            if not events_by_reason[reason]["first_timestamp"]:
                events_by_reason[reason]["first_timestamp"] = str(event.first_timestamp)
            events_by_reason[reason]["last_timestamp"] = str(event.last_timestamp)
    except (ApiException, MaxRetryError, NewConnectionError, OSError):
        pass
    return events_by_reason


def _classify_pod_state(pod: Any) -> dict[str, Any]:
    """Classify pod state: Pending, Init, CrashLoop, Running-Unready, Terminating."""
    phase = pod.status.phase or "Unknown"

    classification = {
        "phase": phase,
        "state_type": "Unknown",
        "ready": False,
        "reason": "",
    }

    if pod.status.conditions:
        for condition in pod.status.conditions:
            if condition.type == "Ready":
                classification["ready"] = condition.status == "True"

    if phase == "Pending":
        classification["state_type"] = "Pending"
        classification["reason"] = "Pod scheduling not yet assigned or waiting for resources"
    elif phase == "Init":
        classification["state_type"] = "Init"
        classification["reason"] = "Init container(s) running or blocked"
    elif phase == "Running":
        if not classification["ready"]:
            classification["state_type"] = "Running-Unready"
            classification["reason"] = "Container running but readiness/liveness failing"
        else:
            classification["state_type"] = "Running"
            classification["reason"] = "Pod running and ready"
    elif phase == "Succeeded":
        classification["state_type"] = "Succeeded"
        classification["reason"] = "Pod completed successfully"
    elif phase == "Failed":
        classification["state_type"] = "Failed"
        classification["reason"] = "Pod encountered an unrecoverable error"
    elif phase == "Unknown":
        classification["state_type"] = "Unknown"
        classification["reason"] = "Pod state could not be determined"

    if pod.status.container_statuses:
        for status in pod.status.container_statuses:
            waiting_reason = status.state.waiting.reason if status.state and status.state.waiting else None
            if waiting_reason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}:
                classification["state_type"] = "Crashed"
                classification["reason"] = f"Container in {waiting_reason}"
            if (status.restart_count or 0) > 3 and "Restart" not in classification["reason"]:
                classification["reason"] += f" (high restart count: {status.restart_count})"

    return classification


def _parse_resource_quantity(qty: str) -> int:
    """Parse Kubernetes resource quantities (e.g., '512Mi', '1Gi') to bytes."""
    qty = (qty or "0").strip()
    multipliers = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
    }
    for suffix, mult in multipliers.items():
        if qty.endswith(suffix):
            return int(qty[: -len(suffix)]) * mult
    return int(qty) if qty.isdigit() else 0


def _parse_cpu_quantity(qty: str) -> int:
    """Parse CPU quantities (e.g., '100m', '1') to millicores."""
    qty = (qty or "0").strip()
    if qty.endswith("m"):
        return int(qty[:-1])
    return int(float(qty) * 1000)


def _check_node_capacity(core: client.CoreV1Api, pod: Any) -> dict[str, Any]:
    """Check node capacity vs pod resource requests."""
    result = {
        "nodes_available": 0,
        "node_pressure": [],
        "insufficient_resources": [],
        "taints": [],
    }

    try:
        nodes = core.list_node()
        result["nodes_available"] = len(nodes.items)

        for node in nodes.items:
            if node.spec.taints:
                for taint in node.spec.taints:
                    result["taints"].append(
                        {
                            "node": node.metadata.name,
                            "key": taint.key,
                            "effect": taint.effect,
                        }
                    )

            for condition in (node.status.conditions or []):
                if condition.status == "True" and "Pressure" in condition.type:
                    result["node_pressure"].append(
                        {
                            "node": node.metadata.name,
                            "type": condition.type,
                            "message": condition.message,
                        }
                    )

            allocatable = node.status.allocatable or {}
            for container in (pod.spec.containers or []):
                if container.resources and container.resources.requests:
                    requests = container.resources.requests
                    for res_type, req_val in requests.items():
                        try:
                            if res_type == "memory":
                                req_bytes = _parse_resource_quantity(req_val)
                                avail_bytes = _parse_resource_quantity(allocatable.get("memory", "0"))
                                if req_bytes > avail_bytes:
                                    result["insufficient_resources"].append(
                                        {
                                            "node": node.metadata.name,
                                            "resource": res_type,
                                            "requested": req_val,
                                            "available": allocatable.get("memory", "0"),
                                        }
                                    )
                            elif res_type == "cpu":
                                req_m = _parse_cpu_quantity(req_val)
                                avail_m = _parse_cpu_quantity(allocatable.get("cpu", "0"))
                                if req_m > avail_m:
                                    result["insufficient_resources"].append(
                                        {
                                            "node": node.metadata.name,
                                            "resource": res_type,
                                            "requested": req_val,
                                            "available": allocatable.get("cpu", "0"),
                                        }
                                    )
                        except (ValueError, TypeError):
                            pass
    except (ApiException, MaxRetryError, NewConnectionError, OSError):
        pass

    return result


def _check_namespace_quota(core: client.CoreV1Api, namespace: str) -> dict[str, Any]:
    """Check namespace resource quotas."""
    result = {
        "quotas": [],
        "limits": [],
    }
    try:
        quotas = core.list_namespaced_resource_quota(namespace=namespace)
        for quota in quotas.items:
            hard = quota.spec.hard or {}
            used = quota.status.used or {}
            for resource, hard_val in hard.items():
                used_val = used.get(resource, "0")
                try:
                    hard_int = (
                        _parse_resource_quantity(hard_val)
                        if resource == "memory"
                        else _parse_cpu_quantity(hard_val)
                        if resource == "cpu"
                        else int(hard_val)
                    )
                    used_int = (
                        _parse_resource_quantity(used_val)
                        if resource == "memory"
                        else _parse_cpu_quantity(used_val)
                        if resource == "cpu"
                        else int(used_val)
                    )
                    pct = (used_int / hard_int * 100) if hard_int > 0 else 0
                    result["quotas"].append(
                        {
                            "resource": resource,
                            "used": used_val,
                            "hard": hard_val,
                            "percent": round(pct, 1),
                        }
                    )
                except (ValueError, TypeError):
                    pass

        limits = core.list_namespaced_limit_range(namespace=namespace)
        for limit in limits.items:
            for limit_type in (limit.spec.limits or []):
                result["limits"].append(
                    {
                        "type": limit_type.type,
                        "max": limit_type.max,
                        "min": limit_type.min,
                    }
                )
    except (ApiException, MaxRetryError, NewConnectionError, OSError):
        pass
    return result


def _check_pvc_mounts(core: client.CoreV1Api, namespace: str, pod: Any) -> dict[str, Any]:
    """Check PVC binding and mount status."""
    result = {
        "pvcs": [],
        "unbound": [],
    }
    try:
        for volume in (pod.spec.volumes or []):
            if volume.persistent_volume_claim:
                pvc_name = volume.persistent_volume_claim.claim_name
                try:
                    pvc = core.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
                    result["pvcs"].append(
                        {
                            "name": pvc_name,
                            "phase": pvc.status.phase,
                            "size": pvc.spec.resources.requests.get("storage", "Unknown")
                            if pvc.spec.resources
                            else "Unknown",
                        }
                    )
                    if pvc.status.phase != "Bound":
                        result["unbound"].append(pvc_name)
                except ApiException:
                    result["unbound"].append(pvc_name)
    except (ApiException, MaxRetryError, NewConnectionError, OSError):
        pass
    return result


def _check_image_pull_secrets(core: client.CoreV1Api, namespace: str, pod: Any) -> dict[str, Any]:
    """Check image pull secrets and registry connectivity."""
    result = {
        "image_pull_secrets": [],
        "missing_secrets": [],
    }
    try:
        for secret_ref in (pod.spec.image_pull_secrets or []):
            secret_name = secret_ref.name
            try:
                secret = core.read_namespaced_secret(name=secret_name, namespace=namespace)
                result["image_pull_secrets"].append(
                    {
                        "name": secret_name,
                        "type": secret.type,
                        "found": True,
                    }
                )
            except ApiException:
                result["missing_secrets"].append(secret_name)
    except (ApiException, MaxRetryError, NewConnectionError, OSError):
        pass
    return result


def _rank_hypotheses(
    pod_state: dict[str, Any],
    pod_events: dict[str, Any],
    node_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rank root cause hypotheses with confidence scores."""
    hypotheses: list[dict[str, Any]] = []

    if pod_state["state_type"] == "Pending" and "FailedScheduling" in pod_events:
        event_msg = pod_events["FailedScheduling"].get("messages", [""])[0]
        if "Insufficient" in event_msg or "insufficient" in event_msg:
            hypotheses.append(
                {
                    "rank": 1,
                    "root_cause": "Unschedulable: insufficient cluster resources",
                    "confidence": "high",
                    "evidence": [
                        f"Pod event: FailedScheduling ({pod_events['FailedScheduling']['count']} times)",
                        f"Message: {event_msg}",
                    ],
                    "suggested_action": "Scale cluster or reduce pod resource requests",
                }
            )
        elif "Taints" in event_msg or "tolerations" in event_msg:
            hypotheses.append(
                {
                    "rank": 1,
                    "root_cause": "Unschedulable: node taints not tolerated",
                    "confidence": "high",
                    "evidence": [
                        "Pod event: FailedScheduling - taints mismatch",
                        f"Message: {event_msg}",
                    ],
                    "suggested_action": "Check node taints or add pod tolerations",
                }
            )
        else:
            hypotheses.append(
                {
                    "rank": 1,
                    "root_cause": "Unschedulable: scheduling constraint not met",
                    "confidence": "medium",
                    "evidence": [f"Pod event: FailedScheduling - {event_msg}"],
                    "suggested_action": "Review pod constraints and node availability",
                }
            )

    if pod_state["state_type"] == "Crashed" and "CrashLoopBackOff" in pod_state["reason"]:
        hypotheses.append(
            {
                "rank": 1 if not hypotheses else 2,
                "root_cause": "Container crash loop: application error",
                "confidence": "high",
                "evidence": ["Container in CrashLoopBackOff state"],
                "suggested_action": "Check container logs (current and previous) for error details",
            }
        )

    if "ImagePullBackOff" in pod_state["reason"] or "ImagePullBackOff" in pod_events:
        hypotheses.append(
            {
                "rank": 1 if not hypotheses else len(hypotheses) + 1,
                "root_cause": "Image pull failure: registry or credentials issue",
                "confidence": "high",
                "evidence": ["Pod container waiting with ImagePullBackOff"],
                "suggested_action": "Verify image exists in registry and image pull secret is configured",
            }
        )

    if pod_state["state_type"] == "Running-Unready":
        hypotheses.append(
            {
                "rank": 1 if not hypotheses else len(hypotheses) + 1,
                "root_cause": "Readiness probe failure: application not responding",
                "confidence": "high",
                "evidence": ["Pod running but readiness probe failing"],
                "suggested_action": "Check application logs and readiness probe configuration",
            }
        )

    if node_info["insufficient_resources"]:
        for res_issue in node_info["insufficient_resources"][:1]:
            hypotheses.append(
                {
                    "rank": 1 if not hypotheses else len(hypotheses) + 1,
                    "root_cause": f"Insufficient {res_issue['resource']}: pod request exceeds node capacity",
                    "confidence": "high",
                    "evidence": [
                        f"Node {res_issue['node']}: requested {res_issue['requested']}, available {res_issue['available']}",
                    ],
                    "suggested_action": f"Scale cluster or reduce pod {res_issue['resource']} requests",
                }
            )

    if node_info["node_pressure"]:
        hypotheses.append(
            {
                "rank": 1 if not hypotheses else len(hypotheses) + 1,
                "root_cause": "Node resource pressure: memory/disk full or threshold exceeded",
                "confidence": "medium",
                "evidence": [p["type"] for p in node_info["node_pressure"]],
                "suggested_action": "Free up node resources or evict non-critical pods",
            }
        )

    if node_info["taints"] and pod_state["state_type"] == "Pending":
        hypotheses.append(
            {
                "rank": 1 if not hypotheses else len(hypotheses) + 1,
                "root_cause": "Node taints prevent pod scheduling",
                "confidence": "medium",
                "evidence": [f"Node {t['node']} has taint {t['key']}={t['effect']}" for t in node_info["taints"]],
                "suggested_action": "Add pod tolerations or select different nodes",
            }
        )

    if not hypotheses:
        hypotheses.append(
            {
                "rank": 1,
                "root_cause": "Unable to determine root cause from available evidence",
                "confidence": "low",
                "evidence": [f"Pod state: {pod_state['state_type']}, Phase: {pod_state['phase']}"],
                "suggested_action": "Review pod events and logs manually using kubectl describe and kubectl logs",
            }
        )

    return hypotheses


def diagnose(payload: DiagnoseRequest) -> DiagnoseResponse:
    namespace = payload.namespace or os.getenv("KUBE_NAMESPACE", "default")
    evidence: list[dict[str, Any]] = []
    service_list_intent = is_service_list_intent(payload.message)
    all_namespaces = wants_all_namespaces(payload.message)
    error_check_intent = is_error_check_intent(payload.message)

    core = client.CoreV1Api()
    apps = client.AppsV1Api()

    try:
        if service_list_intent and all_namespaces:
            services = core.list_service_for_all_namespaces()
        else:
            services = core.list_namespaced_service(namespace=namespace)

        endpoints = core.list_namespaced_endpoints(namespace=namespace)
        pods = core.list_namespaced_pod(namespace=namespace)
        deployments = apps.list_namespaced_deployment(namespace=namespace)

        evidence.append({"tool": "get_pods", "namespace": namespace, "count": len(pods.items)})
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

    if service_list_intent:
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

    if error_check_intent or deployments.items or pods.items:
        pod_details = []
        deployment_issues: list[dict[str, Any]] = []
        pod_issues: list[dict[str, Any]] = []
        hypotheses_list: list[dict[str, Any]] = []

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

        for pod in pods.items[:40]:
            pod_info = {
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "restarts": sum(container.restart_count or 0 for container in (pod.status.container_statuses or [])),
            }
            pod_details.append(pod_info)

            if pod.status.phase not in {"Pending", "Failed"} and not any(
                s for s in (pod.status.container_statuses or []) if s.restart_count and s.restart_count >= 3
            ):
                continue

            pod_state = _classify_pod_state(pod)
            pod_events = _analyze_pod_events(core, namespace, pod.metadata.name)
            node_info = _check_node_capacity(core, pod)
            pvc_info = _check_pvc_mounts(core, namespace, pod)
            image_secrets = _check_image_pull_secrets(core, namespace, pod)

            logs_data = {}
            for container in (pod.spec.containers or [])[:1]:
                current_logs = _get_pod_logs(core, namespace, pod.metadata.name, container.name, previous=False)
                previous_logs = _get_pod_logs(core, namespace, pod.metadata.name, container.name, previous=True)
                logs_data[container.name] = {
                    "current": current_logs[:500] if current_logs else None,
                    "previous": previous_logs[:500] if previous_logs else None,
                }

            init_logs = {}
            for init_container in (pod.spec.init_containers or [])[:1]:
                init_log = _get_pod_logs(core, namespace, pod.metadata.name, init_container.name, previous=True)
                init_logs[init_container.name] = init_log[:500] if init_log else None

            quota_info = _check_namespace_quota(core, namespace)
            hypotheses = _rank_hypotheses(pod_state, pod_events, node_info)

            if pod_state["state_type"] in {"Pending", "Crashed", "Running-Unready", "Failed"}:
                pod_issues.append(
                    {
                        "pod": pod.metadata.name,
                        "state_type": pod_state["state_type"],
                        "reason": pod_state["reason"],
                        "top_hypothesis": hypotheses[0]["root_cause"] if hypotheses else "Unknown",
                    }
                )

            evidence.append(
                {
                    "stage": "pod_analysis",
                    "pod": pod.metadata.name,
                    "classification": pod_state,
                    "events": pod_events,
                    "node_info": node_info,
                    "pvc_info": pvc_info,
                    "image_secrets": image_secrets,
                    "logs": logs_data,
                    "init_logs": init_logs,
                    "quota_info": quota_info,
                    "hypotheses": hypotheses,
                }
            )

            if not hypotheses_list:
                hypotheses_list = hypotheses

        endpoint_map: dict[str, int] = {}
        for ep in endpoints.items:
            total_addresses = 0
            for subset in ep.subsets or []:
                total_addresses += len(subset.addresses or [])
            endpoint_map[ep.metadata.name] = total_addresses

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
        evidence.append({"tool": "pod_summary", "items": pod_details})

        if error_check_intent and not pods.items and not deployments.items and not services.items:
            hypotheses_list = [
                {
                    "rank": 1,
                    "root_cause": f"Service or workload not found in namespace '{namespace}'",
                    "confidence": "high",
                    "evidence": [
                        f"Namespace '{namespace}' contains: 0 pods, 0 deployments, 0 services",
                        "User query indicates issue with service, but no resources exist",
                    ],
                    "suggested_action": "1) Verify namespace name is correct; 2) Check if service should be deployed; 3) Verify service definition exists",
                }
            ]

        if hypotheses_list:
            top_hypothesis = hypotheses_list[0]
            answer = (
                f"**Diagnosis for namespace {namespace}:**\n\n"
                f"**Top Root Cause (Confidence: {top_hypothesis['confidence']}):**\n"
                f"{top_hypothesis['root_cause']}\n\n"
                f"**Evidence:**\n"
                f"{''.join([f'  • {e}' + chr(10) for e in top_hypothesis.get('evidence', [])])}\n"
                f"**Recommended Action:**\n"
                f"{top_hypothesis.get('suggested_action', 'Review pod details')}\n\n"
            )

            if len(hypotheses_list) > 1:
                answer += "**Alternative hypotheses:**\n"
                for h in hypotheses_list[1:3]:
                    answer += f"  {h['rank']}. {h['root_cause']} (confidence: {h['confidence']})\n"
        else:
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
                "Review evidence for detailed pod analysis including logs, events, and hypotheses."
            )

        return DiagnoseResponse(answer=answer, evidence=evidence)

    if not pods.items:
        answer = (
            f"No pods were found in namespace {namespace}. "
            "Check whether the namespace is correct or whether workloads have been deployed yet."
        )
    else:
        pod_details = [
            {
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "restarts": sum(container.restart_count or 0 for container in (pod.status.container_statuses or [])),
            }
            for pod in pods.items[:5]
        ]
        answer = (
            f"I found {len(pods.items)} pod(s) and {len(deployments.items)} deployment(s) in namespace {namespace}. "
            f"Current pod summary: {pod_details}. "
            "To get detailed diagnostics including logs and events, ask about specific issues."
        )
        evidence.append({"tool": "pod_summary", "items": pod_details})

    return DiagnoseResponse(answer=answer, evidence=evidence)
