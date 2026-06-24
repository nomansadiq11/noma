# Agent Implementation Testing Guide

## Quick Verification

### 1. Code Compiles ✅
```bash
python3 -m py_compile agent/app/main.py
# Should complete without errors
```

---

## Test Scenario 1: Healthy Cluster (Baseline)

### Setup
```bash
# Start Docker Compose
docker-compose up -d
```

### Query
```bash
curl -X POST http://localhost:8001/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "message": "check pods in default namespace",
    "namespace": "default"
  }'
```

### Expected Response
```json
{
  "answer": "I found 0 pod(s) and 0 deployment(s) in namespace default...",
  "evidence": [
    {"tool": "get_pods", "namespace": "default", "count": 0},
    ...
  ]
}
```

---

## Test Scenario 2: Pod Stuck in Pending (Resource Exhaustion)

### Setup
```bash
# Create deployment with unrealistic resource request
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: stuck-pod-test
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: stuck
  template:
    metadata:
      labels:
        app: stuck
    spec:
      containers:
      - name: app
        image: nginx:latest
        resources:
          requests:
            memory: "50Gi"  # Way too much
            cpu: "100"      # Way too much
EOF

# Wait 5 seconds for pod to enter Pending state
sleep 5
```

### Query
```bash
curl -X POST http://localhost:8001/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Why is my stuck-pod-test deployment stuck?",
    "namespace": "default"
  }'
```

### Expected Response (Key Parts)

**Answer should include:**
```
**Top Root Cause (Confidence: high):**
Unschedulable: insufficient cluster resources

**Evidence:**
  • Pod event: FailedScheduling
  • Message: Insufficient memory...

**Recommended Action:**
Scale cluster or reduce pod resource requests

**Alternative hypotheses:**
  2. Insufficient memory: pod request exceeds node capacity (confidence: high)
```

**Evidence should include:**
```json
{
  "stage": "pod_analysis",
  "pod": "stuck-pod-test-xxxx",
  "classification": {
    "phase": "Pending",
    "state_type": "Pending",
    "ready": false
  },
  "events": {
    "FailedScheduling": {
      "count": 3,
      "messages": ["Insufficient memory..."]
    }
  },
  "node_info": {
    "insufficient_resources": [
      {
        "node": "kind-noma-control-plane",
        "resource": "memory",
        "requested": "50Gi",
        "available": "xxMi"
      }
    ]
  },
  "hypotheses": [
    {
      "rank": 1,
      "root_cause": "Unschedulable: insufficient cluster resources",
      "confidence": "high"
    }
  ]
}
```

### Cleanup
```bash
kubectl delete deployment stuck-pod-test
```

---

## Test Scenario 3: Pod in CrashLoop (App Error)

### Setup
```bash
# Create a pod that fails immediately
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: crashloop-test
  namespace: default
spec:
  containers:
  - name: app
    image: busybox:latest
    command: ["sh", "-c", "exit 1"]  # Immediately fails
  restartPolicy: Always
EOF

# Wait 10 seconds for restarts
sleep 10
```

### Query
```bash
curl -X POST http://localhost:8001/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Why is crashloop-test crashing?",
    "namespace": "default"
  }'
```

### Expected Response (Key Parts)

**Answer should include:**
```
**Top Root Cause (Confidence: high):**
Container crash loop: application error

**Evidence:**
  • Container in CrashLoopBackOff state

**Recommended Action:**
Check container logs (current and previous) for error details
```

**Evidence should include:**
```json
{
  "stage": "pod_analysis",
  "pod": "crashloop-test",
  "classification": {
    "phase": "Running",
    "state_type": "Crashed",
    "reason": "Container in CrashLoopBackOff"
  },
  "logs": {
    "app": {
      "current": null,
      "previous": null  # Would contain exit trace if available
    }
  },
  "hypotheses": [
    {
      "rank": 1,
      "root_cause": "Container crash loop: application error",
      "confidence": "high"
    }
  ]
}
```

### Cleanup
```bash
kubectl delete pod crashloop-test
```

---

## Test Scenario 4: Pod with Image Pull Error

### Setup
```bash
# Try to pull a non-existent image
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: imagepull-test
  namespace: default
spec:
  containers:
  - name: app
    image: nonexistent:never-existed-12345
  restartPolicy: OnFailure
EOF

# Wait 5 seconds
sleep 5
```

### Query
```bash
curl -X POST http://localhost:8001/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Why is imagepull-test failing to start?",
    "namespace": "default"
  }'
```

### Expected Response (Key Parts)

**Answer should include:**
```
**Top Root Cause (Confidence: high):**
Image pull failure: registry or credentials issue

**Evidence:**
  • Pod container waiting with ImagePullBackOff

**Recommended Action:**
Verify image exists in registry and image pull secret is configured
```

