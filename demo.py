"""Double-click demo launcher for the CV -> MES defect detection pipeline.

Everything here is also doable from a terminal, and the README still documents
that route. This exists so the project can be *shown* without one: no shell, no
remembered flags, no three windows to arrange, and no chance of typing the
wrong virtualenv path in front of an audience.

Deliberately stdlib-only. It runs on whatever Python is on PATH, which is not
one of the three project virtualenvs and does not have psycopg2, so database
reads shell out to a venv that does. Adding a dependency to the launcher would
mean installing something before you can launch anything, which defeats it.

    python demo.py          # or double-click Start Demo.bat
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

REPO = Path(__file__).resolve().parent
BACKEND_DIR = REPO / "sample-MES-ClosedLoop-Strands-Agent"
CHATBOT_DIR = REPO / "industrial-data-store-simulation-chatbot"
MLOPS_DIR = REPO / "steel-defect-detection-mlops"
WEIGHTS = MLOPS_DIR / "runs/detect/steel_defect_colab_50_epochs/weights/best.pt"
BURST_IMAGES = MLOPS_DIR / "data/demo_burst"

INFERENCE_PORT = 8080
BRIDGE_PORT = 8081
DASHBOARD_PORT = 8502

# Keep child consoles from flashing up; the whole point is to avoid terminals.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def load_launch_module():
    """Reuse Launch.py's env loading and per-service allowlists.

    Re-implementing them here would mean two copies of the rule about which
    service may see which secret, and they would drift.
    """
    spec = importlib.util.spec_from_file_location("root_launch", REPO / "Launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = load_launch_module()


def venv_python(project: Path) -> Path:
    name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return project / ".venv" / name


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def preflight(values: dict) -> list[tuple[str, bool, str]]:
    """Everything that must be true before a run, checked before starting one.

    Reported all at once rather than failing at the first problem, so a fresh
    setup can be fixed in a single pass instead of one restart per issue.
    """
    checks: list[tuple[str, bool, str]] = []

    env_present = (REPO / ".env").exists()
    checks.append((
        "Configuration (.env)",
        env_present,
        "found" if env_present else "missing - copy .env.example to .env",
    ))

    checks.append((
        "Trained model (best.pt)",
        WEIGHTS.exists(),
        "found" if WEIGHTS.exists() else f"missing at {WEIGHTS.relative_to(REPO)}",
    ))

    mlops_ready = venv_python(MLOPS_DIR).exists()
    checks.append((
        "Camera environment",
        mlops_ready,
        "ready" if mlops_ready else "no .venv in steel-defect-detection-mlops",
    ))

    backend_ready = venv_python(BACKEND_DIR).exists()
    checks.append((
        "Agent environment",
        backend_ready,
        "ready" if backend_ready else "no .venv in sample-MES-ClosedLoop-Strands-Agent",
    ))

    reachable, detail = database_status(values)
    checks.append(("Database (mescopy_v1)", reachable, detail))

    has_key = len(values.get("ANTHROPIC_API_KEY", "").strip()) > 10
    checks.append((
        "Anthropic API key",
        has_key,
        "set - full demo available" if has_key
        else "not set - free demo only (this is fine)",
    ))

    return checks


def database_status(values: dict) -> tuple[bool, str]:
    python = venv_python(BACKEND_DIR)
    if not python.exists():
        return False, "cannot check without the agent environment"
    rows = query_database(
        values,
        "select count(*) from defects",
        "select count(*) from agentalerts",
    )
    if rows is None:
        return False, "unreachable - check MES_PG_PASSWORD in .env"
    # rows is one result set per statement, each a list of rows, each a list
    # of column values - so a single count needs all three indices.
    defects, alerts = rows[0][0][0], rows[1][0][0]
    return True, f"connected - {defects} defects, {alerts} alert(s)"


def query_database(values: dict, *statements: str):
    """Run read-only SQL through a venv that has psycopg2, and return rows."""
    python = venv_python(BACKEND_DIR)
    if not python.exists():
        return None
    script = (
        "import json,os,sys,psycopg2\n"
        "c=psycopg2.connect(host=os.environ['PGH'],port=int(os.environ['PGP']),"
        "dbname=os.environ['PGD'],user=os.environ['PGU'],"
        "password=os.environ.get('PGW') or None,connect_timeout=4)\n"
        "out=[]\n"
        "for statement in json.loads(sys.argv[1]):\n"
        "    cur=c.cursor(); cur.execute(statement)\n"
        "    out.append([[('' if v is None else str(v)) for v in r] for r in cur.fetchall()])\n"
        "print(json.dumps(out))\n"
    )
    environment = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "WINDIR")},
        "PGH": values.get("MES_PG_HOST", "127.0.0.1"),
        "PGP": values.get("MES_PG_PORT", "5432"),
        "PGD": values.get("MES_PG_DBNAME", "mescopy_v1"),
        "PGU": values.get("MES_PG_USER", "postgres"),
        "PGW": values.get("MES_PG_PASSWORD", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        finished = subprocess.run(
            [str(python), "-c", script, json.dumps(list(statements))],
            capture_output=True, text=True, timeout=25,
            cwd=BACKEND_DIR, env=environment, creationflags=NO_WINDOW,
        )
        if finished.returncode != 0:
            return None
        return json.loads(finished.stdout.strip())
    except Exception:
        return None


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class DemoLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CV → MES Agentic Defect Detection — Demo")
        self.geometry("1060x720")
        self.minsize(900, 620)

        self.messages: queue.Queue[str] = queue.Queue()
        # Worker threads must not touch widgets, and must not call .after()
        # either - tkinter is only safe from the thread that owns the loop.
        # Background work posts a callable here; the main thread runs it.
        self.tasks: queue.Queue = queue.Queue()
        self.processes: dict[str, subprocess.Popen] = {}
        self.mode: str | None = None
        self.database_ready = False
        self.values = dict(launch.load_launch_env_file())
        self.values.update({k: v for k, v in os.environ.items() if k.startswith("MES_")})

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self.drain_messages)
        self.after(400, self.run_preflight)
        self.after(2500, self.poll_state)

    # -- layout ------------------------------------------------------------

    def _build(self):
        header = ttk.Frame(self, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="CV → MES Agentic Defect Detection",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="A YOLOv8 camera feeds a synthetic factory MES; Claude agents "
                 "investigate each defect burst and write a root-cause report.",
            foreground="#555",
        ).pack(anchor="w")

        body = ttk.Frame(self, padding=(14, 0, 14, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=330)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)

        # Preflight
        checks_box = ttk.LabelFrame(left, text="Before we start", padding=10)
        checks_box.pack(fill="x")
        self.checks_frame = ttk.Frame(checks_box)
        self.checks_frame.pack(fill="x")
        ttk.Button(
            checks_box, text="Re-check", command=self.run_preflight,
        ).pack(anchor="e", pady=(8, 0))

        # Controls
        controls = ttk.LabelFrame(left, text="Run the demo", padding=10)
        controls.pack(fill="x", pady=(12, 0))

        self.free_button = ttk.Button(
            controls, text="▶  Free demo  (no API cost)",
            command=lambda: self.start("free"),
        )
        self.free_button.pack(fill="x")
        ttk.Label(
            controls,
            text="Runs the camera, the confidence gate and the batching.\n"
                 "The agent call is stubbed, so this spends nothing.",
            foreground="#555", justify="left",
        ).pack(anchor="w", pady=(3, 10))

        self.full_button = ttk.Button(
            controls, text="▶  Full demo  (uses API credit)",
            command=lambda: self.start("full"),
        )
        self.full_button.pack(fill="x")
        ttk.Label(
            controls,
            text="Starts every service and runs the real agents.\n"
                 "About 73 seconds from image to finished report.",
            foreground="#555", justify="left",
        ).pack(anchor="w", pady=(3, 10))

        self.burst_button = ttk.Button(
            controls, text="📷  Fire a camera burst",
            command=self.fire_burst, state="disabled",
        )
        self.burst_button.pack(fill="x", pady=(4, 0))

        self.dashboard_button = ttk.Button(
            controls, text="🔎  Open the trace dashboard",
            command=lambda: webbrowser.open(f"http://localhost:{DASHBOARD_PORT}"),
            state="disabled",
        )
        self.dashboard_button.pack(fill="x", pady=(6, 0))

        self.stop_button = ttk.Button(
            controls, text="⏹  Stop everything",
            command=self.stop_all, state="disabled",
        )
        self.stop_button.pack(fill="x", pady=(10, 0))

        # Service state
        services = ttk.LabelFrame(left, text="Services", padding=10)
        services.pack(fill="x", pady=(12, 0))
        self.service_labels = {}
        for key, caption in (
            ("inference", f"Camera / inference  :{INFERENCE_PORT}"),
            ("bridge", f"Bridge + gate       :{BRIDGE_PORT}"),
            ("dashboard", f"Trace dashboard     :{DASHBOARD_PORT}"),
        ):
            row = ttk.Frame(services)
            row.pack(fill="x", pady=1)
            dot = ttk.Label(row, text="●", foreground="#bbb")
            dot.pack(side="left")
            ttk.Label(row, text="  " + caption).pack(side="left")
            self.service_labels[key] = dot

        # Alerts
        ttk.Label(
            right, text="Investigations", font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, sticky="w")
        columns = ("id", "status", "machine", "order", "defect", "n", "created")
        self.alerts = ttk.Treeview(
            right, columns=columns, show="headings", height=8,
        )
        for column, heading, width in (
            ("id", "Alert", 55), ("status", "Status", 90),
            ("machine", "Machine", 75), ("order", "Work order", 85),
            ("defect", "Defect", 110), ("n", "Detections", 80),
            ("created", "Created", 170),
        ):
            self.alerts.heading(column, text=heading)
            self.alerts.column(column, width=width, anchor="w")
        self.alerts.grid(row=1, column=0, sticky="nsew", pady=(4, 10))

        ttk.Label(
            right, text="Activity", font=("Segoe UI", 11, "bold"),
        ).grid(row=2, column=0, sticky="w")
        log_frame = ttk.Frame(right)
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_frame, wrap="none", height=10, background="#1e1e1e",
            foreground="#d6d6d6", insertbackground="#d6d6d6",
            font=("Consolas", 9), relief="flat",
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set, state="disabled")

        self.status = ttk.Label(
            self, text="Ready.", relief="sunken", anchor="w", padding=(8, 4),
        )
        self.status.pack(fill="x", side="bottom")

    # -- plumbing ----------------------------------------------------------

    def say(self, text: str):
        self.messages.put(text)

    def post(self, callback):
        """Ask the main thread to run this. Safe to call from any thread."""
        self.tasks.put(callback)

    def drain_messages(self):
        try:
            while True:
                line = self.messages.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", line.rstrip() + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                callback = self.tasks.get_nowait()
                try:
                    callback()
                except Exception as error:      # a broken update must not
                    self.say(f"[ui] {error}")   # stop the pump
        except queue.Empty:
            pass
        self.after(120, self.drain_messages)

    def run_preflight(self):
        self.status.configure(text="Checking prerequisites…")

        def work():
            self.values = dict(launch.load_launch_env_file())
            results = preflight(self.values)
            self.post(lambda: self.show_preflight(results))

        threading.Thread(target=work, daemon=True).start()

    def show_preflight(self, results):
        for child in self.checks_frame.winfo_children():
            child.destroy()
        self.database_ready = True
        for label, ok, detail in results:
            row = ttk.Frame(self.checks_frame)
            row.pack(fill="x", pady=1)
            ttk.Label(
                row, text="✓" if ok else "✗", width=2,
                foreground="#1a8a3a" if ok else "#c0392b",
            ).pack(side="left")
            ttk.Label(row, text=label, width=22).pack(side="left")
            ttk.Label(row, text=detail, foreground="#555").pack(side="left")
            if label.startswith("Database") and not ok:
                self.database_ready = False
        self.status.configure(text="Ready.")
        self.refresh_alerts()

    def stream_output(self, name: str, process: subprocess.Popen):
        def pump():
            for raw in iter(process.stdout.readline, ""):
                line = raw.rstrip()
                # This panel is what an audience reads. The service-status
                # poll hits /health every three seconds on two services, so
                # left unfiltered it buries the detections and the alert
                # lifecycle - the only lines anyone actually wants to see.
                if line and "/health" not in line:
                    self.say(f"[{name}] {line}")
            process.stdout.close()

        threading.Thread(target=pump, daemon=True).start()

    def spawn(self, name: str, command, cwd: Path, environment: dict):
        process = subprocess.Popen(
            command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=NO_WINDOW,
        )
        self.processes[name] = process
        self.stream_output(name, process)
        return process

    # -- actions -----------------------------------------------------------

    def start(self, mode: str):
        if self.processes:
            messagebox.showinfo("Already running", "Stop the current run first.")
            return
        if not getattr(self, "database_ready", False):
            messagebox.showerror(
                "Database unreachable",
                "The demo needs PostgreSQL with the mescopy_v1 database.\n\n"
                "Restore it with:\n"
                "  psql -U postgres -d mescopy_v1 -f "
                "sample-MES-ClosedLoop-Strands-Agent/mescopy_backup.sql\n\n"
                "and set MES_PG_PASSWORD in .env.",
            )
            return

        busy = [p for p in (INFERENCE_PORT, BRIDGE_PORT) if not port_is_free(p)]
        if busy:
            messagebox.showerror(
                "Ports in use",
                f"Something is already listening on {busy}.\n"
                "Close it, or use 'Stop everything' if this launcher started it.",
            )
            return

        self.mode = mode
        parent = dict(self.values)
        parent.update(os.environ)
        parent.setdefault("MES_DB_BACKEND", "postgres")
        if len(parent.get("MES_INTERNAL_API_TOKEN", "")) < 32:
            import secrets
            parent["MES_INTERNAL_API_TOKEN"] = secrets.token_urlsafe(32)

        self.free_button.configure(state="disabled")
        self.full_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        if mode == "free":
            self.say("=== Free demo: the agent call is stubbed, this spends nothing ===")
            self.spawn(
                "camera",
                [str(venv_python(MLOPS_DIR)), "-m", "uvicorn",
                 "deployment.api:app", "--host", "127.0.0.1",
                 "--port", str(INFERENCE_PORT)],
                MLOPS_DIR, launch.service_environment("inference", parent),
            )
            bridge_env = launch.service_environment("bridge", parent)
            bridge_env["MES_ANALYZE_STUB"] = "1"
            self.spawn(
                "bridge",
                [str(venv_python(BACKEND_DIR)), "-m", "uvicorn",
                 "bridge.bridge:app", "--host", "127.0.0.1",
                 "--port", str(BRIDGE_PORT)],
                CHATBOT_DIR, bridge_env,
            )
            self.say("Starting the camera and the bridge…")
        else:
            if len(self.values.get("ANTHROPIC_API_KEY", "").strip()) <= 10:
                messagebox.showerror(
                    "No API key",
                    "The full demo calls the Anthropic API.\n"
                    "Set ANTHROPIC_API_KEY in .env, or use the free demo.",
                )
                self.reset_buttons()
                return
            if not messagebox.askokcancel(
                "This costs money",
                "The full demo runs the real agents against the Anthropic API.\n\n"
                "One burst investigation is roughly 40-260 seconds of model time.\n\n"
                "Continue?",
            ):
                self.reset_buttons()
                return
            self.say("=== Full demo: real agent runs, this spends API credit ===")
            self.spawn(
                "launch", [sys.executable, "Launch.py"], REPO, parent,
            )
            self.say("Starting all services via Launch.py (the frontend build takes a minute)…")

        self.status.configure(text="Starting…")

    def fire_burst(self):
        if not self.processes:
            return
        self.say("=== Firing a camera burst: 5 images, 0.5 s apart ===")
        parent = dict(self.values)
        parent.update(os.environ)
        environment = launch.service_environment("bridge", parent)
        environment["MES_ANALYZE_STUB"] = "1" if self.mode == "free" else "0"
        self.burst_button.configure(state="disabled")

        def work():
            try:
                finished = subprocess.run(
                    [str(venv_python(BACKEND_DIR)), "-m", "bridge.simulator",
                     "--image-dir", str(BURST_IMAGES), "--interval", "0.5"],
                    cwd=CHATBOT_DIR, env=environment, capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=300, creationflags=NO_WINDOW,
                )
                for line in (finished.stdout + finished.stderr).splitlines():
                    if line.strip():
                        self.say(f"[burst] {line.strip()}")
                self.say("=== Burst sent. The batch window closes 30 s after the "
                         "last detection, then the investigation starts. ===")
            except Exception as error:
                self.say(f"[burst] failed: {error}")
            finally:
                self.post(lambda: self.burst_button.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def stop_all(self):
        self.say("Stopping…")
        self.stop_button.configure(state="disabled")

        def work():
            for name, process in list(self.processes.items()):
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                            capture_output=True, creationflags=NO_WINDOW,
                        )
                    else:
                        process.terminate()
                    process.wait(timeout=15)
                except Exception:
                    pass
                self.say(f"stopped {name}")
            self.processes.clear()
            # Launch.py's children outlive it; clear the ports it opened.
            for port in (INFERENCE_PORT, BRIDGE_PORT, DASHBOARD_PORT, 8000, 3000):
                free_port(port)
            self.mode = None
            self.post(self.reset_buttons)
            self.say("All stopped.")

        threading.Thread(target=work, daemon=True).start()

    def reset_buttons(self):
        self.free_button.configure(state="normal")
        self.full_button.configure(state="normal")
        self.burst_button.configure(state="disabled")
        self.dashboard_button.configure(state="disabled")
        self.stop_button.configure(state="disabled" if not self.processes else "normal")
        self.status.configure(text="Ready.")

    # -- periodic ----------------------------------------------------------

    def poll_state(self):
        def work():
            state = {
                "inference": http_ok(f"http://127.0.0.1:{INFERENCE_PORT}/health"),
                "bridge": http_ok(f"http://127.0.0.1:{BRIDGE_PORT}/health"),
                "dashboard": not port_is_free(DASHBOARD_PORT),
            }
            self.post(lambda: self.show_state(state))

        if self.processes:
            threading.Thread(target=work, daemon=True).start()
            self.refresh_alerts()
        self.after(3000, self.poll_state)

    def show_state(self, state: dict):
        for key, up in state.items():
            self.service_labels[key].configure(
                foreground="#1a8a3a" if up else "#bbb")
        pipeline_up = state["inference"] and state["bridge"]
        self.burst_button.configure(
            state="normal" if pipeline_up and self.processes else "disabled")
        self.dashboard_button.configure(
            state="normal" if state["dashboard"] else "disabled")
        if pipeline_up:
            self.status.configure(
                text="Pipeline up. Fire a camera burst to start an investigation.")

    def refresh_alerts(self):
        def work():
            rows = query_database(
                self.values,
                "select alertid, status, machineid, orderid, defecttype, "
                "detectioncount, to_char(createdat, 'YYYY-MM-DD HH24:MI:SS') "
                "from agentalerts order by alertid desc limit 12",
            )
            if rows is not None:
                self.post(lambda: self.show_alerts(rows[0]))

        threading.Thread(target=work, daemon=True).start()

    def show_alerts(self, rows):
        selected = set(self.alerts.selection())
        self.alerts.delete(*self.alerts.get_children())
        for row in rows:
            item = self.alerts.insert("", "end", iid=row[0], values=row)
            if item in selected:
                self.alerts.selection_add(item)

    def on_close(self):
        if self.processes:
            if not messagebox.askokcancel(
                "Quit", "Services are still running. Stop them and quit?"
            ):
                return
            self.stop_all()
            time.sleep(1.5)
        self.destroy()


def free_port(port: int):
    if os.name != "nt":
        return
    try:
        finished = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            creationflags=NO_WINDOW, timeout=15,
        )
        for line in finished.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" \
                    and parts[1].endswith(f":{port}"):
                subprocess.run(["taskkill", "/T", "/F", "/PID", parts[4]],
                               capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        pass


if __name__ == "__main__":
    DemoLauncher().mainloop()
