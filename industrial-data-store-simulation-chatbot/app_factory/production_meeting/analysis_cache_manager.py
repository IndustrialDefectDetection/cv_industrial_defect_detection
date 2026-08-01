"""
Analysis Cache Manager for Production Meeting Insights

This module handles loading cached daily analysis results and provides fallback mechanisms.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

from app_factory.shared.output_security import (
    ensure_private_directory,
    open_private_json,
)
from app_factory.shared.display_security import safe_log_text

logger = logging.getLogger(__name__)

class AnalysisCacheManager:
    """Manages cached analysis results for fast retrieval"""

    def __init__(self, cache_dir: str = "reports/daily_analysis"):
        self.cache_dir = Path(cache_dir)
        if self.cache_dir.parent.name == "reports":
            ensure_private_directory(self.cache_dir.parent)
        self.cache_dir = ensure_private_directory(self.cache_dir)
    
    def get_cache_filename(self, date: datetime = None) -> Path:
        """Get cache filename for a specific date"""
        if date is None:
            date = datetime.now()
        
        date_str = date.strftime("%Y-%m-%d")
        return self.cache_dir / f"daily_analysis_{date_str}.json"
    
    def load_cached_analysis(self, date: datetime = None) -> Optional[Dict[str, Any]]:
        """Load cached analysis for a specific date"""
        cache_file = self.get_cache_filename(date)
        
        if not cache_file.exists():
            logger.warning(
                "No cached analysis found for %s",
                safe_log_text(date or "today"),
            )
            return None

        try:
            with open_private_json(cache_file) as f:
                analysis_data = json.load(f)
            
            logger.info(
                "Loaded cached analysis from %s",
                safe_log_text(cache_file),
            )
            return analysis_data
            
        except Exception as e:
            logger.error(
                "Failed to load cached analysis from %s: %s",
                safe_log_text(cache_file),
                safe_log_text(e),
            )
            return None
    
    def get_latest_analysis(self, max_age_hours: int = 24) -> Optional[Dict[str, Any]]:
        """Get the most recent analysis within the specified age limit"""
        
        # Try today first
        today_analysis = self.load_cached_analysis()
        if today_analysis:
            generated_at = datetime.fromisoformat(today_analysis['generated_at'])
            age_hours = (datetime.now() - generated_at).total_seconds() / 3600
            
            if age_hours <= max_age_hours:
                return today_analysis
        
        # Try previous days
        for days_back in range(1, 7):  # Look back up to 7 days
            check_date = datetime.now() - timedelta(days=days_back)
            analysis = self.load_cached_analysis(check_date)
            
            if analysis:
                generated_at = datetime.fromisoformat(analysis['generated_at'])
                age_hours = (datetime.now() - generated_at).total_seconds() / 3600
                
                if age_hours <= max_age_hours:
                    logger.info(f"Using analysis from {days_back} days ago")
                    return analysis
        
        logger.warning(f"No recent analysis found within {max_age_hours} hours")
        return None
    
    def get_analysis_summary(self, analysis_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract summary information from cached analysis"""
        if not analysis_data:
            return {}
        
        summary = {
            'generated_at': analysis_data.get('generated_at'),
            'analysis_date': analysis_data.get('analysis_date'),
            'total_execution_time': analysis_data.get('total_execution_time', 0),
            'available_analyses': list(analysis_data.get('analyses', {}).keys()),
            'is_cached': True
        }
        
        # Count successful vs failed analyses
        analyses = analysis_data.get('analyses', {})
        summary['successful_analyses'] = len([a for a in analyses.values() if 'analysis' in a])
        summary['failed_analyses'] = len([a for a in analyses.values() if 'error' in a])
        
        return summary
    
    def get_specific_analysis(self, analysis_type: str, date: datetime = None) -> Optional[Dict[str, Any]]:
        """Get a specific analysis type from cached data"""
        analysis_data = self.load_cached_analysis(date)
        
        if not analysis_data:
            return None
        
        analyses = analysis_data.get('analyses', {})
        return analyses.get(analysis_type)
    
    def list_available_dates(self, days_back: int = 30) -> List[Dict[str, Any]]:
        """List available cached analysis dates"""
        available_dates = []
        
        for days in range(days_back):
            check_date = datetime.now() - timedelta(days=days)
            cache_file = self.get_cache_filename(check_date)
            
            if cache_file.exists():
                try:
                    with open_private_json(cache_file) as f:
                        # Read metadata from the same no-follow descriptor as
                        # the JSON, avoiding a path substitution between stat
                        # and load.
                        file_stat = os.fstat(f.fileno())
                        file_size = file_stat.st_size
                        modified_time = datetime.fromtimestamp(
                            file_stat.st_mtime
                        )
                        data = json.load(f)
                    
                    available_dates.append({
                        'date': check_date.strftime('%Y-%m-%d'),
                        'file_path': str(cache_file),
                        'file_size': file_size,
                        'modified_time': modified_time.isoformat(),
                        'generated_at': data.get('generated_at'),
                        'total_execution_time': data.get('total_execution_time', 0),
                        'analysis_count': len(data.get('analyses', {}))
                    })
                    
                except Exception as e:
                    logger.warning(
                        "Error reading cache file %s: %s",
                        safe_log_text(cache_file),
                        safe_log_text(e),
                    )
        
        return sorted(available_dates, key=lambda x: x['date'], reverse=True)
    
    def is_analysis_fresh(self, max_age_hours: int = 6) -> bool:
        """Check if today's analysis is fresh enough"""
        today_analysis = self.load_cached_analysis()
        
        if not today_analysis:
            return False
        
        try:
            generated_at = datetime.fromisoformat(today_analysis['generated_at'])
            age_hours = (datetime.now() - generated_at).total_seconds() / 3600
            return age_hours <= max_age_hours
        except:
            return False
    
    def get_cache_status(self) -> Dict[str, Any]:
        """Get overall cache status information"""
        available_dates = self.list_available_dates(7)  # Last 7 days
        
        status = {
            'cache_directory': str(self.cache_dir),
            'cache_exists': self.cache_dir.exists(),
            'available_analyses': len(available_dates),
            'latest_analysis': None,
            'is_fresh': False,
            'cache_size_mb': 0
        }
        
        if available_dates:
            status['latest_analysis'] = available_dates[0]
            status['is_fresh'] = self.is_analysis_fresh()
        
        # Calculate total cache size
        try:
            total_size = 0
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    with open_private_json(cache_file) as cache_stream:
                        total_size += os.fstat(cache_stream.fileno()).st_size
                except Exception:
                    logger.warning(
                        "Ignoring unsafe cache file while calculating size: %s",
                        cache_file,
                    )
            status['cache_size_mb'] = round(total_size / (1024 * 1024), 2)
        except:
            pass

        return status
