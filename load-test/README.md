# Load test

A small async client that drives the vLLM OpenAI-compatible `/v1/completions`
endpoint to produce throughput/latency numbers and, in ramp mode, to exercise
KEDA autoscaling by stepping concurrency up over time.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Fixed concurrency for 60s:

```bash
python load_test.py --base-url http://localhost:8000 --model HiTZ/GoLLIE-7B \
  --concurrency 8 --duration 60
```

Ramp through concurrency levels (watch autoscaling react in another terminal):

```bash
python load_test.py --base-url http://localhost:8000 --model HiTZ/GoLLIE-7B \
  --ramp 1,2,4,8,16,32 --duration 60 --output results.json

# in another terminal:
watch kubectl get scaledobject,hpa,pods -l app.kubernetes.io/name=vllm-inference
```

## What it measures (and what it doesn't)

- Reports end-to-end request latency (mean/p50/p95/p99), successful
  throughput (req/s), and completion tokens/sec, computed from the `usage`
  field of each response.
- It does **not** measure time-to-first-token — that needs a streaming
  client. TTFT is instead sourced directly from vLLM's own
  `vllm:time_to_first_token_seconds` histogram in the Grafana dashboard
  (`../monitoring/grafana/dashboards/vllm-inference-dashboard.json`).
- This is a closed-loop load generator (each worker immediately issues the
  next request after the previous one completes), not an open-loop
  constant-arrival-rate generator — at high queueing it will understate how
  an open-loop production traffic pattern would behave.

This script was smoke-tested against a local mock server that mimics the
`/v1/completions` response shape, to verify the request loop, percentile
math, ramp sequencing, and JSON output. It has **not** been run against a
live vLLM/GPU deployment — do that yourself with the command above before
citing any numbers from this tool.
