# README Evidence Notes (rough — keep updating while building)

Working file. Add to it as you build; the polished README gets assembled from this later.
Items marked ⚠️ TODO are things not yet captured anywhere — grab them before they're forgotten.

---

## 1. What I personally changed

- Rerouted 8 predefined SQL queries that joined on a nonexistent `WorkOrders.ShiftID` column — verified the real schema with PRAGMA, joined through `WorkOrders → Employees (EmployeeID) → Shifts (e.ShiftID)` instead.
- Added time-overlap conditions (`dt.StartTime BETWEEN wo.ActualStartTime AND wo.ActualEndTime`) to the downtime queries to kill a join fan-out.
- Rewrote the Run-button flow in `app.py`: `analysis_running` flag + one-shot `work_pending` flag so only one entry point can start an analysis (Strands agents are not re-entrant).
- Wrote a markdown → ReportLab renderer for the PDF generator (was printing raw markdown), with proper escaping.
- Built the `fetch_defect_records` tool (Monitor previously had no sanctioned path to the Defects table).
- Wrote `OUTPUT_RULES` / `SUPERVISOR_OUTPUT_RULES` constants appended to every agent system prompt: word caps, fixed report sections, HIGH/MEDIUM/LOW certainty (no invented percentages), every finding must cite its tool/data source, no unsupported benchmark comparisons.
- Added client timeouts, automatic retries, and `_call_agent_with_retry` (1 def + 5 wrappers) with graceful degradation.
- DRY refactor: run-capture logic consolidated into `_save_agent_output` (call sites went 6 → 2: def + one call in the helper).
- Built full run-artifact capture: every analysis writes a `runs/` folder with params, full logs, and each agent's prompts, outputs, and complete tool-call transcripts.
- Moved config to `.env` via `os.getenv` (model switchable through `MES_MODEL_ID`; max tokens, temperature, log level, email settings).
- Deployed to Streamlit Community Cloud with private viewer access for review.
- ⚠️ TODO as you build the demo features: trace panel, cost meter, chaos toggle, provenance click-through — log each here the day you land it.

## 2. Important bugs I solved (root cause + fix + symptom — README gold)

1. **Schema drift.** 8 queries referenced `WorkOrders.ShiftID`, which doesn't exist in the generated DB. Symptom: silent query failures / blocked agents. Fix: PRAGMA-verified schema, rerouted joins. Lesson: trust PRAGMA over the repo's intuition.
2. **Join fan-out.** Downtime joins had no time condition → event counts inflated ~20x. Fix: time-overlap BETWEEN conditions. Juiciest single number in the project.
3. **Streamlit race.** Re-render auto-trigger fired duplicate concurrent agent invocations. Fix: single Run-button path with run/pending flags.
4. **Hallucinated table names.** Monitor had no legitimate tool for the Defects table, so it invented table names when blocked. Fix: build the tool, not more guardrails. Best story in the repo.
5. **Fabricated statistics + token-limit retry loops.** Prompts effectively instructed fabrication ("provide confidence levels"). Fix: OUTPUT_RULES. This is what took runs from ~3 h to <10 min.
6. **Hung connection mid-run.** Fix: timeouts + retry + graceful degradation.
7. **Raw-markdown PDFs.** Fix: markdown→ReportLab renderer.

## 3. Architecture decisions and why

- **Hub-and-spoke topology** (supervisor as hub), not pipeline (supervisor must skip/reorder agents based on user scope) and not peer-to-peer (traceability + single escalation point beat agent autonomy here).
- **Categorical certainty (HIGH/MED/LOW) over numeric confidence** — numbers the model can't ground are fabrication by instruction.
- **Tool coverage over guardrails** — an agent with no legitimate path to data it needs will hallucinate one; give it the tool.
- **`mes.db` is read-only, always** — fresh data comes from rerunning the generator, never from writes.
- **Parameterized SQL house style** — `?` placeholders only, `days_back` validated 0–3650, named columns (no `SELECT *`), `ORDER BY`, `LIMIT` cap.
- **Single entry point for runs** — because Strands agents aren't re-entrant.
- **Human-approval gate before the Executor** — Executor stays simulated/dry-run.
- **Run artifacts for every analysis** — debuggability and auditability as a default, not an afterthought.
- **Planned: read-only MCP server for DB access; JSON-schema handoffs; mini-RAG over SOPs** (from the v2 PRD).
- ⚠️ TODO: when you make a decision *against* something (e.g., rejecting checkpoint/resume until the schema phase), note it here — "deliberately not built" sections read very well.

