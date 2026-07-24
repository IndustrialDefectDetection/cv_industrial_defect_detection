"""
Bridge package — CONTRACTS.md implementation for CV → MES agentic defect detection.

Provides:
- POST /detection endpoint (bridge.py)
- §4 lookup helpers (mes_lookups.py)
- Batch window management (batch_manager.py)
- The analyze_batch seam (§6) for AgentAlerts (analyze_batch.py)
- Simulator script (simulator.py)
"""

__version__ = "1.0.0"