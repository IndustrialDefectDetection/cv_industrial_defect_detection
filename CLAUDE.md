# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A two-person portfolio build: an end-to-end **CV → MES agentic defect detection pipeline**. A YOLO steel-defect camera feed is bridged into a synthetic Manufacturing Execution System, where a Claude agent investigates defect bursts and writes root-cause reports that surface on a live dashboard.

Target pipeline: **inference API (port 8080) → bridge with confidence gate + 30s batching (port 8081) → `analyze_batch()` agent runner → `AgentAlerts` table → dashboard (port 8501)**.

**`CONTRACTS.md` at the repo root is the binding interface document** — ports, the detection payload, the `VisionDetections`/`AgentAlerts` schemas, and the `analyze_batch(batch) -> int` seam between the bridge and the agent. Read it before touching anything that crosses a sub-project boundary; changing it requires updating the file in the same commit.

The repo is a monorepo of three upstream projects (converted from submodules to plain directories) plus a new frontend. Each has its own toolchain — there is no shared build.

## Sub-projects

### steel-defect-detection-mlops — the camera side
YOLOv8n detecting 6 NEU-DET defect classes (`crazing`, `inclusion`, `patches`, `pitted_surface`, `rolled-in_scale`, `scratches`). Plain pip project.

```bash
pip install -r requirements.txt
uvicorn deployment.api:app --port 8080     # inference API; docs at /docs
streamlit run streamlit_app.py             # standalone upload-and-detect UI
dvc repro                                  # preprocess (xml_to_yolo) + train pipeline
mlflow ui --port 5000
```

- **Model weights are not in git.** `deployment/api.py` expects `runs/detect/steel_defect_colab_50_epochs/weights/best.pt` (override with `MODEL_PATH` env var); training happens on Colab via `notebooks/steel_defect_colab_50_epochs.ipynb`-style notebooks. Without the file, `/predict` 503s.
- Test images: `data/dataset/images/test/`.

### industrial-data-store-simulation-chatbot — the MES side (bridge, agent, and dashboard land here)
Synthetic e-bike factory MES in SQLite + Streamlit apps + Strands SDK agents. Uses **uv**, driven by the Makefile:

```bash
make setup            # uv sync + generate mes.db
make setup-db         # (re)generate mes.db with synthetic data (90d back / 14d ahead)
make test             # uv run pytest tests/
uv run pytest tests/test_integration_agent_tools.py -k <name>   # single test
make start-chat       # MES chatbot UI (Streamlit)
make start-dashboard  # production meeting dashboard (Streamlit, port 8501)
```

Architecture facts that take multiple files to learn:
- `app_factory/shared/database.py` `DatabaseManager` resolves `mes.db` **relative to the working directory** — always run bridge/agent/dashboard from this repo's root.
- `mes.db` is **wiped and regenerated** by `app_factory/data_generator/sqlite-synthetic-mes-data.py`. Any table you add (e.g. `VisionDetections`, `AgentAlerts`) must be created with `CREATE TABLE IF NOT EXISTS` at service startup, and IDs must be looked up live, never hardcoded. That generator file is also the single source of truth for the MES schema (`Machines`, `WorkOrders`, `QualityControl`, `Defects`, `Downtimes`, `OEEMetrics`, ...); there is no separate schema file.
- The agent pattern to copy is in `app_factory/mes_agents/`: a persistent `strands.Agent(system_prompt, tools=[...], model)` plus `@tool`-decorated functions (`tools/database_tools.py`) whose **docstrings are the tool's entire interface to the model**, returning `{'success': ..., ...}` dicts instead of raising.
- `run_sqlite_query` is deliberately **read-only** (blocks INSERT/UPDATE/DELETE/DDL). Agent-side writes (e.g. to `AgentAlerts`) must be plain Python DB calls, not agent tools.
- The data generator secretly injects quality incidents (machine + defect category) — these are the ground truth for evaluating agent reports.

### sample-MES-ClosedLoop-Strands-Agent — reference multi-agent implementation
Upstream AWS sample (Supervisor → Monitor/Analyzer/Planner/Verifier/Executor agents over MES data), **modified to run on the direct Anthropic API** instead of Bedrock (`strands.models.anthropic.AnthropicModel`; `strands-agents[anthropic]`).

```bash
python startup.py     # bootstraps .venv, installs requirements, creates/validates .env, launches
```

