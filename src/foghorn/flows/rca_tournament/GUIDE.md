---
graph_id: rca_tournament
title: RCA Tournament
summary: Competing root-cause hypotheses (component + category) are scored in parallel against the evidence and ranked; runs standalone from a scenario id and as the incident commander's subgraph.
features:
  - Send map-reduce (one worker per hypothesis) with a per-Send timeout
  - custom reducer collecting scores (merge by id), defer=True reduce node
  - hypothesis.update custom events with for/against evidence
  - compiled subgraph reuse (xray inside incident_commander)
studio_steps:
  - Run S10 below. The trace shows gather -> plan -> N parallel score_hypothesis tasks -> rank (deferred, waits for all) -> report.
  - Open the output: the winning hypothesis lists its for/against evidence; the innocent recommendation deploy is in red_herrings.
  - Switch the assistant context model_profile to naive: it blames the nearest change instead of weighing the evidence.
  - Pass evidence and candidates yourself (RcaInput) to score a hand-made situation; a component named in candidates wins ties.
sample_inputs:
  - name: S10 two changes, one culprit
    input: {scenario_id: S10}
  - name: S05 connection pool exhausted (red herring deploy)
    input: {scenario_id: S05}
  - name: S12 alert storm (zone partition)
    input: {scenario_id: S12}
  - name: S13 telemetry gap (name the blind component)
    input: {scenario_id: S13, candidates: [payments-service]}
output_schema: "RcaOutput: result (GraphResult), hypotheses[] ({id, component, category, score, evidence, label, for, against, rank, delta}), red_herrings[] ({ref, component, why})"
nodes: [gather, plan, score_hypothesis, rank, report]
---

## How a hypothesis is scored

`foghorn.flows.rca_tournament.scoring` holds plain, testable rules. For a (component, category) pair the score is the weight of what supports it minus what contradicts it, every term carrying a sentence and the evidence ref behind it:

* the component's own anomaly, whether it is the earliest on its dependency chain (onset ordering along the call graph), and how many of the anomalous components it explains (itself and its callers);
* a change of the right kind within minutes before the onset (deploy / flag / config / migration / cron job / certificate rotation / maintenance window);
* a log signature family that fits the category (NullPointerException, certificate has expired, No space left on device, remaining connection slots, deserialization failed, ...);
* the metric shape of the category (hit ratio collapse, consumer lag, memory climbing slowly, many zone-b services failing together, flat telemetry);
* against: its own metrics are normal, it is explained by an anomalous callee, its degradation already subsided, or a more specific signature points elsewhere.

`gather` (standalone only) investigates through the pure tool API with the commander's own planning functions; the commander passes its evidence in through the shared state keys instead.
