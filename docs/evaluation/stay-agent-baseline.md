# Stay Agent V1 baseline

## Dataset and routing

`evals/cases/stay-agent/suite.v1.json` freezes six structured Chinese-city requests:

- four executable accommodation-area cases across Beijing, Shanghai and Chengdu, covering history/low-walking, Bund/city-view intent, family/panda travel and senior/low-walking travel;
- one request whose lodging-inclusive budget is missing a room count;
- one request for a destination outside the EzTrip V1 city catalog.

The two blocked cases stop at deterministic capability routing and make zero model and zero Provider calls. Every executable case has three explicitly labelled fixture candidates: two potentially relevant candidates and one area/preference decoy. Every candidate is materialized as `DataMode.FIXTURE` with a stable provider ID, retrieval timestamp and raw-spec SHA-256.

The scenario Provider returns the same three-item catalog for every search in one case. This isolates query strategy, grounded selection and evidence handling; it does not measure AMap keyword recall or hotel data quality.

## Checks and point-in-time result

Executable cases check capability readiness, request/context identity, required context references, Provider call count, candidate grounding, source traceability, labelled relevance and required recommendation-group coverage. Commercial truth checks require:

- no recommendation to contain an unverified price field;
- every availability status to remain `unknown`;
- every booking capability to remain disabled.

The committed `deepseek-v4-pro` point-in-time report from 2026-08-21 records:

| Metric | Result |
|---|---:|
| Cases | 6/6 passed |
| Executable / blocked cases | 4 / 2 |
| Model calls | 8 |
| Fixture Provider calls | 12 |
| Required context references | 13/13 covered |
| Grounded recommendations | 8/8 |
| Source-traceable recommendations | 8/8 |
| Labelled-relevant recommendations | 8/8 |
| Required recommendation groups | 4/4 covered |
| Unverified price fields | 0 |
| Availability unknown / booking disabled | 8/8 / 8/8 |
| Token usage | 12,321 |
| Per-executable-case model latency p50 / p95 | 7,755 / 8,184 ms |

## Claim boundary

This is a development-set regression result, not an untouched holdout. It supports the narrow claim that this version obeyed its candidate/source/commercial-truth boundary and matched six labelled cases in one recorded run.

It does not support “100% hotel recommendation accuracy”, real-time prices, availability, booking, production latency, AMap recall, accommodation quality or user satisfaction. Candidate names and labels are clearly marked fixtures; the AMap adapter is verified separately with synthetic protocol tests. EZ-204 must test how Stay results interact with Explore and Weather under parallel fan-out, partial failures and state merge.
