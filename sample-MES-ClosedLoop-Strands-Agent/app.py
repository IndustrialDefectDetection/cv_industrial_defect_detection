"""
 Intelligent Agentic AI for Autonomous Manufacturing Operation 
Implements Monitor -> Analyzer -> Planner -> Verifier workflow with defect type selection
"""
import streamlit as st
from datetime import datetime, timedelta
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sys
import os
import json 
import logging
import time
from pathlib import Path
import base64
import urllib.parse

# # Add parent directory to path for imports
# parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# sys.path.append(parent_dir)

from strands_agent import MESAgentManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize session state
if 'analysis_started' not in st.session_state:
    st.session_state.analysis_started = False
if 'current_analysis' not in st.session_state:
    st.session_state.current_analysis = {}
if 'defect_types' not in st.session_state:
    st.session_state.defect_types = []
if 'selected_defect' not in st.session_state:
    st.session_state.selected_defect = None
if 'analysis_running' not in st.session_state:
    st.session_state.analysis_running = False

# Create reports directory
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

# JSON serialization helper
def datetime_to_string(obj):
    """Convert datetime objects to string for JSON serialization"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj

def serialize_analysis_results(results):
    """Convert analysis results to JSON-serializable format"""
    if not isinstance(results, dict):
        return results
    
    serialized = {}
    for key, value in results.items():
        if isinstance(value, datetime):
            serialized[key] = value.isoformat()
        elif isinstance(value, dict):
            serialized[key] = serialize_analysis_results(value)
        elif isinstance(value, list):
            serialized[key] = [serialize_analysis_results(item) if isinstance(item, dict) else datetime_to_string(item) for item in value]
        else:
            serialized[key] = value
    
    return serialized

# Initialize agent manager with caching
@st.cache_resource
def get_agent_manager():
    """Initialize and cache the MES Agent Manager"""
    try:
        manager = MESAgentManager()

        return manager
    except Exception as e:
        st.error(f"Failed to initialize agent manager: {e}")
        logger.error(f"Agent manager initialization error: {e}")
        return None

@st.cache_data(ttl=300)  # Cache for 5 minutes
def load_defect_types(days_back: int = 365):
    """Load available defect types from the database"""
    try:
        agent_manager = get_agent_manager()
        if agent_manager is None:
            logger.error("Agent manager is None")
            return []
        
        logger.info(f"Loading defect types for last {days_back} days")
        result = agent_manager.get_defect_types(days_back)
        
        logger.info(f"Defect types result: {result}")
        
        # Check if we have rows (the function returns rows directly, not wrapped in success)
        if result and result.get('rows'):
            defect_types = [row['DefectType'] for row in result['rows'] if row.get('DefectType')]
            logger.info(f"Found {len(defect_types)} defect types: {defect_types}")
            return defect_types
        elif result and result.get('error'):
            logger.error(f"Database error: {result['error']}")
            st.error(f"Database error: {result['error']}")
            return []
        else:
            logger.warning("No defect types found in database")
            return []
            
    except Exception as e:
        logger.error(f"Error loading defect types: {e}")
        st.error(f"Error loading defect types: {e}")
        return []

def get_available_reports():
    """Get list of available PDF reports in the reports directory"""
    try:
        pdf_files = list(REPORTS_DIR.glob("*.pdf"))
        return sorted(pdf_files, key=lambda x: x.stat().st_mtime, reverse=True)
    except Exception as e:
        logger.error(f"Error loading reports: {e}")
        return []

def get_pdf_from_url():
    """Check URL parameters for PDF file to display"""
    query_params = st.query_params
    pdf_file = query_params.get("pdf", None)
    
    if pdf_file:
        # Decode URL-encoded filename
        pdf_file = urllib.parse.unquote(pdf_file)
        pdf_path = REPORTS_DIR / pdf_file
        
        if pdf_path.exists() and pdf_path.suffix.lower() == '.pdf':
            return pdf_path
    
    return None

def generate_pdf_url(pdf_filename):
    """Generate shareable URL for PDF file"""
    encoded_filename = urllib.parse.quote(pdf_filename)
    base_url = st.get_option("browser.serverAddress") or "localhost"
    port = st.get_option("server.port") or 8501
    
    # Get current URL components
    try:
        # Try to get the current URL from session state or use default
        current_url = f"http://{base_url}:{port}"
        pdf_url = f"{current_url}?pdf={encoded_filename}"
        return pdf_url
    except:
        # Fallback URL generation
        return f"?pdf={encoded_filename}"

def display_pdf_viewer(pdf_path):
    """Display PDF in Streamlit using base64 encoding"""
    try:
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        
        base64_pdf = base64.b64encode(pdf_data).decode('utf-8')
        pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="600" type="application/pdf"></iframe>'
        st.markdown(pdf_display, unsafe_allow_html=True)
        
        return pdf_data
    except Exception as e:
        st.error(f"Error displaying PDF: {e}")
        return None

def run_defect_analysis(defect_type: str, days_back: int = 7, include_oee: bool = True, include_downtime: bool = True, include_changeover: bool = True, include_maintenance: bool = True):
    """Run comprehensive defect analysis using the supervisor agent"""
    
    agent_manager = get_agent_manager()
    if agent_manager is None:
        st.error("Agent manager not available")
        return None
    
    try:
        # Show progress
        st.write("### 🤖 Supervisor Agent - Orchestrating Analysis Workflow")
        
        # Show analysis scope
        scope_items = []
        if include_oee:
            scope_items.append("🔍 OEE Performance Analysis")
        if include_downtime:
            scope_items.append("⏱️ Downtime & Stoppages")
        if include_changeover:
            scope_items.append("🔄 Batch Changeover Analysis")
        if include_maintenance:
            scope_items.append("🔧 Maintenance Correlation")
        
        if scope_items:
            st.info(f"**Analysis Scope:** {' • '.join(scope_items)}")
        else:
            st.warning("**No analysis scope selected** - Running basic analysis only")
        
        with st.spinner(f'Running comprehensive analysis for {defect_type} using AI agent workflow...'):
            # Use the supervisor agent to run the complete analysis
            analysis_results = agent_manager.run_defect_analysis(
                defect_type=defect_type, 
                days_back=days_back,
                include_oee=include_oee,
                include_downtime=include_downtime,
                include_changeover=include_changeover,
                include_maintenance=include_maintenance
            )
        
        if analysis_results and analysis_results.get('status') == 'completed':
            st.success("✅ Complete analysis workflow executed successfully")
            
            # Store results in session state
            st.session_state.current_analysis = analysis_results
            st.session_state.analysis_started = True
            print("analysis_results:",analysis_results)
            return analysis_results
        else:
            st.error(f"Analysis failed: {analysis_results.get('error', 'Unknown error')}")
            return None
        
    except Exception as e:
        st.error(f"Error during analysis: {e}")
        logger.error(f"Analysis error: {e}")
        return None

def render_defect_selection():
    """Render defect type selection interface in sidebar"""
    
    st.sidebar.subheader("🎯 Incident Simulation")
    st.sidebar.info("Sends Event (Drop in OEE, Line stoppage, Downtime due to tool changeover)")
    
    # Load defect types
    with st.sidebar:
        with st.spinner("Loading available defect types..."):
            defect_types = load_defect_types(365)  # Load from last 365 days
            st.session_state.defect_types = defect_types
        


        
        if not defect_types:
            st.warning("⚠️ No defect types found in the database. Please check your data connection.")
            
            # Show database connection status
            agent_manager = get_agent_manager()
            if agent_manager:
                test_result = agent_manager.test_database_connection()
                if test_result.get('success'):
                    st.info(f"✅ Database connected: {test_result.get('total_tables', 0)} tables found")
                    for table, count in test_result.get('tables', {}).items():
                        st.write(f"- {table}: {count} records")
                else:
                    st.error(f"❌ Database connection failed: {test_result.get('error')}")
            
            return None
        
        # Defect type dropdown
        selected_defect = st.selectbox(
            "Select Event Type for Analysis:",
            options=[None] + defect_types,
            index=0,
            disabled=st.session_state.analysis_running,
            format_func=lambda x: "-- Select an Event type --" if x is None else x,
            help="Choose a specific defect type to analyze using the AI agent workflow"
        )
        
        st.session_state.selected_defect = selected_defect
        
    return selected_defect

def render_defect_preview(defect_type: str):
    """Render detailed preview information for selected defect type in main area"""
    
    try:
        agent_manager = get_agent_manager()
        if agent_manager is None:
            return
        
        result = agent_manager.get_defect_preview(defect_type)
        
        if result and result.get('rows') and len(result['rows']) > 0:
            data = result['rows'][0]
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Total Occurrences", f"{data.get('TotalOccurrences', 0):,}")
                st.metric("Machines Affected", f"{data.get('MachinesAffected', 0):,}")
            
            with col2:
                severity = data.get('AvgSeverity') or 0
                severity_color = "🔴" if severity > 3 else "🟡" if severity > 2 else "🟢"
                st.metric("Avg Severity", f"{severity_color} {severity:.1f}/5")
                st.metric("Products Affected", f"{data.get('ProductsAffected', 0):,}")
            
            with col3:
                st.metric("Root Cause Variety", f"{data.get('RootCauseVariety', 0):,}")
                last_occurrence = data.get('LastOccurrence')
                if last_occurrence:
                    try:
                        last_date = pd.to_datetime(last_occurrence).strftime('%Y-%m-%d')
                        st.metric("Last Occurrence", last_date)
                    except:
                        st.metric("Last Occurrence", str(last_occurrence))
                else:
                    st.metric("Last Occurrence", "Unknown")
            
            # Risk assessment
            risk_score = 0
            if severity > 3:
                risk_score += 2
            if data.get('TotalOccurrences', 0) > 10:
                risk_score += 1
            if data.get('MachinesAffected', 0) > 3:
                risk_score += 1
            
            if risk_score >= 3:
                risk_level = "🔴 High"
            elif risk_score >= 2:
                risk_level = "🟡 Medium"
            else:
                risk_level = "🟢 Low"
            
            st.info(f"**Risk Assessment:** {risk_level} | **Recommendation:** {'Immediate analysis recommended' if risk_score >= 3 else 'Standard analysis sufficient'}")
            
        else:
            st.warning(f"No preview data available for defect type: {defect_type}")
            if result and result.get('error'):
                st.error(f"Database error: {result['error']}")
            
    except Exception as e:
        st.error(f"Error loading defect preview: {e}")
        logger.error(f"Defect preview error: {e}")

def render_sidebar_configuration():
    """Render sidebar configuration options"""
    
    with st.sidebar:
        st.divider()
        
        st.subheader("⚙️ Reasoning Configuration")
        
        # Time period selection
        time_option = st.selectbox(
            "Look back Period",
            ["Last 3 days", "Last 7 days", "Last 14 days", "Last 30 days","Last 120 days","Last 180 days","Last 365 days"],
            index=1,  # Default to 7 days
            disabled=st.session_state.analysis_running
        )
        days_back = int(time_option.split()[1])
        
        # Analysis scope
        st.subheader("🔍 Reasoning Scope")
        include_oee = st.checkbox("OEE Performance Analysis", value=False, disabled=st.session_state.analysis_running)
        include_downtime = st.checkbox("Downtime & Stoppages", value=False, disabled=st.session_state.analysis_running)
        include_changeover = st.checkbox("Batch Changeover Analysis", value=False, disabled=st.session_state.analysis_running)
        include_maintenance = st.checkbox("Maintenance Correlation", value=True, disabled=st.session_state.analysis_running)
   
        # Defect selection (moved here)
        selected_defect = render_defect_selection()
        
        if st.session_state.get('analysis_started'):
            if st.button("🔄 New Analysis", use_container_width=True):
                st.session_state.analysis_started = False
                st.session_state.current_analysis = {}
                st.session_state.selected_defect = None
                st.rerun()
        
        # PDF Reports section
        st.divider()
        st.subheader("📁 Available Reports")
        
        available_reports = get_available_reports()
        if available_reports:
            st.write(f"Found {len(available_reports)} report(s)")
            
            for report_file in available_reports[:5]:  # Show last 5 reports
                file_name = report_file.name
                file_size = report_file.stat().st_size
                file_date = datetime.fromtimestamp(report_file.stat().st_mtime)

                if st.button(f"{file_name}", key=f"url_{file_name}"):
                    # Update URL parameters to show this PDF
                    st.query_params["pdf"] = file_name
                    st.rerun()
        else:
            st.info("No PDF reports found")
    
    return {
        'selected_defect': selected_defect,
        'days_back': days_back,
        'include_oee': include_oee,
        'include_downtime': include_downtime,
        'include_changeover': include_changeover,
        'include_maintenance': include_maintenance
    }

def render_main_dashboard():
    """Render the main dashboard interface"""
    
    st.header("🏭 Intelligent Agentic AI for Autonomous Manufacturing Operation")
    st.markdown("""
    **AI-powered defect analysis** using specialized agents working in sequence to monitor, analyze, plan, and verify quality improvements.
    """)
    
    # Check for URL-based PDF viewing first
    url_pdf = get_pdf_from_url()
    if url_pdf:
        st.subheader(f"📄 Shared PDF Report: {url_pdf.name}")
        
        col1, col2, col3 = st.columns([2,2, 1])
        with col1:
            if st.button("Approve plan"):
                # todo take action
                st.query_params.clear()
                st.rerun()
        with col2:
            if st.button("Reject plan"):
                # todo take action
                st.query_params.clear()
                st.rerun()        

        with col3:
            if st.button("❌ Close PDF View"):
                # Clear URL parameters
                st.query_params.clear()
                st.rerun()
        
        pdf_data = display_pdf_viewer(url_pdf)
        if pdf_data:
            st.download_button(
                "📥 Download This Report",
                data=pdf_data,
                file_name=url_pdf.name,
                mime="application/pdf"
            )
        
        # Show sharing info
        st.info("🔗 This PDF is being viewed via a shareable URL. You can bookmark or share this link with others.")
        return
    
    # Agent workflow overview
    st.subheader("🤖 AI Agent Workflow")
    st.error("""
                           **🔍 Supervisor Agent** - Continuous monitoring
