import json
import os
import re
import uuid
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .policy import namespace_mutation_allowed, user_may_approve
from .rag import knowledge_base
from .store import delete_proposal, get_proposal, store_proposal

app = FastAPI(title="noma backend", version="0.1.0")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    cluster: str | None = None
    namespace: str | None = None
    user_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    evidence: list[dict[str, Any]]


class PlannerDecision(BaseModel):
    ack: str = "Let me check that for you."
    action: Literal["ask_followup", "run_agent", "answer_directly"] = "run_agent"
    followup_question: str | None = None
    direct_answer: str | None = None
    namespace: str | None = None
    cluster: str | None = None
    reason: str | None = None


class ApprovalDecision(BaseModel):
    approved: bool = True


def is_service_list_intent(message: str) -> bool:
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


def is_error_check_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False

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
    return any(term in text for term in indicators)


def parse_approval_message(message: str) -> tuple[bool, str | None]:
    text = (message or "").strip().lower()
    match = re.search(r"\bapprove\s+remediation\s+([a-f0-9\-]{8,})\b", text)
    if match:
        return True, match.group(1)
    return False, None


def extract_issue_summary(agent_data: dict[str, Any]) -> dict[str, Any]:
    result = {
        "pod_issues": [],
        "deployment_issues": [],
        "service_issues": [],
    }
    for item in agent_data.get("evidence", []):
        tool = item.get("tool")
        if tool == "pod_issues":
            result["pod_issues"] = item.get("items", [])
        if tool == "deployment_issues":
            result["deployment_issues"] = item.get("items", [])
        if tool == "service_issues":
            result["service_issues"] = item.get("items", [])
    return result


def fallback_remediation(payload: ChatRequest, agent_data: dict[str, Any]) -> dict[str, Any] | None:
    issues = extract_issue_summary(agent_data)
    pod_issues = issues["pod_issues"]
    if not pod_issues:
        return None

    image_issue = next(
        (x for x in pod_issues if x.get("reason") in {"ErrImagePull", "ImagePullBackOff"}),
        None,
    )
    if not image_issue:
        return None

    namespace = payload.namespace or "default"
    deployment = None
    deployment_issues = issues["deployment_issues"]
    if deployment_issues:
        deployment = deployment_issues[0].get("name")
    if not deployment:
        deployment = image_issue.get("pod", "").rsplit("-", 2)[0] or "service-a"

    container = image_issue.get("container") or "nginx"
    image = "nginx:latest"
    return {
        "title": "Fix invalid container image",
        "action_type": "set_deployment_image",
        "namespace": namespace,
        "deployment": deployment,
        "container": container,
        "image": image,
        "reason": f"Detected {image_issue.get('reason')} for container {container}",
        "kubectl_preview": f"kubectl -n {namespace} set image deployment/{deployment} {container}={image}",
        "rollback_hint": f"kubectl -n {namespace} rollout undo deployment/{deployment}",
        "safe_to_apply": True,
    }


async def propose_remediation_with_llm(
    payload: ChatRequest,
    agent_data: dict[str, Any],
    knowledge_hits: list[dict[str, Any]],
) -> dict[str, Any] | None:
    llm_messages = [
        {
            "role": "system",
            "content": (
                "You are a Kubernetes remediation planner. Return JSON only with keys: "
                "title, action_type, namespace, deployment, container, image, reason, kubectl_preview, safe_to_apply, rollback_hint. "
                "Allowed action_type values: set_deployment_image, rollout_restart_deployment, no_action. "
                "rollback_hint must be the exact kubectl command to undo the proposed action. "
                "Only propose safe, namespace-scoped actions."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_message": payload.message,
                    "namespace": payload.namespace,
                    "agent_answer": agent_data.get("answer"),
                    "agent_evidence": agent_data.get("evidence", []),
                    "knowledge_context": format_knowledge_context(knowledge_hits),
                }
            ),
        },
    ]

    raw, error = await call_llm_json(llm_messages)
    if not raw:
        return fallback_remediation(payload, agent_data)

    action_type = str(raw.get("action_type", "")).strip()
    if action_type not in {"set_deployment_image", "rollout_restart_deployment"}:
        return fallback_remediation(payload, agent_data)

    proposal = {
        "title": str(raw.get("title") or "Proposed remediation"),
        "action_type": action_type,
        "namespace": str(raw.get("namespace") or payload.namespace or "default"),
        "deployment": str(raw.get("deployment") or "").strip(),
        "container": str(raw.get("container") or "").strip(),
        "image": str(raw.get("image") or "").strip(),
        "reason": str(raw.get("reason") or "Generated remediation plan"),
        "kubectl_preview": str(raw.get("kubectl_preview") or ""),
        "rollback_hint": str(raw.get("rollback_hint") or ""),
        "safe_to_apply": bool(raw.get("safe_to_apply", True)),
    }

    if not proposal["deployment"]:
        fallback = fallback_remediation(payload, agent_data)
        return fallback

    if action_type == "set_deployment_image" and (not proposal["container"] or not proposal["image"]):
        fallback = fallback_remediation(payload, agent_data)
        return fallback

    return proposal


