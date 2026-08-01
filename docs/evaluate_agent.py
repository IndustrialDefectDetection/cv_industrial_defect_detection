"""Score the agent's reports against the incidents the generator injected.

Ground truth is not guesswork: sqlite-synthetic-mes-data.py writes the name
of each injected incident into QualityControl.Comments, so the database
records which runs were sabotaged, by what, and when. The agent is never
shown that column in a form that names the incident - it sees defect counts,
machines, dates and root-cause fields - so recovering the cause is a real
inference.

Writes the results as JSON for the write-up to consume.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

BACKEND = Path(r"C:\Users\ATUL ANAND\OneDrive\Documentos\Desktop\Projects\cv_industrial_defect_detection\sample-MES-ClosedLoop-Strands-Agent")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)
os.environ.setdefault("MES_DB_BACKEND", "postgres")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2

from agent_tracer import AgentTracer
from strands_agent import MESAgentManager

PG = dict(host="localhost", port=5432, user="postgres", password="postgres",
          dbname="mescopy_v1")
SUPPLIER = "Component supplier quality issue"
DEFECT_TYPES = ["Sensor Malfunction", "Battery Cell Variance", "Motor Coil Problem"]
OUT = Path(r"C:\Users\ATULAN~1\AppData\Local\Temp\claude\C--Users-ATUL-ANAND-OneDrive-Documentos-Desktop-Projects-cv-industrial-defect-detection\14085b65-a7c8-4f73-9741-57da86dd8b52\scratchpad\evaluation.json")


def ground_truth(cur, defect_type):
    cur.execute("""
        SELECT m.Name, COUNT(*) c
        FROM Defects d
        JOIN QualityControl qc ON d.CheckID = qc.CheckID
        JOIN WorkOrders w ON qc.OrderID = w.OrderID
        JOIN Machines m ON w.MachineID = m.MachineID
        WHERE d.DefectType = %s AND qc.Comments LIKE %s
        GROUP BY m.Name ORDER BY c DESC
    """, (defect_type, f"%{SUPPLIER}%"))
    machines = cur.fetchall()

    cur.execute("""
        SELECT MIN(qc.Date)::date, MAX(qc.Date)::date, COUNT(*)
        FROM Defects d JOIN QualityControl qc ON d.CheckID = qc.CheckID
        WHERE d.DefectType = %s AND qc.Comments LIKE %s
    """, (defect_type, f"%{SUPPLIER}%"))
    start, end, incident_count = cur.fetchone()

    cur.execute("""
        SELECT COUNT(*) FROM Defects d JOIN QualityControl qc ON d.CheckID = qc.CheckID
        WHERE d.DefectType = %s
    """, (defect_type,))
    total = cur.fetchone()[0]

    return {"defect_type": defect_type, "incident": SUPPLIER,
            "top_machines": [m for m, _ in machines[:3]],
            "machine_counts": {m: c for m, c in machines[:5]},
            "window": [str(start), str(end)],
            "incident_defects": incident_count, "total_defects": total,
            "incident_share": round(incident_count / total, 3) if total else 0}


CAUSE_WORDS = ["supplier", "vendor", "incoming material", "material batch",
               "bad batch", "purchased", "procure", "component quality"]
WRONG_CAUSE_WORDS = ["operator error", "training", "vision system",
                     "camera", "sensor drift", "calibration drift"]


def score(report, truth):
    low = report.lower()
    named = [m for m in truth["top_machines"] if m.lower() in low]
    cause_hits = sorted({w for w in CAUSE_WORDS if w in low})
    wrong_hits = sorted({w for w in WRONG_CAUSE_WORDS if w in low})
    # Did it quote a date inside the incident window?
    dates = set(re.findall(r"20\d\d-\d\d-\d\d", report))
    in_window = sorted(d for d in dates if truth["window"][0] <= d <= truth["window"][1])
    return {
        "machines_named": named,
        "top_machine_named": bool(named and named[0] == truth["top_machines"][0])
                             if truth["top_machines"] else None,
        "any_top3_machine_named": bool(named),
        "supplier_cause_terms": cause_hits,
        "identified_supplier_cause": bool(cause_hits),
        "competing_cause_terms": wrong_hits,
        "dates_in_incident_window": in_window[:6],
        "cited_window": bool(in_window),
        "report_chars": len(report),
    }


conn = psycopg2.connect(**PG)
cur = conn.cursor()
manager = MESAgentManager(tracer=AgentTracer())

results = []
for defect_type in DEFECT_TYPES:
    truth = ground_truth(cur, defect_type)
    print(f"\n=== {defect_type} ===")
    print(f"  truth: {truth['incident_defects']}/{truth['total_defects']} defects "
          f"({truth['incident_share']:.0%}) flagged, window {truth['window'][0]}..{truth['window'][1]}")
    print(f"  truth machines: {truth['top_machines']}")

    start = time.time()
    try:
        out = manager.run_defect_analysis(
            defect_type=defect_type, days_back=30,
            include_oee=False, include_downtime=True,
            include_changeover=False, include_maintenance=True)
        report = out.get("supervisor_orchestration") or ""
        status = out.get("status", "completed")
    except Exception as e:
        report, status = "", f"error: {type(e).__name__}: {e}"
    duration = time.time() - start

    marks = score(report, truth) if report else {}
    print(f"  run: {status} in {duration:.0f}s, {len(report)} chars")
    if marks:
        print(f"  machine named : {marks['machines_named']}")
        print(f"  supplier cause: {marks['identified_supplier_cause']} {marks['supplier_cause_terms']}")
        print(f"  window cited  : {marks['cited_window']} {marks['dates_in_incident_window'][:3]}")
        print(f"  competing     : {marks['competing_cause_terms']}")

    results.append({"truth": truth, "status": status,
                    "duration_s": round(duration, 1), "scores": marks,
                    "report": report})
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")

conn.close()
print(f"\nwrote {OUT}")
