# Architecture & System Design

This document describes the overall system architecture, data flows, and design decisions.

---

## High-Level Architecture

```
┌─────────────┐
│   Browser   │
│  Chat UI    │
└──────┬──────┘
       │
       │ HTTP
       ▼
┌──────────────────────────────────┐
│     FastAPI Backend (8000)       │
│  ┌────────────────────────────┐  │
│  │  Planner LLM               │  │
│  │ (Decides action type)      │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │  Summarizer LLM            │  │
│  │ (Grounds answer in facts)  │  │
│  └────────────────────────────┘  │
└──────────────────────────────────┘
       │          │
       │ (call)   │ (retrieve)
       ▼          ▼
┌──────────────┐  ┌──────────────────────┐
│ K8s Agent    │  │ Qdrant Vector DB     │
│ (8001)       │  │ + Knowledge Docs     │
│              │  │                      │
│ Queries live │  │ services.md,         │
│ K8s cluster  │  │ troubleshooting.md   │
└──────────────┘  └──────────────────────┘
       │
       │ (queries)
       ▼
┌──────────────────────────────┐
│  Local Kind Cluster          │
│  ┌──────────┐                │
│  │ Pods     │                │
│  │ Deployms │                │
│  │ Events   │                │
│  └──────────┘                │
└──────────────────────────────┘
```

---

## Request Flow

### 1. User Message → Planner
```
User: "Why are my pods crashing?"
        │
        ▼
Backend receives message
        │
        ▼
Planner LLM decides:
  - ask_followup (ask for clarification)
  - run_agent (query K8s)
  - answer_directly (use knowledge base only)
```

**Planner Prompt Tuning:**
- Recognizes Kubernetes keywords: pod, deploy, cluster, logs, events, status, crash, restart, error, diagnose
- Defaults to `run_agent` for ambiguous Kubernetes questions
- Only asks follow-ups for non-Kubernetes issues

### 2. Agent Query (if run_agent)
```
Agent receives decision
        │
        ▼
Connects to local kind cluster
        │
        ▼
Queries:
  - Pod status, restarts, logs
  - Deployment replicas, status
  - Events related to failures
  - Resource utilization
        │
        ▼
Returns structured evidence:
{
  "tools": [
    {"tool": "get_pods", "result": [...]}
  ]
}
```

### 3. Knowledge Retrieval (Parallel)
```
User query
        │
        ▼
Embed query with Ollama
        │
        ▼
Search Qdrant for relevant docs
  - services.md sections matching keywords
  - troubleshooting.md matching symptoms
        │
        ▼
Retrieved context (top-5 relevant sections)
```

### 4. Summarizer LLM
```
Inputs:
  - User query
  - Agent result (if from K8s)
  - Retrieved knowledge (from Qdrant)
        │
        ▼
Summarizer LLM generates:
  - Grounded answer (facts from evidence)
  - Actionable next steps
  - Links to relevant docs
        │
        ▼
Response to user
```

---

## Data Flow Diagram

### Message Processing Pipeline

```
1. Input Stage:
   ├─ User message received at /chat/message
   └─ Parse JSON, extract: message, namespace

2. Planner Stage:
   ├─ Send to Ollama (http://host.docker.internal:11434)
   ├─ LLM decides action: run_agent | ask_followup | answer_directly
   └─ Return plan with reasoning

3. Execution Stage (if run_agent):
   ├─ Call K8s Agent (http://agent:8001/diagnose)
   ├─ Query pods, deployments, events from kind cluster
   └─ Collect structured evidence

4. Knowledge Stage (Parallel with 3):
   ├─ Embed user query + agent results with Ollama
   ├─ Query Qdrant for semantic matches
   └─ Retrieve relevant docs (services, troubleshooting)

5. Summarizer Stage:
   ├─ Combine: user intent + agent evidence + docs
   ├─ Send to Ollama for final synthesis
   └─ Return grounded answer

6. Response Stage:
   └─ Return to user with evidence chain
```

---

## System Components

### 1. Backend (FastAPI, port 8000)
**Responsibilities:**
- HTTP API for chat messages (`/chat/message`)
- Orchestrates planner → agent → summarizer pipeline
- Calls Qdrant for knowledge retrieval
- Error handling and fallback logic

**Key Functions:**
```
- call_llm_json()         # Calls Ollama, retries multiple endpoints
- plan_request()          # Planner decision logic
- retrieve_knowledge()    # Qdrant semantic search (future)
- summarize_with_llm()    # Generates grounded answer
```

**Environment Variables:**
```
LLM_BASE_URL=http://host.docker.internal:11434
LLM_MODEL=llama2:latest
KUBE_API_SERVER=https://host.docker.internal:6443
KUBE_SKIP_TLS_VERIFY=true
```

### 2. K8s Agent (FastAPI, port 8001)
**Responsibilities:**
- Queries live kind cluster
- Returns pod/deployment/event information
- Collects diagnostic evidence

**Endpoints:**
```
GET /diagnose              # Returns all cluster diagnostics
GET /health               # Health check
```

