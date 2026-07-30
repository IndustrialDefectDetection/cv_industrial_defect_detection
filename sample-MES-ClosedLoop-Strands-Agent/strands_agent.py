"""
Strands agents for MES  application
Contains Monitor, Analyzer, Planner, and Verifier agents for manufacturing quality analysis
"""

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import sys

# Print agent output as UTF-8 whatever the console is.
#
# Strands' default PrintingCallbackHandler prints every streamed token to
# stdout. When a host process is launched by Launch.py its stdout is a pipe,
# so Python falls back to the locale encoding - cp1252 on Windows - and the
# first emoji the model writes raises UnicodeEncodeError *inside the
# streaming callback*, which aborts the whole agent call. The conversational
# agent heads its answers with emoji, so this broke every chat turn. It lives
# here rather than in api.py because app.py, trace_viewer.py and the bridge
# all reach the agents without importing api. errors='replace' costs an
# unprintable glyph a '?' in the console instead of costing the user their
# answer.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):  # already wrapped, or not a TextIO
        pass

import boto3
import pandas as pd
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.anthropic import AnthropicModel
from strands.types.exceptions import MaxTokensReachedException
from chat_agent import build_conversational_agent

from agent_tracer import AgentTracer, attach_tracer


# One reports directory beside this file, so the API can serve what the
# agents write regardless of the process's working directory.
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _md_inline(text):
    """Escape XML-unsafe chars, then convert inline markdown to ReportLab tags.

    Escaping first matters: agent text contains <, > and & (SQL, thresholds
    like "OEE < 0.6"), which ReportLab parses as markup and chokes on.
    """
    text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<font face="Courier">\1</font>', text)
    return text


def _markdown_to_flowables(text, styles):
    """Convert a block of markdown-ish agent text into ReportLab flowables.

    The agents write markdown - headings, bullets, and pipe tables. Rendered
    with str() it lands in the PDF as literal asterisks and, for nested
    dicts, raw Python repr with curly braces. This turns it into real
    headings, bullets and tables.
    """
    flowables = []
    bullet_style = ParagraphStyle('MDBullet', parent=styles['Normal'],
                                  leftIndent=18, bulletIndent=6, spaceAfter=4)
    lines = str(text).split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if re.fullmatch(r'[=\-_*]{3,}', line):
            flowables.append(Spacer(1, 8))
            continue
        # Pipe table: gather consecutive rows, drop the |---|---| separator.
        if line.startswith('|') and line.endswith('|'):
            rows = [line]
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(lines[i].strip())
                i += 1
            data = []
            for r in rows:
                cells = [c.strip() for c in r.strip('|').split('|')]
                if all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
                    continue
                data.append([Paragraph(_md_inline(c), styles['Normal']) for c in cells])
            if data:
                tbl = Table(data, hAlign='LEFT')
                tbl.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#dbe5f1')),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('FONTSIZE', (0, 0), (-1, -1), 8),
                ]))
                flowables.append(tbl)
                flowables.append(Spacer(1, 10))
            continue
        m = re.match(r'(#{1,4})\s+(.*)', line)
        if m:
            level = min(len(m.group(1)), 3)
            flowables.append(Paragraph(_md_inline(m.group(2)), styles[f'Heading{level}']))
            flowables.append(Spacer(1, 6))
            continue
        m = re.fullmatch(r'\*\*(.+?)\*\*:?', line)
        if m:
            flowables.append(Paragraph(f'<b>{_md_inline(m.group(1))}</b>', styles['Heading3']))
            flowables.append(Spacer(1, 4))
            continue
        m = re.match(r'[-*•]\s+(.*)', line)
        if m:
            flowables.append(Paragraph(_md_inline(m.group(1)), bullet_style, bulletText='•'))
            continue
        m = re.match(r'(\d+)[.)]\s+(.*)', line)
        if m:
            flowables.append(Paragraph(_md_inline(m.group(2)), bullet_style,
                                       bulletText=f'{m.group(1)}.'))
            continue
        flowables.append(Paragraph(_md_inline(line), styles['Normal']))
        flowables.append(Spacer(1, 6))
    return flowables


def _to_postgres(sql: str) -> str:
    """Translate this file's SQLite-dialect SQL to PostgreSQL.

    Every query runs through one chokepoint (_execute_safe_query), so the
    dialect gap is closed in one place rather than by hand-editing ~90 sites
    across fifteen tool queries - where a mistranslated date cast returns the
    wrong rows silently instead of raising. The differential test compares
    both backends row for row.

    Handled (the only SQLite-specific constructs this file uses):
      date(X)                  -> CAST(X AS DATE)
      strftime('%w', X)        -> day-of-week as text, as SQLite returns
      strftime('%H', X)        -> zero-padded hour as text
      julianday(X)             -> days as a float; differences stay correct
                                  because the shared epoch offset cancels
      datetime(X, '-72 hours') -> X - INTERVAL '72 hours'
      ?                        -> %s
    """
    # julianday first: it wraps expressions the date() rule would also match.
    sql = re.sub(r"julianday\(\s*([^()]+?)\s*\)",
                 r"(EXTRACT(EPOCH FROM \1) / 86400.0)", sql, flags=re.I)
    def _interval(m):
        # SQLite's datetime(X, '-72 hours') means 72 hours BEFORE X. Getting
        # this sign wrong does not raise - it silently searches the wrong
        # direction in time and returns no rows.
        target, sign, amount, unit = m.group(1), m.group(2), m.group(3), m.group(4)
        op = "-" if sign == "-" else "+"
        return f"({target} {op} INTERVAL '{amount} {unit}')"

    sql = re.sub(r"datetime\(\s*([^,]+?)\s*,\s*'([+-]?)(\d+)\s+(\w+?)s?'\s*\)",
                 _interval, sql, flags=re.I)
    sql = re.sub(r"strftime\(\s*'%w'\s*,\s*([^()]+?)\s*\)",
                 r"EXTRACT(DOW FROM \1)::int::text", sql, flags=re.I)
    sql = re.sub(r"strftime\(\s*'%H'\s*,\s*([^()]+?)\s*\)",
                 r"TO_CHAR(\1, 'HH24')", sql, flags=re.I)
    sql = re.sub(r"\bdate\(\s*([^()]+?)\s*\)", r"CAST(\1 AS DATE)", sql, flags=re.I)
    return sql.replace("?", "%s")


def _column_case_map(sql: str) -> dict:
    """{lowercase: OriginalCase} for every identifier written in the query.

    Used to restore column names after PostgreSQL lower-cases them. The
    query text is the source of truth for the intended spelling, so no
    hand-maintained mapping can drift out of date.
    """
    mapping = {}
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql):
        low = token.lower()
        # Keep the first spelling that isn't already all-lowercase, so
        # 'DefectType' wins over a later bare 'defecttype'.
        if token != low and low not in mapping:
            mapping[low] = token
    return mapping


def _render_report_value(value, styles, depth=0):
    """Flatten any agent-supplied value into readable flowables.

    Dicts and lists are walked structurally instead of str()'d, because
    str({'immediate_actions': [...]}) renders Python's repr - curly braces,
    quotes and all - into the finished PDF.
    """
    flowables = []
    heading = f"Heading{min(depth + 2, 4)}"
    if isinstance(value, dict):
        for key, inner in value.items():
            label = str(key).replace('_', ' ').title()
            if isinstance(inner, (dict, list)):
                flowables.append(Paragraph(_md_inline(label), styles[heading]))
                flowables.extend(_render_report_value(inner, styles, depth + 1))
            else:
                flowables.append(Paragraph(
                    f"<b>{_md_inline(label)}:</b> {_md_inline(inner)}", styles['Normal']))
                flowables.append(Spacer(1, 6))
    elif isinstance(value, (list, tuple)):
        bullet_style = ParagraphStyle('MDBullet', parent=styles['Normal'],
                                      leftIndent=18, bulletIndent=6, spaceAfter=4)
        for item in value:
            if isinstance(item, (dict, list)):
                flowables.extend(_render_report_value(item, styles, depth + 1))
            else:
                text = str(item).strip()
                if text:
                    flowables.append(Paragraph(_md_inline(text), bullet_style,
                                               bulletText='•'))
    else:
        flowables.extend(_markdown_to_flowables(value, styles))
    return flowables


def render_markdown_report_pdf(markdown_text, filename=None):
    """Render the Supervisor's final markdown report as a PDF.

    This is the report a human actually reads - the eight-section synthesis
    shown in the dashboard. Previously only the Planner's intermediate
    action plan was ever written to disk.
    """
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("ReportLab is not installed; cannot render the report PDF.")

    report_text = str(markdown_text).strip()
    if not report_text:
        raise ValueError("Cannot generate a PDF from an empty report.")

    if filename is None:
        filename = f"MES_Final_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]

    filepath = REPORTS_DIR / f"{filename}.pdf"
    doc = SimpleDocTemplate(str(filepath), pagesize=A4)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle", parent=styles["Title"], fontSize=24, spaceAfter=30,
        textColor=colors.darkblue, alignment=TA_CENTER)

    story = [
        Paragraph("Manufacturing Execution System Analysis Report", title_style),
        Spacer(1, 20),
        Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}",
                  styles["Normal"]),
        Spacer(1, 20),
    ]
    story.extend(_markdown_to_flowables(report_text, styles))
    doc.build(story)

    if not filepath.exists() or filepath.stat().st_size == 0:
        raise RuntimeError(f"PDF generation produced no usable file: {filepath}")

    logger.info("Final report PDF: %s (%s bytes)", filepath, filepath.stat().st_size)
    return filepath


class RunCancelled(BaseException):
    """Raised inside a run's worker thread when the user cancelled it.

    Python cannot kill a thread, so cancellation is cooperative: cancel()
    sets a flag and the run unwinds at its next checkpoint (a subagent
    delegation or a database query). Callers should treat this as a normal
    outcome, not a failure - see run_defect_analysis's 'cancelled' status.

    Deliberately derived from BaseException, not Exception, for the same
    reason KeyboardInterrupt is: this is a control-flow signal that must
    unwind the whole run, and the code it passes through is full of broad
    `except Exception` handlers that log-and-continue (the window-stats
    pre-check, the per-agent retry loop, the SDK's own tool wrappers). As
    an Exception it gets swallowed by the first of those and the
    "cancelled" run carries on into a live API call. Anything catching it
    must name it explicitly.
    """

load_dotenv(Path(__file__).parent / ".env")

# PDF generation imports
try:
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

