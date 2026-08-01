# Evaluation — is the agent's report actually right?

**Status: in progress. 1 of 3 planned runs complete.** The first run already
invalidated one of the three metrics, so the design below needs a change before
the remaining runs are worth paying for. Written up now so the reasoning is not
lost.

Producing a confident-sounding report is easy. This document is about whether
the report is *true*.

## Ground truth

The synthetic data is sabotaged on purpose. `sqlite-synthetic-mes-data.py`
injects quality incidents and writes each one's name into
`QualityControl.Comments`:

| Injected incident | QC records |
|---|---|
| Component supplier quality issue — bad batch from supplier | 993 |
| Calibration drift | 26 |
| Process change adaptation | 19 |
| New material batch variation | 26 |
| **Total flagged** | **1,064 of 5,017** |

The supplier incident is concentrated in a window 7–14 days before the data
anchor, giving a visible spike. So the database records which runs were
sabotaged, by what, and when — a real answer key rather than a judgement call.

Ground truth per defect type is derived by joining
`Defects → QualityControl → WorkOrders → Machines` and filtering on the
incident comment. For `Sensor Malfunction`: **129 of 434** defects flagged,
concentrated on `Machine Mot-50` (35), `Machine Mot-51` (32) and
`Machine Bat-40` (23).

## Method

`docs/evaluate_agent.py` derives the ground truth, runs
`run_defect_analysis(defect_type, days_back=30)` for real, and scores the
report on three things:

1. **Machine** — does it name the machines that actually carry the incident?
2. **Window** — does it cite dates inside the true incident window?
3. **Cause** — does it identify the supplier/material cause?

## Result: Sensor Malfunction

Run completed in **469 s**, producing a 23,052-character report.

| Metric | Result |
|---|---|
| Window | ✅ Cited **2026-06-27 to 2026-06-28**, inside the true window |
| Cause | ⚠️ Named "Supplier Quality" as the dominant cause, MEDIUM certainty — **but see the confound below** |
| Machine | ❌ **Named no machine at all.** `Mot-50` and `Mot-51` never appear |

> **HYPOTHESIS 1: Supplier Quality (62 records, 73 units affected)** —
> Certainty: MEDIUM. […] The peak defect window (2026-06-27 to 2026-06-28)
> coincides with a potential supplier batch issue, as no maintenance or process
> disruptions were detected during this period.

Locating the window is a genuine result: dates carry no incident label, so it
had to find the spike from defect counts over time. It also correctly ruled out
maintenance and downtime as explanations, which is the kind of negative finding
that makes a report useful.

Not naming a machine is a real miss. The report reasons at the aggregate level
throughout, and "which machine do I go and look at" is the first question an
operator would ask.

## The confound this run exposed

**`Defects.RootCause` already contains the string `Supplier Quality`** — and it
is the single most common value in the column:

| RootCause | rows |
|---|---|
| **Supplier Quality** | **939** |
| Design Issue | 878 |
| Process Variation | 870 |
| Operator Error | 825 |
| Machine Calibration | 730 |

The agent can read that column directly. So "did it say supplier?" does not
measure inference — an agent that blindly reported the modal `RootCause` would
score correct on every defect type. Of the 939 supplier-attributed defects, 602
are also incident-flagged, so the column correlates with the injected incident
without being identical to it, which is precisely what makes the metric
misleading rather than merely useless.

The cause metric as written is therefore **not evidence of root-cause
reasoning**, and the score above should not be quoted as if it were.

## What to change before the remaining runs

1. **Drop or rewrite the cause metric.** Either exclude `RootCause` from the
   tools the agent may read for evaluation runs, or score only on findings not
   present verbatim in any column.
2. **Keep the window metric** — it is uncontaminated and the agent passed it.
3. **Keep the machine metric** — also uncontaminated, and the agent failed it.
   Worth testing whether asking the question more specifically fixes it, since
   this may be a prompt problem rather than a reasoning one.
4. Then run the remaining defect types (`Battery Cell Variance`,
   `Motor Coil Problem`) and report all three.

## A separate, cleaner failure worth keeping

From the camera-burst investigation (`AgentAlerts` #1), unrelated to the
injected incidents: the agent ranked a **"Vision System Processing Anomaly"** as
its HIGH-certainty hypothesis, reasoning that the first image took 1,200 ms
against a 41–45 ms baseline — a 25× spike at the exact onset of the burst.

The observation is real and sharply argued. The conclusion is wrong: that was
**model warm-up on the first inference request**, an artifact of how the demo
was started, not a fact about the production line.

This is the honest headline of the evaluation so far. The failure mode is not
hallucination — every number cited was true. It is a confident causal story
built on a correlation with a mundane technical explanation the agent had no
way to know about.

## Reproducing

```bash
cd sample-MES-ClosedLoop-Strands-Agent
.venv/Scripts/python.exe ../docs/evaluate_agent.py
```

Costs one real multi-agent run per defect type (~470 s each) and writes results
incrementally, so a stopped run keeps what it has. Raw output of the completed
run is in `docs/evaluation-run1.json`, including the full report text.
