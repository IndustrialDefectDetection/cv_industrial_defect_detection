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
python -m venv .venv && .venv/Scripts/activate        # own venv; do not reuse the agent's
pip install ultralytics fastapi uvicorn python-multipart prometheus-client
uvicorn deployment.api:app --port 8080     # inference API; docs at /docs
streamlit run streamlit_app.py             # standalone upload-and-detect UI
dvc repro                                  # preprocess (xml_to_yolo) + train pipeline
mlflow ui --port 5000
```

- The five packages above are all `deployment/api.py` imports — enough to serve `/predict`. `requirements.txt` additionally pulls jupyter, mlflow, tensorboard, black, mypy and pre-commit; install it only when you need training or experiment tracking.
- **Model weights are not in git**, but `best.pt` **is present on this machine** (6.3 MB). `deployment/api.py` expects `runs/detect/steel_defect_colab_50_epochs/weights/best.pt` (override with `MODEL_PATH`); training happens on Colab via `notebooks/steel_defect_colab_50_epochs.ipynb`-style notebooks. Without the file, `/predict` 503s — so a fresh clone still needs it copied across.
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
- **`trace_viewer.py` (Streamlit, port 8502) is the project's under-the-hood dashboard** — live per-agent timeline, tool calls, SQL, run stats, session Q&A history. Contract: `TRACE_API.md`. Launched by root `Launch.py` alongside `api.py` (port 8000) + the Next.js frontend, or standalone: `streamlit run trace_viewer.py --server.port 8502`.
- Has its own `mes.db` copy (distinct from the chatbot repo's) and an in-progress SQLite→PostgreSQL migration (`setupdatabase.py`, needs psycopg2 + pgloader, DB name `mescopy_v1`).
- Entry UI is `app.py` (Streamlit); agents live in `strands_agent.py`.

### frontend — Next.js UI (early scaffold)
Next.js 16.2.10 / React 19 / TypeScript / Tailwind v4, App Router, only the default page so far. Planned: chatbot-style defect intake, dashboards, root-cause report views.

```bash
npm run dev / build / lint
```

**Read `frontend/AGENTS.md` before writing any frontend code.** Its key warning: this Next.js version has breaking changes relative to training data — consult `node_modules/next/dist/docs/` for current APIs; prefer small incremental edits.

## Task plan and current status (as of 2026-08-01 — verify before trusting; update markers as tasks move)

Two-person build: **A** = agentic side (agent runner, tools, prompts, dashboard tab, evaluation, README) · **B** = DB + plumbing (model training, tables, lookup helpers, simulator, bridge, startup script). Interfaces are pinned in CONTRACTS.md.

Execution order, with status (✅ done · 🟡 partial · ⬜ open):

1. ✅ **Train YOLO on Colab (B)** — `best.pt` (6.3 MB) is now at `runs/detect/steel_defect_colab_50_epochs/weights/best.pt`. No longer blocks anything.
2. ✅ Verify API with one test image (B) — `POST /predict` returns `pitted_surface` at 0.92 confidence with a well-formed bbox, and the payload matches the fields `simulator.py` reads. Measured across one image per class: 5 of 6 classes detected, ~50 ms inference. **Crazing detected nothing** — it is the subtle-texture class and the weakest of the six; don't build a demo around it. Only 4 of 14 detections cleared the 0.80 pipeline gate, so use `patches`, `pitted_surface` or `scratches` images to trigger a burst reliably.
3. 🟡 CONTRACTS.md — drafted by A, awaiting B's approval.
4. ✅ Create `VisionDetections` + `AgentAlerts` tables (B) — both live in Postgres `mescopy_v1`, created idempotently at bridge startup.
5. ✅ Lookup helpers `get_frame_machines()` / `get_active_work_order()` (B) — `bridge/mes_lookups.py`; resolved OrderID 4901 in the live run.
6. ✅ Read existing agent code (A) — findings are the architecture notes above.
7. ✅ Fake camera / replay script with burst mode (B) — `bridge/simulator.py`; `--interval 0.5` over a folder of images is the burst.
8. ✅ Bridge: save all detections, gate ≥ 0.80, 30s batching (B) — verified live: 24 detections saved, 6 cleared the gate, one batch.
9. ✅ Wire the seam with a stub `analyze_batch()` (both) — `MES_ANALYZE_STUB=1` still runs the whole lifecycle for free.
10. ✅ Real `analyze_batch()` agent runner (A) — posts to `/investigate`; alert goes `pending → analyzing → done`.
11. ✅ A's agent tool `get_recent_detections(machine_id, hours)` (A) — registered on Monitor and Analyzer.
12. 🟡 Prompt iteration loop (A) — **checkpoint met**: a burst produced a 4,733-char root-cause report naming the machine, work order, product, operator and shift correctly. Still worth iterating: the report ranked "vision system anomaly" HIGH certainty on the strength of a 1,200 ms inference time that was really just model warm-up on the first request.
13. ✅ Guardrails (A) — one run at a time, an hourly budget on every paid endpoint, and failures marked `failed` with a reason. See the spend note under cross-cutting gotchas.
14. ✅ Live alerts view (A) — decided in favour of the trace viewer (`trace_viewer.py`, port 8502), fed by `GET /alerts` so it stays a pure HTTP client. The Next.js frontend is Rithvik's chat UI, not the alerts surface.
15. 🟡 Evaluation vs. the generator's injected incidents → `docs/evaluation.md` (A) — 1 of 3 runs done, and **re-scored**. The scorer had a false-negative bug: it matched `Machines.Name` verbatim (`Machine Mot-50`) against reports that write `Mot-50`, and so recorded "named no machine" for a report that named all three top machines. Fixed via `machine_mentioned()`; two metrics flipped to true. The confounded cause metric is replaced by `localised_the_incident` (cause **and** machine **and** in-window date). `evaluate_agent.py --rescore FILE` re-scores paid-for runs offline, no DB and no credit. Remaining: `Battery Cell Variance` and `Motor Coil Problem`, ~470 s and real credit each.
16. ✅ One-command startup (B) — `python Launch.py` starts inference (8080), bridge (8081), agent API (8000), Next.js (3000) and the trace viewer (8502), refuses to start if a port is taken, and reads the root `.env`.
17. 🟡 README + demo GIF + latency numbers (A) — README cut 550 → 245 lines; the pasted-in upstream half (Bedrock, `mes.db` setup, a competing setup procedure, a roadmap listing the shipped trace panel as planned) is gone. Hero image is real model output: `docs/make_burst_figure.py` draws all 24 detections and marks the 6 clearing the gate, reproducible offline. **The demo GIF is still the one missing asset** — it needs all five services up plus a screen recording.

**Explicitly cut — don't let these creep back in:** Kafka/queues, WebSockets, Grafana polish, steelMLOps CI fixes.

*The Postgres migration was on this list and is now done and shipped.* It stopped being optional once the camera and the agent needed one database: the bridge wrote detections to Postgres while the agent read SQLite, so the agent reported `no such table: VisionDetections` while every service claimed to be healthy. `MES_DB_BACKEND=postgres` is the default and `mescopy_v1` is the shared database.

## Cross-cutting gotchas

- Three Python toolchains coexist: uv (chatbot), plain venv via `startup.py` (sample-MES), pip requirements (mlops). Don't mix them.
- Two Claude access paths coexist: Bedrock/boto3 (chatbot repo, original code) and direct Anthropic API via `.env` (sample-MES, the direction the project is moving).
- Agent runs cost real money and take ~40–260s. The design guards against spam by contract, and all of it is implemented: confidence gate ≥ 0.80 and 30-second batching in the bridge; one run at a time (`_run_guard`) and an hourly budget on *every* cost-bearing endpoint — `/investigate`, `/analysis` and `/chat/` all go through `_acquire_run_slot` → `_reserve_run_budget` (`MES_MAX_RUNS_PER_HOUR`, default 10, clamped 1–100 → HTTP 429 with `Retry-After`); one supervisor delegation per chat question (`MES_CHAT_SUPERVISOR_CALLS`); failures mark alerts `failed` with a reason rather than crashing (CONTRACTS.md §5–6).
- Everything reads and writes PostgreSQL `mescopy_v1`. If a service reports missing tables or no defects, check `MES_DB_BACKEND` before anything else — a service left on SQLite looks healthy and simply finds nothing.
- **Secure file output has two implementations, chosen by platform** — `sample-MES-.../report_paths.py` (PDF reports) and `industrial-data-store-simulation-chatbot/app_factory/shared/output_security.py` (cached analyses, scheduler logs). POSIX writes relative to an open directory descriptor with `O_NOFOLLOW` and `fchmod`; Windows validates by path and inherits the NTFS ACL, because it has none of those primitives. Both refuse symlinks, create exclusively, and publish atomically without overwriting. A platform that is neither fails closed. Tests assert exact 0600/0700 modes on POSIX only — don't "fix" that by asserting them everywhere.
- **Secrets reach the services through the root `.env`** (gitignored; copy `.env.example`). `Launch.py` reads it and hands each service only its allowlisted names. The frontend depends on this specifically: `frontend/scripts/run-next-secure.mjs` refuses to start on Windows if any `.env` file exists inside `frontend/`, so its `BETTER_AUTH_SECRET`/`DATABASE_URL` must arrive through the process environment. Real environment variables override the file.
