# Troubleshooting Guide

This document contains common Kubernetes issues, their symptoms, root causes, and solutions.

---

## Pod Crashes / CrashLoopBackOff

### Symptoms
- Pod status shows `CrashLoopBackOff`
- Recent restarts: 5+ restarts in last few minutes
- Logs show immediate error and exit

### Common Causes & Solutions

#### 1. Out of Memory (OOMKilled)
**Indicator:** `kubectl describe pod` shows "Last State: Terminated (OOMKilled)"

**Root Cause:** Container exceeded memory limit

**Solutions:**
```bash
# Check current usage
kubectl top pod {pod-name}

# Increase memory limit in deployment
kubectl set resources deployment {deployment-name} --limits=memory=2Gi --requests=memory=1Gi

# Or edit YAML
kubectl edit deployment {deployment-name}
# Change memory under spec.containers[].resources.limits.memory
```

#### 2. Application Error / Startup Failure
**Indicator:** `kubectl logs {pod-name}` shows application error

**Root Cause:** Application crashed during initialization

**Solutions:**
```bash
# Check logs for detailed error
kubectl logs {pod-name} --tail=50

# Check previous log (if pod restarted)
kubectl logs {pod-name} --previous

# Check environment variables are set
kubectl describe pod {pod-name} | grep -A 20 "Environment"
```

#### 3. Dependency Not Ready
**Indicator:** Logs show "Connection refused" or "Service not found"

**Root Cause:** Required service (database, cache, etc.) not ready when pod starts

**Solutions:**
```bash
# Add startup/readiness probes to give services time to initialize
kubectl patch deployment {deployment-name} --type='json' -p='[{"op": "add", "path": "/spec/template/spec/containers/0/startupProbe", "value": {"httpGet": {"path": "/health", "port": 8080}, "failureThreshold": 30, "periodSeconds": 10}}]'

# Or use init containers to wait for dependencies
# Check if dependency is running
kubectl get svc {required-service-name}
```

---

## ImagePullBackOff

### Symptoms
- Pod status shows `ImagePullBackOff`
- Event shows "Failed to pull image"

### Common Causes & Solutions

#### 1. Image Not Found
**Indicator:** Error message contains "image not found" or "404"

**Root Cause:** Image doesn't exist in registry

**Solutions:**
```bash
# Verify image exists in registry
docker pull {image:tag}

# Check image name in deployment
kubectl get deployment {deployment-name} -o yaml | grep image

# Rebuild and push image
docker build -t {registry}/{image}:{tag} .
docker push {registry}/{image}:{tag}
```

#### 2. Registry Credentials Missing
**Indicator:** Error message contains "Unauthorized" or "401"

**Root Cause:** Pod doesn't have credentials to access private registry

**Solutions:**
```bash
# Create image pull secret
kubectl create secret docker-registry regcred \
  --docker-server=docker.io \
  --docker-username={username} \
  --docker-password={password} \
  --docker-email={email}

# Add to deployment
kubectl patch serviceaccount default -p '{"imagePullSecrets": [{"name": "regcred"}]}'
```

---

## Pending Pod

### Symptoms
- Pod status shows `Pending`
- Pod not being scheduled to any node

### Common Causes & Solutions

#### 1. Insufficient Resources
**Indicator:** `kubectl describe pod {pod-name}` shows "Insufficient cpu" or "Insufficient memory"

**Root Cause:** No node has enough resources to schedule pod

**Solutions:**
```bash
# Check node resources
kubectl top nodes

# Reduce pod resource requests
kubectl edit deployment {deployment-name}
# Lower spec.containers[].resources.requests

# Add more nodes to cluster (if using managed K8s)
kubectl scale nodes {node-group} --replicas=3
```

#### 2. Node Affinity / Tolerations Issue
**Indicator:** `kubectl describe pod` shows node selector not matched

**Root Cause:** Pod requires specific node label/taint but no matching nodes

**Solutions:**
```bash
# Check node labels
kubectl get nodes --show-labels

# Check pod node selectors
kubectl get pod {pod-name} -o yaml | grep -A 5 nodeSelector

# Either add label to node or remove node selector from pod
kubectl label node {node-name} workload-type=compute
```

---

## Service Connection Issues

### Symptoms
- Application can't connect to another service
- Error: "Connection refused" or "Name resolution failed"

### Common Causes & Solutions

#### 1. Service DNS Not Resolving
**Indicator:** `nslookup {service-name}` fails inside pod

**Root Cause:** CoreDNS pod not running or service name incorrect

