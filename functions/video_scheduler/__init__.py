"""
Azure Function: video_scheduler
Timer-triggered function that runs every 5 minutes.
Fetches active video feeds from Synapse and dispatches capture+analysis jobs.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.servicebus import ServiceBusClient, ServiceBusMessage

# Add parent to path for shared imports
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.config import get_settings
from shared.db_client import get_db_client
from shared.video_capture import get_video_capture
from shared.vlm_analyzer import get_vlm_analyzer
from shared.models import IntervalAggregate

logger = logging.getLogger(__name__)

_run_log: list[str] = []


def _log(msg: str) -> None:
    """Log to both the standard logger and an in-memory list for blob upload."""
    logger.info(msg)
    _run_log.append(msg)


def _flush_log_to_blob(settings) -> None:
    """Upload the run log to blob storage so we can read it externally."""
    try:
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient.from_connection_string(settings.storage_connection_string)
        content = "\n".join(_run_log)
        blob = client.get_blob_client("video-frames", "scheduler-run.log")
        blob.upload_blob(content.encode(), overwrite=True)
    except Exception as e:
        logger.error("Failed to flush log to blob: %s", e)


def main(mytimer: func.TimerRequest) -> None:
    """
    Timer trigger: runs every 5 minutes (cron: 0 */5 * * * *).
    """
    _run_log.clear()
    utc_now = datetime.now(timezone.utc)
    interval_start = _floor_to_5min(utc_now)
    interval_end = interval_start + timedelta(minutes=5)

    if mytimer.past_due:
        _log(f"WARN: Timer is past due! Running at {utc_now.isoformat()}")

    _log(f"START: scheduler triggered at {utc_now.isoformat()} interval={interval_start.isoformat()}")

    try:
        settings = get_settings()
        _log("OK: get_settings()")
    except Exception as e:
        _log(f"FAIL: get_settings(): {e}")
        return

    try:
        db = get_db_client()
        _log("OK: get_db_client()")
    except Exception as e:
        _log(f"FAIL: get_db_client(): {e}")
        _flush_log_to_blob_raw(settings)
        return

    try:
        capturer = get_video_capture()
        _log("OK: get_video_capture()")
    except Exception as e:
        _log(f"FAIL: get_video_capture(): {e}")
        _flush_log_to_blob_raw(settings)
        return

    try:
        analyzer = get_vlm_analyzer()
        _log("OK: get_vlm_analyzer()")
    except Exception as e:
        _log(f"FAIL: get_vlm_analyzer(): {e}")
        _flush_log_to_blob_raw(settings)
        return

    # Get active feeds from database
    try:
        feeds = db.get_active_feeds()
        _log(f"OK: db.get_active_feeds() returned {len(feeds)} feeds")
    except Exception as e:
        _log(f"WARN: db.get_active_feeds() failed: {e} — falling back to env var feeds")
        feeds = _get_feeds_from_env(settings)
        _log(f"INFO: env var feeds: {len(feeds)} feeds")

    if not feeds:
        _log("WARN: No active video feeds configured. Skipping analysis.")
        _flush_log_to_blob_raw(settings)
        return

    total_persons = 0
    total_frames = 0
    total_tokens = 0

    for feed in feeds:
        job_id = str(uuid.uuid4())
        job_start = time.time()
        _log(f"INFO: Processing feed_id={feed.feed_id} name={feed.feed_name} url={feed.feed_url}")

        try:
            # Step 1: Capture frames — attach blob handler to root logger to capture all errors
            import logging as _logging

            class _BlobLogHandler(_logging.Handler):
                def emit(self, record):
                    _run_log.append(f"[{record.name}] {record.levelname}: {self.format(record)}")

            _blob_handler = _BlobLogHandler()
            _blob_handler.setLevel(_logging.WARNING)
            _root_logger = _logging.getLogger()
            _root_logger.addHandler(_blob_handler)

            _log(f"INFO: Starting capture_frames for feed_id={feed.feed_id}")
            frames = capturer.capture_frames(
                feed_url=feed.feed_url,
                feed_id=feed.feed_id,
                interval_start=interval_start,
                num_frames=settings.frames_per_interval,
                frame_interval_seconds=10.0,
            )
            _root_logger.removeHandler(_blob_handler)
            _log(f"INFO: capture_frames returned {len(frames)} frames for feed_id={feed.feed_id}")
            total_frames += len(frames)

            if not frames:
                _log(f"WARN: No frames captured for feed_id={feed.feed_id}")
                try:
                    db.log_analysis_job(
                        job_id=job_id,
                        feed_id=feed.feed_id,
                        interval_start=interval_start,
                        status="failed",
                        frames_captured=0,
                        error_message="No frames captured from stream",
                        duration_seconds=time.time() - job_start,
                    )
                except Exception as dbe:
                    _log(f"WARN: db.log_analysis_job failed: {dbe}")
                _flush_log_to_blob_raw(settings)
                continue

            # Step 2: Analyze frames with VLM
            _log(f"INFO: Starting VLM analysis for {len(frames)} frames")
            frame_results = analyzer.analyze_multiple_frames(
                frames=frames,
                feed_id=feed.feed_id,
                feed_url=feed.feed_url,
                interval_start=interval_start,
            )
            _log(f"INFO: VLM analysis complete: {len(frame_results)} results")

            # Step 3: Insert raw observations
            persons_in_interval = 0
            for frame_result in frame_results:
                if not frame_result.error:
                    try:
                        obs_count = db.insert_raw_observations(frame_result)
                        persons_in_interval += obs_count
                    except Exception as dbe:
                        _log(f"WARN: insert_raw_observations failed: {dbe}")

            total_persons += persons_in_interval

            # Step 4: Build and store interval aggregate
            aggregate = IntervalAggregate.from_frame_results(
                feed_id=feed.feed_id,
                interval_start=interval_start,
                interval_end=interval_end,
                frame_results=frame_results,
            )
            try:
                db.insert_interval_aggregate(aggregate)
                _log(f"OK: insert_interval_aggregate feed_id={feed.feed_id} total={aggregate.total_count}")
            except Exception as dbe:
                _log(f"WARN: insert_interval_aggregate failed: {dbe}")

            # Step 5: Log job success
            job_duration = time.time() - job_start
            tokens_used = analyzer.total_tokens_used
            total_tokens = tokens_used
            try:
                db.log_analysis_job(
                    job_id=job_id,
                    feed_id=feed.feed_id,
                    interval_start=interval_start,
                    status="success",
                    frames_captured=len(frames),
                    persons_detected=persons_in_interval,
                    vlm_calls_made=len(frames),
                    total_tokens_used=tokens_used,
                    duration_seconds=job_duration,
                )
            except Exception as dbe:
                _log(f"WARN: log_analysis_job failed: {dbe}")

            _log(f"OK: feed_id={feed.feed_id} frames={len(frames)} persons={persons_in_interval} tokens={tokens_used} duration={job_duration:.1f}s")

        except Exception as e:
            import traceback
            _log(f"FAIL: feed_id={feed.feed_id}: {traceback.format_exc()[:1000]}")
            try:
                db.log_analysis_job(
                    job_id=job_id,
                    feed_id=feed.feed_id,
                    interval_start=interval_start,
                    status="failed",
                    error_message=str(e)[:2000],
                    duration_seconds=time.time() - job_start,
                )
            except Exception:
                pass

        _flush_log_to_blob_raw(settings)

    _log(f"DONE: feeds={len(feeds)} frames={total_frames} persons={total_persons} tokens={total_tokens}")
    _flush_log_to_blob_raw(settings)


def _flush_log_to_blob_raw(settings) -> None:
    """Upload the run log to blob storage."""
    try:
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient.from_connection_string(settings.storage_connection_string)
        content = "\n".join(_run_log)
        blob = client.get_blob_client("video-frames", "scheduler-run.log")
        blob.upload_blob(content.encode(), overwrite=True)
    except Exception as e:
        logger.error("Failed to flush log to blob: %s", e)


def _floor_to_5min(dt: datetime) -> datetime:
    """Floor a datetime to the nearest 5-minute boundary."""
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)


def _get_feeds_from_env(settings) -> list:
    """Build feed objects from environment variable as fallback."""
    from shared.models import VideoFeed

    feeds = []
    for i, url in enumerate(settings.video_feed_list, start=1):
        feeds.append(VideoFeed(
            feed_id=i,
            feed_name=f"Feed {i}",
            feed_url=url,
            location_name="Unknown",
            timezone="UTC",
        ))
    return feeds
