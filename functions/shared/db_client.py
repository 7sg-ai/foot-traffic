"""
Azure Synapse Analytics database client.
Handles all read/write operations for foot traffic data.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

import pyodbc
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import get_settings
from .models import IntervalAggregate, FrameAnalysisResult, VideoFeed

logger = logging.getLogger(__name__)


class SynapseClient:
    """Client for Azure Synapse Analytics Dedicated SQL Pool."""

    def __init__(self):
        self._settings = get_settings()
        self._conn_str = self._settings.synapse_connection_string

    @contextmanager
    def get_connection(self) -> Generator[pyodbc.Connection, None, None]:
        """Context manager for database connections."""
        conn = None
        try:
            conn = pyodbc.connect(self._conn_str, autocommit=False)
            yield conn
            conn.commit()
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(pyodbc.OperationalError),
    )
    def insert_interval_aggregate(self, agg: IntervalAggregate) -> None:
        """Insert or update a 5-minute interval aggregate."""
        sql = """
        MERGE traffic.interval_aggregates AS target
        USING (
            SELECT
                :feed_id AS feed_id,
                :interval_start AS interval_start
        ) AS source
        ON target.feed_id = source.feed_id
           AND target.interval_start = source.interval_start
        WHEN MATCHED THEN
            UPDATE SET
                interval_end            = ?,
                total_count             = ?,
                frames_analyzed         = ?,
                count_male              = ?,
                count_female            = ?,
                count_gender_unknown    = ?,
                count_children          = ?,
                count_teens             = ?,
                count_young_adults      = ?,
                count_adults            = ?,
                count_seniors           = ?,
                avg_estimated_age       = ?,
                ethnicity_breakdown     = ?,
                count_business_attire   = ?,
                count_casual_attire     = ?,
                count_athletic_attire   = ?,
                count_uniform_attire    = ?,
                count_working           = ?,
                count_leisure           = ?,
                count_walking           = ?,
                count_running           = ?,
                count_standing          = ?,
                count_cycling           = ?,
                count_shopping          = ?,
                count_using_phone       = ?,
                count_carrying_items    = ?,
                count_in_groups         = ?,
                pct_male                = ?,
                pct_female              = ?,
                pct_working             = ?,
                pct_using_phone         = ?,
                avg_confidence_score    = ?,
                processing_status       = ?,
                error_message           = ?,
                updated_at              = GETUTCDATE()
        WHEN NOT MATCHED THEN
            INSERT (
                aggregate_id, feed_id, interval_start, interval_end,
                total_count, frames_analyzed,
                count_male, count_female, count_gender_unknown,
                count_children, count_teens, count_young_adults, count_adults, count_seniors,
                avg_estimated_age, ethnicity_breakdown,
                count_business_attire, count_casual_attire, count_athletic_attire, count_uniform_attire,
                count_working, count_leisure,
                count_walking, count_running, count_standing, count_cycling, count_shopping,
                count_using_phone, count_carrying_items, count_in_groups,
                pct_male, pct_female, pct_working, pct_using_phone,
                avg_confidence_score, processing_status, error_message
            )
            VALUES (
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?
            );
        """
        # Synapse doesn't support MERGE with named params via pyodbc the same way,
        # so we use a simpler INSERT with duplicate check approach:
        self._upsert_interval_aggregate(agg)

    def _upsert_interval_aggregate(self, agg: IntervalAggregate) -> None:
        """Upsert interval aggregate using DELETE + INSERT pattern (Synapse compatible)."""
        delete_sql = """
        DELETE FROM traffic.interval_aggregates
        WHERE feed_id = ? AND interval_start = ?
        """

        insert_sql = """
        INSERT INTO traffic.interval_aggregates (
            aggregate_id, feed_id, interval_start, interval_end,
            total_count, frames_analyzed,
            count_male, count_female, count_gender_unknown,
            count_children, count_teens, count_young_adults, count_adults, count_seniors,
            avg_estimated_age, ethnicity_breakdown,
            count_business_attire, count_casual_attire, count_athletic_attire, count_uniform_attire,
            count_working, count_leisure,
            count_walking, count_running, count_standing, count_cycling, count_shopping,
            count_using_phone, count_carrying_items, count_in_groups,
            pct_male, pct_female, pct_working, pct_using_phone,
            avg_confidence_score, processing_status, error_message
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?
        )
        """

        # Generate a numeric aggregate_id from a hash of feed_id + interval_start
        agg_id = abs(hash(f"{agg.feed_id}_{agg.interval_start.isoformat()}")) % (10**15)

        params = (
            agg_id,
            agg.feed_id,
            agg.interval_start,
            agg.interval_end,
            agg.total_count,
            agg.frames_analyzed,
            agg.count_male,
            agg.count_female,
            agg.count_gender_unknown,
            agg.count_children,
            agg.count_teens,
            agg.count_young_adults,
            agg.count_adults,
            agg.count_seniors,
            agg.avg_estimated_age,
            agg.ethnicity_breakdown_json(),
            agg.count_business_attire,
            agg.count_casual_attire,
            agg.count_athletic_attire,
            agg.count_uniform_attire,
            agg.count_working,
            agg.count_leisure,
            agg.count_walking,
            agg.count_running,
            agg.count_standing,
            agg.count_cycling,
            agg.count_shopping,
            agg.count_using_phone,
            agg.count_carrying_items,
            agg.count_in_groups,
            agg.pct_male,
            agg.pct_female,
            agg.pct_working,
            agg.pct_using_phone,
            agg.avg_confidence_score,
            agg.processing_status,
            agg.error_message,
        )

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(delete_sql, (agg.feed_id, agg.interval_start))
            cursor.execute(insert_sql, params)

        logger.info(
            "Upserted interval aggregate feed_id=%d interval=%s total=%d",
            agg.feed_id,
            agg.interval_start.isoformat(),
            agg.total_count,
        )

    def insert_raw_observations(self, frame_result: FrameAnalysisResult) -> int:
        """Insert raw person observations from a frame analysis. Returns count inserted."""
        if not frame_result.persons:
            return 0

        insert_sql = """
        INSERT INTO traffic.raw_observations (
            observation_id, feed_id, captured_at, interval_start, frame_blob_url,
            gender, age_group, age_estimate_min, age_estimate_max, apparent_ethnicity,
            attire_type, is_working, activity, carrying_items, using_phone, group_size,
            confidence_score, vlm_raw_response, processing_duration_ms, model_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        rows = []
        for i, person in enumerate(frame_result.persons):
            obs_id = abs(hash(
                f"{frame_result.feed_id}_{frame_result.captured_at.isoformat()}_{i}"
            )) % (10**15)

            rows.append((
                obs_id,
                frame_result.feed_id,
                frame_result.captured_at,
                frame_result.interval_start,
                frame_result.frame_blob_url,
                person.gender,
                person.age_group,
                person.age_estimate_min,
                person.age_estimate_max,
                person.apparent_ethnicity,
                person.attire_type,
                1 if person.is_working else (0 if person.is_working is False else None),
                person.activity,
                1 if person.carrying_items else 0,
                1 if person.using_phone else 0,
                person.group_size,
                person.confidence_score,
                frame_result.vlm_raw_response[:4000] if frame_result.vlm_raw_response else None,
                frame_result.processing_duration_ms,
                frame_result.model_version,
            ))

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(insert_sql, rows)

        logger.info(
            "Inserted %d raw observations for feed_id=%d at %s",
            len(rows),
            frame_result.feed_id,
            frame_result.captured_at.isoformat(),
        )
        return len(rows)

    def log_analysis_job(
        self,
        job_id: str,
        feed_id: int,
        interval_start: datetime,
        status: str,
        frames_captured: Optional[int] = None,
        persons_detected: Optional[int] = None,
        vlm_calls_made: Optional[int] = None,
        total_tokens_used: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Log an analysis job record."""
        sql = """
        INSERT INTO traffic.analysis_jobs (
            job_id, feed_id, interval_start, status,
            frames_captured, persons_detected, vlm_calls_made, total_tokens_used,
            duration_seconds, error_message, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        completed_at = datetime.utcnow() if status in ("success", "failed") else None

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (
                job_id, feed_id, interval_start, status,
                frames_captured, persons_detected, vlm_calls_made, total_tokens_used,
                duration_seconds, error_message, completed_at,
            ))

    def get_active_feeds(self) -> list[VideoFeed]:
        """Retrieve all active video feeds."""
        sql = """
        SELECT feed_id, feed_name, feed_url, location_name, latitude, longitude, timezone
        FROM traffic.video_feeds
        WHERE is_active = 1
        ORDER BY feed_id
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()

        return [
            VideoFeed(
                feed_id=row[0],
                feed_name=row[1],
                feed_url=row[2],
                location_name=row[3],
                latitude=row[4],
                longitude=row[5],
                timezone=row[6] or "UTC",
            )
            for row in rows
        ]

    def get_interval_aggregates(
        self,
        feed_id: Optional[int] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 288,  # 24 hours of 5-min intervals
    ) -> list[dict]:
        """Query interval aggregates with optional filters."""
        conditions = ["processing_status = 'complete'"]
        params = []

        if feed_id is not None:
            conditions.append("feed_id = ?")
            params.append(feed_id)
        if start_time is not None:
            conditions.append("interval_start >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("interval_start <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions)

        sql = f"""
        SELECT TOP {limit}
            ia.feed_id, f.feed_name, f.location_name,
            ia.interval_start, ia.interval_end,
            ia.total_count, ia.frames_analyzed,
            ia.count_male, ia.count_female, ia.count_gender_unknown,
            ia.count_children, ia.count_teens, ia.count_young_adults,
            ia.count_adults, ia.count_seniors, ia.avg_estimated_age,
            ia.ethnicity_breakdown,
            ia.count_business_attire, ia.count_casual_attire,
            ia.count_athletic_attire, ia.count_uniform_attire,
            ia.count_working, ia.count_leisure,
            ia.count_walking, ia.count_running, ia.count_standing,
            ia.count_cycling, ia.count_shopping,
            ia.count_using_phone, ia.count_carrying_items, ia.count_in_groups,
            ia.pct_male, ia.pct_female, ia.pct_working, ia.pct_using_phone,
            ia.avg_confidence_score
        FROM traffic.interval_aggregates ia
        JOIN traffic.video_feeds f ON f.feed_id = ia.feed_id
        WHERE {where_clause}
        ORDER BY ia.interval_start DESC
        """

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            rows = cursor.fetchall()

        return [dict(zip(columns, row)) for row in rows]

    def execute_custom_query(self, sql: str, params: Optional[list] = None) -> list[dict]:
        """Execute a custom SQL query and return results as list of dicts."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or [])
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
                return [dict(zip(columns, row)) for row in rows]
            return []


# Singleton
_db_client: Optional[SynapseClient] = None


def get_db_client() -> SynapseClient:
    global _db_client
    if _db_client is None:
        _db_client = SynapseClient()
    return _db_client