## 4. Before-and-after measurements

- End-to-end run: **~3 hours (with failures/retry loops) → under 10 minutes**.
- Downtime event counts: **~20x inflated → correct** after fan-out fix.
- Fabricated stats in reports: present → eliminated (findings must cite source).
- Capture-logic call sites: 6 → 2 after DRY refactor (grep-verified).
- ⚠️ TODO: tokens + $ cost per run, before vs. after the output-rules fix — capture from run artifacts before memory fades; this is the cost-story number.
- ⚠️ TODO: one concrete before/after report excerpt (invented percentages vs. cited HIGH/MED/LOW finding). Save the actual text now.

## 5. Commands needed to run the project

```bash
# from project root, Git Bash on Windows
source .venv/Scripts/activate        # note: Scripts, not bin, on Windows
python -m streamlit run app.py       # app at http://localhost:8501
```

- Config read from `.env` at import time (`MES_MODEL_ID` etc.).
- Fresh data: rerun the generator in the nested `industrial-data-store-simulation-chatbot` repo (`app_factory/data_generator/sqlite-synthetic-mes-data.py`) and copy `mes.db` to project root.
- Refactor verification greps:
```bash
grep -c "MES_API_TIMEOUT" strands_agent.py         # want 1
grep -c "_call_agent_with_retry" strands_agent.py  # want 6
grep -c "_save_agent_output" strands_agent.py      # want 2
```
- ⚠️ TODO: exact deploy steps you actually used for Community Cloud (repo URL, secrets set, sharing settings) — write them down while fresh.

## 6. Screenshots of good demo runs (⚠️ all TODO — capture on your next clean run)

- [ ] Streamlit UI at defect selection
- [ ] A run in progress (agent status / trace panel once built)
- [ ] Final PDF report — a page with cited findings
- [ ] Before/after report comparison (pull "before" from an old runs/ folder if one survives!)
- [ ] A `runs/` folder tree + one tool-call transcript open
- [ ] Deployed Community Cloud app in a browser
- [ ] (later) MCP server tool list; approval-gate card; cost meter

Tip: screenshot the *old, broken* behavior too if any old run artifacts still show it — before-pictures are the rarest asset and the first thing people delete.

## 7. Known limitations and lessons learned

Limitations:
- All data synthetic; no factory hardware, no production MES writes, Executor simulated.
- No auth beyond Community Cloud viewer allow-list; API cost per run is real (private sharing on purpose).
- Free-text inter-agent handoffs until the schema phase lands; no checkpoint/resume yet (deliberate — parked for the MCP/schema phase).
- Strands agents non-re-entrant → single-run-at-a-time by design.

Lessons (the README's personality lives here):
- Hallucination is often a coverage problem, not a model problem.
- A prompt asking for confidence percentages is an instruction to fabricate.
- Trust PRAGMA over intuition — verify the schema the queries assume.
- Reliability work is invisible when it works; artifacts are how you prove it happened.
- Fix data bugs in code (SQL conditions), fix reasoning bugs in prompts — know which kind you have.

---

## Juiciest README hooks (the shortlist)

1. "~3 hours → under 10 minutes" — the headline metric.
2. The 20x downtime inflation caught by one missing time condition.
3. The agent that invented table names because nobody gave it a real tool.
4. "Provide confidence levels" = instructed fabrication → HIGH/MED/LOW + citations.
5. Every claim in every report traces back to a logged tool call.
