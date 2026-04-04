"""
Streamlit app database client for Azure Synapse Analytics.
Provides cached data access methods for the dashboard.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

import pandas as pd
import pyodbc
import streamlit as st
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


def _build_connection_string() -> str:
    """Build ODBC connection string from environment variables."""
    server = os.environ["SYNAPSE_SERVER"]
    database = os.environ["SYNAPSE_DATABASE"]
    username = os.environ["SYNAPSE_USERNAME"]
    password = os.environ["SYNAPSE_PASSWORD"]

    return (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"Connection Timeout=30;"
    )


class SynapseStreamlitClient:
    """
    Synapse client optimized for Streamlit with caching.
    Uses st.cache_data for expensive queries.
    """

    def __init__(self):
        self._conn_str = _build_connection_string()

    @contextmanager
    def get_connection(self) -> Generator[pyodbc.Connection, None, None]:
        """Context manager for read-only database connections."""
        conn = None
        try:
            conn = pyodbc.connect(self._conn_str, autocommit=True, readonly=True)
            yield conn
        except Exception:
            raise
        finally:
            if conn:
                conn.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(pyodbc.OperationalError),
    )
    def execute_query(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        """Execute a SQL query and return a DataFrame."""
        with self.get_connection() as conn:
            return pd.read_sql(sql, conn, params=params or [])

    @st.cache_data(ttl=300, show_spinner=False)  # Cache for 5 minutes
    def get_interval_aggregates_df(
        _self,
        feed_id: Optional[int] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 2016,  # 7 days of 5-min intervals
    ) -> pd.DataFrame:
        """
        Fetch interval aggregates as a DataFrame.
        Cached for 5 minutes to match the data collection interval.
        """
        conditions = ["ia.processing_status = 'complete'"]
        params = []

        if feed_id is not None:
            conditions.append("ia.feed_id = ?")
            params.append(feed_id)
        if start_time is not None:
            conditions.append("ia.interval_start >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("ia.interval_start <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions)

        sql = f"""
        SELECT TOP {limit}
            ia.feed_id,
            f.feed_name,
            f.location_name,
            ia.interval_start,
            ia.interval_end,
            ia.total_count,
            ia.frames_analyzed,
            ia.count_male,
            ia.count_female,
            ia.count_gender_unknown,
            ia.count_children,
            ia.count_teens,
            ia.count_young_adults,
            ia.count_adults,
            ia.count_seniors,
            ia.avg_estimated_age,
            ia.ethnicity_breakdown,
            ia.count_business_attire,
            ia.count_casual_attire,
            ia.count_athletic_attire,
            ia.count_uniform_attire,
            ia.count_working,
            ia.count_leisure,
            ia.count_walking,
            ia.count_running,
            ia.count_standing,
            ia.count_cycling,
            ia.count_shopping,
            ia.count_using_phone,
            ia.count_carrying_items,
            ia.count_in_groups,
            ia.pct_male,
            ia.pct_female,
            ia.pct_working,
            ia.pct_using_phone,
            ia.avg_confidence_score
        FROM traffic.interval_aggregates ia
        JOIN traffic.video_feeds f ON f.feed_id = ia.feed_id
        WHERE {where_clause}
        ORDER BY ia.interval_start DESC
        """

        try:
            df = _self.execute_query(sql, params)
            if not df.empty:
                df["interval_start"] = pd.to_datetime(df["interval_start"])
                df["interval_end"] = pd.to_datetime(df["interval_end"])
                # Sort ascending for time-series charts
                df = df.sort_values("interval_start", ascending=True).reset_index(drop=True)
            return df
        except Exception as e:
            logger.error("Failed to fetch interval aggregates: %s", e)
            return pd.DataFrame()

    @st.cache_data(ttl=300, show_spinner=False)
    def get_summary_stats(
        _self,
        feed_id: Optional[int] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> dict:
        """Get summary statistics for KPI cards."""
        conditions = ["ia.processing_status = 'complete'"]
        params = []

        if feed_id is not None:
            conditions.append("ia.feed_id = ?")
            params.append(feed_id)
        if start_time is not None:
            conditions.append("ia.interval_start >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("ia.interval_start <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions)

        sql = f"""
        SELECT
            SUM(ia.total_count)             AS total_pedestrians,
            SUM(ia.count_male)              AS total_male,
            SUM(ia.count_female)            AS total_female,
            SUM(ia.count_working)           AS total_working,
            SUM(ia.count_leisure)           AS total_leisure,
            SUM(ia.count_using_phone)       AS total_phone,
            SUM(ia.count_children)          AS total_children,
            SUM(ia.count_teens)             AS total_teens,
            SUM(ia.count_young_adults)      AS total_young_adults,
            SUM(ia.count_adults)            AS total_adults,
            SUM(ia.count_seniors)           AS total_seniors,
            AVG(ia.avg_confidence_score)    AS avg_confidence,
            COUNT(*)                        AS intervals_analyzed,
            MAX(ia.interval_start)          AS latest_interval
        FROM traffic.interval_aggregates ia
        WHERE {where_clause}
        """

        try:
            df = _self.execute_query(sql, params)
            if df.empty or df.iloc[0]["total_pedestrians"] is None:
                return {}

            row = df.iloc[0]
            total = row["total_pedestrians"] or 0

            return {
                "total_pedestrians": int(total),
                "total_male": int(row["total_male"] or 0),
                "total_female": int(row["total_female"] or 0),
                "total_working": int(row["total_working"] or 0),
                "total_leisure": int(row["total_leisure"] or 0),
                "total_phone": int(row["total_phone"] or 0),
                "total_children": int(row["total_children"] or 0),
                "total_teens": int(row["total_teens"] or 0),
                "total_young_adults": int(row["total_young_adults"] or 0),
                "total_adults": int(row["total_adults"] or 0),
                "total_seniors": int(row["total_seniors"] or 0),
                "avg_confidence": float(row["avg_confidence"] or 0),
                "intervals_analyzed": int(row["intervals_analyzed"] or 0),
                "latest_interval": row["latest_interval"],
                "pct_male": round(row["total_male"] / total * 100, 1) if total > 0 else 0,
                "pct_female": round(row["total_female"] / total * 100, 1) if total > 0 else 0,
                "pct_working": round(row["total_working"] / total * 100, 1) if total > 0 else 0,
                "pct_phone": round(row["total_phone"] / total * 100, 1) if total > 0 else 0,
            }
        except Exception as e:
            logger.error("Failed to fetch summary stats: %s", e)
            return {}

    @st.cache_data(ttl=600, show_spinner=False)  # Cache for 10 minutes
    def get_feeds_dataframe(_self) -> pd.DataFrame:
        """Get all active video feeds."""
        sql = """
        SELECT feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone
        FROM traffic.video_feeds
        WHERE is_active = 1
        ORDER BY feed_id
        """
        try:
            return _self.execute_query(sql)
        except Exception as e:
            logger.error("Failed to fetch feeds: %s", e)
            return pd.DataFrame()

    @st.cache_data(ttl=60, show_spinner=False)  # Cache for 1 minute
    def get_recent_jobs(_self, limit: int = 20) -> pd.DataFrame:
        """Get recent analysis jobs for monitoring."""
        sql = f"""
        SELECT TOP {limit}
            aj.job_id,
            f.feed_name,
            aj.interval_start,
            aj.status,
            aj.frames_captured,
            aj.persons_detected,
            aj.vlm_calls_made,
            aj.total_tokens_used,
            aj.duration_seconds,
            aj.error_message,
            aj.started_at,
            aj.completed_at
        FROM traffic.analysis_jobs aj
        JOIN traffic.video_feeds f ON f.feed_id = aj.feed_id
        ORDER BY aj.started_at DESC
        """
        try:
            df = _self.execute_query(sql)
            if not df.empty:
                df["started_at"] = pd.to_datetime(df["started_at"])
                df["completed_at"] = pd.to_datetime(df["completed_at"])
            return df
        except Exception as e:
            logger.error("Failed to fetch recent jobs: %s", e)
            return pd.DataFrame()

    def execute_custom_query(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        """Execute a custom SQL query (not cached — used by AI query engine)."""
        try:
            return self.execute_query(sql, params)
        except Exception as e:
            logger.error("Custom query failed: %s\nSQL: %s", e, sql)
            raise

    @st.cache_data(ttl=300, show_spinner=False)
    def get_hourly_trend(_self, days: int = 7, feed_id: Optional[int] = None) -> pd.DataFrame:
        """Get hourly traffic trends for the past N days."""
        conditions = [
            "ia.processing_status = 'complete'",
            f"ia.interval_start >= DATEADD(DAY, -{days}, GETUTCDATE())",
        ]
        if feed_id is not None:
            conditions.append(f"ia.feed_id = {feed_id}")

        where_clause = " AND ".join(conditions)

        sql = f"""
        SELECT
            DATEPART(HOUR, ia.interval_start)       AS hour_of_day,
            DATEPART(WEEKDAY, ia.interval_start)    AS day_of_week,
            DATENAME(WEEKDAY, ia.interval_start)    AS day_name,
            AVG(CAST(ia.total_count AS FLOAT))      AS avg_count,
            AVG(ia.pct_male)                        AS avg_pct_male,
            AVG(ia.pct_female)                      AS avg_pct_female,
            AVG(ia.pct_working)                     AS avg_pct_working,
            COUNT(*)                                AS data_points
        FROM traffic.interval_aggregates ia
        WHERE {where_clause}
        GROUP BY
            DATEPART(HOUR, ia.interval_start),
            DATEPART(WEEKDAY, ia.interval_start),
            DATENAME(WEEKDAY, ia.interval_start)
        ORDER BY day_of_week, hour_of_day
        """
        try:
            return _self.execute_query(sql)
        except Exception as e:
            logger.error("Failed to fetch hourly trend: %s", e)
            return pd.DataFrame()


# ─── Singleton ────────────────────────────────────────────────────────────────
_client: Optional[SynapseStreamlitClient] = None


@st.cache_resource
def get_synapse_client() -> SynapseStreamlitClient:
    """Get or create the Synapse client singleton (cached as a resource)."""
    return SynapseStreamlitClient()
