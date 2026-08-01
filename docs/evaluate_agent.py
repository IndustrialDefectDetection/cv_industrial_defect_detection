"""Score the agent's reports against the incidents the generator injected.

Ground truth is not guesswork: sqlite-synthetic-mes-data.py writes the name
of each injected incident into QualityControl.Comments, so the database
records which runs were sabotaged, by what, and when. The agent is never
shown that column in a form that names the incident - it sees defect counts,
machines, dates and root-cause fields - so recovering the cause is a real
inference.

Writes the results as JSON for the write-up to consume.

    python docs/evaluate_agent.py                 # run the agent (costs credit)
    python docs/evaluate_agent.py --rescore FILE  # re-score saved reports, free

The second form exists because the scoring is the part most likely to be wrong.
Re-scoring a run already paid for must never require paying for it again.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "sample-MES-ClosedLoop-Strands-Agent"
DEFAULT_OUT = REPO / "docs" / "evaluation-latest.json"

SUPPLIER = "Component supplier quality issue"
DEFECT_TYPES = ["Sensor Malfunction", "Battery Cell Variance", "Motor Coil Problem"]


def pg_settings():
    """Read the connection from the environment, like every other service."""
    return dict(
        host=os.getenv("MES_PG_HOST", "127.0.0.1"),
        port=int(os.getenv("MES_PG_PORT", "5432")),
        user=os.getenv("MES_PG_USER", "postgres"),
        password=os.getenv("MES_PG_PASSWORD") or None,
        dbname=os.getenv("MES_PG_DBNAME", "mescopy_v1"),
    )


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


# Confounded, and left in place only as a floor. Defects.RootCause already
# contains the literal string 'Supplier Quality' in 939 rows - the single most
# common value - so an agent that reported the modal root cause without doing
# any reasoning would score 'identified_supplier_cause' correct. Judge the
# agent on 'localised_the_incident' below instead.
CAUSE_WORDS = ["supplier", "vendor", "incoming material", "material batch",
               "bad batch", "purchased", "procure", "component quality"]
WRONG_CAUSE_WORDS = ["operator error", "training", "vision system",
                     "camera", "sensor drift", "calibration drift"]


def machine_mentioned(machine_name, report_lower):
    """Does the report name this machine, however it chooses to write it?

    The Machines table stores 'Machine Mot-50'. Reports overwhelmingly write
    'Mot-50' - in a table cell, or as 'machine Mot-50', or 'Mot-50 (Motor
    Assembly)'. Substring-matching the stored name therefore scored a report
    that had correctly identified the top machine as having named no machine
    at all, which is how run 1 came to record a failure the agent did not
    commit. Match the identifier instead, bounded so Mot-5 cannot match
    Mot-50.
    """
    if machine_name.lower() in report_lower:
        return True
    identifier = machine_name.split()[-1].lower()
    if not any(character.isdigit() for character in identifier):
        # Nothing distinctive enough to match on without inviting collisions.
        return False
    return re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])",
                     report_lower) is not None


def score(report, truth):
    low = report.lower()
    named = [m for m in truth["top_machines"] if machine_mentioned(m, low)]
    cause_hits = sorted({w for w in CAUSE_WORDS if w in low})
    wrong_hits = sorted({w for w in WRONG_CAUSE_WORDS if w in low})
    # Did it quote a date inside the incident window?
    dates = set(re.findall(r"20\d\d-\d\d-\d\d", report))
    in_window = sorted(d for d in dates if truth["window"][0] <= d <= truth["window"][1])
    top_named = (bool(named and named[0] == truth["top_machines"][0])
                 if truth["top_machines"] else None)
    return {
        "machines_named": named,
        "top_machine_named": top_named,
        "any_top3_machine_named": bool(named),
        "supplier_cause_terms": cause_hits,
        "identified_supplier_cause": bool(cause_hits),
        # Not a headline metric: see the note on CAUSE_WORDS. Kept because a
        # report that never reaches a supplier cause has certainly missed it.
        "competing_cause_terms": wrong_hits,
        "dates_in_incident_window": in_window[:6],
        "cited_window": bool(in_window),
        # The metric that parroting cannot pass. Naming the cause is cheap;
        # naming the cause *and* the machines it actually hit *and* a date
        # inside the injected window requires having queried the data.
        "localised_the_incident": bool(cause_hits) and bool(named) and bool(in_window),
        "report_chars": len(report),
    }


def show(marks):
    print(f"  machine named : {marks['top_machine_named']} {marks['machines_named']}")
    print(f"  supplier cause: {marks['identified_supplier_cause']} {marks['supplier_cause_terms']}")
    print(f"  window cited  : {marks['cited_window']} {marks['dates_in_incident_window'][:3]}")
    print(f"  competing     : {marks['competing_cause_terms']}")
    print(f"  LOCALISED     : {marks['localised_the_incident']}")


def rescore(path, out_path):
    """Re-apply the current scoring to reports already paid for."""
    results = json.loads(Path(path).read_text(encoding="utf-8"))
    for entry in results:
        if not entry.get("report"):
            continue
        before = entry.get("scores", {})
        entry["scores"] = score(entry["report"], entry["truth"])
        print(f"\n=== {entry['truth']['defect_type']} (re-scored) ===")
        show(entry["scores"])
        for key, new in entry["scores"].items():
            old = before.get(key)
            if isinstance(new, bool) and isinstance(old, bool) and old != new:
                print(f"  CHANGED {key}: {old} -> {new}")
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


def run_live(out_path):
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    os.environ.setdefault("MES_DB_BACKEND", "postgres")

    import psycopg2

    from agent_tracer import AgentTracer
    from strands_agent import MESAgentManager

    conn = psycopg2.connect(**pg_settings())
    cur = conn.cursor()
    manager = MESAgentManager(tracer=AgentTracer())

    results = []
    for defect_type in DEFECT_TYPES:
        truth = ground_truth(cur, defect_type)
        print(f"\n=== {defect_type} ===")
        print(f"  truth: {truth['incident_defects']}/{truth['total_defects']} defects "
              f"({truth['incident_share']:.0%}) flagged, window "
              f"{truth['window'][0]}..{truth['window'][1]}")
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
            show(marks)

        results.append({"truth": truth, "status": status,
                        "duration_s": round(duration, 1), "scores": marks,
                        "report": report})
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    conn.close()
    print(f"\nwrote {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rescore", metavar="FILE",
                        help="re-score saved reports offline; spends nothing")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    arguments = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if arguments.rescore:
        rescore(arguments.rescore, arguments.out)
    else:
        run_live(arguments.out)


if __name__ == "__main__":
    main()