& Feedback
        
        """)
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.info("""
        **🔍 Context Builder Agent**
        
        • Captures OEE drops
        • Fetches context data  
        • Historical patterns
        • Operator logs
        • Work order analysis
        """)
    
    with col2:
        st.warning("""
        **🎯 Analyzer Agent**
        
        • Root cause identification
        • Correlation analysis
        • Performance reasoning
        • Statistical confidence
        • Impact quantification
        """)
    
    with col3:
        st.success("""
        **📋 Planner Agent**
        
        • Actionable plans
        • PDF report creation
        • Resource estimation
        • Implementation roadmap
        • Success metrics
        """)
    
    with col4:
        st.error("""
        **✅ Verifier Agent**
        
        • Finding validation
        • Quality assurance
        """)
    with col5:
        st.info("""
        **✅ Executor Agent**
        

        • Email notifications
        • Human review

        """)    
    
    st.divider()
    
    # Get sidebar configuration
    config = render_sidebar_configuration()
    selected_defect = config['selected_defect']
    
    # Check if PDF viewer should be shown (session state)
    if 'selected_pdf' in st.session_state:
        st.subheader(f"📄 PDF Report Viewer: {st.session_state.selected_pdf.name}")
        
        col1, col2 = st.columns([3, 1])
        with col2:
            if st.button("❌ Close Viewer"):
                del st.session_state.selected_pdf
                st.rerun()
        
        pdf_data = display_pdf_viewer(st.session_state.selected_pdf)
        if pdf_data:
            st.download_button(
                "📥 Download This Report",
                data=pdf_data,
                file_name=st.session_state.selected_pdf.name,
                mime="application/pdf"
            )
        return
    
    # Main content area
    if selected_defect:
        # Show detailed defect preview in main area
        st.subheader(f"📊 Defect Analysis: {selected_defect}")
        with st.expander("Detailed Defect Information", expanded=True):
            render_defect_preview(selected_defect)
        
        if "analysis_running" not in st.session_state:
            st.session_state.analysis_running = False

        run_clicked = st.button(
            "🚀 Run Analysis",
            disabled=st.session_state.analysis_running,
            use_container_width=True
        )

        if run_clicked and not st.session_state.analysis_running:
            st.session_state.analysis_running = True
            st.session_state.work_pending = True
            st.rerun()

        if st.session_state.analysis_running and st.session_state.get("work_pending"):
            st.session_state.work_pending = False
            try:
                st.divider()
                st.subheader(f"🔄 Analyzing Defect Type: {selected_defect}")

                analysis_results = run_defect_analysis(
                    defect_type=selected_defect,
                    days_back=config['days_back'],
                    include_oee=config['include_oee'],
                    include_downtime=config['include_downtime'],
                    include_changeover=config['include_changeover'],
                    include_maintenance=config['include_maintenance']
                )

                if analysis_results:
                    st.success("✅ Analysis completed successfully!")
            finally:
                st.session_state.analysis_running = False
                st.rerun()
                                
        elif st.session_state.analysis_started and st.session_state.current_analysis:
            # Show completed results
            render_analysis_results()
            
        else:
            # Show welcome message
            st.info("👈 Please select a defect type from the sidebar to begin analysis")
                
def render_analysis_results():
    """Render comprehensive analysis results"""
    
    analysis = st.session_state.current_analysis
    defect_type = analysis.get('defect_type', 'Unknown')
    
    st.divider()
    st.subheader(f"📊 Analysis Results: {defect_type}")
    
    # Results overview
    col1, col2, col3, col4 = st.columns(4)
    
    duration = analysis.get('total_duration', 0)
    analysis_scope = analysis.get('analysis_scope', {})
    scope_summary = analysis_scope.get('scope_summary', 'Basic Analysis')
    
    with col1:
        st.metric("Defect Type", defect_type)
    with col2:
        st.metric("Analysis Duration", f"{duration:.1f}s")
    with col3:
        st.metric("Agents Executed", "5/5", "✅")
    with col4:
        st.metric("Analysis Scope", scope_summary)
    
    # Show analysis scope details
    if analysis_scope:
        scope_details = []
        if analysis_scope.get('include_oee'):
            scope_details.append("🔍 OEE Analysis")
        if analysis_scope.get('include_downtime'):
            scope_details.append("⏱️ Downtime Analysis")
        if analysis_scope.get('include_changeover'):
            scope_details.append("🔄 Changeover Analysis")
        if analysis_scope.get('include_maintenance'):
            scope_details.append("🔧 Maintenance Analysis")
        
        if scope_details:
            st.info(f"**Enabled Analysis Areas:** {' • '.join(scope_details)}")
    
    # Create tabs for detailed results
    tab2, tab3 = st.tabs([
        "📊 Executive Summary",
        "📈 Performance Metrics"
    ])
    
    with tab2:
        render_executive_summary(analysis)
    
    with tab3:
        render_performance_metrics(analysis)

def render_performance_metrics(analysis):
    """Render performance metrics and charts"""
    
    st.subheader("📈 Performance Metrics")
    
    # Create sample metrics based on analysis
    defect_type = analysis.get('defect_type', 'Unknown')
    analysis_scope = analysis.get('analysis_scope', {})
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("**🎯 Analysis Performance**")
        
        # Analysis timing metrics
        duration = analysis.get('total_duration', 0)
        st.metric("Total Analysis Time", f"{duration:.2f}s")
        st.metric("Agent Coordination", "Successful", "✅")
        st.metric("Data Quality Score", "95%", "+5%")
        
        # Scope coverage
        enabled_count = sum([
            analysis_scope.get('include_oee', False),
            analysis_scope.get('include_downtime', False),
            analysis_scope.get('include_changeover', False),
            analysis_scope.get('include_maintenance', False)
        ])
        st.metric("Analysis Coverage", f"{enabled_count}/4 areas", f"+{enabled_count}")
    
    with col2:
        st.markdown("**📊 Impact Projection**")
        
        # Projected improvements (sample data)
        st.metric("Projected Defect Reduction", "15-25%", "+20%")
        st.metric("Expected OEE Improvement", "3-7%", "+5%")
        st.metric("ROI Timeline", "2-4 months", "📈")
        st.metric("Confidence Level", "High", "🎯")

def render_executive_summary(analysis):
    """Render executive summary of the analysis"""
    
    defect_type = analysis.get('defect_type', 'Unknown')
    analysis_scope = analysis.get('analysis_scope', {})
    
    st.subheader("📋 Executive Summary")
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Parse end_time safely
        end_time_str = analysis.get('end_time', '')
        if end_time_str:
            try:
                end_time = datetime.fromisoformat(end_time_str)
                end_time_formatted = end_time.strftime('%Y-%m-%d %H:%M')
            except:
                end_time_formatted = end_time_str
        else:
            end_time_formatted = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        scope_summary = analysis_scope.get('scope_summary', 'Basic Analysis')
        
        st.markdown(f"""
        **🎯 Analysis Target:**
        - Defect Type: {defect_type}
        - Analysis Period: {analysis.get('analysis_period', 7)} days
        - Analysis Scope: {scope_summary}
        - Completed: {end_time_formatted}
        
        **🔍 Key Findings:**
        - Supervisor agent coordinated complete workflow
        - All specialized agents executed successfully within scope
        - Comprehensive analysis completed with actionable insights
        - Workflow orchestration ensured proper data flow and scope adherence
        """)
    
    with col2:
        enabled_areas = []
        if analysis_scope.get('include_oee'):
            enabled_areas.append("OEE performance optimization")
        if analysis_scope.get('include_downtime'):
            enabled_areas.append("downtime reduction strategies")
        if analysis_scope.get('include_changeover'):
            enabled_areas.append("changeover time improvements")
        if analysis_scope.get('include_maintenance'):
            enabled_areas.append("maintenance schedule optimization")
        
        areas_text = ", ".join(enabled_areas) if enabled_areas else "basic operational improvements"
        
        st.markdown(f"""
        **⚡ Immediate Actions Required:**
        1. Review supervisor agent findings and recommendations
        2. Implement coordinated action plans focusing on {areas_text}
        3. Schedule validation meetings with stakeholders
        4. Monitor defect trends using integrated insights
        
        **📈 Success Metrics:**
        - Integrated defect reduction strategy established
        - Scope-specific performance monitoring KPIs defined
        - Coordinated implementation timeline created
        - Comprehensive resource allocation planned for enabled areas
        """)
    
    # Risk assessment
    st.markdown("**⚠️ Risk Assessment:**")
    st.info(f"Comprehensive supervisor-coordinated analysis completed for {defect_type} within the specified scope ({scope_summary}). The integrated workflow has provided validated insights from all specialized agents for effective defect reduction.")

def main():
    """Main function to run the MES dashboard"""
    
    st.set_page_config(
        page_title="MES Quality Management", 
        page_icon="🏭",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    render_main_dashboard()

if __name__ == "__main__":
    main()