def llm_base_url() -> str:
    return os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def llm_api_key() -> str | None:
    return os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")


def llm_model() -> str:
    return os.getenv("LLM_MODEL", "gpt-4o-mini")


def llm_endpoints() -> list[tuple[str, str]]:
    base = llm_base_url().rstrip("/")
    endpoints: list[tuple[str, str]] = []

    if base.endswith("/v1"):
        endpoints.append((f"{base}/chat/completions", "openai"))
        endpoints.append((f"{base[:-3]}/api/chat", "ollama"))
        endpoints.append((f"{base[:-3]}/api/generate", "ollama-generate"))
    else:
        endpoints.append((f"{base}/v1/chat/completions", "openai"))
        endpoints.append((f"{base}/chat/completions", "openai"))
        endpoints.append((f"{base}/api/chat", "ollama"))
        endpoints.append((f"{base}/api/generate", "ollama-generate"))

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, kind in endpoints:
        if url not in seen:
            unique.append((url, kind))
            seen.add(url)
    return unique


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


async def call_llm_json(messages: list[dict[str, str]]) -> tuple[dict[str, Any] | None, str | None]:
    api_key = llm_api_key()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: str | None = None
    async with httpx.AsyncClient(timeout=45.0) as client:
        content: str | None = None
        for url, kind in llm_endpoints():
            try:
                if kind == "openai":
                    payload = {
                        "model": llm_model(),
                        "messages": messages,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                    }
                else:
                    if kind == "ollama":
                        payload = {
                            "model": llm_model(),
                            "messages": messages,
                            "stream": False,
                            "format": "json",
                            "options": {"temperature": 0},
                        }
                    else:
                        prompt = "\n\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])
                        payload = {
                            "model": llm_model(),
                            "prompt": prompt,
                            "stream": False,
                            "format": "json",
                            "options": {"temperature": 0},
                        }

                response = await client.post(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    body = response.text.strip().replace("\n", " ")
                    last_error = f"{url} returned {response.status_code}: {body[:200]}"
                    continue

                data = response.json()
                if kind == "openai":
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                elif kind == "ollama":
                    content = data.get("message", {}).get("content", "")
                else:
                    content = data.get("response", "")
                break
            except httpx.HTTPError as exc:
                last_error = f"{url} failed: {str(exc)}"
                continue

    if content is None:
        return None, last_error or "LLM endpoint request failed"

    parsed = _extract_json_object(content)
    if parsed is None:
        return None, "LLM returned non-JSON planner output"
    return parsed, None


async def plan_request(payload: ChatRequest, knowledge_hits: list[dict[str, Any]]) -> PlannerDecision:
    default_namespace = payload.namespace or "default"

    if is_service_list_intent(payload.message):
        return PlannerDecision(
            ack="Let me check that for you.",
            action="run_agent",
            namespace=payload.namespace,
            cluster=payload.cluster,
            reason="Deterministic routing for service listing request",
        )

    if is_error_check_intent(payload.message):
        return PlannerDecision(
            ack="Let me check that for you.",
            action="run_agent",
            namespace=payload.namespace or default_namespace,
            cluster=payload.cluster,
            reason="Deterministic routing for error diagnosis request",
        )

    fallback = PlannerDecision(
        ack="Let me check that for you.",
        action="run_agent",
        namespace=default_namespace,
        cluster=payload.cluster,
        reason="LLM planner unavailable, using default agent path",
    )

    llm_messages = [
        {
            "role": "system",
            "content": (
                "You are a Kubernetes triage planner. Return JSON only with keys: "
                "ack, action, followup_question, direct_answer, namespace, cluster, reason. "
                "Actions: ask_followup, run_agent, answer_directly. "
                "Default action is run_agent unless the request is explicitly unclear. "
                "Keywords that mean run_agent: pod, deploy, cluster, container, error, crash, restart, logs, events, status, why, check, diagnose, troubleshoot. "
                "Only ask_followup if the request is vague AND not Kubernetes-related. "
                "ack must be a short first response like 'Let me check that for you.'"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "message": payload.message,
                    "cluster": payload.cluster,
                    "namespace": payload.namespace,
                    "knowledge_context": format_knowledge_context(knowledge_hits),
                }
            ),
        },
    ]

    try:
        raw, planner_error = await call_llm_json(llm_messages)
    except httpx.HTTPError as exc:
        fallback.reason = f"LLM request failed: {str(exc)}"
        return fallback

    if not raw:
        if planner_error:
            fallback.reason = planner_error
        return fallback

    try:
        decision = PlannerDecision.model_validate(raw)
    except Exception:
        return fallback

    if not decision.namespace and payload.namespace:
        decision.namespace = payload.namespace
    if not decision.cluster and payload.cluster:
        decision.cluster = payload.cluster

    return decision


