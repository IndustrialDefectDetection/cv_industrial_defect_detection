# Evaluation — is the agent's report actually right?

**Status: 1 of 3 planned runs complete, and re-scored after a bug was found in
the scorer.** Producing a confident-sounding report is easy. This document is
about whether the report is *true* — and, as it turned out, about whether the
thing measuring the report is true.

## Ground truth

The synthetic data is sabotaged on purpose.
`sqlite-synthetic-mes-data.py` injects quality incidents and writes each one's
name into `QualityControl.Comments`:

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
The agent is never shown that comment column in a form that names the incident.

Ground truth per defect type joins `Defects → QualityControl → WorkOrders →
Machines` and filters on the incident comment. For `Sensor Malfunction`:
**129 of 434** defects flagged, concentrated on `Machine Mot-50` (35),
`Machine Mot-51` (32) and `Machine Bat-40` (23).

## The scorer was wrong before the agent was

The first write-up of this run reported, as its headline finding, that the agent
**named no machine at all**. That was false, and the mistake was mine.

`Machines.Name` stores `Machine Mot-50`. The scorer asked:

```python
named = [m for m in truth["top_machines"] if m.lower() in low]   # wrong
```

The report writes the machine as `Mot-50` — in a markdown table cell, and as
"Motor Assembly (Mot-51 and Mot-50 combined)". The literal string
`Machine Mot-50` appears in the report **zero** times; `Mot-50` appears **six**
times and `Mot-51` **seven**. Substring-matching the stored name scored a
correct answer as a total miss.

The fix matches the identifier, bounded so `Mot-5` cannot match `Mot-50`:

```python
def machine_mentioned(machine_name, report_lower):
    if machine_name.lower() in report_lower:
        return True
    identifier = machine_name.split()[-1].lower()          # 'mot-50'
    return re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])",
                     report_lower) is not None
```

Re-scoring the saved report costs nothing — the run was already paid for — so
`evaluate_agent.py --rescore` exists to apply new scoring to old reports
without buying them again. That flag is the actual lesson from this run.

## Result: Sensor Malfunction (corrected)

Run completed in **469 s**, producing a 23,052-character report.

| Metric | Result |
|---|---|
| Window | ✅ Cited **2026-06-27 to 2026-06-28**, inside the true window |
| Machine | ✅ Named **all three** of the top-3 incident machines — `Mot-50`, `Mot-51`, `Bat-40` |
| Cause | ⚠️ Named "Supplier Quality", MEDIUM certainty — **but the metric is confounded, see below** |
| Localised the incident | ✅ cause **and** machines **and** an in-window date |

One honest qualification on the machine metric: the agent ranked `Mot-51`
(114 records) above `Mot-50` (112), while the incident-flagged ground truth
ranks `Mot-50` first. That is not an error — the agent counted *all* Sensor
Malfunction defects, since it cannot see which were injected. Both top machines
are correct; only the tie-break differs. `top_machine_named` in the JSON means
"the true top machine is named somewhere", not "ranked first".

> **HYPOTHESIS 1: Supplier Quality (62 records, 73 units affected)** —
> Certainty: MEDIUM. […] The peak defect window (2026-06-27 to 2026-06-28)
> coincides with a potential supplier batch issue, as no maintenance or process
> disruptions were detected during this period.

Locating the window is a genuine result: dates carry no incident label, so the
spike had to be found from defect counts over time. The agent also ruled out
maintenance and downtime explicitly, which is the kind of negative finding that
makes a report useful.

## The confound this run exposed

**`Defects.RootCause` already contains the string `Supplier Quality`** — the
single most common value in the column:

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
without being identical to it, which is what makes the metric misleading rather
than merely useless.

**The replacement metric is `localised_the_incident`**: the report must name the
supplier cause **and** at least one genuinely affected machine **and** a date
inside the injected window. Parroting a column value cannot satisfy all three;
only querying the data can. The run passes it.

## A separate, cleaner failure worth keeping

From the camera-burst investigation (`AgentAlerts` #1), unrelated to the
injected incidents: the agent ranked a **"Vision System Processing Anomaly"** as
its HIGH-certainty hypothesis, reasoning that the first image took 1,200 ms
against a 41–45 ms baseline — a 25× spike at the exact onset of the burst.

The observation is real and sharply argued. The conclusion is wrong: that was
**model warm-up on the first inference request**, an artifact of how the demo
was started, not a fact about the production line.

This remains the honest headline of the evaluation. The failure mode is not
hallucination — every number cited was true. It is a confident causal story
built on a correlation with a mundane technical explanation the agent had no way
to know about. The fix is not a better prompt but better evidence: the agent
should be told which latency samples are cold-start.

## What is left

1. Run the remaining defect types (`Battery Cell Variance`, `Motor Coil
   Problem`) and report all three against `localised_the_incident`.
2. Feed the agent a cold-start marker so warm-up latency stops reading as a
   process anomaly.

## Reproducing

```bash
# Score reports already collected - free.
python docs/evaluate_agent.py --rescore docs/evaluation-run1.json

# Run the agent for real - one multi-agent run per defect type, ~470 s each.
python docs/evaluate_agent.py
```

Connection settings come from `MES_PG_*`, like every other service. Results are
written incrementally, so a stopped run keeps what it has. Raw output of the
completed run is in `docs/evaluation-run1.json`, including the full report text.
