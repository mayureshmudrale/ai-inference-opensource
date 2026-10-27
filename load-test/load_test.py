#!/usr/bin/env python3
"""Closed-loop load generator for a vLLM OpenAI-compatible server.

Sends concurrent completion requests, then reports throughput and latency
percentiles. Supports a single fixed-concurrency run, or a "ramp" through
several concurrency levels in sequence so you can watch KEDA scale the
Deployment out (and back in) as load changes — e.g. in a second terminal:

    watch kubectl get scaledobject,hpa,pods -l app.kubernetes.io/name=vllm-inference

This measures end-to-end request latency only (time to the full,
non-streamed response). It does NOT measure time-to-first-token, since that
requires a streaming client; the Grafana dashboard sources TTFT directly
from vLLM's own histogram metrics instead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import aiohttp

DEFAULT_PROMPTS = [
    "The quick brown fox jumps over the lazy dog. Continue the story:",
    "Summarize the key benefits of container orchestration in two sentences:",
    "Write a short product description for a wireless noise-cancelling headset:",
    "List three best practices for securing a Kubernetes cluster:",
    "Explain the difference between horizontal and vertical pod autoscaling:",
]


@dataclass
class RequestResult:
    ok: bool
    latency_s: float
    completion_tokens: int = 0
    error: str = ""


@dataclass
class LevelSummary:
    concurrency: int
    duration_s: float
    requests_ok: int
    requests_error: int
    throughput_rps: float
    tokens_per_sec: float
    latency_mean_s: float | None
    latency_p50_s: float | None
    latency_p95_s: float | None
    latency_p99_s: float | None
    errors_sample: list = field(default_factory=list)


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, int(round(p * (len(sorted_values) - 1)))))
    return sorted_values[idx]


async def worker(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompts: list[str],
    max_tokens: int,
    deadline: float,
    results: list[RequestResult],
) -> None:
    while time.monotonic() < deadline:
        prompt = random.choice(prompts)
        payload = {
            "model": model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        start = time.perf_counter()
        try:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                elapsed = time.perf_counter() - start
                if resp.status == 200:
                    completion_tokens = data.get("usage", {}).get("completion_tokens", 0)
                    results.append(RequestResult(ok=True, latency_s=elapsed, completion_tokens=completion_tokens))
                else:
                    results.append(RequestResult(ok=False, latency_s=elapsed, error=f"HTTP {resp.status}: {data}"))
        except Exception as exc:  # noqa: BLE001 - we want to record *any* failure as a result
            elapsed = time.perf_counter() - start
            results.append(RequestResult(ok=False, latency_s=elapsed, error=str(exc)))


async def run_level(
    base_url: str,
    model: str,
    prompts: list[str],
    max_tokens: int,
    concurrency: int,
    duration: float,
    request_timeout: float,
) -> LevelSummary:
    url = f"{base_url.rstrip('/')}/v1/completions"
    results: list[RequestResult] = []
    deadline = time.monotonic() + duration
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    connector = aiohttp.TCPConnector(limit=0)

    wall_start = time.perf_counter()
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            asyncio.create_task(worker(session, url, model, prompts, max_tokens, deadline, results))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*tasks)
    wall_time = time.perf_counter() - wall_start

    latencies = sorted(r.latency_s for r in results if r.ok)
    ok = [r for r in results if r.ok]
    errors = [r for r in results if not r.ok]
    total_tokens = sum(r.completion_tokens for r in ok)

    return LevelSummary(
        concurrency=concurrency,
        duration_s=wall_time,
        requests_ok=len(ok),
        requests_error=len(errors),
        throughput_rps=len(ok) / wall_time if wall_time > 0 else 0.0,
        tokens_per_sec=total_tokens / wall_time if wall_time > 0 else 0.0,
        latency_mean_s=statistics.mean(latencies) if latencies else None,
        latency_p50_s=percentile(latencies, 0.50),
        latency_p95_s=percentile(latencies, 0.95),
        latency_p99_s=percentile(latencies, 0.99),
        errors_sample=[e.error for e in errors[:5]],
    )


def fmt_s(value: float | None) -> str:
    return f"{value * 1000:.0f}ms" if value is not None else "n/a"


def print_summary(summary: LevelSummary) -> None:
    print(
        f"concurrency={summary.concurrency:<4} "
        f"ok={summary.requests_ok:<6} err={summary.requests_error:<4} "
        f"throughput={summary.throughput_rps:7.2f} req/s "
        f"tokens/s={summary.tokens_per_sec:8.1f} "
        f"latency(mean/p50/p95/p99)="
        f"{fmt_s(summary.latency_mean_s)}/{fmt_s(summary.latency_p50_s)}/"
        f"{fmt_s(summary.latency_p95_s)}/{fmt_s(summary.latency_p99_s)}"
    )
    if summary.errors_sample:
        print(f"  sample errors: {summary.errors_sample}")


async def main_async(args: argparse.Namespace) -> list[LevelSummary]:
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompts = [line.strip() for line in f if line.strip()]
        if not prompts:
            print(f"error: {args.prompt_file} contained no non-empty lines", file=sys.stderr)
            sys.exit(1)
    else:
        prompts = DEFAULT_PROMPTS

    levels = args.ramp if args.ramp else [args.concurrency]
    summaries: list[LevelSummary] = []
    for i, concurrency in enumerate(levels):
        print(f"\n=== level {i + 1}/{len(levels)}: concurrency={concurrency}, duration={args.duration}s ===")
        summary = await run_level(
            base_url=args.base_url,
            model=args.model,
            prompts=prompts,
            max_tokens=args.max_tokens,
            concurrency=concurrency,
            duration=args.duration,
            request_timeout=args.request_timeout,
        )
        print_summary(summary)
        summaries.append(summary)
    return summaries


def parse_ramp(value: str) -> list[int]:
    try:
        levels = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --ramp value: {value}") from exc
    if not levels or any(n <= 0 for n in levels):
        raise argparse.ArgumentTypeError("--ramp must be a comma-separated list of positive integers")
    return levels


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000", help="vLLM server base URL")
    parser.add_argument("--model", required=True, help="model name as passed to --model on the vLLM server")
    parser.add_argument("--max-tokens", type=int, default=64, help="max_tokens per completion request")
    parser.add_argument("--concurrency", type=int, default=1, help="fixed concurrent request count (ignored if --ramp is set)")
    parser.add_argument("--ramp", type=parse_ramp, default=None, help="comma-separated concurrency levels to step through, e.g. 1,2,4,8,16,32")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds to sustain load per level")
    parser.add_argument("--request-timeout", type=float, default=120.0, help="per-request timeout in seconds")
    parser.add_argument("--prompt-file", default=None, help="optional file with one prompt per line")
    parser.add_argument("--output", default=None, help="optional path to write results as JSON")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summaries = asyncio.run(main_async(args))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump([s.__dict__ for s in summaries], f, indent=2)
        print(f"\nWrote results to {args.output}")


if __name__ == "__main__":
    main()