**Kubernetes Access:**
- Service account: `kube-agent`
- Permissions: Read pods, deployments, events, logs
- Connection: TLS verify disabled for local kind

### 3. Qdrant Vector DB (port 6333)
**Responsibilities:**
- Stores embeddings of knowledge base docs
- Provides semantic search for relevant sections
- Recovers from disk on restart

**Collections:**
```
knowledge_base            # All .md docs from docs/
  ├─ services.md sections
  ├─ troubleshooting.md sections
  └─ architecture.md sections
```

### 4. Local Ollama (host.docker.internal:11434)
**Responsibilities:**
- Provides embeddings for knowledge retrieval
- Runs planner and summarizer LLMs
- Multi-endpoint compatibility (OpenAI API, Ollama API)

**Models Used:**
```
llama2:latest             # Planner + Summarizer
mistral:latest            # (Optional) faster for simple queries
nomic-embed-text:latest   # Embeddings for Qdrant
```

### 5. Kind Cluster (local Kubernetes)
**Responsibilities:**
- Local development/testing cluster
- Runs test services for agent to query

**Setup:**
```
Cluster name: noma
Nodes: 1 control-plane
API endpoint: 127.0.0.1:random_port (mapped to host.docker.internal in .env)
```

---

## Decision Records

### 1. Why Semantic Search + Knowledge Base?
**Problem:** Hardcoded agent responses don't scale; need contextual answers grounded in your actual system.

**Decision:** Use Qdrant + Ollama embeddings for semantic knowledge retrieval.

**Rationale:**
- Lightweight and local-first
- Grows with your documentation
- Supports both keyword and fuzzy search
- Embedding updates when docs change

### 2. Why Planner Before Agent?
**Problem:** Agent calls are expensive (latency); not all queries need K8s inspection.

**Decision:** Use LLM planner to decide action before calling agent.

**Rationale:**
- Routes questions efficiently
- Can answer general K8s questions without querying cluster
- Reduces latency for FAQ-type questions

### 3. Why Ollama Instead of Cloud LLM?
**Problem:** Cloud LLMs cost money, have latency, require internet.

**Decision:** Use local Ollama for all LLM calls.

**Rationale:**
- Zero cost per inference
- Instant response
- Works offline
- Full control over model behavior

### 4. Why Multi-Endpoint LLM Compatibility?
**Problem:** Ollama API changed, OpenAI clients expect different format.

**Decision:** `call_llm_json()` tries multiple endpoint styles (OpenAI `/v1/chat/completions`, Ollama `/api/generate`, `/api/chat`).

**Rationale:**
- Works with any local or cloud LLM
- Graceful fallback if one endpoint fails
- Users can swap LLM without code changes

---

## Error Handling & Resilience

### Graceful Degradation
```
1. If K8s Agent unavailable:
   - Planner returns ask_followup or answer_directly
   - User still gets response, but without live data

2. If Qdrant unavailable:
   - Skip knowledge retrieval
   - Use only agent + LLM reasoning

3. If Ollama unavailable:
   - Fall back to OpenAI API (if credentials available)
   - Or return cached responses
```

### Retry Logic
```
Planner LLM call:
  ├─ Try Ollama /api/generate
  ├─ Try Ollama /api/chat
  ├─ Try OpenAI /v1/chat/completions
  └─ If all fail, log error + return default response

Agent call:
  ├─ Try local endpoint (8001)
  ├─ Retry once on network error
  └─ Return diagnostic message if unable to connect
```

---

## Performance Considerations

### Latency Budget
```
User message received
  ├─ Parse + planner LLM call:     2-5s (Ollama)
  ├─ Agent query (if run_agent):   1-3s (kind cluster is fast)
  ├─ Knowledge retrieval (async):  <1s (Qdrant)
  ├─ Summarizer LLM call:          2-5s (Ollama)
  └─ Total expected:               5-15s per response

Optimizations (future):
  - Cache frequent queries
  - Parallel planner + knowledge retrieval
  - Use faster model for planner (mistral)
  - Stream token output to user
```

### Storage
```
Vector DB (Qdrant):
  - Estimated: ~100KB per doc
  - With 50 docs: ~5MB
  - Grows linearly with knowledge base

Backend cache:
  - Recent conversations (optional)
  - Query embeddings cache
```

---

## Future Enhancements

### Phase 2: Rich Agent Capabilities
- [ ] Query metrics from Prometheus
- [ ] Get logs with date range filtering
- [ ] Deploy/rollback automation
- [ ] Multi-cluster support

### Phase 3: Memory & Context
- [ ] Store conversation history (postgres)
- [ ] Multi-turn context awareness
- [ ] User preferences and saved queries

### Phase 4: Automation & Safety
- [ ] Write approval workflows for risky actions
- [ ] Audit logging for all LLM decisions
- [ ] Rate limiting + cost tracking

### Phase 5: Multi-LLM
- [ ] Route complex tasks to GPT-4, simple to Ollama
- [ ] Fine-tune Ollama models on your runbooks
- [ ] Streaming responses to UI
