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


def main(mytimer: func.TimerRequest) -> None:
    """
    Timer trigger: runs every 5 minutes (cron: 0 */5 * * * *).
    
    For each active video feed:
    1. Captures frames from the public video stream
    2. Sends frames to VLM for demographic analysis
    3. Aggregates results into 5-minute intervals
    4. Stores aggregates in Synapse Analytics
    """
    utc_now = datetime.now(timezone.utc)
    
    # Calculate the current 5-minute interval bucket
    interval_start = _floor_to_5min(utc_now)
    interval_end = interval_start + timedelta(minutes=5)

    if mytimer.past_due:
        logger.warning("Timer is past due! Running at %s", utc_now.isoformat())

    logger.info(
        "Video scheduler triggered at %s | interval: %s -> %s",
        utc_now.isoformat(),
        interval_start.isoformat(),
        interval_end.isoformat(),
    )

    settings = get_settings()
    db = get_db_client()
    capturer = get_video_capture()
    analyzer = get_vlm_analyzer()

    # Get active feeds from database
    try:
        feeds = db.get_active_feeds()
    except Exception as e:
        logger.error("Failed to fetch active feeds from Synapse: %s", e)
        # Fall back to environment variable feeds
        feeds = _get_feeds_from_env(settings)

    if not feeds:
        logger.warning("No active video feeds configured. Skipping analysis.")
        return

    logger.info("Processing %d active video feeds", len(feeds))

    total_persons = 0
    total_frames = 0
    total_tokens = 0

    for feed in feeds:
        job_id = str(uuid.uuid4())
        job_start = time.time()

        logger.info(
            "Processing feed: %s (%s) | job_id=%s",
            feed.feed_name,
            feed.feed_url,
            job_id,
        )

        try:
            # Step 1: Capture frames from the video stream
            frames = capturer.capture_frames(
                feed_url=feed.feed_url,
                feed_id=feed.feed_id,
                interval_start=interval_start,
                num_frames=settings.frames_per_interval,
                # 10 s sleep between frames: 5 frames × 10 s = ~50 s total capture
                # time, well within the 10-min function timeout.  The old value of
                # 60 s caused the function to burn through thousands of cap.grab()
                # calls trying to skip video, which timed out on live HLS streams.
                frame_interval_seconds=10.0,
            )

            total_frames += len(frames)

            if not frames:
                logger.warning(
                    "No frames captured for feed_id=%d (%s)",
                    feed.feed_id,
                    feed.feed_name,
                )
                db.log_analysis_job(
                    job_id=job_id,
                    feed_id=feed.feed_id,
                    interval_start=interval_start,
                    status="failed",
                    frames_captured=0,
                    error_message="No frames captured from stream",
                    duration_seconds=time.time() - job_start,
                )
                continue

            # Step 2: Analyze frames with VLM
            frame_results = analyzer.analyze_multiple_frames(
                frames=frames,
                feed_id=feed.feed_id,
                feed_url=feed.feed_url,
                interval_start=interval_start,
            )

            # Step 3: Insert raw observations
            persons_in_interval = 0
            for frame_result in frame_results:
                if not frame_result.error:
                    obs_count = db.insert_raw_observations(frame_result)
                    persons_in_interval += obs_count

            total_persons += persons_in_interval

            # Step 4: Build and store interval aggregate
            aggregate = IntervalAggregate.from_frame_results(
                feed_id=feed.feed_id,
                interval_start=interval_start,
                interval_end=interval_end,
                frame_results=frame_results,
            )

            db.insert_interval_aggregate(aggregate)

            # Step 5: Log job success
            job_duration = time.time() - job_start
            tokens_used = analyzer.total_tokens_used
            total_tokens = tokens_used

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

            logger.info(
                "Feed %s complete: frames=%d, persons=%d, tokens=%d, duration=%.1fs",
                feed.feed_name,
                len(frames),
                persons_in_interval,
                tokens_used,
                job_duration,
            )

        except Exception as e:
            logger.exception(
                "Failed to process feed_id=%d (%s): %s",
                feed.feed_id,
                feed.feed_name,
                e,
            )
            db.log_analysis_job(
                job_id=job_id,
                feed_id=feed.feed_id,
                interval_start=interval_start,
                status="failed",
                error_message=str(e)[:2000],
                duration_seconds=time.time() - job_start,
            )

    logger.info(
        "Scheduler run complete: feeds=%d, frames=%d, persons=%d, tokens=%d",
        len(feeds),
        total_frames,
        total_persons,
        total_tokens,
    )


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
