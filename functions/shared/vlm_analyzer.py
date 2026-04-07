"""
Vision Language Model (VLM) analyzer using Azure OpenAI GPT-5.4.
Analyzes video frames to extract pedestrian demographic data.

Model: gpt-5.3-chat (2025-02-01) — vision-capable (text + image)
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

from openai import AzureOpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .config import get_settings
from .models import PersonObservation, FrameAnalysisResult

logger = logging.getLogger(__name__)

# Blob name used for VLM analysis debug logging (mirrors capture-status.log pattern)
VLM_LOG_BLOB = "vlm-analysis.log"

# Model version reference (for documentation / alerting purposes)
# gpt-5.3-chat (2025-02-01): vision-capable (text + image)
# Used in place of gpt-5.4 which is restricted in this subscription
# API version 2025-01-01-preview: current stable preview as of Mar 2026
OPENAI_MODEL_NAME = "gpt-5.3-chat"
OPENAI_MODEL_VERSION = "2025-02-01"
OPENAI_MODEL_RETIREMENT = "2027-09-05"
OPENAI_API_VERSION = "2025-01-01-preview"

# ─── System prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert pedestrian traffic analyst. Your task is to analyze video frames 
from public spaces and provide structured demographic data about visible pedestrians.

IMPORTANT GUIDELINES:
- All observations are estimates based on visual appearance only
- Be objective and consistent in your categorizations
- If you cannot determine a characteristic, use null
- Focus on aggregate patterns, not individual identification
- This data is used for urban planning and retail analytics purposes only

For each visible person, provide:
1. gender: "male", "female", or "unknown"
2. age_group: "child" (0-12), "teen" (13-17), "young_adult" (18-35), "adult" (36-60), "senior" (60+)
3. age_estimate_min and age_estimate_max: numeric range
4. apparent_ethnicity: "white", "black", "hispanic", "east_asian", "south_asian", "middle_eastern", "mixed", or "unknown"
5. attire_type: "business", "casual", "athletic", "uniform", "formal", or "other"
6. is_working: true if appears to be commuting/working (business attire, briefcase, purposeful stride), false for leisure
7. activity: "walking", "running", "standing", "cycling", "shopping", "sitting", or "other"
8. carrying_items: true/false (bags, briefcase, packages)
9. using_phone: true/false (visibly using mobile device)
10. group_size: 1 if alone, 2+ if in a group
11. confidence_score: 0.0-1.0 (your confidence in this observation)

Also provide:
- scene_description: brief description of the scene
- weather_conditions: "sunny", "cloudy", "rainy", "snowy", "night", or "unknown"
- time_of_day: "morning", "afternoon", "evening", or "night"
- crowd_density: "sparse" (<5 people), "moderate" (5-20), "dense" (20-50), "very_dense" (50+)

Respond ONLY with valid JSON matching the schema provided."""

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "persons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "person_index": {"type": "integer"},
                    "gender": {"type": ["string", "null"]},
                    "age_group": {"type": ["string", "null"]},
                    "age_estimate_min": {"type": ["integer", "null"]},
                    "age_estimate_max": {"type": ["integer", "null"]},
                    "apparent_ethnicity": {"type": ["string", "null"]},
                    "attire_type": {"type": ["string", "null"]},
                    "is_working": {"type": ["boolean", "null"]},
                    "activity": {"type": ["string", "null"]},
                    "carrying_items": {"type": ["boolean", "null"]},
                    "using_phone": {"type": ["boolean", "null"]},
                    "group_size": {"type": ["integer", "null"]},
                    "confidence_score": {"type": "number"},
                },
                "required": ["person_index", "confidence_score"],
            },
        },
        "scene_description": {"type": "string"},
        "weather_conditions": {"type": ["string", "null"]},
        "time_of_day": {"type": ["string", "null"]},
        "crowd_density": {"type": ["string", "null"]},
        "total_persons_detected": {"type": "integer"},
    },
    "required": ["persons", "total_persons_detected"],
}


