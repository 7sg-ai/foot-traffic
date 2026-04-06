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
from .models import IntervalAggregate, FrameAnalysisResult, VideoFeed, ZeroPersonFrame

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
        """Insert or update a 5-minute interval aggregate (DELETE + INSERT upsert)."""
        self._upsert_interval_aggregate(agg)

    def _upsert_interval_aggregate(self, agg: IntervalAggregate) -> None:
        """Upsert interval aggregate using DELETE + INSERT pattern (Synapse compatible).

        Synapse Dedicated SQL Pool does not support MERGE with pyodbc named params,
        and INSERT ... VALUES does not allow function calls (e.g. GETUTCDATE()).
        We use INSERT ... SELECT so that GETUTCDATE() is evaluated server-side.
        """
        delete_sql = """
        DELETE FROM traffic.interval_aggregates
        WHERE feed_id = ? AND interval_start = ?
        """

        # INSERT ... SELECT allows GETUTCDATE() for created_at / updated_at.
        # Column order must exactly match the SELECT column order.
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
            avg_confidence_score, processing_status, error_message,
            created_at, updated_at
        )
        SELECT
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
            ?, ?, ?,
            GETUTCDATE(), GETUTCDATE()
        """

        # Generate a stable numeric aggregate_id from feed_id + interval_start
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
        """Insert raw person observations from a frame analysis. Returns count inserted.

        Uses INSERT ... SELECT so that GETUTCDATE() is evaluated server-side for
        created_at (Synapse Dedicated SQL Pool does not allow function calls in VALUES).

        When the VLM returns zero persons (but no error), we insert a single
        sentinel row with NULL demographic fields so that:
          1. The raw VLM response is preserved for debugging.
          2. analysis_jobs.persons_detected correctly reflects 0 (not missing data).
        """
        if not frame_result.persons:
            # Still persist the raw response for zero-person frames so we can
            # inspect what the model actually returned.
            if frame_result.vlm_raw_response:
                self._insert_zero_person_sentinel(frame_result)
            return 0

        # INSERT ... SELECT allows GETUTCDATE() for created_at.
        insert_sql = """
        INSERT INTO traffic.raw_observations (
            observation_id, feed_id, captured_at, interval_start, frame_blob_url,
            gender, age_group, age_estimate_min, age_estimate_max, apparent_ethnicity,
            attire_type, is_working, activity, carrying_items, using_phone, group_size,
            confidence_score, vlm_raw_response, processing_duration_ms, model_version,
            created_at
        )
        SELECT
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            GETUTCDATE()
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

    def _insert_zero_person_sentinel(self, frame_result: FrameAnalysisResult) -> None:
        """Insert a sentinel row for a frame where the VLM returned 0 persons.

        All demographic fields are NULL; the raw VLM response is stored so we
        can diagnose whether the model is genuinely seeing an empty scene or
        returning a malformed / unexpected JSON structure.
        """
        insert_sql = """
        INSERT INTO traffic.raw_observations (
            observation_id, feed_id, captured_at, interval_start, frame_blob_url,
            gender, age_group, age_estimate_min, age_estimate_max, apparent_ethnicity,
            attire_type, is_working, activity, carrying_items, using_phone, group_size,
            confidence_score, vlm_raw_response, processing_duration_ms, model_version,
            created_at
        )
        SELECT
            ?, ?, ?, ?, ?,
            NULL, NULL, NULL, NULL, NULL,
            NULL, NULL, NULL, NULL, NULL, NULL,
            NULL, ?, ?, ?,
            GETUTCDATE()
        """
        obs_id = abs(hash(
            f"{frame_result.feed_id}_{frame_result.captured_at.isoformat()}_sentinel"
        )) % (10**15)

        params = (
            obs_id,
            frame_result.feed_id,
            frame_result.captured_at,
            frame_result.interval_start,
            frame_result.frame_blob_url,
            frame_result.vlm_raw_response[:4000] if frame_result.vlm_raw_response else None,
            frame_result.processing_duration_ms,
            frame_result.model_version,
        )

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(insert_sql, params)
            logger.info(
                "Inserted zero-person sentinel for feed_id=%d at %s (raw_response len=%d)",
                frame_result.feed_id,
                frame_result.captured_at.isoformat(),
                len(frame_result.vlm_raw_response or ""),
            )
        except Exception as e:
            # Non-fatal: log and continue — don't let diagnostic writes block the pipeline
            logger.warning(
                "Failed to insert zero-person sentinel for feed_id=%d: %s",
                frame_result.feed_id,
                e,
            )

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
        """Log an analysis job record.

        Uses INSERT ... SELECT so that GETUTCDATE() is evaluated server-side for
        started_at (Synapse Dedicated SQL Pool does not allow function calls in VALUES).
        completed_at is only set for terminal statuses (success / failed).
        """
        completed_at = datetime.utcnow() if status in ("success", "failed") else None

        if completed_at is not None:
            sql = """
            INSERT INTO traffic.analysis_jobs (
                job_id, feed_id, interval_start, status,
                frames_captured, persons_detected, vlm_calls_made, total_tokens_used,
                duration_seconds, error_message,
                started_at, completed_at
            )
            SELECT
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                GETUTCDATE(), ?
            """
            params = (
                job_id, feed_id, interval_start, status,
                frames_captured, persons_detected, vlm_calls_made, total_tokens_used,
                duration_seconds, error_message,
                completed_at,
            )
        else:
            sql = """
            INSERT INTO traffic.analysis_jobs (
                job_id, feed_id, interval_start, status,
                frames_captured, persons_detected, vlm_calls_made, total_tokens_used,
                duration_seconds, error_message,
                started_at
            )
            SELECT
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                GETUTCDATE()
            """
            params = (
                job_id, feed_id, interval_start, status,
                frames_captured, persons_detected, vlm_calls_made, total_tokens_used,
                duration_seconds, error_message,
            )

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)

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

    def get_zero_person_frames(
        self,
        lookback_days: int = 7,
        limit: int = 50,
    ) -> list:
        """Return sentinel rows for frames where the VLM detected 0 persons.

        Sentinel rows are identified by gender IS NULL (all demographic fields
        are NULL) and frame_blob_url IS NOT NULL.  We exclude frames that have
        no stored blob — those cannot be reprocessed.

        Returns a list of ZeroPersonFrame objects ordered oldest-first so we
        reprocess the most stale data first.
        """
        cutoff = __import__("datetime").datetime.utcnow() - __import__("datetime").timedelta(days=lookback_days)

        sql = f"""
        SELECT TOP {limit}
            feed_id,
            captured_at,
            interval_start,
            frame_blob_url
        FROM traffic.raw_observations
        WHERE gender IS NULL
          AND frame_blob_url IS NOT NULL
          AND frame_blob_url <> ''
          AND captured_at >= ?
        ORDER BY captured_at ASC
        """

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (cutoff,))
            rows = cursor.fetchall()

        result = [
            ZeroPersonFrame(
                feed_id=row[0],
                captured_at=row[1],
                interval_start=row[2],
                frame_blob_url=row[3],
            )
            for row in rows
        ]

        logger.info(
            "get_zero_person_frames: found %d sentinel rows (lookback=%d days, limit=%d)",
            len(result),
            lookback_days,
            limit,
        )
        return result

    def delete_zero_person_sentinel(
        self,
        feed_id: int,
        captured_at,
    ) -> None:
        """Delete the sentinel row(s) for a specific frame before reprocessing.

        Matches on feed_id + captured_at + gender IS NULL to avoid accidentally
        removing real observations.
        """
        sql = """
        DELETE FROM traffic.raw_observations
        WHERE feed_id = ?
          AND captured_at = ?
          AND gender IS NULL
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (feed_id, captured_at))

        logger.info(
            "Deleted zero-person sentinel for feed_id=%d captured_at=%s",
            feed_id,
            captured_at,
        )

    def get_frame_results_for_interval(
        self,
        feed_id: int,
        interval_start,
    ) -> list:
        """Reconstruct FrameAnalysisResult objects from raw_observations for an interval.

        Used when rebuilding an interval aggregate after reprocessing.  Only
        non-sentinel rows (gender IS NOT NULL) are included — sentinel rows
        represent zero-person frames and contribute 0 to the aggregate.

        Returns a list of FrameAnalysisResult objects (one per distinct
        captured_at timestamp that has at least one real observation).
        """
        from .models import FrameAnalysisResult, PersonObservation

        sql = """
        SELECT
            captured_at,
            frame_blob_url,
            gender,
            age_group,
            age_estimate_min,
            age_estimate_max,
            apparent_ethnicity,
            attire_type,
            is_working,
            activity,
            carrying_items,
            using_phone,
            group_size,
            confidence_score,
            vlm_raw_response,
            processing_duration_ms,
            model_version
        FROM traffic.raw_observations
        WHERE feed_id = ?
          AND interval_start = ?
          AND gender IS NOT NULL
        ORDER BY captured_at ASC
        """

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (feed_id, interval_start))
            rows = cursor.fetchall()

        # Group rows by captured_at to reconstruct per-frame results
        from collections import defaultdict
        frames_map: dict = defaultdict(list)
        meta_map: dict = {}

        for row in rows:
            (
                captured_at, frame_blob_url,
                gender, age_group, age_estimate_min, age_estimate_max,
                apparent_ethnicity, attire_type, is_working, activity,
                carrying_items, using_phone, group_size, confidence_score,
                vlm_raw_response, processing_duration_ms, model_version,
            ) = row

            key = captured_at
            if key not in meta_map:
                meta_map[key] = {
                    "frame_blob_url": frame_blob_url,
                    "vlm_raw_response": vlm_raw_response,
                    "processing_duration_ms": processing_duration_ms or 0,
                    "model_version": model_version or "",
                }

            person_index = len(frames_map[key]) + 1
            try:
                person = PersonObservation(
                    person_index=person_index,
                    gender=gender,
                    age_group=age_group,
                    age_estimate_min=age_estimate_min,
                    age_estimate_max=age_estimate_max,
                    apparent_ethnicity=apparent_ethnicity,
                    attire_type=attire_type,
                    is_working=bool(is_working) if is_working is not None else None,
                    activity=activity,
                    carrying_items=bool(carrying_items) if carrying_items is not None else None,
                    using_phone=bool(using_phone) if using_phone is not None else None,
                    group_size=group_size,
                    confidence_score=float(confidence_score) if confidence_score is not None else 0.7,
                )
                frames_map[key].append(person)
            except Exception as e:
                logger.warning(
                    "Skipping malformed observation row for feed_id=%d captured_at=%s: %s",
                    feed_id, captured_at, e,
                )

        frame_results = []
        for captured_at, persons in frames_map.items():
            meta = meta_map[captured_at]
            frame_results.append(
                FrameAnalysisResult(
                    feed_id=feed_id,
                    feed_url="",
                    captured_at=captured_at,
                    interval_start=interval_start,
                    frame_blob_url=meta["frame_blob_url"],
                    persons=persons,
                    total_persons_detected=len(persons),
                    vlm_raw_response=meta["vlm_raw_response"],
                    processing_duration_ms=meta["processing_duration_ms"],
                    model_version=meta["model_version"],
                )
            )

        logger.info(
            "get_frame_results_for_interval: feed_id=%d interval=%s → %d frames, %d persons",
            feed_id,
            interval_start,
            len(frame_results),
            sum(len(fr.persons) for fr in frame_results),
        )
        return frame_results

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
