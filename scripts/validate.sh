#!/usr/bin/env bash
# Validates the Helm chart with zero cluster/kubeconfig contact and no GPU
# hardware required:
#   1. `helm lint` across default values and the GPU node-pool override.
#   2. `helm template` to confirm every manifest renders.
#   3. (optional) `kubeconform` for offline Kubernetes/CRD schema checks, if
#      installed. kubeconform is a standalone binary that validates against
#      bundled/downloaded JSON schemas — it never reads a kubeconfig or
#      talks to a live API server, unlike `kubectl --dry-run`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/../helm/vllm-inference"
OUT_DEFAULT="/tmp/vllm-inference-rendered-default.yaml"
OUT_GPU="/tmp/vllm-inference-rendered-gpu.yaml"

command -v helm >/dev/null 2>&1 || { echo "helm is required but not installed" >&2; exit 1; }

echo "==> helm lint (default values)"
helm lint "${CHART_DIR}"

echo "==> helm lint (values-gpu-node-pool.yaml override)"
helm lint "${CHART_DIR}" -f "${CHART_DIR}/values-gpu-node-pool.yaml"

echo "==> helm template (default values)"
helm template validate-release "${CHART_DIR}" > "${OUT_DEFAULT}"

echo "==> helm template (GPU node-pool override)"
helm template validate-release "${CHART_DIR}" -f "${CHART_DIR}/values-gpu-node-pool.yaml" > "${OUT_GPU}"

if command -v kubeconform >/dev/null 2>&1; then
  echo "==> kubeconform (offline schema validation, including KEDA/Prometheus CRDs)"
  kubeconform -strict -summary \
    -schema-location default \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
    "${OUT_DEFAULT}" "${OUT_GPU}"
else
  echo "==> kubeconform not installed, skipping offline schema validation"
  echo "    (install: https://github.com/yannh/kubeconform — optional, helm"
  echo "    lint/template above already caught template and YAML errors)"
fi

echo "==> All checks passed. Rendered manifests written to:"
echo "    ${OUT_DEFAULT}"
echo "    ${OUT_GPU}"
echo
echo "This script never reads a kubeconfig or contacts a Kubernetes API"
echo "server — it is safe to run with any context active, or none at all."