**Evidence should include:**
```json
{
  "classification": {
    "state_type": "Crashed",
    "reason": "Container in ImagePullBackOff"
  },
  "hypotheses": [
    {
      "rank": 1,
      "root_cause": "Image pull failure: registry or credentials issue",
      "confidence": "high"
    }
  ]
}
```

### Cleanup
```bash
kubectl delete pod imagepull-test
```

---

## Test Scenario 5: Query Without Errors

### Query
```bash
curl -X POST http://localhost:8001/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "message": "List all services",
    "namespace": "default"
  }'
```

### Expected Response
```json
{
  "answer": "I found X service(s) in namespace default: ...",
  "evidence": [...]
}
```

---

## Verification Checklist

Run through each scenario and verify:

- [ ] **Scenario 1**: Baseline query works, returns pod count
- [ ] **Scenario 2**: Pending pod triggers FailedScheduling hypothesis (HIGH confidence)
- [ ] **Scenario 3**: CrashLoop pod triggers crash hypothesis (HIGH confidence)
- [ ] **Scenario 4**: ImagePull error triggers image hypothesis (HIGH confidence)
- [ ] **Scenario 5**: Service query works without triggering deep diagnostics
- [ ] **All scenarios**: Evidence contains hypotheses with confidence scores
- [ ] **All scenarios**: Evidence contains node_info with capacity checks
- [ ] **All scenarios**: Answer is structured and actionable

---

## Debugging Tips

### Check Agent Service Logs
```bash
docker logs noma_agent_1 -f
```

### Test Agent Directly (No Docker)
```bash
# In agent directory
export KUBE_CONTEXT=kind-noma
export KUBE_API_SERVER=https://127.0.0.1:6443
export KUBE_SKIP_TLS_VERIFY=true
python3 agent/app/main.py
```

### Manual Kubernetes Inspection
```bash
# Compare with agent output
kubectl get pods -n default
kubectl describe pod <pod-name> -n default
kubectl logs <pod-name> -n default
kubectl get events -n default --sort-by='.lastTimestamp'
```

### Backend Integration Test
```bash
curl -X POST http://localhost:8000/chat/message \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Why is my stuck-pod-test failing?",
    "namespace": "default"
  }'
```

---

## Expected Performance

| Operation | Time | Notes |
|-----------|------|-------|
| Service listing | ~50ms | Fast path, no deep analysis |
| Healthy pod query | ~100ms | Minimal evidence |
| Deep diagnostics (stuck pod) | ~400-500ms | Logs + events + node checks |
| Hypothesis ranking | ~10ms | Very fast, runs last |

---

## Success Criteria

✅ **The implementation is successful if:**

1. Code compiles without syntax errors
2. All 9 diagnostic functions are called during deep analysis
3. Pod state is correctly classified (Pending, Crashed, Running-Unready, etc.)
4. Events are analyzed and grouped by reason
5. Node capacity is compared against pod requests
6. Hypotheses are ranked by confidence
7. Logs are fetched when available
8. Evidence structure includes all diagnostic data
9. Answer is structured and actionable
10. Remediation integration works (hypotheses feed into backend proposals)

---

## Integration with Backend

The backend at `/chat/message` now receives much richer evidence:

```python
# Backend extracts top hypothesis
top_hypothesis = evidence[-1]["hypotheses"][0]

# Proposes remediation based on root cause
if "insufficient cluster resources" in top_hypothesis["root_cause"]:
    # Suggest: scale cluster or reduce resources

if "crash loop" in top_hypothesis["root_cause"]:
    # Suggest: review logs, rollback deploy

if "image pull failure" in top_hypothesis["root_cause"]:
    # Suggest: fix image tag or secret
```

---

## Common Issues & Fixes

### "Node info shows no resources"
- Ensure nodes are healthy: `kubectl get nodes`
- Check node status: `kubectl describe node`

### "Logs show as null"
- Pod must be in cluster for logs to exist
- Pending pods have no logs (never started)
- Previous logs only exist if pod restarted

### "Events not showing"
- Events are namespaced: must query correct namespace
- Events expire after ~1 hour in Kubernetes
- Use `kubectl get events` to compare

### "Pod state not Pending despite high memory request"
- Node might have capacity: check `kubectl top nodes`
- Kubelet might not enforce limits strictly in dev
- Try even higher resource requests

---

## Next Test: Production Scenario

Once basic tests pass, try:

1. Deploy real Airflow scheduler with memory leak
2. Watch pod restart and diagnosis improve
3. Verify remediation proposal is safe
4. Check approval workflow

Example coming soon in `PRODUCTION_TEST_SCENARIOS.md`
