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
                max_tokens=4096,
                temperature=0.1,  # Low temperature for consistent categorization
            )

            # Track token usage — store on the result so callers can sum per-job
            # totals without relying on the singleton's lifetime counter.
            tokens_this_call = response.usage.total_tokens if response.usage else 0
            if tokens_this_call == 0:
                # usage can be None or zero for some Azure OpenAI API versions;
                # log so we can detect the issue without silently dropping counts.
                logger.warning(
                    "VLM response.usage is %s for feed_id=%d — token count will be 0 for this call",
                    response.usage,
                    feed_id,
                )
            self._total_tokens_used += tokens_this_call
            result.tokens_this_call = tokens_this_call

            raw_response = response.choices[0].message.content
            result.vlm_raw_response = raw_response

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
            else:
                logger.info(
                    "VLM analysis complete: feed_id=%d, persons=%d, tokens=%d",
                    feed_id,
                    len(persons),
                    tokens_this_call,
                )

        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse VLM JSON response for feed_id=%d: %s — raw: %.500s",
                feed_id,
                e,
                result.vlm_raw_response or "<no response captured>",
            )
            result.error = f"JSON parse error: {e}"
        except Exception as e:
            logger.error("VLM analysis failed: %s", e)
            result.error = str(e)
            raise  # Re-raise for retry logic

        finally:
            result.processing_duration_ms = int((time.time() - start_time) * 1000)

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
                # Add error result so we track the failure
                results.append(FrameAnalysisResult(
                    feed_id=feed_id,
                    feed_url=feed_url,
                    captured_at=captured_at,
                    interval_start=interval_start,
                    frame_blob_url=blob_url,
                    error=str(e),
                ))

        return results


# Singleton
_vlm_analyzer: Optional[VLMAnalyzer] = None


def get_vlm_analyzer() -> VLMAnalyzer:
    global _vlm_analyzer
    if _vlm_analyzer is None:
        _vlm_analyzer = VLMAnalyzer()
    return _vlm_analyzer