- `startup.py` prompts for and validates `ANTHROPIC_API_KEY` against the Anthropic API; `.env` also carries `MES_MODEL_ID`, `MES_MAX_TOKENS`, `MES_TEMPERATURE`.
- Has its own `mes.db` copy (distinct from the chatbot repo's) and an in-progress SQLite→PostgreSQL migration (`setupdatabase.py`, needs psycopg2 + pgloader, DB name `mescopy_v1`).
- Entry UI is `app.py` (Streamlit); agents live in `strands_agent.py`.

### frontend — Next.js UI (early scaffold)
Next.js 16.2.10 / React 19 / TypeScript / Tailwind v4, App Router, only the default page so far. Planned: chatbot-style defect intake, dashboards, root-cause report views.

```bash
npm run dev / build / lint
```

**Read `frontend/AGENTS.md` before writing any frontend code.** Its key warning: this Next.js version has breaking changes relative to training data — consult `node_modules/next/dist/docs/` for current APIs; prefer small incremental edits.

## Task plan and current status (as of 2026-07-19 — verify before trusting; update markers as tasks move)

Two-person build: **A** = agentic side (agent runner, tools, prompts, dashboard tab, evaluation, README) · **B** = DB + plumbing (model training, tables, lookup helpers, simulator, bridge, startup script). Interfaces are pinned in CONTRACTS.md.

Execution order, with status (✅ done · 🟡 partial · ⬜ open):

1. 🟡 **Train YOLO on Colab (B)** — training ran (50/100-epoch artifacts committed in `runs/detect/`) but `best.pt` was never copied to this machine, so `/predict` still 503s. *This blocks everything.*
2. ⬜ Verify API with one test image (B) — first checkpoint: a curl returns real detections.
3. 🟡 CONTRACTS.md — drafted by A, awaiting B's approval.
4. ⬜ Create `VisionDetections` + `AgentAlerts` tables (B) — note `mes.db` doesn't exist in the chatbot repo yet; run `make setup-db` first.
5. ⬜ Lookup helpers `get_frame_machines()` / `get_active_work_order()` (B).
6. ✅ Read existing agent code (A) — findings are the architecture notes above.
7. ⬜ Fake camera / replay script with burst mode (B).
8. ⬜ Bridge: save all detections, gate ≥ 0.80, 30s batching (B).
9. ⬜ Wire the seam with a stub `analyze_batch()` (both) — checkpoint: printed batch end-to-end, zero LLM cost.
10. ⬜ Real `analyze_batch()` agent runner (A).
11. ⬜ A's agent tool, e.g. `get_recent_detections(machine_id, hours)` (A).
12. ⬜ Prompt iteration loop (A) — checkpoint: a burst produces a root-cause report in the DB.
13. ⬜ Guardrails: one run at a time, hourly cap, failures → `failed` (A).
14. ⬜ Live dashboard tab (A) — **open decision:** plan says Streamlit tab, but a Next.js frontend was started; pick deliberately.
15. ⬜ Evaluation vs. the generator's injected incidents → `docs/evaluation.md` (A).
16. ⬜ One-command startup script (B).
17. ⬜ README + demo GIF + latency numbers (A).

**Explicitly cut — don't let these creep back in:** Postgres migration (use WAL mode if SQLite locks), Kafka/queues, WebSockets, Grafana polish, steelMLOps CI fixes. *Drift note: recent commits added a SQLite→Postgres migration (`mescopy`/pgloader) in sample-MES — that's on the cut list; park until after task 17.*

## Cross-cutting gotchas

- Three Python toolchains coexist: uv (chatbot), plain venv via `startup.py` (sample-MES), pip requirements (mlops). Don't mix them.
- Two Claude access paths coexist: Bedrock/boto3 (chatbot repo, original code) and direct Anthropic API via `.env` (sample-MES, the direction the project is moving).
- Agent runs cost real money and take ~60s. The design guards against spam by contract: confidence gate ≥ 0.80, 30-second batching, one run at a time, max-runs-per-hour cap, failures mark alerts `failed` rather than crashing (see CONTRACTS.md §5–6).
- If SQLite write-locks appear with multiple services on `mes.db`, enable WAL mode (`PRAGMA journal_mode=WAL`) — Postgres migration is explicitly out of scope for the pipeline.