# Setup logging
def setup_logging():
    log_level = os.getenv('MES_LOG_LEVEL', 'INFO').upper()
    log_format = os.getenv('MES_LOG_FORMAT', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    logging.basicConfig(
        level=getattr(logging, log_level),
        format=log_format
    )
    
    return logging.getLogger(__name__)

logger = setup_logging()

class MESAgentManager:
    """Manager class for MES agents focused on manufacturing quality analysis"""
    
    def __init__(self, db_path: str = None, model_id: str = None, region_name: str = None,
                 tracer: "AgentTracer" = None):
        """Initialize the MES Agent Manager"""

        # Live trace of the multi-agent run ("under the hood" view). Shared,
        # thread-safe; the workflow can run in a background thread while a UI
        # polls it. See agent_tracer.AgentTracer. Concurrent-run guarding is
        # the caller's job (api.py holds the one-run-at-a-time lock).
        self.tracer = tracer or AgentTracer()

        # Set by cancel() when the user abandons a run; checked at every
        # delegation and query checkpoint (see _check_cancelled). This
        # manager outlives individual runs, so run_defect_analysis clears
        # it at the start of each run rather than relying on a fresh object.
        self._cancelled = threading.Event()
        self._chat_running = threading.Event()
        
        # Get parameters from environment variables with fallbacks
        if db_path is None:
            db_path = os.getenv('MES_DB_PATH')
            if db_path is None:
                proj_dir = os.path.abspath('')
                db_path = os.path.join(proj_dir, 'mes.db')
        
        if model_id is None:
            model_id = os.getenv("MES_MODEL_ID", "claude-haiku-4-5-20251001")
        
        if region_name is None:
            region_name = os.getenv('AWS_REGION', 'us-west-2')
        
        # Email configuration from environment variables
        self.sender_email = os.getenv('MES_SENDER_EMAIL', 'operations.team@example.com')
        self.recipient_email = os.getenv('MES_RECIPIENT_EMAIL', 'operations.team@example.com')
        # Defaults to this deployment's dashboard, not the upstream sample's
        # long-dead CloudFront demo host.
        self.base_url = os.getenv('MES_BASE_URL', 'http://localhost:8502')
        
        # Database path
        self.db_path = db_path

        # PostgreSQL is the default: the CV pipeline's bridge writes camera
        # detections there, so an agent on SQLite cannot see them and would
        # report "no detections" when it simply looked in the wrong database.
        # MES_DB_BACKEND=sqlite still runs against the local mes.db.
        self.db_backend = os.getenv("MES_DB_BACKEND", "postgres").strip().lower()
        if self.db_backend not in ("sqlite", "postgres"):
            raise ValueError(
                f"MES_DB_BACKEND must be 'sqlite' or 'postgres', got {self.db_backend!r}")
        if self.db_backend == "postgres":
            self._verify_postgres_ready()

        # Newest timestamp in the database. Look-back windows count back from
        # this anchor instead of from today, so the frozen synthetic dataset
        # stays inside every window no matter when the demo runs (a "last 7
        # days" run against data that ends weeks ago otherwise queries an
        # empty window and the agents speculate about why).
        self.data_anchor_date = self._load_data_anchor()

        # Anthropic API key from .env / environment
        api_key = os.getenv("ANTHROPIC_API_KEY")

        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is missing. Add it to your .env file.")

        self.model = AnthropicModel(
            client_args={
                "api_key": api_key,
                # Without an explicit timeout the SDK default is 10 minutes,
                # so one stalled request looks like a permanently frozen run.
                # 120s comfortably covers a legitimate long generation while
                # bounding a hang; _call_agent_with_retry handles the retry.
                "timeout": float(os.getenv("MES_API_TIMEOUT", "120")),
                "max_retries": int(os.getenv("MES_API_RETRIES", "2")),
            },
            model_id=model_id,
            # A cap, not a target: the 600-word output rules keep normal
            # replies short. 4096 was too low for the Planner, which has to
            # emit a whole report dict as generate_pdf_report's arguments -
            # it hit MaxTokensReachedException mid-call and the run paid for
            # a second full invocation to continue.
            max_tokens=int(os.getenv("MES_MAX_TOKENS", "16384")),
            params={
                "temperature": float(os.getenv("MES_TEMPERATURE", "0.2")),
            },
        )

        self.region_name = region_name
        self.model_id = model_id

        # How many tries each agent gets before it is declared unavailable.
        # Three rather than two because the usual cause is the output token
        # limit, where an attempt *continues* the previous partial reply
        # instead of redoing it - so a long Planner report can genuinely need
        # a third. Every attempt is a full billed call, hence the cap of 5.
        self._agent_max_attempts = max(1, min(
            int(os.getenv("MES_AGENT_MAX_ATTEMPTS", "3")), 5))

        # Chat is multi-turn, so the conversational agent keeps its history -
        # but unbounded it re-sends every earlier report on every turn.
        self._chat_history_limit = max(2, int(
            os.getenv("MES_CHAT_HISTORY_MESSAGES", "12")))

        # How many times one chat question may delegate to the supervisor.
        # The chat agent decides for itself when to call ask_mes_supervisor,
        # and nothing in the model stops it deciding twice - but a delegation
        # is a full multi-agent workflow costing minutes and real credit, so
        # the budget is enforced here rather than merely requested in a
        # prompt. One is the right default for a chat box: a question earns an
        # analysis, and anything further is a follow-up the user should choose
        # to ask.
        self._chat_supervisor_budget = max(1, min(
            int(os.getenv("MES_CHAT_SUPERVISOR_CALLS", "1")), 3))
        self._chat_supervisor_calls = 0

        # Define allowed table names for security
        self.allowed_tables = {
            'OEEMetrics', 'Machines', 'WorkCenters', 'Downtimes', 'WorkOrders',
            'Products', 'Shifts', 'Employees', 'Defects', 'QualityControl',
            # Written by the CV pipeline: the camera's detections, and the
            # alerts raised from them (CONTRACTS.md §3).
            'VisionDetections', 'AgentAlerts'
        }
        
        # Log configuration
        logger.info(f"MES Agent Manager initialized with:")
        logger.info(f"  Database Path: {self.db_path}")
        logger.info(f"  Model ID: {model_id}")
        logger.info(f"  AWS Region: {region_name}")
        logger.info(f"  Sender Email: {self.sender_email}")
        logger.info(f"  Recipient Email: {self.recipient_email}")
        logger.info(f"  Base URL: {self.base_url}")
        logger.info(f"  Max Tokens: {os.getenv('MES_MAX_TOKENS', '16384')}")
        logger.info(f"  Temperature: {os.getenv('MES_TEMPERATURE', '0.2')}")
        logger.info(f"  Agent attempts: {self._agent_max_attempts}")
        
        # Initialize tools and agents
        self._init_database_tools()
        self._init_email_tools()
        self._init_monitor_tools()
        self._init_analyzer_tools()
        self._init_planner_tools()
        self._init_executor_tools()
        self._init_verifier_tools()
        self._init_agents()
        self._init_supervisor_agent()
        self.conversational_agent = build_conversational_agent(
            self.model,
            self.supervisor_agent,
            self.tracer,
            call_supervisor=self._call_supervisor_for_chat,
        )

    def get_conversational_agent(self):
        """Return the conversational agent for external use"""
        return self.conversational_agent

    def run_chat(self, query: str):
        """Run one conversational turn with cooperative cancellation enabled."""
        self.prepare_chat_turn()
        self._chat_running.set()
        try:
            response = self.conversational_agent(query)
            if response.stop_reason == "cancelled" or self._cancelled.is_set():
                raise RunCancelled("Chat response cancelled by the user")
            return response
        finally:
            self._chat_running.clear()
    
    def get_db_connection(self):
        """Open a connection to whichever backend is configured.

        SQLite stays the default so existing setups keep working; set
        MES_DB_BACKEND=postgres to run against the migrated mescopy_v1 that
        the CV pipeline's bridge also writes to.
        """
        if self.db_backend == "postgres":
            import psycopg2
            return psycopg2.connect(
                host=os.getenv("MES_PG_HOST", "localhost"),
                port=os.getenv("MES_PG_PORT", "5432"),
                user=os.getenv("MES_PG_USER", "postgres"),
                password=os.getenv("MES_PG_PASSWORD", "postgres"),
                dbname=os.getenv("MES_PG_DBNAME", "mescopy_v1"),
            )
        if not os.path.exists(self.db_path):
            logger.warning(f"Database file not found: {self.db_path}")
            raise FileNotFoundError(f"Database file not found: {self.db_path}")
        return sqlite3.connect(self.db_path)

    def _verify_postgres_ready(self):
        """Fail at startup, with instructions, if Postgres is not usable.

        Deliberately loud rather than falling back to SQLite: a silent
        fallback would read a different database than the one the camera
        writes to, and the agent would confidently report "no detections"
        for defects that were recorded perfectly well.
        """
        dbname = os.getenv("MES_PG_DBNAME", "mescopy_v1")
        setup = "python setupdatabase.py   (from sample-MES-ClosedLoop-Strands-Agent)"
        try:
            conn = self.get_db_connection()
        except Exception as e:
            raise RuntimeError(
                f"MES_DB_BACKEND=postgres but the database '{dbname}' is not reachable: {e}\n"
                f"  * Is the PostgreSQL server running?\n"
                f"  * Has the database been created and populated? Run:\n"
                f"      {setup}\n"
                f"  * Credentials come from MES_PG_USER / MES_PG_PASSWORD "
                f"(defaults: postgres/postgres).\n"
                f"  * To use the local SQLite copy instead, set MES_DB_BACKEND=sqlite."
            ) from e
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'")
                tables = {r[0].lower() for r in cur.fetchall()}
        finally:
            conn.close()

        missing_mes = {"machines", "workorders", "defects"} - tables
        if missing_mes:
            raise RuntimeError(
                f"Database '{dbname}' is missing the MES tables {sorted(missing_mes)}.\n"
                f"  The historical data has not been copied across yet. Run:\n"
                f"      {setup}")
        if "visiondetections" not in tables:
            # Not fatal - the MES tools still work - but the camera tool will not.
            logger.warning(
                "VisionDetections is missing from '%s'; get_recent_detections "
                "will be unavailable. Run %s", dbname, setup)
        logger.info("  Backend: PostgreSQL '%s' (%d tables)", dbname, len(tables))

    def _load_data_anchor(self):
        """Newest timestamp across the main time-bearing tables."""
        try:
            conn = self.get_db_connection()
            sql = ("SELECT MAX(t) FROM ("
                   "SELECT MAX(Date) as t FROM QualityControl "
                   "UNION ALL SELECT MAX(StartTime) FROM Downtimes "
                   "UNION ALL SELECT MAX(ActualEndTime) FROM WorkOrders)")
            if self.db_backend == "postgres":
                # Postgres requires a name for a derived table, and its
                # UNION arms must share a type - the columns are all
                # timestamps, so cast them explicitly.
                sql = ("SELECT MAX(t) FROM ("
                       "SELECT MAX(Date)::timestamp as t FROM QualityControl "
                       "UNION ALL SELECT MAX(StartTime)::timestamp FROM Downtimes "
                       "UNION ALL SELECT MAX(ActualEndTime)::timestamp FROM WorkOrders"
                       ") AS newest")
            cur = conn.cursor()
            cur.execute(sql)
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row[0]:
                anchor = pd.to_datetime(row[0]).to_pydatetime()
                logger.info(f"  Data anchor (newest record): {anchor:%Y-%m-%d}")
                return anchor
        except Exception as e:
            logger.warning(f"Data anchor detection failed, falling back to now: {e}")
        return datetime.now()

    def _cutoff_date(self, days_back) -> str:
        """Start date of a look-back window, counted back from the data
        anchor (newest database record) rather than from today.

        "Last N days" means N calendar dates ending at the anchor date
        inclusive, so the subtraction is days_back - 1 (a plain -days_back
        yields N+1 dates, an off-by-one readers notice in the UI)."""
        days_back = int(days_back)
        if days_back < 0 or days_back > 3650:
            raise ValueError("days_back must be between 0 and 3650")
        return (self.data_anchor_date - timedelta(days=max(days_back - 1, 0))).strftime('%Y-%m-%d')

    def _validate_table_name(self, table_name: str) -> bool:
        """Validate table name against allowed list"""
        return table_name in self.allowed_tables
    
    def _execute_safe_query(self, query: str, params: tuple = None):
        """Execute SQL query safely with parameterized queries"""
        # Cancellation checkpoint: stops a cancelled agent mid-phase, at its
        # next query, rather than only between phases.
        self._check_cancelled()
        logger.info(f"Executing parameterized SQL query")
        start_time = time.time()
        
        try:
            conn = self.get_db_connection()
            # One place to close the dialect gap - see _to_postgres.
            sql = _to_postgres(query) if self.db_backend == "postgres" else query
            if params:
                df = pd.read_sql_query(sql, conn, params=params)
            else:
                df = pd.read_sql_query(sql, conn)
            conn.close()

            if self.db_backend == "postgres":
                # PostgreSQL folds unquoted identifiers to lower case, while
                # SQLite echoes back whatever the query wrote. Without this,
                # the same tool hands the model 'defecttype' on one backend
                # and 'DefectType' on the other - and the output rules tell
                # it to cite specific column names like DefectRecords.
                # The original query text carries the intended spelling.
                df = df.rename(columns=_column_case_map(query))
            
            # Process datetime columns
            for col in df.columns:
                if df[col].dtype == 'object':
                    try:
                        if df[col].str.contains('-').any() and df[col].str.contains(':').any():
                            df[col] = pd.to_datetime(df[col]).dt.strftime('%Y-%m-%d %H:%M')
                    except:
                        pass
            
            # Round float columns
            for col in df.select_dtypes(include=['float']).columns:
                df[col] = df[col].round(2)
            
            result = {
                "success": True,
                "rows": df.to_dict(orient="records"),
                "column_names": df.columns.tolist(),
                "row_count": len(df),
                "execution_time_ms": round((time.time() - start_time) * 1000, 2),
                "dataframe": df
            }
            
            logger.info(f"Query executed successfully: {len(df)} rows returned")
            # Surface the actual SQL that ran onto the live trace ("the code
            # they're running"), attributed to whichever agent/tool is active.
            self._trace_query(query, params, result)
            return result

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error executing SQL query: {error_msg}")
            error_result = {
                "success": False,
                "error": error_msg,
                "execution_time_ms": round((time.time() - start_time) * 1000, 2)
            }
            self._trace_query(query, params, error_result)
            return error_result

    def _trace_query(self, query: str, params, result: dict):
        """Push the executed SQL to the tracer, never breaking the query path."""
        tracer = getattr(self, "tracer", None)
        if tracer is None:
            return
        try:
            tracer.log_query(query, params, result)
        except Exception as trace_err:  # tracing must never affect analysis
            logger.debug(f"Tracer log_query failed: {trace_err}")
    
    def _init_database_tools(self):
        """Initialize core database tools"""
        
        @tool
        def execute_sql(sql_query: str):
            """Execute predefined SQL queries against the MES database - only allows specific safe queries"""
            logger.info(f"Executing predefined SQL query")
            
            # Define allowed safe queries with parameterized structure
            allowed_queries = {
                "get_tables": "SELECT name FROM sqlite_master WHERE type='table'",
                "get_recent_oee": """
                    SELECT 
                        oee.Date,
                        m.Name as MachineName,
                        m.Type as MachineType,
                        wc.Name as WorkCenterName,
                        oee.Availability,
                        oee.Performance,
                        oee.Quality,
                        oee.OEE
                    FROM 
                        OEEMetrics oee
                    JOIN 
                        Machines m ON oee.MachineID = m.MachineID
                    JOIN 
                        WorkCenters wc ON m.WorkCenterID = wc.WorkCenterID
                    ORDER BY 
                        oee.Date DESC
                    LIMIT 100
                """,
                "get_recent_downtime": """
                    SELECT 
                        dt.StartTime,
                        dt.EndTime,
                        dt.Duration,
                        dt.Reason,
                        m.Name as MachineName,
                        m.Type as MachineType
                    FROM 
                        Downtimes dt
                    JOIN 
                        Machines m ON dt.MachineID = m.MachineID
                    ORDER BY 
                        dt.StartTime DESC
                    LIMIT 100
                """
            }
            
            # Check if the query is in allowed list
            query_key = sql_query.strip().lower()
            if query_key in allowed_queries:
                return self._execute_safe_query(allowed_queries[query_key])
            else:
                # For security, only allow predefined queries
                logger.warning(f"Query not in allowed list: {sql_query}")
                return {
                    "success": False,
                    "error": "Only predefined safe queries are allowed for security reasons"
                }
        
        self.execute_sql_tool = execute_sql

    def _init_email_tools(self):
        """Initialize Email tools"""
        
        @tool
        def send_email(subject: str, email_body: str, pdf_filename: str = None):
            """Send email for the short term action items with PDF link"""
            if os.getenv("MES_EMAIL_DRY_RUN", "true").lower() == "true":
                return {
                    "success": True,
                    "message": "Dry run: email not sent",
                    "subject": subject,
                    "body": email_body,
                    "pdf_filename": pdf_filename,
                }

            logger.info(f"Sending email with following detail: subject - {subject}")
            start_time = time.time()
            
            # Create SES client using IAM role
            ses_client = boto3.client('ses', region_name=self.region_name)

            # Email parameters from environment variables
            SENDER = self.sender_email
            RECIPIENT = self.recipient_email
            SUBJECT = subject
            
            # Add PDF link to email body if filename is provided
            if pdf_filename:
                # The app serves reports at ?pdf=<filename>, so one query
                # string - the old form repeated the filename and put the
                # first pair before the '?', producing a dead link.
                pdf_link = f"{self.base_url}/?pdf={pdf_filename}"
                email_body += f"\n\nDetailed PDF Report: {pdf_link}"
            
            # Email content
            BODY_TEXT = email_body
            BODY_HTML = f"""
            <html>
            <body>
                <h1>MES Execution Plan</h1>
                <p>{email_body.replace(chr(10), '<br>')}</p>
            </body>
            </html>
            """
            
            try:
                response = ses_client.send_email(
                    Destination={
                        'ToAddresses': [RECIPIENT]
                    },
                    Message={
                        'Body': {
                            'Html': {
                                'Charset': 'UTF-8',
                                'Data': BODY_HTML
                            },
                            'Text': {
                                'Charset': 'UTF-8',
                                'Data': BODY_TEXT
                            }
                        },
                        'Subject': {
                            'Charset': 'UTF-8',
                            'Data': SUBJECT
                        }
                    },
                    Source=SENDER
                )
                logger.info(f"Email sent! Message ID: {response['MessageId']}")
                
                result = {
                    "success": True,
                    "message": f"Email sent! Message ID: {response['MessageId']}",
                    "execution_time_ms": round((time.time() - start_time) * 1000, 2)
                }
                return result
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error sending email: {error_msg}")
                error_result = {
                    "success": False,
                    "error": error_msg,
                    "execution_time_ms": round((time.time() - start_time) * 1000, 2)
                }
                return error_result
        
        self.execute_email_send = send_email

    def _init_monitor_tools(self):
        """Initialize Monitor Agent tools - Captures & contextualizes operational data"""
        
        @tool
        def fetch_oee_metrics(days_back: int = 7):
            """Fetch OEE metrics and identify drops in performance"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                oee.Date,
                m.Name as MachineName,
                m.Type as MachineType,
                wc.Name as WorkCenterName,
                oee.Availability,
                oee.Performance,
                oee.Quality,
                oee.OEE,
                CASE 
                    WHEN oee.OEE < 0.6 THEN 'Critical'
                    WHEN oee.OEE < 0.75 THEN 'Low'
                    ELSE 'Acceptable'
                END as OEEStatus
            FROM 
                OEEMetrics oee
            JOIN 
                Machines m ON oee.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON m.WorkCenterID = wc.WorkCenterID
            WHERE 
                oee.Date >= ?
            ORDER BY
                oee.OEE ASC, oee.Date DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_downtime_events(days_back: int = 7):
            """Fetch downtime events and line stoppages"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                dt.StartTime,
                dt.EndTime,
                dt.Duration,
                dt.Reason,
                m.Name as MachineName,
                m.Type as MachineType,
                wc.Name as WorkCenterName,
                wo.OrderID,
                p.Name as ProductName,
                s.Name as ShiftName,
                e.Name as OperatorName
            FROM 
                Downtimes dt
            JOIN 
                Machines m ON dt.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON m.WorkCenterID = wc.WorkCenterID
            LEFT JOIN 
                WorkOrders wo ON m.MachineID = wo.MachineID
            LEFT JOIN 
                Products p ON wo.ProductID = p.ProductID
            LEFT JOIN 
                Employees e ON wo.EmployeeID = e.EmployeeID
            LEFT JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            WHERE 
                date(dt.StartTime) >= ?
            ORDER BY
                dt.Duration DESC, dt.StartTime DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_historical_patterns(days_back: int = 7):
            """Fetch historical stoppage patterns and context"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                date(dt.StartTime) as StoppageDate,
                strftime('%w', dt.StartTime) as DayOfWeek,
                strftime('%H', dt.StartTime) as HourOfDay,
                dt.Reason,
                COUNT(*) as EventCount,
                AVG(dt.Duration) as AvgDuration,
                m.Type as MachineType,
                wc.Name as WorkCenterName,
                s.Name as ShiftName
            FROM 
                Downtimes dt
            JOIN 
                Machines m ON dt.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON m.WorkCenterID = wc.WorkCenterID
            LEFT JOIN 
                WorkOrders wo ON m.MachineID = wo.MachineID
            LEFT JOIN
                Employees e ON wo.EmployeeID = e.EmployeeID
            LEFT JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            WHERE 
                date(dt.StartTime) >= ?
            GROUP BY
                date(dt.StartTime), strftime('%w', dt.StartTime),
                strftime('%H', dt.StartTime),
                dt.Reason, m.Type, wc.Name, s.Name
            ORDER BY
                EventCount DESC, AvgDuration DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_work_orders_context(days_back: int = 7):
            """Fetch work orders context and batch reports"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                wo.OrderID,
                wo.Status,
                wo.PlannedStartTime,
                wo.ActualStartTime,
                wo.PlannedEndTime,
                wo.ActualEndTime,
                wo.Quantity as PlannedQuantity,
                wo.ActualProduction,
                wo.Scrap,
                p.Name as ProductName,
                p.Category as ProductCategory,
                m.Name as MachineName,
                wc.Name as WorkCenterName,
                e.Name as OperatorName,
                s.Name as ShiftName,
                ROUND((wo.ActualProduction * 100.0 / wo.Quantity), 2) as CompletionRate
            FROM 
                WorkOrders wo
            JOIN 
                Products p ON wo.ProductID = p.ProductID
            JOIN 
                Machines m ON wo.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON wo.WorkCenterID = wc.WorkCenterID
            JOIN 
                Employees e ON wo.EmployeeID = e.EmployeeID
            JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            WHERE
                date(wo.ActualStartTime) >= ?
            ORDER BY
                wo.ActualStartTime DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_operator_logs(days_back: int = 7):
            """Fetch operator logs and shift performance"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT
                MAX(wo.ActualStartTime) as WorkDate,
                e.Name as OperatorName,
                e.Role as OperatorRole,
                s.Name as ShiftName,
                COUNT(wo.OrderID) as OrdersHandled,
                AVG(wo.ActualProduction * 100.0 / wo.Quantity) as AvgCompletionRate,
                SUM(wo.Scrap) as TotalScrap,
                wc.Name as WorkCenterName,
                m.Type as MachineType
            FROM 
                WorkOrders wo
            JOIN 
                Employees e ON wo.EmployeeID = e.EmployeeID
            JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            JOIN 
                WorkCenters wc ON wo.WorkCenterID = wc.WorkCenterID
            JOIN 
                Machines m ON wo.MachineID = m.MachineID
            WHERE 
                date(wo.ActualStartTime) >= ?
            GROUP BY
                date(wo.ActualStartTime), e.EmployeeID, s.ShiftID, wc.WorkCenterID,
                e.Name, e.Role, s.Name, wc.Name, m.Type
            ORDER BY
                WorkDate DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_defect_records(defect_type: str, days_back: int = 7):
            """Fetch individual defect occurrences for ONE defect type from the
            Defects table, with timestamps and full context. Returns one row per
            occurrence: check date/time, severity, quantity, location, recorded
            root cause, action taken, plus the product, machine, work center,
            operator, and shift involved. Use this for defect timelines and
            correlating defect timing against maintenance or downtime events.
            Newest first, capped at 100 rows."""
            cutoff_date = self._cutoff_date(days_back)

            query = """
            SELECT
                qc.Date as CheckDate,
                d.DefectType,
                d.Severity,
                d.Quantity as DefectQuantity,
                d.Location,
                d.RootCause,
                d.ActionTaken,
                p.Name as ProductName,
                m.Name as MachineName,
                wc.Name as WorkCenterName,
                e.Name as OperatorName,
                s.Name as ShiftName,
                wo.OrderID
            FROM
                Defects d
            JOIN
                QualityControl qc ON d.CheckID = qc.CheckID
            JOIN
                WorkOrders wo ON qc.OrderID = wo.OrderID
            JOIN
                Products p ON wo.ProductID = p.ProductID
            JOIN
                Machines m ON wo.MachineID = m.MachineID
            JOIN
                WorkCenters wc ON wo.WorkCenterID = wc.WorkCenterID
            JOIN
                Employees e ON wo.EmployeeID = e.EmployeeID
            JOIN
                Shifts s ON e.ShiftID = s.ShiftID
            WHERE
                d.DefectType = ?
                AND date(qc.Date) >= ?
            ORDER BY
                qc.Date DESC
            LIMIT 100
            """

            return self._execute_safe_query(query, (defect_type, cutoff_date))

        @tool
        def summarize_defect_distribution(defect_type: str, days_back: int = 7):
            """The ONLY sanctioned source for counts of one defect type.
            Returns SQL-computed totals of defect records and affected
            units (SUM of the Quantity column) grouped by machine, shift,
            calendar date, recorded root cause, and severity - one row per
            (Dimension, Item). Cite these numbers verbatim; never count
            raw rows from other tools yourself."""
            cutoff_date = self._cutoff_date(days_back)

            query = """
            SELECT 'ByMachine' as Dimension, m.Name as Item,
                COUNT(*) as DefectRecords, SUM(d.Quantity) as UnitsAffected
            FROM Defects d
            JOIN QualityControl qc ON d.CheckID = qc.CheckID
            JOIN WorkOrders wo ON qc.OrderID = wo.OrderID
            JOIN Machines m ON wo.MachineID = m.MachineID
            WHERE d.DefectType = ? AND date(qc.Date) >= ?
            GROUP BY m.MachineID
            UNION ALL
            SELECT 'ByShift', s.Name, COUNT(*), SUM(d.Quantity)
            FROM Defects d
            JOIN QualityControl qc ON d.CheckID = qc.CheckID
            JOIN WorkOrders wo ON qc.OrderID = wo.OrderID
            JOIN Employees e ON wo.EmployeeID = e.EmployeeID
            JOIN Shifts s ON e.ShiftID = s.ShiftID
            WHERE d.DefectType = ? AND date(qc.Date) >= ?
            GROUP BY s.ShiftID
            UNION ALL
            -- cast to text so this arm matches the TEXT of the other UNION
            -- arms; PostgreSQL will not union a DATE with a name column
            SELECT 'ByDate', CAST(date(qc.Date) AS TEXT), COUNT(*), SUM(d.Quantity)
            FROM Defects d
            JOIN QualityControl qc ON d.CheckID = qc.CheckID
            WHERE d.DefectType = ? AND date(qc.Date) >= ?
            GROUP BY date(qc.Date)
            UNION ALL
            SELECT 'ByRootCause', d.RootCause, COUNT(*), SUM(d.Quantity)
            FROM Defects d
            JOIN QualityControl qc ON d.CheckID = qc.CheckID
            WHERE d.DefectType = ? AND date(qc.Date) >= ?
            GROUP BY d.RootCause
            UNION ALL
            SELECT 'BySeverity', 'Severity ' || d.Severity, COUNT(*), SUM(d.Quantity)
            FROM Defects d
            JOIN QualityControl qc ON d.CheckID = qc.CheckID
            WHERE d.DefectType = ? AND date(qc.Date) >= ?
            GROUP BY d.Severity
            ORDER BY Dimension, DefectRecords DESC
            LIMIT 200
            """

            params = (defect_type, cutoff_date) * 5
            return self._execute_safe_query(query, params)

        @tool
        def get_recent_detections(machine_id: int, hours: int = 2):
            """Camera detections recorded by the live vision system for ONE
            machine in the last N hours. This is the CAMERA's own evidence -
            what the model actually saw on the line - and is separate from
            the Defects table, which holds human quality-control records.

            Returns one row per detection: timestamp, defect class, the
            model's confidence (0-1), the image filename, the work order
            running at the time, and how long inference took. Newest first,
            capped at 200 rows.

            Use this when investigating a defect burst flagged by the camera,
            to see exactly which detections triggered it and how confident
            the model was. Only detections at or above the 0.80 confidence
            gate are batched into an alert, but this returns every saved
            detection, including lower-confidence ones."""
            hours = int(hours)
            if hours < 0 or hours > 8760:
                raise ValueError("hours must be between 0 and 8760")

            if self.db_backend != "postgres":
                # A raw "no such table" error invites the model to conclude
                # that no defects were detected - a false finding rather
                # than a gap. Say what is actually true.
                return {
                    "success": False,
                    "error": (
                        "Camera detections are unavailable: this agent is running "
                        "against SQLite, and VisionDetections is written by the "
                        "vision pipeline into PostgreSQL. This is a tool limitation, "
                        "NOT evidence that no detections occurred - classify it as "
                        "'not exposed by available tools'."
                    ),
                }

            # The cutoff is computed here rather than in SQL on purpose:
            # "N hours ago" is spelled differently in SQLite and PostgreSQL,
            # and passing a plain timestamp as a parameter sidesteps the
            # difference entirely.
            cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')

            query = """
            SELECT
                Timestamp,
                DefectType,
                Confidence,
                ImageName,
                OrderID,
                InferenceTimeMs
            FROM
                VisionDetections
            WHERE
                MachineID = ?
                AND Timestamp >= ?
            ORDER BY
                Timestamp DESC
            LIMIT 200
            """
            return self._execute_safe_query(query, (int(machine_id), cutoff))

        # Shared with the Analyzer so both agents cite the same counts.
        self._summarize_defect_distribution_tool = summarize_defect_distribution
        # The Analyzer needs the camera evidence too, not just the Monitor.
        self._get_recent_detections_tool = get_recent_detections

        self.monitor_tools = [
            fetch_oee_metrics,
            fetch_downtime_events,
            fetch_historical_patterns,
            fetch_work_orders_context,
            fetch_operator_logs,
            fetch_defect_records,
            summarize_defect_distribution,
            get_recent_detections
        ]

    def _init_analyzer_tools(self):
        """Initialize Analyzer Agent tools - Identifies root causes and performs reasoning"""
        
        @tool
        def analyze_downtime_correlations(days_back: int = 7):
            """Analyze correlations between downtime and specific factors"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                dt.Reason as DowntimeReason,
                s.Name as ShiftName,
                e.Name as OperatorName,
                p.Name as ProductName,
                m.Type as MachineType,
                COUNT(*) as EventCount,
                AVG(dt.Duration) as AvgDuration,
                SUM(dt.Duration) as TotalDuration,
                wc.Name as WorkCenterName
            FROM 
                Downtimes dt
            JOIN 
                Machines m ON dt.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON m.WorkCenterID = wc.WorkCenterID
            LEFT JOIN 
                WorkOrders wo ON m.MachineID = wo.MachineID
            LEFT JOIN 
                Products p ON wo.ProductID = p.ProductID
            LEFT JOIN 
                Employees e ON wo.EmployeeID = e.EmployeeID
            LEFT JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            WHERE 
                date(dt.StartTime) >= ?
            GROUP BY
                dt.Reason, s.Name, e.Name, p.Name, m.Type, wc.Name
            ORDER BY
                TotalDuration DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def analyze_batch_changeover_time(days_back: int = 7):
            """Analyze batch changeover times vs benchmarks"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            WITH changeover_times AS (
                SELECT 
                    wo1.OrderID as PrevOrder,
                    wo2.OrderID as NextOrder,
                    wo1.ProductID as PrevProduct,
                    wo2.ProductID as NextProduct,
                    wo1.MachineID,
                    (julianday(wo2.ActualStartTime) - julianday(wo1.ActualEndTime)) * 24 as ChangeoverHours,
                    m.Name as MachineName,
                    m.Type as MachineType,
                    p1.Name as PrevProductName,
                    p2.Name as NextProductName,
                    s.Name as ShiftName
                FROM 
                    WorkOrders wo1
                JOIN 
                    WorkOrders wo2 ON wo1.MachineID = wo2.MachineID 
                    AND wo2.ActualStartTime > wo1.ActualEndTime
                JOIN 
                    Machines m ON wo1.MachineID = m.MachineID
                JOIN 
                    Products p1 ON wo1.ProductID = p1.ProductID
                JOIN 
                    Products p2 ON wo2.ProductID = p2.ProductID
                LEFT JOIN
                    Employees e2 ON wo2.EmployeeID = e2.EmployeeID
                LEFT JOIN 
                    Shifts s ON e2.ShiftID = s.ShiftID
                WHERE 
                    date(wo1.ActualEndTime) >= ?
                    AND date(wo2.ActualStartTime) >= ?
                    AND (julianday(wo2.ActualStartTime) - julianday(wo1.ActualEndTime)) * 24 < 24
                    AND (julianday(wo2.ActualStartTime) - julianday(wo1.ActualEndTime)) * 24 > 0
            )
            SELECT 
                MachineType,
                MachineName,
                PrevProductName,
                NextProductName,
                ShiftName,
                COUNT(*) as ChangeoverCount,
                AVG(ChangeoverHours * 60) as AvgChangeoverMinutes,
                MIN(ChangeoverHours * 60) as MinChangeoverMinutes,
                MAX(ChangeoverHours * 60) as MaxChangeoverMinutes,
                CASE 
                    WHEN AVG(ChangeoverHours * 60) > 120 THEN 'Excessive'
                    WHEN AVG(ChangeoverHours * 60) > 60 THEN 'Above Benchmark'
                    ELSE 'Acceptable'
                END as ChangeoverStatus
            FROM 
                changeover_times
            GROUP BY 
                MachineType, MachineName, PrevProductName, NextProductName, ShiftName
            ORDER BY
                AvgChangeoverMinutes DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date, cutoff_date))

        @tool
        def identify_performance_patterns(days_back: int = 7):
            """Identify patterns in machine and operator performance"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                m.Name as MachineName,
                m.Type as MachineType,
                e.Name as OperatorName,
                s.Name as ShiftName,
                wc.Name as WorkCenterName,
                COUNT(wo.OrderID) as TotalOrders,
                AVG(oee.OEE) as AvgOEE,
                AVG(oee.Availability) as AvgAvailability,
                AVG(oee.Performance) as AvgPerformance,
                AVG(oee.Quality) as AvgQuality,
                SUM(wo.Scrap) as TotalScrap,
                AVG(wo.ActualProduction * 100.0 / wo.Quantity) as AvgCompletionRate,
                COUNT(dt.Duration) as DowntimeEvents,
                SUM(dt.Duration) as TotalDowntime
            FROM 
                WorkOrders wo
            JOIN 
                Machines m ON wo.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON m.WorkCenterID = wc.WorkCenterID
            JOIN 
                Employees e ON wo.EmployeeID = e.EmployeeID
            JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            LEFT JOIN 
                OEEMetrics oee ON m.MachineID = oee.MachineID 
                AND date(oee.Date) = date(wo.ActualStartTime)
            LEFT JOIN 
                Downtimes dt ON m.MachineID = dt.MachineID 
                AND date(dt.StartTime) = date(wo.ActualStartTime)
            WHERE 
                date(wo.ActualStartTime) >= ?
            GROUP BY
                m.MachineID, e.EmployeeID, s.ShiftID, wc.Name
            ORDER BY
                AvgOEE ASC, TotalDowntime DESC
            LIMIT 100
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def analyze_quality_defects(days_back: int = 7, defect_type: str = None):
            """Analyze quality defects and their recorded root causes,
            grouped by defect type, root cause, product, machine, operator
            and shift. Always pass defect_type to restrict the analysis to
            the defect under investigation; omitting it returns every
            defect type in the plant, which is a very large payload only
            useful for deliberate cross-defect comparison."""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = self._cutoff_date(days_back)
            
            query = """
            SELECT 
                d.DefectType,
                d.Severity,
                d.Location,
                d.RootCause,
                d.ActionTaken,
                COUNT(*) as DefectCount,
                p.Name as ProductName,
                p.Category as ProductCategory,
                m.Name as MachineName,
                m.Type as MachineType,
                wc.Name as WorkCenterName,
                e.Name as OperatorName,
                s.Name as ShiftName,
                AVG(qc.DefectRate) as AvgDefectRate,
                AVG(qc.YieldRate) as AvgYieldRate
            FROM 
                Defects d
            JOIN 
                QualityControl qc ON d.CheckID = qc.CheckID
            JOIN 
                WorkOrders wo ON qc.OrderID = wo.OrderID
            JOIN 
                Products p ON wo.ProductID = p.ProductID
            JOIN 
                Machines m ON wo.MachineID = m.MachineID
            JOIN 
                WorkCenters wc ON wo.WorkCenterID = wc.WorkCenterID
            JOIN 
                Employees e ON wo.EmployeeID = e.EmployeeID
            JOIN 
                Shifts s ON e.ShiftID = s.ShiftID
            WHERE
                date(qc.Date) >= ?
                {defect_filter}
            GROUP BY
                d.DefectType, d.RootCause, p.ProductID, m.MachineID, e.EmployeeID, s.ShiftID,
                d.Severity, d.Location, d.ActionTaken, wc.Name
            ORDER BY
                DefectCount DESC, d.Severity DESC
            LIMIT 100
            """

            # The filter clause is a fixed literal; the value itself stays
            # a bound parameter.
            if defect_type:
                query = query.format(defect_filter="AND d.DefectType = ?")
                return self._execute_safe_query(query, (cutoff_date, defect_type))
            query = query.format(defect_filter="")
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def correlate_defects_with_maintenance(defect_type: str, days_back: int = 7):
            """Directly match each recorded defect of ONE type to the
            downtime events (maintenance appears as Reason values) that
            ended on the SAME machine within the 72 hours before the
            defect's quality check. Returns one row per defect-downtime
            pair with machine, reason, both timestamps, and the gap in
            hours. Use this for maintenance-defect correlation instead of
            eyeballing separate defect and downtime lists - it is the only
            tool that enforces same-machine, time-ordered matching."""
            cutoff_date = self._cutoff_date(days_back)

            query = """
            SELECT
                qc.Date as DefectTime,
                d.DefectType,
                d.Severity,
                m.Name as MachineName,
                m.Type as MachineType,
                dt.Reason as DowntimeReason,
                dt.StartTime as DowntimeStart,
                dt.EndTime as DowntimeEnd,
                ROUND((julianday(qc.Date) - julianday(dt.EndTime)) * 24, 1) as HoursBeforeDefect
            FROM
                Defects d
            JOIN
                QualityControl qc ON d.CheckID = qc.CheckID
            JOIN
                WorkOrders wo ON qc.OrderID = wo.OrderID
            JOIN
                Machines m ON wo.MachineID = m.MachineID
            JOIN
                Downtimes dt ON dt.MachineID = wo.MachineID
                AND dt.EndTime <= qc.Date
                AND dt.EndTime >= datetime(qc.Date, '-72 hours')
            WHERE
                d.DefectType = ?
                AND date(qc.Date) >= ?
            ORDER BY
                qc.Date DESC, HoursBeforeDefect ASC
            LIMIT 100
            """

            return self._execute_safe_query(query, (defect_type, cutoff_date))

        self.analyzer_tools = [
            analyze_downtime_correlations,
            analyze_batch_changeover_time,
            identify_performance_patterns,
            analyze_quality_defects,
            correlate_defects_with_maintenance,
            self._summarize_defect_distribution_tool,
            self._get_recent_detections_tool
        ]

    def _init_planner_tools(self):
        """Initialize Planner Agent tools - Suggests actionable plans and creates PDF reports"""
        
        @tool
        def create_action_plan(analysis_data: str, priority_level: str = "High"):
            """Create actionable improvement plan based on analysis"""
            action_plan = {
                "priority": priority_level,
                "timestamp": datetime.now().isoformat(),
                "analysis_summary": analysis_data[:500] + "..." if len(analysis_data) > 500 else analysis_data,
                "immediate_actions": [
                    "Review identified problem areas",
                    "Implement monitoring for critical metrics",
                    "Schedule maintenance for problem machines"
                ],
                "short_term_actions": [
                    "Standardize changeover procedures",
                    "Provide additional operator training",
                    "Optimize batch scheduling"
                ],
                "long_term_actions": [
                    "Invest in predictive maintenance systems",
                    "Upgrade critical equipment",
                    "Implement advanced quality control"
                ]
            }
            
            return action_plan

        @tool
        def generate_pdf_report(report_data: dict, filename: str = None):
            """Generate PDF report with analysis findings and action plans"""
            if not REPORTLAB_AVAILABLE:
                return {"error": "ReportLab not available for PDF generation"}
            
            if filename is None:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"MES_Analysis_Report_{timestamp}"
            
            # Remove .pdf extension if already present
            if filename.endswith('.pdf'):
                filename = filename[:-4]
            
            pdf_filename = filename + '.pdf'
            
            try:
                from pathlib import Path
                reports_dir = Path("reports")
                reports_dir.mkdir(exist_ok=True)
                filepath = reports_dir / pdf_filename
                
                # Create PDF document
                doc = SimpleDocTemplate(str(filepath), pagesize=A4)
                styles = getSampleStyleSheet()
                story = []
                
                # Title
                title_style = ParagraphStyle(
                    'CustomTitle',
                    parent=styles['Title'],
                    fontSize=24,
                    spaceAfter=30,
                    textColor=colors.darkblue,
                    alignment=TA_CENTER
                )
                
                story.append(Paragraph("Manufacturing Execution System Analysis Report", title_style))
                story.append(Spacer(1, 30))
                story.append(Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}", styles['Normal']))
                story.append(Spacer(1, 20))
                
                # Add executive summary if available
                if 'executive_summary' in report_data:
                    story.append(Paragraph("Executive Summary", styles['Heading1']))
                    story.extend(_markdown_to_flowables(
                        report_data['executive_summary'], styles))
                    story.append(PageBreak())
                
                # Add report content
                for section, content in report_data.items():
                    if section == 'executive_summary':
                        continue  # Already handled above
                        
                    # Format section title
                    section_title = section.replace('_', ' ').title()
                    story.append(Paragraph(section_title, styles['Heading1']))
                    story.append(Spacer(1, 12))
                    
                    # Everything goes through the markdown renderer, which
                    # escapes XML-unsafe characters and turns headings,
                    # bullets and pipe tables into real flowables. Nested
                    # values are unwrapped rather than str()'d - a bare
                    # str(dict) put Python's {'a': 1} repr, braces and
                    # quotes included, straight into the PDF.
                    story.extend(_render_report_value(content, styles))
                    story.append(Spacer(1, 20))
                
                # Add footer with timestamp
                story.append(Spacer(1, 30))
                story.append(Paragraph(f"Report generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}", styles['Normal']))
                
                # Build PDF
                doc.build(story)
                
                return {
                    "success": True,
                    "filename": filename,
                    "pdf_filename": pdf_filename,
                    "filepath": str(filepath),
                    "file_size": os.path.getsize(filepath)
                }
                
            except Exception as e:
                logger.error(f"PDF generation error: {e}")
                return {"error": f"Failed to generate PDF: {str(e)}"}

        self.planner_tools = [
            create_action_plan,
            generate_pdf_report
        ]

    def _init_verifier_tools(self):
        """Initialize Verifier Agent tools - Handles human validation only"""
        
        @tool
        def validate_findings(findings: dict, validation_criteria: dict = None):
            """Validate analysis findings with human-in-the-loop process"""
            if validation_criteria is None:
                validation_criteria = {
                    "min_confidence": 0.8,
                    "require_human_review": True,
                    "critical_threshold": 0.95
                }
            
            validation_result = {
                "timestamp": datetime.now().isoformat(),
                "findings_summary": str(findings)[:200] + "...",
                "validation_status": "pending_human_review",
                "confidence_score": 0.85,
                "requires_escalation": validation_criteria.get("require_human_review", True),
                "next_steps": [
                    "Human expert review required",
                    "Validate against historical patterns",
                    "Confirm with operations team"
                ]
            }
            
            return validation_result

        self.verifier_tools = [
            validate_findings
        ]

    def _init_executor_tools(self):
        """Initialize Executor Agent tools - Sends notifications and call MES API to execute validated actions"""
        
        @tool
        def send_email_notification(subject: str, message: str, pdf_filename: str = None, priority: str = "Normal"):
            """Send email notification using SES with optional PDF link"""
            result = self.execute_email_send(subject, message, pdf_filename)
            logger.info(f"Email notification sent: {subject}")
            return result

        self.executor_tools = [
            send_email_notification
        ]

    def _init_agents(self):
        """Initialize the specialized agents"""

        # Appended to every subagent prompt. The word cap is the single
        # biggest latency lever after tool payload size: output tokens are
        # generated at roughly 50-80/second, so an unbounded report costs a
        # minute of pure generation per agent, five times per run.
        OUTPUT_RULES = """

=== OUTPUT FORMAT RULES (mandatory) ===
- Maximum 600 words total. Be dense, not decorative.
- Use exactly these sections and nothing else:
  1. KEY FINDINGS (max 5 bullet points)
  2. SUPPORTING DATA (max 1 table, max 10 rows)
  3. GAPS / MISSING DATA (what you could not determine and why)
  4. HANDOFF NOTES (max 3 bullets for the next agent)
- No emoji, no ASCII-art charts, no decorative separators.
- Report only numbers that appear in tool results. Never compute
  totals, percentages, correlations, confidence percentages, or
  dollar amounts yourself. If a number was not returned by a tool,
  write "not available in data" instead.
- Express certainty only as HIGH / MEDIUM / LOW with a one-line reason.
- Every KEY FINDING must end with its data source in brackets:
  [source: <exact tool name>, <row count if known>, <date range>].
  The source must be the exact name of a tool called in this
  conversation (e.g. fetch_defect_records). A finding you cannot
  attribute to a tool result must not be stated.
- Never count, sum, or take percentages over raw rows yourself - that
  includes counting how many rows share a machine, shift, date, or
  cause. Cite counts only from tool columns that contain them
  (DefectRecords, DefectCount, EventCount, ...); for per-machine,
  per-shift, per-date, per-cause, or severity totals of a defect, use
  summarize_defect_distribution. A grouped result's ROW COUNT is not a
  record count - never present it as one.
- Row-level tools are capped (100 rows) and ordered: they give examples
  and timelines, never totals. If a result is at the cap, say so rather
  than treating it as the complete set.
- Defect rows are records, not units: one record may cover several units
  (Quantity column). Say "N defect records"; state a unit count only
  when it comes from summing Quantity in a tool result.
- Describe operator findings neutrally as associations ("records
  associated with operator X"). Never attribute fault to a named
  person; recommend reviewing procedures or conditions instead.
- If a tool call fails or a query is rejected, that data is
  unavailable: write "not available in data". Never estimate or
  extrapolate what the blocked query would have returned.
"""

        # Monitor Agent - Captures & contextualizes data
        self.monitor_agent = Agent(
            model=self.model,
            tools=self.monitor_tools + [self.execute_sql_tool],
            system_prompt="""You are the Monitor Agent for a Manufacturing Execution System (MES).

Your primary responsibilities:
1. **Capture Manufacturing Events**: Monitor OEE drops, line stoppages, and downtime events
2. **Fetch Historical Context**: Retrieve historical stoppage patterns, operator logs, and work orders
3. **Contextualize Data**: Provide relevant context including batch reports, maintenance records, and shift information

Key monitoring areas:
- OEE metrics and performance drops
- Production line stoppages and downtime events
- Tool changeover times and batch transitions
- Operator performance and shift patterns
- Work order completion rates and delays

When analyzing events, always:
- Fetch relevant historical patterns for comparison
- Include operator, shift, and product context
- Identify time-based patterns (hour, day, shift)
- Correlate events with maintenance schedules
- Provide comprehensive context for analysis

Focus on capturing complete operational context to enable effective root cause analysis.

DATABASE FACTS: There is no Maintenance, maintenance_log, or CMMS table. Maintenance events are recorded as Reason values (e.g. 'Scheduled Maintenance', 'Cleaning', 'Software Error') inside the Downtimes data, which fetch_downtime_events and fetch_historical_patterns already return. Never query tables not returned by your tools.""" + OUTPUT_RULES
        )
        
        # Analyzer Agent - Identifies root causes and performs reasoning
        self.analyzer_agent = Agent(
            model=self.model,
            tools=self.analyzer_tools + [self.execute_sql_tool],
            system_prompt="""You are the Analyzer Agent for a Manufacturing Execution System (MES).

Your primary responsibilities:
1. **Root Cause Analysis**: Identify primary and secondary causes of manufacturing issues
2. **Correlation Analysis**: Find relationships between downtime, operators, shifts, and products
3. **Performance Reasoning**: Analyze excessive batch changeover times vs benchmarks
4. **Pattern Recognition**: Identify systematic issues across machines, products, and processes

Analysis focus areas:
- Correlation between downtime and specific shift/operator/product combinations
- Excessive batch changeover time analysis vs industry benchmarks
- Machine performance patterns and efficiency trends
- Quality defect patterns and their root causes
- Systematic vs random failure analysis

Your reasoning process should:
1. Start with the most impactful issues (highest cost, frequency, or risk)
2. Look for statistical correlations and patterns
3. Consider multiple contributing factors
4. Differentiate between symptoms and root causes
5. Rate certainty as HIGH / MEDIUM / LOW based on evidence strength
6. Recommend data-driven solutions

Base every claim on tool-returned data and provide actionable insights for the planning phase.

DATABASE FACTS: There is no Maintenance, maintenance_log, quality_defects, or CMMS table. Maintenance events are recorded as Reason values inside the Downtimes data returned by analyze_downtime_correlations. For maintenance-defect correlation use correlate_defects_with_maintenance, which enforces same-machine, time-ordered matching, rather than eyeballing separate defect and downtime lists. Never query tables not returned by your tools.""" + OUTPUT_RULES
        )
        
        # Planner Agent - Suggests actionable plans and creates PDF reports
        self.planner_agent = Agent(
            model=self.model,
            tools=self.planner_tools,
            system_prompt="""You are the Planner Agent for a Manufacturing Execution System (MES).

Your primary responsibilities:
1. **Action Plan Creation**: Develop prioritized, actionable improvement plans in natural language human readable format
2. **PDF Report Generation**: Create comprehensive PDF reports with findings and recommendations in good human readable format only
3. **Resource Planning**: Estimate resources, timelines, and costs for improvements
4. **Implementation Strategy**: Provide step-by-step implementation guidance

When creating action plans:
- Prioritize by impact (quality, cost, safety, efficiency)
- Provide clear timelines (immediate, short-term, long-term)
- Specify required resources and responsibilities
- Include success metrics and KPIs
- Consider implementation feasibility and risk

Plan structure should include:
1. **Immediate Actions** (0-30 days): Quick wins and critical fixes
2. **Short-term Actions** (1-3 months): Process improvements and training
3. **Long-term Actions** (3-12 months): Strategic investments and upgrades

For PDF reports, include:
- Executive summary with key findings
- Detailed analysis results
- Prioritized action plans with timelines
- Implementation roadmap and success metrics

When generating PDF reports, always return the filename in your response so it can be passed to the Executor Agent for email notifications.

Grounding rule: propose only actions that follow from findings actually
present in the analysis you were given. Owners, departments, budgets, and
resource-hour figures are not in the data - if you name one, mark it as a
proposal requiring human assignment, never as an established fact.

Always focus on measurable, actionable recommendations that improve manufacturing performance.""" + OUTPUT_RULES
        )
        
        # Verifier Agent - Handles human validation only
        self.verifier_agent = Agent(
            model=self.model,
            tools=self.verifier_tools,
            system_prompt="""You are the Verifier Agent for a Manufacturing Execution System (MES).

Your primary responsibilities:
1. **Human-in-the-Loop Validation**: Facilitate human expert review of AI findings
2. **Quality Assurance**: Validate analysis findings against established criteria
3. **Alert Management**: Create validation reports for monitoring dashboards

Validation triggers:
- Critical OEE drops (below 60%)
- Extended downtime events (>2 hours)
- Quality issues with high severity (>3/5)
- Maintenance overdue warnings
- Unusual pattern detection

Validation process:
1. Check findings against historical baselines
2. Assess confidence levels of analysis
3. Determine need for human expert review
4. Escalate critical issues appropriately
5. Track validation outcomes for continuous improvement

Human validation criteria:
- Complex root cause scenarios
- High-impact business decisions
- Safety-related findings
- Strategic investment recommendations
- Unusual or unprecedented patterns

Always maintain audit trails and ensure validation results are properly documented. 
Note: Email notifications are handled by the Executor Agent.""" + OUTPUT_RULES
        )

        # Executor Agent - Sends email notification and call MES APIs to execute actions
        self.executor_agent = Agent(
            model=self.model,
            tools=self.executor_tools,
            system_prompt="""You are the Executor Agent for a Manufacturing Execution System (MES).

Your primary responsibilities:
1. **Action Plan Execution**: Transform human understandable action plan into MES specific technical action items
2. **Implementation Strategy**: Receive implementation strategy in terms of medium, short term and long term and take action as appropriate 
3. **Email Generation**: Receive comprehensive PDF reports with findings and recommendations and send it in a summarized as well as detailed text through one email
4. **MES API Execution**: Based on actionable plan, execute MES API for immediate actionable item

When executing action plans:
- Accept actionable plan from planner agent
- Draft email to emphasize on four factors (quality, cost, safety, efficiency)
- Send only one email for short-term and long-term action items
- Execute MES API for immediate action item

Email report should include:
1. Detail of all short-term and long-term actionable items
2. Provide detail of which manufacturing department needs to take the action
3. Provide summary of all the issues, findings, root cause analysis
4. Detailed report attached as received from planner agent

When sending email notifications, it is mandatory to pass PDF filename that is provided from the Planner Agent, include it in the send_email_notification call to attach the PDF link in format(https://dfmw0zqekwl4n.cloudfront.net/proxy/8501/pdf=pdf_filename.pdf?pdf=pdf_filename.pdf) to the email.

The email system may run in dry-run mode. Report the notification status
exactly as the tool returns it: if the result says dry run, state plainly
that no email was actually sent and the draft is a preview. Never claim
delivery to recipients that the tool result does not confirm.

Always focus on clear and concise email body with actionable recommendations, ownership, timeline and risks if not done on time.""" + OUTPUT_RULES
        )

        # Wire the live tracer into every sub-agent so the dashboard can watch
        # their streamed messages, tool calls and results in real time.
        attach_tracer(self.monitor_agent, "Monitor", self.tracer)
        attach_tracer(self.analyzer_agent, "Analyzer", self.tracer)
        attach_tracer(self.planner_agent, "Planner", self.tracer)
        attach_tracer(self.verifier_agent, "Verifier", self.tracer)
        attach_tracer(self.executor_agent, "Executor", self.tracer)

    def cancel(self):
        """Ask an in-flight run on this manager to stop as soon as it can.

        The conversational agent stops during model streaming or before its
        next tool. Structured analysis stops at its next delegation or query
        checkpoint.
        Safe to call from another thread, and safe when nothing is running.
        """
        self._cancelled.set()
        if self._chat_running.is_set():
            self.conversational_agent.cancel()
        logger.info("Cancellation requested for the current run")

    def _check_cancelled(self):
        """Raise RunCancelled if this run has been cancelled. Cheap enough
        to call at every delegation/query boundary."""
        if self._cancelled.is_set():
            raise RunCancelled("Investigation cancelled by the user")

    def _cancelled_result(self, defect_type, days_back, scope_text, start_time):
        """The outcome dict for a deliberately cancelled run."""
        self.tracer.run_end("cancelled")
        end_time = datetime.now()
        return {
            'defect_type': defect_type,
            'analysis_period': days_back,
            'analysis_scope': {'scope_summary': scope_text},
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'total_duration': (end_time - start_time).total_seconds(),
            'supervisor_orchestration': '',
            'status': 'cancelled',
        }

    def _reset_conversations(self):
        """Clear every agent's message history so a run starts clean."""
        agents = [
            ("supervisor", getattr(self, "supervisor_agent", None)),
            ("monitor", self.monitor_agent),
            ("analyzer", self.analyzer_agent),
            ("planner", self.planner_agent),
            ("verifier", self.verifier_agent),
            ("executor", self.executor_agent),
        ]
        for name, agent_obj in agents:
            if agent_obj is None:
                continue
            try:
                messages = getattr(agent_obj, "messages", None)
                if messages is not None:
                    del messages[:]
            except Exception as e:
                logger.warning(f"Could not reset {name} conversation: {e}")

    def _call_agent_with_retry(self, agent_name: str, agent_obj, prompt: str):
        """Run one subagent turn, retrying on failure.

        Necessary companion to the bounded API timeout: a timeout raises
        where it previously hung, and a bare exception reaches the
        supervisor as a tool error, which it then improvises around. Once
        the attempts are spent this hands back an explicit "unavailable"
        string the supervisor is instructed to report as a gap.

        MES_AGENT_MAX_ATTEMPTS (default 3) sets the budget.
        """
        # Cancellation checkpoint: abandoning a run between phases skips the
        # remaining agents entirely rather than paying for all five.
        self._check_cancelled()
        max_attempts = self._agent_max_attempts
        last_error = None
        for attempt in range(1, max_attempts + 1):
            final = attempt == max_attempts
            try:
                return agent_obj(prompt)
            except RunCancelled:
                raise  # a cancelled run is not a failure to retry - unwind
            except MaxTokensReachedException as e:
                # Not a failure: the model filled max_tokens mid-reply and the
                # partial message is already in history, so calling again
                # continues it (the SDK's documented recovery). Say so plainly
                # rather than showing the trace a scary error. This is also why
                # extra attempts help here where they would not help a genuine
                # error - each one resumes rather than restarts.
                last_error = e
                logger.warning(
                    f"{agent_name} agent hit the output token limit "
                    f"(attempt {attempt}/{max_attempts})"
                    + (" - giving up" if final else " - continuing the reply"))
                self.tracer.error(
                    agent_name.capitalize(),
                    f"output token limit reached on attempt {attempt}/{max_attempts}"
                    + ("" if final else " - continuing the truncated reply")
                    + " (raise MES_MAX_TOKENS, or MES_AGENT_MAX_ATTEMPTS to keep"
                      " continuing, if this recurs)")
            except Exception as e:
                last_error = e
                logger.warning(
                    f"{agent_name} agent attempt {attempt}/{max_attempts} failed: {e}"
                    + (" - giving up" if final else " - retrying"))
                self.tracer.error(
                    agent_name.capitalize(),
                    f"attempt {attempt}/{max_attempts} failed: {type(e).__name__}: {e}")
        return (f"[{agent_name} agent unavailable after {max_attempts} attempts: "
                f"{last_error}. Proceed with available information and state this "
                f"gap explicitly.]")

    def _call_supervisor_for_chat(self, request: str) -> str:
        """Delegate a chat question to the supervisor and return its text.

        Two things the bare call in chat_agent.py did not do. It resets the
        workflow agents first: the supervisor and its five subagents are a
        work engine, not a conversation, so without this the third question
        of a session re-reads the first two answers' entire transcripts,
        tool results included. And it goes through _call_agent_with_retry,
        so a token-limit or timeout inside the supervisor comes back as the
        documented "unavailable" sentence rather than as a raw tool error
        the chat agent has to improvise around.

        The conversational agent's own history is deliberately untouched -
        that is what makes follow-up questions work.

        Refuses past MES_CHAT_SUPERVISOR_CALLS delegations for one question,
        so a model that decides to "check one more thing" cannot quietly turn
        a single chat message into several multi-minute billed workflows.
        """
        if self._chat_supervisor_calls >= self._chat_supervisor_budget:
            logger.warning(
                "chat turn tried delegation %d; budget is %d - refusing",
                self._chat_supervisor_calls + 1, self._chat_supervisor_budget)
            self.tracer.error(
                "Chat",
                f"supervisor delegation budget of {self._chat_supervisor_budget} "
                f"reached for this question - answering from what was gathered "
                f"(raise MES_CHAT_SUPERVISOR_CALLS to allow more)")
            return (f"[The MES supervisor has already run "
                    f"{self._chat_supervisor_calls} time(s) for this question, "
                    f"which is the limit. Do not call this tool again. Answer the "
                    f"user now using the findings you already have; if something "
                    f"is still missing, say plainly what it is and invite them to "
                    f"ask for it as a follow-up.]")

        self._chat_supervisor_calls += 1
        logger.info("chat delegating to supervisor (%d/%d)",
                    self._chat_supervisor_calls, self._chat_supervisor_budget)
        self._reset_conversations()
        result = self._call_agent_with_retry("supervisor", self.supervisor_agent, request)
        if isinstance(result, str):
            return result          # the "unavailable" sentence
        try:
            return result.message["content"][0]["text"]
        except (AttributeError, KeyError, IndexError, TypeError):
            # A reply whose first block is not text (e.g. an unresolved tool
            # use) would otherwise raise inside the tool and reach the chat
            # agent as a stack trace.
            return ("[supervisor returned no readable text. Report this as a gap "
                    "rather than answering from memory.]")

    def prepare_chat_turn(self):
        """Get the conversational agent ready for one more turn.

        Trims its history so a long session does not re-send every earlier
        report, refreshes the per-question delegation budget, and clears any
        cancel flag left over from a previous run.
        """
        self._cancelled.clear()
        self._chat_supervisor_calls = 0
        messages = getattr(self.conversational_agent, "messages", None)
        if not messages or len(messages) <= self._chat_history_limit:
            return
        # Cut from the front, then walk forward to the first message that can
        # legally begin a conversation: a user message that is not a
        # toolResult. Sending a toolResult whose toolUse has been trimmed away
        # is a 400 from the API, so if no safe cut point exists, cut nothing.
        start = len(messages) - self._chat_history_limit
        while start < len(messages):
            message = messages[start]
            content = message.get("content") or []
            is_tool_result = any(isinstance(block, dict) and "toolResult" in block
                                 for block in content)
            if message.get("role") == "user" and not is_tool_result:
                break
            start += 1
        if start < len(messages):
            del messages[:start]

    def _init_supervisor_agent(self):
        """Initialize the Supervisor Agent that orchestrates the workflow"""

        @tool
        def call_monitor_agent(prompt: str):
            """Call the Monitor Agent to capture operational data"""
            return self._call_agent_with_retry("monitor", self.monitor_agent, prompt)

        @tool
        def call_analyzer_agent(prompt: str):
            """Call the Analyzer Agent to perform root cause analysis"""
            return self._call_agent_with_retry("analyzer", self.analyzer_agent, prompt)

        @tool
        def call_planner_agent(prompt: str):
            """Call the Planner Agent to create action plans"""
            return self._call_agent_with_retry("planner", self.planner_agent, prompt)
        
        @tool
        def call_verifier_agent(prompt: str):
            """Call the Verifier Agent to validate findings"""
            return self._call_agent_with_retry("verifier", self.verifier_agent, prompt)

        @tool
        def call_executor_agent(prompt: str):
            """Call the Executor Agent to execute actions"""
            return self._call_agent_with_retry("executor", self.executor_agent, prompt)
        
        self.supervisor_agent = Agent(
            model=self.model,
            tools=[call_monitor_agent, call_analyzer_agent, call_planner_agent, call_verifier_agent, call_executor_agent],
            system_prompt="""You are the Supervisor Agent for the Manufacturing Execution System (MES) AI workflow.

Your primary responsibility is to orchestrate the complete defect analysis workflow by coordinating five specialized agents:

1. **Monitor Agent**: Captures operational data and contextualizes manufacturing events
2. **Analyzer Agent**: Performs root cause analysis and identifies correlations
3. **Planner Agent**: Creates actionable improvement plans and generates reports and return PDF File Name
4. **Verifier Agent**: Validates findings and manages human validation
5. **Executor Agent**: Executes action plans and sends email notifications

**Workflow Process:**
1. Receive defect analysis request with defect type, time period, and analysis scope parameters
2. Call Monitor Agent to capture comprehensive operational data based on enabled scope
3. Call Analyzer Agent to perform root cause analysis using monitoring data and scope
4. Call Planner Agent to create action plans based on analysis results and scope
5. Call Verifier Agent to validate findings within scope
6. Call Executor Agent to pass PDF file Name, execute immediate actions and send email notifications with PDF link in format(https://dfmw0zqekwl4n.cloudfront.net/proxy/8501/pdf=pdf_filename.pdf?pdf=pdf_filename.pdf)
7. Compile complete analysis results with all agent outputs

**Analysis Scope Parameters:**
- include_oee: Enable/disable OEE performance analysis
- include_downtime: Enable/disable downtime and stoppages analysis
- include_changeover: Enable/disable batch changeover analysis
- include_maintenance: Enable/disable maintenance correlation analysis

**Key Responsibilities:**
- Ensure proper data flow between agents with scope considerations
- Maintain analysis context throughout the workflow
- Coordinate timing and sequencing of agent activities
- Compile comprehensive results from all agents
- Handle error recovery and workflow continuity
- Provide executive summary of complete analysis
- Respect analysis scope limitations and focus areas
- Use Executor Agent for one email notification and action execution
- Pass PDF filename from Planner Agent to Executor Agent for email notifications

**Critical Workflow Note:**
When calling the Planner Agent, extract the PDF filename from the response and pass it to the Executor Agent when calling for email notifications. This ensures the PDF link is included in the email body.

**Output Format:**
Always return a structured analysis result containing:
- Defect type and analysis parameters including scope settings
- Monitoring results with operational context within enabled scope
- Root cause analysis with confidence levels for enabled areas
- Action plans with timelines and resources for enabled scope
- Verification results with validation status
- Execution results with notification status
- Executive summary with key findings and recommendations

Focus on ensuring each agent receives appropriate context and scope parameters, and that the complete workflow produces actionable, validated insights for manufacturing quality improvement within the specified analysis scope. All email notifications should be handled through the Executor Agent with proper PDF filename passing.

=== OUTPUT RULES (mandatory) ===
- No emoji, no ASCII-art charts, no decorative separators.
- Keep the whole report under 2500 words so it always fits the output
  limit. Your final report is a synthesis for a human domain expert,
  not a transcript: never restate a subagent's report wholesale; carry
  over the load-bearing findings and numbers, attribute each to its
  agent, and leave full detail to the per-agent reports.
- Report only numbers that appear in tool results or subagent reports.
  Never compute totals, percentages, correlations, confidence
  percentages, or dollar amounts yourself. If a number was not
  returned by a tool, write "not available in data" instead.
- Preserve exact numbers from subagent reports verbatim. Never
  recompute, re-split, or restate counts (such as per-machine splits);
  copy them as given, with their sources.
- Never compare values against industry standards, benchmarks, or
  "world-class" figures unless those values appear in tool results.
- Express certainty only as HIGH / MEDIUM / LOW with a one-line reason.
- If a subagent comes back as "[<name> agent unavailable after 2
  attempts...]", that phase did not run: report it as a gap in section
  5 and cap the certainty of anything that depended on it at LOW.
  Never present that phase's conclusions as if they had been produced.
- When two subagents report conflicting numbers for the same fact, do
  not present either as validated: put the conflict in Data Reliability
  Flags, cap dependent certainty at LOW, and name the tool result that
  would resolve it.
- Defect counts are record counts, not unit counts, unless a tool
  summed the Quantity column; keep that distinction wherever counts
  appear.
- Never attribute fault to named individuals; keep operator references
  neutral and aim recommendations at processes and conditions.
- Structure the final report with exactly these numbered sections, in
  this order, each heading followed by a line
  "Source: <the tools/agents the section draws on>":
  1. Defect Occurrence Summary (summary table by machine or product -
     never one row per occurrence)
  2. Maintenance Correlation Findings
  3. Root Cause Hypotheses (ranked; each with a WHY mechanism and
     HIGH/MEDIUM/LOW certainty)
  4. Data Reliability Flags
  5. Gaps / Missing Data
  6. Action Plan (immediate / short-term / long-term, from the Planner)
  7. Verification Outcome and Conditions (from the Verifier)
  8. Notification Status (from the Executor)"""
        )

        attach_tracer(self.supervisor_agent, "Supervisor", self.tracer)

    def investigate_detection_burst(self, machine_id: int, defect_type: str,
                                    detection_count: int, window_start: str,
                                    window_end: str, order_id=None,
                                    detections: list = None) -> dict:
        """Investigate a burst of camera detections flagged by the CV pipeline.

        This is the agent half of CONTRACTS.md §6, reached over HTTP from the
        bridge's analyze_batch. Deliberately narrower than run_defect_analysis:
        an alert wants a root cause fast, so the supervisor is told to run the
        Monitor and Analyzer phases only. Planning, verification and
        notification are what the full workflow is for.

        Returns {'status', 'report', 'duration_s'} and never raises - a failed
        investigation must still let the bridge mark its alert 'failed'.
        """
        start = datetime.now()
        self._cancelled.clear()
        self._reset_conversations()
        self.tracer.reset()
        self.tracer.run_start(
            f"Defect burst: {defect_type} on machine {machine_id}",
            params={"machine_id": machine_id, "defect_type": defect_type,
                    "detections": detection_count,
                    "window": f"{window_start} → {window_end}"},
        )

        sample = ""
        for d in (detections or [])[:10]:
            sample += (f"\n              - {d.get('timestamp')} {d.get('class')} "
                       f"confidence {d.get('confidence')} ({d.get('image_name')})")

        prompt = f"""
            A live camera on the production line has flagged a burst of defects.
            Investigate why it happened and report the most likely root cause.

            What the camera saw:
            - Machine ID: {machine_id}
            - Work order: {order_id if order_id is not None else 'none active'}
            - Dominant defect class: {defect_type}
            - Detections above the 0.80 confidence gate: {detection_count}
            - Time window: {window_start} to {window_end}
            {'Sample detections:' + sample if sample else ''}

            How to investigate:
            1. Call the Monitor Agent. Tell it to use get_recent_detections for
               machine {machine_id} to see the camera's own evidence, and to
               pull the surrounding MES context - the work order running at the
               time, recent downtime and maintenance on that machine, and the
               quality-control records for '{defect_type}'.
            2. Call the Analyzer Agent to determine the most likely root cause,
               using correlate_defects_with_maintenance for the maintenance
               link and summarize_defect_distribution for any counts.
            3. Do NOT call the Planner, Verifier or Executor agents. This is a
               fast root-cause alert, not the full improvement workflow.

            Then write the report yourself, under 600 words, with exactly these
            sections:
            1. What the camera saw (the burst, in plain terms)
            2. Machine and work-order context
            3. Most likely root cause (ranked, each with a WHY mechanism and
               HIGH/MEDIUM/LOW certainty)
            4. What a human should check first
            5. Gaps / missing data

            The camera detections are evidence that something occurred; they do
            not by themselves explain why. Ground every claim in a tool result.
            """

        try:
            response = self.supervisor_agent(prompt)
            self._check_cancelled()
            report = response.message['content'][0]['text']
            duration = (datetime.now() - start).total_seconds()
            self.tracer.run_end("completed")
            logger.info("Burst investigation finished in %.0fs (%s chars)",
                        duration, len(report))
            return {"status": "completed", "report": report, "duration_s": duration}
        except RunCancelled:
            self.tracer.run_end("cancelled")
            return {"status": "cancelled", "report": "", "duration_s":
                    (datetime.now() - start).total_seconds()}
        except Exception as e:
            logger.exception("Burst investigation failed")
            self.tracer.error(None, f"{type(e).__name__}: {e}")
            self.tracer.run_end("failed", error=str(e))
            return {"status": "failed", "report": f"Investigation failed: {e}",
                    "duration_s": (datetime.now() - start).total_seconds()}

    def run_defect_analysis(self, defect_type: str, days_back: int = 7, include_oee: bool = True,
                           include_downtime: bool = True, include_changeover: bool = True, 
                           include_maintenance: bool = True):
        """Run comprehensive defect analysis using supervisor agent orchestration"""
        
        scope_summary = []
        if include_oee:
            scope_summary.append("OEE Analysis")
        if include_downtime:
            scope_summary.append("Downtime Analysis")
        if include_changeover:
            scope_summary.append("Changeover Analysis")
        if include_maintenance:
            scope_summary.append("Maintenance Correlation")
        
        scope_text = ", ".join(scope_summary) if scope_summary else "Basic Analysis"

        start_time = datetime.now()

        # This manager is built once and reused for every run, so a previous
        # cancellation must not carry over and abort the next run instantly.
        self._cancelled.clear()

        # Start every run from a clean conversation. The six Agent objects
        # live for the process, and Strands appends each turn to
        # agent.messages — so without this, run 2 re-reads run 1's entire
        # transcript (including its tool results) and every run is slower
        # and costlier than the last.
        self._reset_conversations()

        # Open a fresh trace for this run so the dashboard shows only this run.
        self.tracer.reset()
        self.tracer.run_start(
            f"Defect analysis: {defect_type}",
            params={"defect_type": defect_type, "days_back": days_back, "scope": scope_text},
        )

        try:
            # Verified data context: without this, an empty or thin window
            # reads to the agents like a monitoring-infrastructure failure
            # and they speculate at length; with it, emptiness has a stated,
            # boring cause and the run ends quickly.
            data_context = ""
            try:
                stats = self.get_defect_window_stats(defect_type, days_back)
                if stats.get("success") and stats.get("rows"):
                    row = stats["rows"][0]
                    window_start = self._cutoff_date(days_back)
                    window_end = self.data_anchor_date.strftime('%Y-%m-%d')
                    data_context = f"""
            Data context (verified directly against the database just before this run):
            - Analysis window: {window_start} to {window_end}. Windows count back from
              the newest record in the database ({window_end}), not from today's date.
              State these window dates exactly; never infer or report a different range.
            - '{defect_type}' records inside this window: {row.get('WindowCount')}
            - '{defect_type}' records in the entire database: {row.get('TotalCount')}
            - Most recent '{defect_type}' record: {row.get('LastOccurrence') or 'none'}
            If records cluster at the window's start date, treat that as a
            window-boundary artifact - data from before the boundary was not
            queried - not as evidence of a sudden process event.
            If the window holds zero records, the correct finding is that the
            window does not overlap this defect's data - report that plainly.
            Do not conclude data-collection or infrastructure failure, and
            correct any subagent that does.
            """
            except Exception as e:
                logger.warning(f"Window stats pre-check failed: {e}")

            # Create comprehensive prompt for supervisor agent
            supervisor_prompt = f"""
            Execute comprehensive defect analysis workflow for defect type '{defect_type}' over the last {days_back} days of recorded data.
            {data_context}
            Analysis Scope Configuration:
            - OEE Analysis: {'Enabled' if include_oee else 'Disabled'}
            - Downtime Analysis: {'Enabled' if include_downtime else 'Disabled'}
            - Changeover Analysis: {'Enabled' if include_changeover else 'Disabled'}
            - Maintenance Correlation: {'Enabled' if include_maintenance else 'Disabled'}
            
            Execute the following workflow steps:
            
            1. **Monitor Phase**: Call Monitor Agent to capture operational data
               - Focus on {defect_type} defect occurrences and context
               - Include enabled analysis areas: {scope_text}
               - Gather historical patterns and operational context
            
            2. **Analysis Phase**: Call Analyzer Agent for root cause analysis
               - Analyze monitoring data for {defect_type} root causes
               - Focus on enabled correlation areas: {scope_text}
               - Provide statistical confidence and impact assessment
            
            3. **Planning Phase**: Call Planner Agent to create action plans
               - Develop comprehensive improvement plans for {defect_type}
               - Address enabled improvement areas: {scope_text}
               - Include immediate, short-term, and long-term actions
               - Generate PDF report and capture the filename for email notifications
            
            4. **Verification Phase**: Call Verifier Agent to validate findings
               - Validate analysis results and action plans
               - Determine notification requirements
               - Assess need for human expert review
            
            5. **Execution Phase**: Call Executor Agent to execute actions
               - Execute immediate action items
               - Send email notifications with detailed reports
               - Include PDF filename from Planner Agent response for email link
               - Coordinate with manufacturing departments
            
            Ensure each agent receives appropriate context from previous phases and respects the analysis scope limitations.
            
            IMPORTANT: Extract the PDF filename from the Planner Agent response and pass it to the Executor Agent for email notifications.
            
            Compile comprehensive results including all agent outputs and provide executive summary.
            """
            
            # Call supervisor agent to orchestrate the workflow
            supervisor_response = self.supervisor_agent(supervisor_prompt)

            # Run-level cancellation guard. The checkpoints inside subagent
            # delegations raise RunCancelled, but the SDK stringifies
            # exceptions raised inside the call_*_agent tools and hands them
            # back to the supervisor as ordinary tool errors - so a cancelled
            # run's supervisor can keep orchestrating dead agents and write a
            # confident report about an investigation that never happened.
            # This check is the guarantee that a cancelled run can never come
            # back as 'completed', whatever the SDK did internally.
            self._check_cancelled()

            end_time = datetime.now()
            
            # Extract supervisor response content
            supervisor_results = supervisor_response.message['content'][0]['text']

            # Render the Supervisor's own final report - the eight-section
            # synthesis a human actually reads. Previously the only PDF a run
            # produced was the Planner's intermediate action plan, so the
            # report shown in the dashboard existed nowhere on disk. A PDF
            # failure must not lose a completed analysis, so it is reported
            # rather than raised.
            final_pdf_name = None
            try:
                final_pdf = render_markdown_report_pdf(
                    supervisor_results,
                    filename=f"MES_Final_Report_{start_time.strftime('%Y%m%d_%H%M%S')}")
                final_pdf_name = final_pdf.name
            except Exception as pdf_error:
                logger.warning(f"Final report PDF generation failed: {pdf_error}")

            # Compile comprehensive results
            analysis_results = {
                'defect_type': defect_type,
                'analysis_period': days_back,
                'analysis_scope': {
                    'include_oee': include_oee,
                    'include_downtime': include_downtime,
                    'include_changeover': include_changeover,
                    'include_maintenance': include_maintenance,
                    'scope_summary': scope_text
                },
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'total_duration': (end_time - start_time).total_seconds(),
                'supervisor_orchestration': supervisor_results,
                'report_pdf': final_pdf_name,
                'workflow_status': 'completed',
                'executive_summary': f"""
                Comprehensive defect analysis completed for {defect_type} defects over {days_back} days using supervisor agent orchestration.
                Analysis scope: {scope_text}
                
                The supervisor agent successfully coordinated all specialized agents to:
                - Monitor operational data and manufacturing events
                - Analyze root causes and correlations
                - Plan actionable improvement strategies
                - Verify findings and validate recommendations
                - Execute immediate actions and send notifications
                
                Total analysis duration: {(end_time - start_time).total_seconds():.2f} seconds
                
                Detailed results from each agent phase are included in the supervisor orchestration output.
                """,
                'status': 'completed'
            }

            self.tracer.run_end("completed")
            return analysis_results

        except RunCancelled:
            # The user abandoned this run. Expected, not an error: log
            # quietly and report 'cancelled' so the UI never shows a scary
            # failure for a deliberate action.
            logger.info("Defect analysis cancelled by the user")
            return self._cancelled_result(defect_type, days_back, scope_text, start_time)

        except Exception as e:
            if self._cancelled.is_set():
                # Cancelling lands mid-tool-call more often than not, which
                # leaves the SDK's conversation inconsistent (a tool_use with
                # no matching tool_result) and makes the next API call fail
                # with a 400. That error is a consequence of the cancel, not
                # a real failure, and the next run starts from a cleared
                # conversation anyway - so report the user's intent.
                logger.info(f"Run cancelled; ignoring downstream error: {e}")
                return self._cancelled_result(defect_type, days_back, scope_text, start_time)
            logger.error(f"Error in supervisor-orchestrated defect analysis workflow: {e}")
            self.tracer.error(None, f"{type(e).__name__}: {e}")
            self.tracer.run_end("failed", error=str(e))
            return {
                'defect_type': defect_type,
                'analysis_period': days_back,
                'analysis_scope': {
                    'include_oee': include_oee,
                    'include_downtime': include_downtime,
                    'include_changeover': include_changeover,
                    'include_maintenance': include_maintenance,
                    'scope_summary': scope_text
                },
                'start_time': start_time.isoformat(),
                'end_time': datetime.now().isoformat(),
                'error': str(e),
                'status': 'failed'
            }

    def get_monitor_agent(self):
        """Get the monitor agent"""
        return self.monitor_agent
    
    def get_analyzer_agent(self):
        """Get the analyzer agent"""
        return self.analyzer_agent
    
    def get_planner_agent(self):
        """Get the planner agent"""
        return self.planner_agent
    
    def get_executor_agent(self):
        """Get the Executor agent"""
        return self.executor_agent
    
    def get_verifier_agent(self):
        """Get the verifier agent"""
        return self.verifier_agent
    
    def get_supervisor_agent(self):
        """Get the supervisor agent"""
        return self.supervisor_agent
    
    def get_defect_types(self, days_back):
        """Execute SQL query directly without going through agent"""
        # Validate input
        days_back = int(days_back)
        if days_back < 0 or days_back > 3650:
            raise ValueError("days_back must be between 0 and 3650")
        
        # Calculate the cutoff date
        cutoff_date = self._cutoff_date(days_back)
        
        sql_query = """
        SELECT DISTINCT d.DefectType
        FROM Defects d
        JOIN QualityControl qc ON d.CheckID = qc.CheckID
        WHERE date(qc.Date) >= ?
        ORDER BY d.DefectType
        """
        
        return self._execute_safe_query(sql_query, (cutoff_date,))

    def get_recent_alerts(self, limit: int = 20):
        """Alerts the CV pipeline raised, newest first (CONTRACTS.md §3).

        A plain read rather than an agent tool: this feeds the dashboard, and
        nothing about it should cost a model call. AgentAlerts only exists
        once the bridge has run, so a missing table means "no camera has run
        yet" and is reported as an empty list, not an error.
        """
        limit = max(1, min(int(limit), 200))
        if self.db_backend != "postgres":
            return {"alerts": [],
                    "note": "AgentAlerts lives in PostgreSQL; set MES_DB_BACKEND=postgres."}

        sql = """
        SELECT AlertID, CreatedAt, MachineID, OrderID, DefectType,
               DetectionCount, WindowStart, WindowEnd, Status, Report, CompletedAt
        FROM AgentAlerts
        ORDER BY AlertID DESC
        LIMIT %s
        """
        conn = self.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (limit,))
                columns = [c[0] for c in cur.description]
                rows = cur.fetchall()
        except Exception as e:
            # psycopg2 poisons the transaction on error, so the caller cannot
            # reuse this connection either way.
            logger.info("AgentAlerts unavailable: %s", e)
            return {"alerts": [], "note": "No alerts yet - the bridge has not run."}
        finally:
            conn.close()

        # PostgreSQL folds unquoted identifiers to lower case; the query text
        # is the source of truth for the intended spelling.
        case = _column_case_map(sql)
        alerts = []
        for row in rows:
            record = {case.get(col, col): value for col, value in zip(columns, row)}
            for field in ("CreatedAt", "WindowStart", "WindowEnd", "CompletedAt"):
                if record.get(field) is not None:
                    record[field] = str(record[field])
            # Seconds from raising the alert to finishing it - the number the
            # README wants to quote, and it is only derivable here.
            record["DurationSeconds"] = None
            alerts.append(record)
        for record, row in zip(alerts, rows):
            created, completed = row[1], row[10]
            if created is not None and completed is not None:
                record["DurationSeconds"] = round((completed - created).total_seconds(), 1)
        return {"alerts": alerts}

    def get_defect_window_stats(self, defect_type, days_back):
        """Pre-run check: how many records of this defect the selected
        look-back window actually holds, and the newest record overall.
        Gives the Supervisor verified context so an empty result is
        reported as 'window predates the data' instead of speculation."""
        cutoff_date = self._cutoff_date(days_back)

        sql_query = """
        SELECT
            COUNT(*) as WindowCount,
            (SELECT COUNT(*)
             FROM Defects d3
             JOIN QualityControl qc3 ON d3.CheckID = qc3.CheckID
             WHERE d3.DefectType = ?) as TotalCount,
            (SELECT MAX(qc2.Date)
             FROM Defects d2
             JOIN QualityControl qc2 ON d2.CheckID = qc2.CheckID
             WHERE d2.DefectType = ?) as LastOccurrence
        FROM Defects d
        JOIN QualityControl qc ON d.CheckID = qc.CheckID
        WHERE d.DefectType = ?
            AND date(qc.Date) >= ?
        """

        return self._execute_safe_query(
            sql_query, (defect_type, defect_type, defect_type, cutoff_date))

    def get_defect_preview(self, defect_type):
        """Execute SQL query directly without going through agent"""
        # Calculate the cutoff date (30 days back)
        cutoff_date = self._cutoff_date(30)
        
        sql_query = """
        SELECT 
            COUNT(*) as TotalOccurrences,
            AVG(d.Severity) as AvgSeverity,
            COUNT(DISTINCT wo.MachineID) as MachinesAffected,
            COUNT(DISTINCT wo.ProductID) as ProductsAffected,
            COUNT(DISTINCT d.RootCause) as RootCauseVariety,
            MAX(qc.Date) as LastOccurrence
        FROM 
            Defects d
        JOIN 
            QualityControl qc ON d.CheckID = qc.CheckID
        JOIN 
            WorkOrders wo ON qc.OrderID = wo.OrderID
        WHERE 
            d.DefectType = ?
            AND date(qc.Date) >= ?
        """
        
        return self._execute_safe_query(sql_query, (defect_type, cutoff_date))

