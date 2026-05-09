#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-noma}"
CONTEXT_NAME="kind-${CLUSTER_NAME}"

kind get clusters | grep -qx "${CLUSTER_NAME}" || kind create cluster --name "${CLUSTER_NAME}"
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl config set-context --current --namespace=noma

API_SERVER="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"${CONTEXT_NAME}\")].cluster.server}")"
if [[ -z "${API_SERVER}" ]]; then
	API_SERVER="$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')"
fi

DOCKER_API_SERVER="${API_SERVER/127.0.0.1/host.docker.internal}"
DOCKER_API_SERVER="${DOCKER_API_SERVER/localhost/host.docker.internal}"

cat > .env <<EOF
KUBE_CONTEXT=${CONTEXT_NAME}
KUBE_NAMESPACE=default
KUBE_API_SERVER=${DOCKER_API_SERVER}
KUBE_SKIP_TLS_VERIFY=true
LLM_BASE_URL=${LLM_BASE_URL:-http://host.docker.internal:11434}
LLM_API_KEY=${LLM_API_KEY:-}
LLM_MODEL=${LLM_MODEL:-llama2:latest}
EOF

echo "Wrote Docker local settings to .env"