def format_knowledge_context(knowledge_hits: list[dict[str, Any]]) -> str:
    if not knowledge_hits:
        return "No knowledge context found."

    blocks: list[str] = []
    for i, hit in enumerate(knowledge_hits, start=1):
        excerpt = (hit.get("content") or "").strip()
        if len(excerpt) > 700:
            excerpt = excerpt[:700].rstrip() + "..."
        blocks.append(
            "\n".join(
                [
                    f"[{i}] source={hit.get('source')} section={hit.get('section')} score={hit.get('score')}",
                    excerpt,
                ]
            )
        )
    return "\n\n".join(blocks)


async def summarize_with_llm(
    payload: ChatRequest,
    plan: PlannerDecision,
    agent_data: dict[str, Any],
    knowledge_hits: list[dict[str, Any]],
) -> str:
    llm_messages = [
        {
            "role": "system",
            "content": (
                "You are a Kubernetes support assistant. Explain findings clearly and briefly. "
                "Use only provided evidence. If evidence is insufficient, ask one focused follow-up question."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_message": payload.message,
                    "planner_reason": plan.reason,
                    "agent_answer": agent_data.get("answer"),
                    "agent_evidence": agent_data.get("evidence", []),
                    "knowledge_context": format_knowledge_context(knowledge_hits),
                }
            ),
        },
    ]

    headers = {"Content-Type": "application/json"}
    api_key = llm_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_error: str | None = None
    async with httpx.AsyncClient(timeout=45.0) as client:
        for url, kind in llm_endpoints():
            try:
                if kind == "openai":
                    payload_body = {
                        "model": llm_model(),
                        "messages": llm_messages,
                        "temperature": 0.2,
                    }
                else:
                    if kind == "ollama":
                        payload_body = {
                            "model": llm_model(),
                            "messages": llm_messages,
                            "stream": False,
                            "options": {"temperature": 0.2},
                        }
                    else:
                        prompt = "\n\n".join([f"{m['role'].upper()}: {m['content']}" for m in llm_messages])
                        payload_body = {
                            "model": llm_model(),
                            "prompt": prompt,
                            "stream": False,
                            "options": {"temperature": 0.2},
                        }

                response = await client.post(url, headers=headers, json=payload_body)
                if response.status_code >= 400:
                    body = response.text.strip().replace("\n", " ")
                    last_error = f"{url} returned {response.status_code}: {body[:200]}"
                    continue

                data = response.json()
                if kind == "openai":
                    return data.get("choices", [{}])[0].get("message", {}).get(
                        "content", "I checked the cluster but could not produce a summary."
                    )
                if kind == "ollama":
                    return data.get("message", {}).get("content", "I checked the cluster but could not produce a summary.")
                return data.get("response", "I checked the cluster but could not produce a summary.")
            except httpx.HTTPError as exc:
                last_error = f"{url} failed: {str(exc)}"
                continue

    return f"I checked the cluster but could not produce an LLM summary. Reason: {last_error or 'unknown error'}"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.on_event("startup")
