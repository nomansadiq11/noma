# Kubernetes Services & Components

## Overview
This document describes all services running in the Kubernetes cluster, their purpose, versions, dependencies, and how they interact.

---

## Service: Service A (API Server)

**Namespace:** `service-a-ns`
**Type:** Deployment
**Image:** `nginx:latest`
**Replicas:** 2

### Purpose
Frontend API server serving HTTP/HTTPS requests. Handles incoming client connections and routes to backend services.

### Ports
- Port 80: HTTP API
- Port 443: HTTPS API

### Environment Variables
- `SERVICE_NAME`: service-a
- `LOG_LEVEL`: info

### Dependencies
- Requires: None (standalone service)
- Used by: Service B, Service C (for inter-service communication)

### Health Checks
- Liveness probe: `GET /` (HTTP 200) every 10s
- Readiness probe: `GET /` (HTTP 200) every 5s
- Initial delay: 10s (liveness), 5s (readiness)

### Resource Limits
- CPU: 100m request / 500m limit
- Memory: 128Mi request / 512Mi limit

### Known Issues & Workarounds
- **Issue:** High connection timeouts during load spikes
  - **Root Cause:** Insufficient replicas or worker threads
  - **Workaround:** Increase replicas to 4, tune nginx worker connections
- **Issue:** 502 Bad Gateway errors
  - **Root Cause:** Backend service unavailable
  - **Workaround:** Check Service B/C health and network connectivity

### Deployment Notes
- Updated on: 2026-05-09
- Last incident: None
- Backup strategy: Stateless (no data persistence needed)

---

## Service: Service B (Processing Worker)

**Namespace:** `service-b-ns`
**Type:** Deployment
**Image:** `nginx:latest`
**Replicas:** 2

### Purpose
Processing and transformation service that handles business logic. Receives requests from Service A and performs data processing.

### Ports
- Port 80: HTTP API
- Port 443: HTTPS API

### Environment Variables
- `SERVICE_NAME`: service-b
- `WORKER_THREADS`: 4
- `LOG_LEVEL`: info

### Dependencies
- Requires: Service A (for incoming requests)
- Used by: Service C (for result forwarding)

### Health Checks
- Liveness probe: `GET /` (HTTP 200) every 10s
- Readiness probe: `GET /` (HTTP 200) every 5s
- Initial delay: 10s (liveness), 5s (readiness)

### Resource Limits
- CPU: 150m request / 750m limit
- Memory: 256Mi request / 1Gi limit

### Known Issues & Workarounds
- **Issue:** Memory usage increasing over time
  - **Root Cause:** Possible memory leak in worker process
  - **Workaround:** Restart pods daily using CronJob, review application logs
- **Issue:** Processing latency increasing after 24h
  - **Root Cause:** Connection pool exhaustion
  - **Workaround:** Add connection pool reset on pod startup

### Deployment Notes
- Updated on: 2026-05-09
- Last incident: Memory leak on 2026-05-05, resolved by rolling restart
- Backup strategy: None (stateless service)

---

## Service: Service C (Data Aggregation)

**Namespace:** `service-c-ns`
**Type:** Deployment
**Image:** `nginx:latest`
**Replicas:** 3

### Purpose
Data aggregation and analytics service that collects results from Service B, performs aggregations, and exposes metrics. Critical for observability and reporting.

### Ports
- Port 80: HTTP API
- Port 443: HTTPS API
- Port 8080: Metrics endpoint (Prometheus format)

### Environment Variables
- `SERVICE_NAME`: service-c
- `AGGREGATION_INTERVAL`: 30s
- `LOG_LEVEL`: info
- `METRICS_ENABLED`: true

### Dependencies
- Requires: Service B (for data input)
- Used by: Monitoring system (Prometheus scrapes metrics)

### Health Checks
- Liveness probe: `GET /` (HTTP 200) every 10s
- Readiness probe: `GET /` (HTTP 200) every 5s
- Initial delay: 10s (liveness), 5s (readiness)

### Resource Limits
- CPU: 200m request / 1000m limit
- Memory: 512Mi request / 2Gi limit

### Known Issues & Workarounds
- **Issue:** Metrics endpoint returns 500 errors under high load
  - **Root Cause:** Aggregation buffer overflow
  - **Workaround:** Increase buffer size, reduce scrape frequency
- **Issue:** Data aggregation lag (results 5+ minutes behind)
  - **Root Cause:** Insufficient CPU allocation for processing
  - **Workaround:** Increase replicas to 5 during peak hours

### Deployment Notes
- Updated on: 2026-05-09
- Last incident: OOM on 2026-05-03 due to large aggregation window
- Backup strategy: Regular snapshots of aggregated metrics to external storage

---

## Quick Reference Table

| Service | Namespace | Type | Replicas | CPU | Memory | Status |
|---------|-----------|------|----------|-----|--------|--------|
| service-a | service-a-ns | Deployment | 2 | 100m/500m | 128Mi/512Mi | Running |
| service-b | service-b-ns | Deployment | 2 | 150m/750m | 256Mi/1Gi | Running |
| service-c | service-c-ns | Deployment | 3 | 200m/1000m | 512Mi/2Gi | Running |

---

## Service Interaction Diagram

```
External Request
    │
    ▼
┌─────────────┐
│ Service A   │ (API Server, 2 replicas)
│ Port 80/443 │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────┐
│ Service B (Processing, 2x)  │
│ Port 80/443                 │
└──────┬──────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ Service C (Aggregation, 3x)  │
│ Port 80/443 + 8080 (metrics) │
└──────┬───────────────────────┘
       │
       ▼
    Prometheus Scrape
    (metrics endpoint)
```

---

## Quick Reference Table

| Service | Namespace | Type | Replicas | CPU | Memory | Status |
|---------|-----------|------|----------|-----|--------|--------|
| service1 | default | Deployment | 2 | 500m | 512Mi | Running |
| service2 | monitoring | Deployment | 1 | 1000m | 1Gi | Running |
