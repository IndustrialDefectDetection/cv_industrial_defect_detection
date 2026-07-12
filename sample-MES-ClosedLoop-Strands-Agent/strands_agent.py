"""
Strands agents for MES  application
Contains Monitor, Analyzer, Planner, and Verifier agents for manufacturing quality analysis
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import boto3
import pandas as pd
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.anthropic import AnthropicModel

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
    
    def __init__(self, db_path: str = None, model_id: str = None, region_name: str = None):
        """Initialize the MES Agent Manager"""
        
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
        self.base_url = os.getenv('MES_BASE_URL', 'https://df4n.cloudfront.net/proxy/8501')
        
        # Database path
        self.db_path = db_path

        # Anthropic API key from .env / environment
        api_key = os.getenv("ANTHROPIC_API_KEY")

        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is missing. Add it to your .env file.")

        self.model = AnthropicModel(
            client_args={
                "api_key": api_key,
            },
            model_id=model_id,
            max_tokens=int(os.getenv("MES_MAX_TOKENS", "4096")),
            params={
                "temperature": float(os.getenv("MES_TEMPERATURE", "0.2")),
            },
        )

        self.region_name = region_name
        
        # Define allowed table names for security
        self.allowed_tables = {
            'OEEMetrics', 'Machines', 'WorkCenters', 'Downtimes', 'WorkOrders', 
            'Products', 'Shifts', 'Employees', 'Defects', 'QualityControl'
        }
        
        # Log configuration
        logger.info(f"MES Agent Manager initialized with:")
        logger.info(f"  Database Path: {self.db_path}")
        logger.info(f"  Model ID: {model_id}")
        logger.info(f"  AWS Region: {region_name}")
        logger.info(f"  Sender Email: {self.sender_email}")
        logger.info(f"  Recipient Email: {self.recipient_email}")
        logger.info(f"  Base URL: {self.base_url}")
        logger.info(f"  Max Tokens: {os.getenv('MES_MAX_TOKENS', '4096')}")
        logger.info(f"  Temperature: {os.getenv('MES_TEMPERATURE', '0.2')}")
        
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
    
    def get_db_connection(self):
        """Get a database connection"""
        if not os.path.exists(self.db_path):
            logger.warning(f"Database file not found: {self.db_path}")
            raise FileNotFoundError(f"Database file not found: {self.db_path}")
        return sqlite3.connect(self.db_path)
    
    def _validate_table_name(self, table_name: str) -> bool:
        """Validate table name against allowed list"""
        return table_name in self.allowed_tables
    
    def _execute_safe_query(self, query: str, params: tuple = None):
        """Execute SQL query safely with parameterized queries"""
        logger.info(f"Executing parameterized SQL query")
        start_time = time.time()
        
        try:
            conn = self.get_db_connection()
            if params:
                df = pd.read_sql_query(query, conn, params=params)
            else:
                df = pd.read_sql_query(query, conn)
            conn.close()
            
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
            return result
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error executing SQL query: {error_msg}")
            error_result = {
                "success": False,
                "error": error_msg,
                "execution_time_ms": round((time.time() - start_time) * 1000, 2)
            }
            return error_result
    
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
                pdf_link = f"{self.base_url}/pdf={pdf_filename}?pdf={pdf_filename}"
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
        def fetch_oee_metrics(days_back: int = 1):
            """Fetch OEE metrics and identify drops in performance"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_downtime_events(days_back: int = 1):
            """Fetch downtime events and line stoppages"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def fetch_historical_patterns(days_back: int = 30):
            """Fetch historical stoppage patterns and context"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
                date(dt.StartTime), dt.Reason, m.Type, wc.Name, s.Name
            ORDER BY 
                EventCount DESC, AvgDuration DESC
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
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
            query = """
            SELECT 
                wo.ActualStartTime as WorkDate,
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
                date(wo.ActualStartTime), e.EmployeeID, s.ShiftID, wc.WorkCenterID
            ORDER BY 
                wo.ActualStartTime DESC
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        self.monitor_tools = [
            fetch_oee_metrics,
            fetch_downtime_events,
            fetch_historical_patterns,
            fetch_work_orders_context,
            fetch_operator_logs
        ]

    def _init_analyzer_tools(self):
        """Initialize Analyzer Agent tools - Identifies root causes and performs reasoning"""
        
        @tool
        def analyze_downtime_correlations(days_back: int = 30):
            """Analyze correlations between downtime and specific factors"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
                dt.Reason, s.Name, e.Name, p.Name, m.Type
            ORDER BY 
                TotalDuration DESC
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def analyze_batch_changeover_time(days_back: int = 30):
            """Analyze batch changeover times vs benchmarks"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
            """
            
            return self._execute_safe_query(query, (cutoff_date, cutoff_date))

        @tool
        def identify_performance_patterns(days_back: int = 30):
            """Identify patterns in machine and operator performance"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
                m.MachineID, e.EmployeeID, s.ShiftID
            ORDER BY 
                AvgOEE ASC, TotalDowntime DESC
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        @tool
        def analyze_quality_defects(days_back: int = 30):
            """Analyze quality defects and their root causes"""
            # Validate input
            days_back = int(days_back)
            if days_back < 0 or days_back > 3650:
                raise ValueError("days_back must be between 0 and 3650")
            
            # Calculate the cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            
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
            GROUP BY 
                d.DefectType, d.RootCause, p.ProductID, m.MachineID, e.EmployeeID, s.ShiftID
            ORDER BY 
                DefectCount DESC, d.Severity DESC
            """
            
            return self._execute_safe_query(query, (cutoff_date,))

        self.analyzer_tools = [
            analyze_downtime_correlations,
            analyze_batch_changeover_time,
            identify_performance_patterns,
            analyze_quality_defects
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
                    # Clean and format the executive summary text
                    summary_text = str(report_data['executive_summary']).strip()
                    if summary_text:
                        # Split into paragraphs and clean up
                        paragraphs = summary_text.split('\n')
                        for para in paragraphs:
                            para = para.strip()
                            if para:
                                story.append(Paragraph(para, styles['Normal']))
                                story.append(Spacer(1, 12))
                    story.append(PageBreak())
                
                # Add report content
                for section, content in report_data.items():
                    if section == 'executive_summary':
                        continue  # Already handled above
                        
                    # Format section title
                    section_title = section.replace('_', ' ').title()
                    story.append(Paragraph(section_title, styles['Heading1']))
                    story.append(Spacer(1, 12))
                    
                    if isinstance(content, str):
                        # Clean and format string content
                        content_text = content.strip()
                        if content_text:
                            # Split long text into paragraphs
                            paragraphs = content_text.split('\n')
                            for para in paragraphs:
                                para = para.strip()
                                if para:
                                    story.append(Paragraph(para, styles['Normal']))
                                    story.append(Spacer(1, 8))
                                    
                    elif isinstance(content, list):
                        # Handle list content
                        for item in content:
                            item_text = str(item).strip()
                            if item_text:
                                story.append(Paragraph(f"• {item_text}", styles['Normal']))
                                story.append(Spacer(1, 6))
                                
                    elif isinstance(content, dict):
                        # Handle dictionary content
                        for key, value in content.items():
                            key_formatted = key.replace('_', ' ').title()
                            story.append(Paragraph(f"<b>{key_formatted}:</b> {str(value)}", styles['Normal']))
                            story.append(Spacer(1, 6))
                            
                    else:
                        # Handle other data types
                        story.append(Paragraph(str(content), styles['Normal']))
                        
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

Focus on capturing complete operational context to enable effective root cause analysis."""
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
5. Provide confidence levels for your analysis
6. Recommend data-driven solutions

Always quantify impact and provide actionable insights for the planning phase."""
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

Always focus on measurable, actionable recommendations that improve manufacturing performance."""
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
Note: Email notifications are handled by the Executor Agent."""
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

Always focus on clear and concise email body with actionable recommendations, ownership, timeline and risks if not done on time."""
        )

    def _init_supervisor_agent(self):
        """Initialize the Supervisor Agent that orchestrates the workflow"""
        
        @tool
        def call_monitor_agent(prompt: str):
            """Call the Monitor Agent to capture operational data"""
            return self.monitor_agent(prompt)
        
        @tool
        def call_analyzer_agent(prompt: str):
            """Call the Analyzer Agent to perform root cause analysis"""
            return self.analyzer_agent(prompt)
        
        @tool
        def call_planner_agent(prompt: str):
            """Call the Planner Agent to create action plans"""
            return self.planner_agent(prompt)
        
        @tool
        def call_verifier_agent(prompt: str):
            """Call the Verifier Agent to validate findings"""
            return self.verifier_agent(prompt)
        
        @tool
        def call_executor_agent(prompt: str):
            """Call the Executor Agent to execute actions"""
            return self.executor_agent(prompt)
        
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

Focus on ensuring each agent receives appropriate context and scope parameters, and that the complete workflow produces actionable, validated insights for manufacturing quality improvement within the specified analysis scope. All email notifications should be handled through the Executor Agent with proper PDF filename passing."""
        )

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
        
        try:
            # Create comprehensive prompt for supervisor agent
            supervisor_prompt = f"""
            Execute comprehensive defect analysis workflow for defect type '{defect_type}' over the last {days_back} days.
            
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
            
            end_time = datetime.now()
            
            # Extract supervisor response content
            supervisor_results = supervisor_response.message['content'][0]['text']
            
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
            
            return analysis_results
            
        except Exception as e:
            logger.error(f"Error in supervisor-orchestrated defect analysis workflow: {e}")
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
        cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
        
        sql_query = """
        SELECT DISTINCT d.DefectType
        FROM Defects d
        JOIN QualityControl qc ON d.CheckID = qc.CheckID
        WHERE date(qc.Date) >= ?
        ORDER BY d.DefectType
        """
        
        return self._execute_safe_query(sql_query, (cutoff_date,))

    def get_defect_preview(self, defect_type):
        """Execute SQL query directly without going through agent"""
        # Calculate the cutoff date (30 days back)
        cutoff_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        
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

