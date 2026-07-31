#!/usr/bin/env python3
"""
Simple script to run daily analysis manually

Usage: python scripts/run_daily_analysis.py
"""

import asyncio
import sys
import os
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app_factory.production_meeting.daily_analysis_scheduler import DailyAnalysisScheduler
from app_factory.shared.display_security import safe_log_text

async def main():
    """Run daily analysis"""
    print("🤖 Starting MES Daily Analysis Generation...")
    print("=" * 50)
    
    try:
        scheduler = DailyAnalysisScheduler()
        await scheduler.run_daily_analysis()
        
        print("\n✅ Daily analysis completed successfully!")
        print("📊 Results cached in: reports/daily_analysis/")
        print("🚀 Your Streamlit app can now use fast cached insights!")
        
    except Exception as e:
        print(f"\n❌ Daily analysis failed: {safe_log_text(e)}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
