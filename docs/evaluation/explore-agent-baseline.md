# Explore Agent V1 baseline

## Dataset

`evals/cases/explore-agent/suite.v1.json` freezes six structured Chinese-city requests across Beijing, Shanghai and Chengdu. The cases include confirmed must-visit attractions, open-ended discovery with no must-visit item, family preferences, attraction plus dining requests and one deliberately irrelevant decoy per case.

Every candidate is materialized as `DataMode.FIXTURE` with a stable provider ID, retrieval timestamp and raw-spec SHA-256. The scenario provider returns the same three-item case catalog for each search so the evaluation isolates query strategy, grounded selection and evidence handling. It does not measure AMap keyword recall.

## Checks and metrics

Each case checks protocol success, request/context identity, required query-kind coverage, required context references, provider call count, candidate grounding, source traceability, labelled relevance and required recommendation-group coverage.

The committed `deepseek-v4-pro` point-in-time report from 2026-08-21 records:

| Metric | Result |
|---|---:|
| Cases | 6/6 passed |
| Model calls | 12 |
| Provider calls | 24 |
| Required query kinds | 9/9 covered |
| Grounded recommendations | 12/12 |
| Source-traceable recommendations | 12/12 |
| Labelled-relevant recommendations | 12/12 |
| Required recommendation groups | 9/9 covered |
| Token usage | 17,679 |
| Per-case model latency p50 / p95 | 7,408 / 7,742 ms |

A non-committed exploratory run on this same development suite selected two irrelevant theme-park decoys and passed 4/6 cases. The selection prompt was then tightened without changing labels, producing the committed 6/6 regression result. Therefore this suite is a prompt-development regression set, not an untouched holdout and not evidence of generalization.

## Claim boundary

The report supports the claim that this version obeyed its candidate/source boundary and matched the six labelled development cases in one recorded run. It does not support "100% recommendation accuracy", production SLA, real-time AMap quality or user satisfaction.

The next useful comparison is not another prompt score. EZ-204 should compare the existing single-Planner path with Explore/Stay/Weather fan-out on frozen disruption and partial-failure cases, including latency, token cost, recovery and final constraint violations.