class VLMAnalyzer:
    """Analyzes video frames using Azure OpenAI GPT-5.4 Vision (text + image)."""

    def __init__(self):
        self._settings = get_settings()
        # Client is created lazily on first use to avoid httpx proxy-detection
        # errors at startup when OpenAI env vars may not yet be available.
        self._client: Optional[AzureOpenAI] = None
        self._deployment = self._settings.openai_deployment
        # Lifetime cumulative counter — use tokens_this_call on FrameAnalysisResult
        # for per-job accounting instead of reading this directly.
        self._total_tokens_used = 0
        # Blob client for status logging — initialised lazily
        self._blob_client = None

    def _get_blob_client(self):
        """Return (and lazily create) the BlobServiceClient for status logging."""
        if self._blob_client is None:
            try:
                from azure.storage.blob import BlobServiceClient
                self._blob_client = BlobServiceClient.from_connection_string(
                    self._settings.storage_connection_string
                )
            except Exception as e:
                logger.warning("VLM blob logger: could not create BlobServiceClient: %s", e)
        return self._blob_client

    def _write_status_blob(self, msg: str) -> None:
        """Append a timestamped status message to vlm-analysis.log in blob storage.

        Mirrors the capture-status.log pattern used by VideoCapture so that
        VLM execution details (persons detected, tokens, raw responses, errors)
        are persisted and inspectable outside the container's live log stream.
        """
        try:
            import datetime as _dt
            ts = _dt.datetime.utcnow().isoformat()
            blob_client_svc = self._get_blob_client()
            if blob_client_svc is None:
                return
            container = self._settings.frames_container
            existing = ""
            try:
                blob = blob_client_svc.get_blob_client(container, VLM_LOG_BLOB)
                existing = blob.download_blob().readall().decode()
            except Exception:
                pass
            content = existing + f"\n[{ts}] {msg}"
            blob = blob_client_svc.get_blob_client(container, VLM_LOG_BLOB)
            blob.upload_blob(content.encode(), overwrite=True)
        except Exception as e:
            # Non-fatal: never let logging break the analysis pipeline
            logger.debug("VLM blob logger write failed: %s", e)

    def _get_client(self) -> AzureOpenAI:
        """Return (and lazily create) the AzureOpenAI client."""
        if self._client is None:
            settings = self._settings
            if not settings.openai_endpoint or not settings.openai_api_key:
                raise RuntimeError(
                    "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set "
                    "before VLM analysis can run."
                )
            self._client = AzureOpenAI(
                azure_endpoint=settings.openai_endpoint,
                api_key=settings.openai_api_key,
                api_version=settings.openai_api_version,
                http_client=None,  # let openai build its own; avoids stale proxy state
            )
        return self._client

    @property
    def total_tokens_used(self) -> int:
        """Lifetime cumulative token counter across all calls since process start."""
        return self._total_tokens_used

    def encode_image_bytes(self, image_bytes: bytes) -> str:
        """Base64-encode image bytes for the API."""
        return base64.b64encode(image_bytes).decode("utf-8")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def analyze_frame(
        self,
        image_bytes: bytes,
        feed_id: int,
        feed_url: str,
        captured_at,
        interval_start,
        frame_blob_url: Optional[str] = None,
        max_persons: int = 20,
    ) -> FrameAnalysisResult:
        """
        Analyze a single video frame using GPT-5.4 Vision (text + image).

        Args:
            image_bytes: Raw JPEG/PNG image bytes
            feed_id: Database feed ID
            feed_url: Source video URL
            captured_at: UTC datetime of frame capture
            interval_start: 5-minute interval bucket start
            frame_blob_url: Azure Blob URL where frame is stored
            max_persons: Maximum persons to analyze per frame

        Returns:
            FrameAnalysisResult with demographic data
        """
        start_time = time.time()

        result = FrameAnalysisResult(
            feed_id=feed_id,
            feed_url=feed_url,
            captured_at=captured_at,
            interval_start=interval_start,
            frame_blob_url=frame_blob_url,
            model_version=self._deployment,
        )

        self._write_status_blob(
            f"vlm: START feed_id={feed_id} frame={frame_blob_url or 'no-url'} "
            f"image_bytes={len(image_bytes)} interval={interval_start}"
        )

        try:
            b64_image = self.encode_image_bytes(image_bytes)

            user_message = (
                f"Analyze this video frame from a public space. "
                f"Identify and describe up to {max_persons} visible pedestrians. "
                f"Return structured JSON following the schema exactly. "
                f"If no people are visible, return an empty persons array."
            )

            response = self._get_client().chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_message},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_image}",
                                    "detail": "high",
                                },
                            },
                        ],
                    },
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=4096,
                # temperature not set — gpt-5.3-chat only supports default (1)
            )

            # Track token usage — store on the result so callers can sum per-job
            # totals without relying on the singleton's lifetime counter.
            tokens_this_call = response.usage.total_tokens if response.usage else 0
            prompt_tokens = response.usage.prompt_tokens if response.usage else 0
            completion_tokens = response.usage.completion_tokens if response.usage else 0
            if tokens_this_call == 0:
                # usage can be None or zero for some Azure OpenAI API versions;
                # log so we can detect the issue without silently dropping counts.
                logger.warning(
                    "VLM response.usage is %s for feed_id=%d — token count will be 0 for this call",
                    response.usage,
                    feed_id,
                )
                self._write_status_blob(
                    f"vlm: WARN feed_id={feed_id} response.usage={response.usage} "
                    f"finish_reason={response.choices[0].finish_reason if response.choices else 'none'}"
                )
            self._total_tokens_used += tokens_this_call
            result.tokens_this_call = tokens_this_call

            raw_response = response.choices[0].message.content
            result.vlm_raw_response = raw_response

            self._write_status_blob(
                f"vlm: RESPONSE feed_id={feed_id} "
                f"tokens={tokens_this_call} (prompt={prompt_tokens} completion={completion_tokens}) "
                f"finish_reason={response.choices[0].finish_reason} "
                f"raw_len={len(raw_response or '')} "
                f"raw={repr((raw_response or '')[:500])}"
            )

            # Parse the JSON response
            parsed = json.loads(raw_response)

            # Build PersonObservation objects
            persons = []
            for p_data in parsed.get("persons", [])[:max_persons]:
                try:
                    person = PersonObservation(
                        person_index=p_data.get("person_index", len(persons) + 1),
                        gender=p_data.get("gender"),
                        age_group=p_data.get("age_group"),
                        age_estimate_min=p_data.get("age_estimate_min"),
                        age_estimate_max=p_data.get("age_estimate_max"),
                        apparent_ethnicity=p_data.get("apparent_ethnicity"),
                        attire_type=p_data.get("attire_type"),
                        is_working=p_data.get("is_working"),
                        activity=p_data.get("activity"),
                        carrying_items=p_data.get("carrying_items"),
                        using_phone=p_data.get("using_phone"),
                        group_size=p_data.get("group_size"),
                        confidence_score=float(p_data.get("confidence_score", 0.7)),
                    )
                    persons.append(person)
                except Exception as e:
                    logger.warning("Failed to parse person observation: %s - %s", p_data, e)
                    continue

            result.persons = persons
            result.total_persons_detected = parsed.get("total_persons_detected", len(persons))
            result.scene_description = parsed.get("scene_description")
            result.weather_conditions = parsed.get("weather_conditions")
            result.time_of_day = parsed.get("time_of_day")
            result.crowd_density = parsed.get("crowd_density")

            if len(persons) == 0:
                logger.warning(
                    "VLM returned 0 persons for feed_id=%d — raw response: %.500s",
                    feed_id,
                    raw_response,
                )
                self._write_status_blob(
                    f"vlm: ZERO_PERSONS feed_id={feed_id} "
                    f"scene={result.scene_description!r} "
                    f"crowd_density={result.crowd_density!r} "
                    f"time_of_day={result.time_of_day!r} "
                    f"weather={result.weather_conditions!r} "
                    f"tokens={tokens_this_call} "
                    f"frame={frame_blob_url or 'no-url'}"
                )
            else:
                logger.info(
                    "VLM analysis complete: feed_id=%d, persons=%d, tokens=%d",
                    feed_id,
                    len(persons),
                    tokens_this_call,
                )
                self._write_status_blob(
                    f"vlm: OK feed_id={feed_id} persons={len(persons)} "
                    f"tokens={tokens_this_call} "
                    f"scene={result.scene_description!r} "
                    f"crowd_density={result.crowd_density!r} "
                    f"frame={frame_blob_url or 'no-url'}"
                )

        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse VLM JSON response for feed_id=%d: %s — raw: %.500s",
                feed_id,
                e,
                result.vlm_raw_response or "<no response captured>",
            )
            result.error = f"JSON parse error: {e}"
            self._write_status_blob(
                f"vlm: JSON_ERROR feed_id={feed_id} error={e} "
                f"raw={repr((result.vlm_raw_response or '')[:500])}"
            )
        except Exception as e:
            logger.error("VLM analysis failed: %s", e)
            result.error = str(e)
            self._write_status_blob(
                f"vlm: EXCEPTION feed_id={feed_id} error={type(e).__name__}: {e}"
            )
            raise  # Re-raise for retry logic

        finally:
            result.processing_duration_ms = int((time.time() - start_time) * 1000)
            self._write_status_blob(
                f"vlm: DONE feed_id={feed_id} "
                f"duration_ms={result.processing_duration_ms} "
                f"persons={len(result.persons) if result.persons else 0} "
                f"error={result.error or 'none'}"
            )

        return result

    def analyze_multiple_frames(
        self,
        frames: list[tuple[bytes, str]],  # (image_bytes, blob_url)
        feed_id: int,
        feed_url: str,
        interval_start,
    ) -> list[FrameAnalysisResult]:
        """
        Analyze multiple frames from a single 5-minute interval.

        Args:
            frames: List of (image_bytes, blob_url) tuples
            feed_id: Database feed ID
            feed_url: Source video URL
            interval_start: 5-minute interval bucket start

        Returns:
            List of FrameAnalysisResult objects
        """
        from datetime import datetime, timedelta

        results = []
        interval_duration = timedelta(minutes=5)
        frame_count = len(frames)

        self._write_status_blob(
            f"vlm: INTERVAL_START feed_id={feed_id} frames={frame_count} interval={interval_start}"
        )

        for i, (image_bytes, blob_url) in enumerate(frames):
            # Distribute frame timestamps evenly across the interval
            offset_seconds = (interval_duration.total_seconds() / max(frame_count, 1)) * i
            captured_at = interval_start + timedelta(seconds=offset_seconds)

            try:
                result = self.analyze_frame(
                    image_bytes=image_bytes,
                    feed_id=feed_id,
                    feed_url=feed_url,
                    captured_at=captured_at,
                    interval_start=interval_start,
                    frame_blob_url=blob_url,
                    max_persons=self._settings.max_persons_per_frame,
                )
                results.append(result)
            except Exception as e:
                logger.error("Frame %d/%d analysis failed: %s", i + 1, frame_count, e)
                self._write_status_blob(
                    f"vlm: FRAME_EXCEPTION feed_id={feed_id} frame={i+1}/{frame_count} "
                    f"error={type(e).__name__}: {e}"
                )
                # Add error result so we track the failure
                results.append(FrameAnalysisResult(
                    feed_id=feed_id,
                    feed_url=feed_url,
                    captured_at=captured_at,
                    interval_start=interval_start,
                    frame_blob_url=blob_url,
                    error=str(e),
                ))

        total_persons = sum(len(r.persons) for r in results if r.persons)
        total_tokens = sum(r.tokens_this_call for r in results)
        error_count = sum(1 for r in results if r.error)
        self._write_status_blob(
            f"vlm: INTERVAL_DONE feed_id={feed_id} interval={interval_start} "
            f"frames={frame_count} persons={total_persons} tokens={total_tokens} errors={error_count}"
        )

        return results


# Singleton
_vlm_analyzer: Optional[VLMAnalyzer] = None


def get_vlm_analyzer() -> VLMAnalyzer:
    global _vlm_analyzer
    if _vlm_analyzer is None:
        _vlm_analyzer = VLMAnalyzer()
    return _vlm_analyzer
