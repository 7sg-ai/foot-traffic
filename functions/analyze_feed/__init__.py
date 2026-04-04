"""
Azure Function: analyze_feed
HTTP-triggered function for on-demand analysis of a specific video feed.
Useful for testing, backfill, and manual triggers from the Streamlit UI.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import azure.functions as func

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.config import get_settings
from shared.db_client import get_db_client
from shared.video_capture import get_video_capture
from shared.vlm_analyzer import get_vlm_analyzer
from shared.models import IntervalAggregate, VideoFeed

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger for on-demand feed analysis.

    POST /api/analyze_feed
    Body: {
        "feed_url": "https://...",
        "feed_id": 1,
        "feed_name": "My Feed",
        "num_frames": 3
    }

    GET /api/analyze_feed?feed_id=1
    Triggers analysis for a registered feed by ID.
    """
    logger.info("analyze_feed HTTP trigger received")

    try:
        # Parse request
        if req.method == "POST":
            try:
                body = req.get_json()
            except ValueError:
                return func.HttpResponse(
                    json.dumps({"error": "Invalid JSON body"}),
                    status_code=400,
                    mimetype="application/json",
                )
            feed_url = body.get("feed_url")
            feed_id = body.get("feed_id", 99)
            feed_name = body.get("feed_name", "Manual Analysis")
            num_frames = min(int(body.get("num_frames", 3)), 10)  # Cap at 10

        elif req.method == "GET":
            feed_id_param = req.params.get("feed_id")
            if not feed_id_param:
                return func.HttpResponse(
                    json.dumps({"error": "feed_id query parameter required"}),
                    status_code=400,
                    mimetype="application/json",
                )
            feed_id = int(feed_id_param)
            feed_url = None
            feed_name = None
            num_frames = int(req.params.get("num_frames", "3"))
        else:
            return func.HttpResponse(
                json.dumps({"error": "Method not allowed"}),
                status_code=405,
                mimetype="application/json",
            )

        settings = get_settings()
        db = get_db_client()

        # If feed_id provided without URL, look up from database
        if feed_url is None:
            feeds = db.get_active_feeds()
            feed = next((f for f in feeds if f.feed_id == feed_id), None)
            if not feed:
                return func.HttpResponse(
                    json.dumps({"error": f"Feed ID {feed_id} not found or inactive"}),
                    status_code=404,
                    mimetype="application/json",
                )
            feed_url = feed.feed_url
            feed_name = feed.feed_name
        else:
            feed = VideoFeed(
                feed_id=feed_id,
                feed_name=feed_name,
                feed_url=feed_url,
            )

        # Calculate interval
        utc_now = datetime.now(timezone.utc)
        interval_start = _floor_to_5min(utc_now)
        interval_end = interval_start + timedelta(minutes=5)

        job_id = str(uuid.uuid4())
        job_start = time.time()

        logger.info(
            "Starting on-demand analysis: feed_id=%d, url=%s, frames=%d, job_id=%s",
            feed_id,
            feed_url,
            num_frames,
            job_id,
        )

        # Capture frames
        capturer = get_video_capture()
        frames = capturer.capture_frames(
            feed_url=feed_url,
            feed_id=feed_id,
            interval_start=interval_start,
            num_frames=num_frames,
            frame_interval_seconds=10.0,
        )

        if not frames:
            return func.HttpResponse(
                json.dumps({
                    "job_id": job_id,
                    "status": "failed",
                    "error": "No frames could be captured from the stream",
                    "feed_id": feed_id,
                    "feed_url": feed_url,
                }),
                status_code=422,
                mimetype="application/json",
            )

        # Analyze frames
        analyzer = get_vlm_analyzer()
        frame_results = analyzer.analyze_multiple_frames(
            frames=frames,
            feed_id=feed_id,
            feed_url=feed_url,
            interval_start=interval_start,
        )

        # Insert raw observations
        total_persons = 0
        for frame_result in frame_results:
            if not frame_result.error:
                total_persons += db.insert_raw_observations(frame_result)

        # Build and store aggregate
        aggregate = IntervalAggregate.from_frame_results(
            feed_id=feed_id,
            interval_start=interval_start,
            interval_end=interval_end,
            frame_results=frame_results,
        )
        db.insert_interval_aggregate(aggregate)

        duration = time.time() - job_start

        # Log job
        db.log_analysis_job(
            job_id=job_id,
            feed_id=feed_id,
            interval_start=interval_start,
            status="success",
            frames_captured=len(frames),
            persons_detected=total_persons,
            vlm_calls_made=len(frames),
            total_tokens_used=analyzer.total_tokens_used,
            duration_seconds=duration,
        )

        # Build response
        response_data = {
            "job_id": job_id,
            "status": "success",
            "feed_id": feed_id,
            "feed_name": feed_name,
            "interval_start": interval_start.isoformat(),
            "interval_end": interval_end.isoformat(),
            "frames_captured": len(frames),
            "persons_detected": total_persons,
            "tokens_used": analyzer.total_tokens_used,
            "duration_seconds": round(duration, 2),
            "aggregate": {
                "total_count": aggregate.total_count,
                "count_male": aggregate.count_male,
                "count_female": aggregate.count_female,
                "count_working": aggregate.count_working,
                "count_leisure": aggregate.count_leisure,
                "pct_male": aggregate.pct_male,
                "pct_female": aggregate.pct_female,
                "pct_working": aggregate.pct_working,
                "age_groups": {
                    "children": aggregate.count_children,
                    "teens": aggregate.count_teens,
                    "young_adults": aggregate.count_young_adults,
                    "adults": aggregate.count_adults,
                    "seniors": aggregate.count_seniors,
                },
                "ethnicity_breakdown": aggregate.ethnicity_breakdown,
                "avg_confidence": aggregate.avg_confidence_score,
            },
        }

        logger.info(
            "On-demand analysis complete: job_id=%s, persons=%d, duration=%.1fs",
            job_id,
            total_persons,
            duration,
        )

        return func.HttpResponse(
            json.dumps(response_data, default=str),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        logger.exception("analyze_feed function failed: %s", e)
        return func.HttpResponse(
            json.dumps({"error": str(e), "status": "error"}),
            status_code=500,
            mimetype="application/json",
        )


def _floor_to_5min(dt: datetime) -> datetime:
    """Floor a datetime to the nearest 5-minute boundary."""
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)