**Solutions:**
```bash
# Check CoreDNS status
kubectl get pods -n kube-system | grep coredns

# Verify service exists
kubectl get svc {service-name}

# Check service endpoints
kubectl get endpoints {service-name}

# Use fully qualified name: {service}.{namespace}.svc.cluster.local
```

#### 2. Service Port Mismatch
**Indicator:** Connection times out but DNS resolves

**Root Cause:** Pod connecting to wrong port

**Solutions:**
```bash
# Check service port configuration
kubectl describe svc {service-name}

# Check deployment target port
kubectl get deployment {deployment-name} -o yaml | grep -A 3 "ports:"

# Port should match between service.spec.ports[].targetPort and deployment.containers[].ports[].containerPort
```

#### 3. Network Policy Blocking Traffic
**Indicator:** Connection works on different namespace but not this one

**Root Cause:** NetworkPolicy restricting traffic

**Solutions:**
```bash
# Check network policies
kubectl get networkpolicy

# Describe policy
kubectl describe networkpolicy {policy-name}

# Temporarily allow all (for testing only!)
kubectl delete networkpolicy --all
```

---

## High CPU / Memory Usage

### Symptoms
- Pod consuming excessive resources
- Node running hot (CPU/memory near limits)
- Performance degradation

### Common Causes & Solutions

#### 1. Memory Leak
**Indicator:** Memory usage increases over time without dropping

**Root Cause:** Application bug retaining memory

**Solutions:**
```bash
# Monitor memory trend
kubectl top pod {pod-name} --containers

# Restart pod to clear memory
kubectl rollout restart deployment {deployment-name}

# Review application logs for errors
kubectl logs {pod-name} | grep -i error

# Check for goroutine leaks (if Go app)
curl http://localhost:6060/debug/pprof/goroutine
```

#### 2. Resource Limit Too Low
**Indicator:** CPU throttling shows in metrics

**Root Cause:** Pod's CPU request is lower than actual usage

**Solutions:**
```bash
# Check current limits
kubectl get deployment {deployment-name} -o yaml | grep -A 5 "resources:"

# Increase limits
kubectl set resources deployment {deployment-name} --limits=cpu=2000m --requests=cpu=1000m
```

---

## Pod Eviction

### Symptoms
- Pod suddenly disappears
- Event shows "Pod evicted"
- Node pressure: DiskPressure, MemoryPressure, or PIDPressure

### Common Causes & Solutions

#### 1. Node Disk Pressure
**Indicator:** Event message contains "DiskPressure"

**Root Cause:** Node disk almost full

**Solutions:**
```bash
# Check node disk usage
kubectl describe node {node-name} | grep -A 5 "Allocatable"

# Check container log disk usage
df -h /var/lib/docker

# Clean up unused images/containers
docker system prune -a

# Increase node disk size (if cloud provider)
```

#### 2. Node Memory Pressure
**Indicator:** Event message contains "MemoryPressure"

**Root Cause:** Node running out of memory

**Solutions:**
```bash
# Check node memory
free -h

# Identify memory-hungry pods
kubectl top pods --all-namespaces | sort --reverse -k 3 | head -20

# Evict low-priority pods or add more nodes
```

---

## Deployment Stuck Updating (Progressing: False)

### Symptoms
- Deployment shows `0/2 updated` or `0/2 available`
- Stuck for several minutes
- New ReplicaSet not getting pods

### Common Causes & Solutions

#### 1. New Image Not Pulling
**Root Cause:** Same as ImagePullBackOff above

**Solutions:**
```bash
kubectl describe replicaset {new-replicaset-name}
```

#### 2. Resource Quota Exceeded
**Indicator:** `kubectl describe replicaset` shows "FailedCreate"

**Root Cause:** Namespace resource quota prevents creating new pods

**Solutions:**
```bash
# Check quota
kubectl describe resourcequota

# Increase quota
kubectl set resourcequota {quota-name} --hard=pods=100,cpu=1000m
```

---

## Template: New Issue

### Symptoms
- What does the user see?
- Pod status / events?

### Common Causes & Solutions

#### 1. Root Cause Title
**Indicator:** How to identify this cause

**Root Cause:** Why it happens

**Solutions:**
```bash
# Diagnostic command
# Fix command
```

---

## Quick Diagnostic Commands

```bash
# Overall cluster health
kubectl get nodes
kubectl get pods --all-namespaces

# Check specific pod
kubectl describe pod {pod-name}
kubectl logs {pod-name} --tail=50

# Check resource usage
kubectl top nodes
kubectl top pods

# Check events
kubectl get events --sort-by='.lastTimestamp'

# Check resource quotas
kubectl describe resourcequota

# Check network policies
kubectl get networkpolicy --all-namespaces
```
