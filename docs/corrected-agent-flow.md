# Corrected K8s Agent Flow (Multi-User + Approval Gates)

## Why this correction

Spawning one Kubernetes pod for every user question is usually too expensive and slow for chat workloads.

A better default is:

- Keep a stateless API + orchestrator that handles all chat requests.
- Use a worker pool for concurrent sessions.
- Create short-lived child pods only when you need isolated execution (for example, long diagnostics, custom scripts, or risky tooling).

## Recommended Runtime Topology

```mermaid
flowchart TD
    U[User in Web UI] --> G[API Gateway / Backend API]
    G --> A[AuthN/AuthZ + RBAC Context]
    A --> O[Orchestrator]

    O --> P[Planner LLM]
    O --> R[RAG Retriever]
    R --> Q[(Qdrant Vector DB)]
    R --> D[(Runbooks / Services Docs)]

    O --> W[Session Worker Pool]
    W --> K[Read-Only K8s Tools]
    K --> C[(Kubernetes Cluster)]

    O --> APP{Mutation Needed?}
    APP -- No --> S[Summarize + Respond]
    APP -- Yes --> PR[Generate Remediation Proposal]
    PR --> UIA[Show Approval Card in UI]
    UIA -->|Approve| EX[Execute Allowed Action]
    UIA -->|Reject| S

    EX --> MUT[Scoped Mutating Tool]
    MUT --> C
    EX --> VER[Post-Action Verification]
    VER --> S

    O --> AUD[(Audit Log: Postgres)]
    O --> SES[(Session State: Redis)]
```

## Approval Logic

- Read actions: no approval required.
- Mutating actions: approval required.
- Allowed mutations (allowlist):
  - set deployment image
  - rollout restart deployment
  - scale deployment
- Every proposal must include:
  - reason
  - exact target resource
  - preview command
  - expected impact
  - rollback suggestion

## Session and Concurrency Model

- Session key: user_id + cluster + namespace + conversation_id.
- Store in Redis:
  - current plan
  - pending approval proposal id
  - last evidence snapshot
- Worker pool handles parallel users safely.
- Optional child pod strategy:
  - create pod only for heavy or isolated tasks
  - attach TTL and cleanup job
  - mount least-privileged kubeconfig

## End-to-End Sequence

```mermaid
sequenceDiagram
    participant User
    participant UI
    participant API as Backend API
    participant ORCH as Orchestrator
    participant RAG as Retriever/Qdrant
    participant AG as K8s Agent
    participant K8S as Kubernetes

    User->>UI: Ask question
    UI->>API: POST /chat/message
    API->>ORCH: route with user context

    par Parallel grounding
        ORCH->>RAG: retrieve runbooks/docs
    and Live diagnosis
        ORCH->>AG: diagnose(message, namespace)
        AG->>K8S: read pods/deployments/services/events
        K8S-->>AG: evidence
        AG-->>ORCH: structured findings
    end

    ORCH->>ORCH: decide next step

    alt read-only answer
        ORCH-->>API: diagnosis + evidence
        API-->>UI: final answer
    else mutation recommended
        ORCH-->>API: remediation proposal + approval token
        API-->>UI: approval card
        User->>UI: approve action
        UI->>API: approve token
        API->>AG: execute allowed mutation
        AG->>K8S: apply patch/restart
        AG->>K8S: verify rollout health
        AG-->>API: execution + verification evidence
        API-->>UI: final outcome
    end
```

## Fit with current repository

This repository already implements most of this baseline:

- Planner and chat orchestration
- RAG retrieval from docs + Qdrant
- Diagnose endpoint with structured evidence
- Remediation endpoint with allowed action checks
- Approval token flow in backend chat handler

Major next upgrades:

- Persist pending approvals in Redis/Postgres instead of in-memory dict.
- Add policy engine for per-user and per-namespace mutation permissions.
- Add rollback proposal in remediation responses.
- Add child pod executor only for heavy/sandbox tasks.