async def startup_index_knowledge() -> None:
    # Build an initial index so first chat request has knowledge retrieval available.
    try:
        await knowledge_base.build_index_if_needed()
    except Exception:
        # Retrieval remains best-effort; request path handles fallback if indexing fails.
        return


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>noma</title>
        <style>
            :root {
                color-scheme: dark;
                --bg: #0b1020;
                --panel: #121a33;
                --panel-soft: #17213f;
                --text: #e8ecff;
                --muted: #8d97bd;
                --accent: #6ee7ff;
                --accent-2: #9b87ff;
                --border: rgba(255, 255, 255, 0.08);
            }
            * { box-sizing: border-box; }
            body {
                margin: 0;
                font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background:
                    radial-gradient(circle at top left, rgba(110, 231, 255, 0.18), transparent 28%),
                    radial-gradient(circle at top right, rgba(155, 135, 255, 0.16), transparent 24%),
                    linear-gradient(180deg, #080b16 0%, #0b1020 100%);
                color: var(--text);
                min-height: 100vh;
            }
            .shell {
                max-width: 1100px;
                margin: 0 auto;
                padding: 32px 20px 40px;
            }
            .hero {
                display: grid;
                gap: 12px;
                margin-bottom: 20px;
            }
            .eyebrow {
                color: var(--accent);
                text-transform: uppercase;
                letter-spacing: 0.16em;
                font-size: 12px;
            }
            h1 {
                margin: 0;
                font-size: clamp(2rem, 4vw, 3.5rem);
                line-height: 1.05;
            }
            .subtitle {
                margin: 0;
                color: var(--muted);
                max-width: 760px;
            }
            .grid {
                display: grid;
                grid-template-columns: 1.5fr 0.9fr;
                gap: 18px;
            }
            .card {
                background: rgba(18, 26, 51, 0.86);
                border: 1px solid var(--border);
                border-radius: 18px;
                box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
                backdrop-filter: blur(16px);
            }
            .chat {
                min-height: 72vh;
                display: flex;
                flex-direction: column;
            }
            .chat-log {
                padding: 18px;
                overflow: auto;
                flex: 1;
                display: grid;
                gap: 12px;
            }
            .msg {
                padding: 14px 16px;
                border-radius: 14px;
                border: 1px solid var(--border);
                white-space: pre-wrap;
                line-height: 1.5;
            }
            .msg.user { background: rgba(110, 231, 255, 0.08); }
            .msg.assistant { background: rgba(155, 135, 255, 0.08); }
            .msg .role {
                display: block;
                margin-bottom: 6px;
                color: var(--muted);
                font-size: 12px;
                text-transform: uppercase;
                letter-spacing: 0.12em;
            }
            .composer {
                border-top: 1px solid var(--border);
                padding: 14px;
                display: grid;
                gap: 10px;
            }
            textarea {
                width: 100%;
                min-height: 88px;
                resize: vertical;
                border-radius: 14px;
                border: 1px solid var(--border);
                background: var(--panel-soft);
                color: var(--text);
                padding: 14px;
                font: inherit;
            }
            .row {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 10px;
            }
            input {
                width: 100%;
                border-radius: 12px;
                border: 1px solid var(--border);
                background: var(--panel-soft);
                color: var(--text);
                padding: 12px 14px;
                font: inherit;
            }
            button {
                justify-self: end;
                border: 0;
                border-radius: 999px;
                padding: 12px 18px;
                background: linear-gradient(135deg, var(--accent), var(--accent-2));
                color: #08101f;
                font-weight: 700;
                cursor: pointer;
            }
            .side {
                padding: 18px;
            }
            .side h2 {
                margin: 0 0 10px;
                font-size: 18px;
            }
            .side p, .side li {
                color: var(--muted);
                line-height: 1.6;
            }
            .evidence {
                margin-top: 12px;
                padding-top: 12px;
                border-top: 1px solid var(--border);
            }
            @media (max-width: 920px) {
                .grid { grid-template-columns: 1fr; }
            }
        </style>
    </head>
    <body>
        <main class="shell">
            <section class="hero">
                <div class="eyebrow">kubernetes troubleshooting agent</div>
                <h1>Ask your cluster what is wrong.</h1>
                <p class="subtitle">A local-first chat interface for diagnosing Kubernetes issues from a kind cluster, with read-only evidence from pods and deployments.</p>
            </section>
            <section class="grid">
                <div class="card chat">
                    <div id="chatLog" class="chat-log"></div>
                    <form id="composer" class="composer">
                        <textarea id="message" placeholder="Try: check pods in default namespace"></textarea>
                        <div class="row">
                            <input id="cluster" placeholder="Cluster (optional)" value="kind-noma" />
                            <input id="namespace" placeholder="Namespace (optional)" value="default" />
                        </div>
                        <button type="submit">Send</button>
                    </form>
                </div>
                <aside class="card side">
                    <h2>What is built first</h2>
                    <p>This first slice includes the chat UI, backend, agent, Postgres, Redis, and kind-based Kubernetes diagnostics.</p>
                    <ul>
                        <li>Read-only cluster queries</li>
                        <li>Local docker-compose runtime</li>
                        <li>RBAC-scoped kind cluster access</li>
                    </ul>
                    <div class="evidence">
                        <h2>Current status</h2>
                        <p id="status">Ready to ask the agent.</p>
                    </div>
                </aside>
            </section>
        </main>
        <script>
            const chatLog = document.getElementById('chatLog');
            const form = document.getElementById('composer');
            const messageInput = document.getElementById('message');
            const clusterInput = document.getElementById('cluster');
            const namespaceInput = document.getElementById('namespace');
            const status = document.getElementById('status');

            function addMessage(role, text) {
                const el = document.createElement('div');
                el.className = `msg ${role}`;
                const roleLabel = document.createElement('span');
                roleLabel.className = 'role';
                roleLabel.textContent = role;
                const body = document.createElement('div');
                body.textContent = text;
                el.appendChild(roleLabel);
                el.appendChild(body);
                chatLog.appendChild(el);
                chatLog.scrollTop = chatLog.scrollHeight;
            }

            addMessage('assistant', 'Start by describing the issue, for example: why are my pods restarting?');

            form.addEventListener('submit', async (event) => {
                event.preventDefault();
                const message = messageInput.value.trim();
                if (!message) return;

                addMessage('user', message);
                messageInput.value = '';
                status.textContent = 'Agent is checking the cluster...';

                const response = await fetch('/chat/message', {
                    method: 'POST',
                    headers: { 'content-type': 'application/json' },
                    body: JSON.stringify({
                        message,
                        cluster: clusterInput.value || null,
                        namespace: namespaceInput.value || null,
                    }),
                });

                const data = await response.json();
                addMessage('assistant', `${data.answer}\n\nEvidence:\n${JSON.stringify(data.evidence, null, 2)}`);
                status.textContent = 'Last request completed.';
            });
        </script>
    </body>
</html>
"""


@app.post("/chat/message", response_model=ChatResponse)
async def chat_message(payload: ChatRequest) -> ChatResponse:
    is_approval, proposal_id = parse_approval_message(payload.message)
    if is_approval and proposal_id:
        record = get_proposal(proposal_id)
        if not record:
            return ChatResponse(
                answer=f"No pending remediation found for id {proposal_id}. It may have expired or already been applied.",
                evidence=[{"stage": "approval", "proposal_id": proposal_id, "status": "not_found"}],
            )

        proposal_user_id = record.get("_user_id", "anonymous")
        proposal = {k: v for k, v in record.items() if k != "_user_id"}

        if not user_may_approve(payload.user_id, proposal_user_id):
            return ChatResponse(
                answer="You are not allowed to approve this remediation (user mismatch).",
                evidence=[{"stage": "approval", "proposal_id": proposal_id, "status": "forbidden"}],
            )

        mutation_ns = proposal.get("namespace", "")
        if not namespace_mutation_allowed(mutation_ns):
            return ChatResponse(
                answer=f"Mutations are not allowed in namespace '{mutation_ns}' by policy.",
                evidence=[{"stage": "approval", "proposal_id": proposal_id, "status": "policy_rejected", "namespace": mutation_ns}],
            )

        agent_url = os.getenv("AGENT_URL", "http://agent:8001")
        async with httpx.AsyncClient(timeout=45.0) as client:
            try:
                response = await client.post(
                    f"{agent_url}/remediate",
                    json={
                        "action_type": proposal["action_type"],
                        "namespace": proposal["namespace"],
                        "deployment": proposal["deployment"],
                        "container": proposal.get("container"),
                        "image": proposal.get("image"),
                        "reason": proposal.get("reason"),
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail="agent remediation apply failed") from exc

        apply_result = response.json()
        delete_proposal(proposal_id)

        verify_result: dict[str, Any] | None = None
        try:
            async with httpx.AsyncClient(timeout=15.0) as vc:
                vr = await vc.get(
                    f"{agent_url}/verify/{proposal['namespace']}/{proposal['deployment']}"
                )
                if vr.status_code == 200:
                    verify_result = vr.json()
        except httpx.HTTPError:
            pass

        health_summary = ""
        if verify_result is not None:
            if verify_result.get("healthy"):
                health_summary = (
                    f"\nVerification: deployment is healthy "
                    f"({verify_result.get('available')}/{verify_result.get('desired')} replicas ready)."
                )
            else:
                health_summary = (
                    f"\nVerification: deployment not yet fully healthy "
                    f"({verify_result.get('available', 0)}/{verify_result.get('desired', 0)} available, "
                    f"{verify_result.get('unavailable', 0)} unavailable). "
                    f"Rollback hint: {proposal.get('rollback_hint') or 'kubectl rollout undo'}."
                )

        return ChatResponse(
            answer=(
                f"Approved remediation {proposal_id} was applied.\n\n"
                f"{apply_result.get('message', 'Remediation execution completed.')}"
                f"{health_summary}"
            ),
            evidence=[
                {"stage": "approval", "proposal_id": proposal_id, "status": "applied", "proposal": proposal},
                {"stage": "remediation_apply", "result": apply_result},
                {"stage": "verification", "result": verify_result},
            ],
        )

    service_list_request = is_service_list_intent(payload.message)
    error_check_request = is_error_check_intent(payload.message)
    evidence: list[dict[str, Any]] = []
    knowledge_items: list[dict[str, Any]] = []
    knowledge_hits, knowledge_error = await knowledge_base.retrieve(payload.message, top_k=4)
    for hit in knowledge_hits:
        knowledge_items.append(
            {
                "source": hit.source,
                "section": hit.section,
                "score": round(hit.score, 4),
                "content": hit.content,
            }
        )

    knowledge_evidence: dict[str, Any] = {
        "stage": "knowledge",
        "query": payload.message,
        "hits": [
            {
                "source": item["source"],
                "section": item["section"],
                "score": item["score"],
            }
            for item in knowledge_items
        ],
    }
    if knowledge_error:
        knowledge_evidence["error"] = knowledge_error
    evidence.append(knowledge_evidence)

    plan = await plan_request(payload, knowledge_items)
    evidence.insert(0, {"stage": "planner", "plan": plan.model_dump(exclude_none=True)})

    if plan.action == "ask_followup":
        question = plan.followup_question or "Which namespace should I check?"
        if knowledge_items:
            question = (
                f"{question}\n\n"
                f"Knowledge found for your query:\n"
                f"{format_knowledge_context(knowledge_items)}"
            )
        return ChatResponse(answer=f"{plan.ack}\n\n{question}", evidence=evidence)

    if plan.action == "answer_directly":
        direct = plan.direct_answer or "I need a little more detail to proceed."
        if knowledge_items:
            direct = (
                f"{direct}\n\nRelevant knowledge:\n"
                f"{format_knowledge_context(knowledge_items)}"
            )
        return ChatResponse(answer=f"{plan.ack}\n\n{direct}", evidence=evidence)

    agent_url = os.getenv("AGENT_URL", "http://agent:8001")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{agent_url}/diagnose",
                json={
                    "message": payload.message,
                    "cluster": plan.cluster or payload.cluster,
                    "namespace": plan.namespace or payload.namespace,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="agent service unavailable") from exc

    data = response.json()
    evidence.append({"stage": "agent", "result": data})

    if error_check_request:
        issues = extract_issue_summary(data)
        has_issues = bool(issues["pod_issues"] or issues["deployment_issues"] or issues["service_issues"])
        if has_issues:
            proposal = await propose_remediation_with_llm(payload, data, knowledge_items)
            if proposal and proposal.get("safe_to_apply", False):
                proposal_id = str(uuid.uuid4())
                store_proposal(proposal_id, proposal, payload.user_id)
                evidence.append({"stage": "remediation_proposal", "proposal_id": proposal_id, "proposal": proposal})
                rollback = proposal.get("rollback_hint") or "kubectl rollout undo (if needed)"
                approval_text = (
                    f"{data.get('answer', 'Issue detected.')}\n\n"
                    f"Proposed remediation: {proposal.get('title')}\n"
                    f"Reason: {proposal.get('reason')}\n"
                    f"Preview: {proposal.get('kubectl_preview') or proposal.get('action_type')}\n"
                    f"Rollback if needed: {rollback}\n\n"
                    f"Approve by sending: approve remediation {proposal_id}"
                )
                return ChatResponse(answer=f"{plan.ack}\n\n{approval_text}", evidence=evidence)

    if service_list_request or error_check_request:
        return ChatResponse(answer=f"{plan.ack}\n\n{data.get('answer', 'I checked the cluster.')}", evidence=evidence)

    try:
        final_answer = await summarize_with_llm(payload, plan, data, knowledge_items)
    except httpx.HTTPError:
        final_answer = data.get("answer", "I checked the cluster but could not summarize the result.")

    return ChatResponse(answer=f"{plan.ack}\n\n{final_answer}", evidence=evidence)
