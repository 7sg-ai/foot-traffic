"""
AI Query Engine for the Streamlit dashboard.
Converts natural language questions into SQL queries against Synapse Analytics,
executes them, and generates human-readable interpretations using Azure OpenAI.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Optional

import pandas as pd
import streamlit as st
from openai import AzureOpenAI

logger = logging.getLogger(__name__)

# ─── Schema context for the LLM ──────────────────────────────────────────────
SCHEMA_CONTEXT = """
You are an expert SQL analyst for a pedestrian foot traffic analytics system.
The database is Azure Synapse Analytics (T-SQL syntax).

AVAILABLE TABLES:

1. traffic.interval_aggregates (main fact table - 5-minute intervals)
   - feed_id INT
   - interval_start DATETIME2  (UTC, 5-minute bucket start)
   - interval_end DATETIME2
   - total_count INT            (total pedestrians in interval)
   - frames_analyzed INT
   - count_male INT
   - count_female INT
   - count_gender_unknown INT
   - count_children INT         (age 0-12)
   - count_teens INT            (age 13-17)
   - count_young_adults INT     (age 18-35)
   - count_adults INT           (age 36-60)
   - count_seniors INT          (age 60+)
   - avg_estimated_age DECIMAL
   - ethnicity_breakdown NVARCHAR(MAX)  (JSON: {"white": 12, "black": 5, ...})
   - count_business_attire INT
   - count_casual_attire INT
   - count_athletic_attire INT
   - count_uniform_attire INT
   - count_working INT          (appears to be working/commuting)
   - count_leisure INT          (appears to be on leisure)
   - count_walking INT
   - count_running INT
   - count_standing INT
   - count_cycling INT
   - count_shopping INT
   - count_using_phone INT
   - count_carrying_items INT
   - count_in_groups INT
   - pct_male DECIMAL           (percentage)
   - pct_female DECIMAL
   - pct_working DECIMAL
   - pct_using_phone DECIMAL
   - avg_confidence_score DECIMAL
   - processing_status NVARCHAR  ('complete', 'pending', 'error')

2. traffic.video_feeds (dimension table)
   - feed_id INT
   - feed_name NVARCHAR(255)
   - feed_url NVARCHAR(2048)
   - location_name NVARCHAR(255)
   - latitude DECIMAL
   - longitude DECIMAL
   - timezone NVARCHAR(64)
   - is_active BIT

3. traffic.raw_observations (individual person observations)
   - observation_id BIGINT
   - feed_id INT
   - captured_at DATETIME2
   - interval_start DATETIME2
   - gender NVARCHAR(32)        ('male', 'female', 'unknown')
   - age_group NVARCHAR(32)     ('child', 'teen', 'young_adult', 'adult', 'senior')
   - apparent_ethnicity NVARCHAR(64)
   - attire_type NVARCHAR(64)   ('business', 'casual', 'athletic', 'uniform', 'formal', 'other')
   - is_working BIT
   - activity NVARCHAR(128)     ('walking', 'running', 'standing', 'cycling', 'shopping', 'sitting', 'other')
   - carrying_items BIT
   - using_phone BIT
   - confidence_score DECIMAL

4. traffic.analysis_jobs (processing log)
   - job_id NVARCHAR(64)
   - feed_id INT
   - interval_start DATETIME2
   - status NVARCHAR(32)        ('running', 'success', 'failed')
   - frames_captured INT
   - persons_detected INT
   - duration_seconds DECIMAL
   - started_at DATETIME2

VIEWS:
- traffic.vw_recent_24h: 24-hour summary per feed
- traffic.vw_hourly_trend_7d: Hourly trends for last 7 days

IMPORTANT SQL RULES:
- Use T-SQL syntax (Azure Synapse)
- Use GETUTCDATE() for current time
- Use DATEADD() for date arithmetic
- Use TOP N instead of LIMIT N
- Always filter by processing_status = 'complete' for interval_aggregates
- Join with traffic.video_feeds to get feed_name and location_name
- For time-based questions, use DATEPART(HOUR, interval_start) for hour of day
- For percentage calculations, handle division by zero with NULLIF()
- Keep queries efficient - avoid SELECT * on large tables
"""

SQL_GENERATION_PROMPT = """Given the user's question and the database schema, generate a T-SQL query.

Rules:
1. Return ONLY the SQL query, no explanation
2. Use T-SQL syntax (Azure Synapse Analytics)
3. Always join with traffic.video_feeds for feed names
4. Filter processing_status = 'complete' for interval_aggregates
5. Use TOP 1000 maximum to prevent huge result sets
6. For "recent" or unspecified time, default to last 24 hours
7. Make the query readable with proper formatting
8. If the question cannot be answered with the available data, return: SELECT 'Data not available for this query' AS message

User question: {question}

Additional context:
- Current time filter: {time_filter}
- Feed filter: {feed_filter}

SQL Query:"""

INTERPRETATION_PROMPT = """You are a data analyst interpreting pedestrian foot traffic data.

The user asked: "{question}"

The SQL query returned these results:
{results_summary}

Provide a clear, concise interpretation of the data in 2-4 sentences. 
- Use specific numbers and percentages from the data
- Highlight the most interesting or actionable insights
- Note any limitations (e.g., "based on VLM estimates")
- Be conversational and helpful

If the data is empty or shows an error, explain what might be happening.
"""


class AIQueryEngine:
    """
    Natural language to SQL query engine using Azure OpenAI.
    Converts user questions into Synapse SQL queries and interprets results.
    """

    def __init__(self):
        # Client is created lazily on first use to avoid httpx proxy-detection
        # errors at startup and to allow the app to load even when OpenAI
        # env vars are not yet available.
        self._client: Optional[AzureOpenAI] = None
        self._endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        self._api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        self._api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
        # gpt-5.3-chat: vision-capable (text + image)
        self._deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.3-chat")

    def _get_client(self) -> AzureOpenAI:
        """Return (and lazily create) the AzureOpenAI client."""
        if self._client is None:
            if not self._endpoint or not self._api_key:
                raise RuntimeError(
                    "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set "
                    "before the AI Query Engine can run."
                )
            self._client = AzureOpenAI(
                azure_endpoint=self._endpoint,
                api_key=self._api_key,
                api_version=self._api_version,
            )
        return self._client

    def generate_sql(
        self,
        question: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        feed_id: Optional[int] = None,
    ) -> str:
        """Generate a SQL query from a natural language question."""

        # Build time filter context
        if start_time and end_time:
            time_filter = (
                f"interval_start BETWEEN '{start_time.strftime('%Y-%m-%d %H:%M')}' "
                f"AND '{end_time.strftime('%Y-%m-%d %H:%M')}'"
            )
        elif start_time:
            time_filter = f"interval_start >= '{start_time.strftime('%Y-%m-%d %H:%M')}'"
        else:
            time_filter = "last 24 hours (DATEADD(HOUR, -24, GETUTCDATE()))"

        feed_filter = f"feed_id = {feed_id}" if feed_id else "all feeds"

        prompt = SQL_GENERATION_PROMPT.format(
            question=question,
            time_filter=time_filter,
            feed_filter=feed_filter,
        )

        response = self._get_client().chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": SCHEMA_CONTEXT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
        )

        sql = response.choices[0].message.content.strip()

        # Clean up markdown code blocks if present
        sql = re.sub(r"```sql\s*", "", sql)
        sql = re.sub(r"```\s*", "", sql)
        sql = sql.strip()

        logger.info("Generated SQL for question '%s': %s", question[:50], sql[:100])
        return sql

    def interpret_results(
        self,
        question: str,
        results_df: pd.DataFrame,
        sql_query: str,
    ) -> str:
        """Generate a natural language interpretation of query results."""

        if results_df.empty:
            results_summary = "The query returned no results."
        elif len(results_df) == 1 and "message" in results_df.columns:
            results_summary = f"System message: {results_df.iloc[0]['message']}"
        else:
            # Summarize the results
            rows_str = results_df.head(20).to_string(index=False, max_cols=10)
            results_summary = (
                f"Rows returned: {len(results_df)}\n"
                f"Columns: {', '.join(results_df.columns.tolist())}\n\n"
                f"Data preview:\n{rows_str}"
            )

        prompt = INTERPRETATION_PROMPT.format(
            question=question,
            results_summary=results_summary,
        )

        response = self._get_client().chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
        )

        return response.choices[0].message.content.strip()

    def query(
        self,
        question: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        feed_id: Optional[int] = None,
    ) -> dict:
        """
        Full pipeline: question → SQL → execute → interpret.

        Returns:
            dict with keys: answer, sql_query, data, error
        """
        from db import get_synapse_client

        result = {
            "answer": None,
            "sql_query": None,
            "data": None,
            "error": None,
        }

        try:
            # Step 1: Generate SQL
            sql = self.generate_sql(
                question=question,
                start_time=start_time,
                end_time=end_time,
                feed_id=feed_id,
            )
            result["sql_query"] = sql

            # Step 2: Execute query
            db = get_synapse_client()
            df = db.execute_custom_query(sql)
            result["data"] = df.to_dict("records") if not df.empty else []

            # Step 3: Interpret results
            answer = self.interpret_results(
                question=question,
                results_df=df,
                sql_query=sql,
            )
            result["answer"] = answer

        except Exception as e:
            logger.exception("AI query pipeline failed: %s", e)
            result["error"] = str(e)
            result["answer"] = (
                f"I encountered an error while processing your question: {e}\n\n"
                "Please try rephrasing your question or check that the data is available."
            )

        return result


# ─── Singleton ────────────────────────────────────────────────────────────────
@st.cache_resource
def get_ai_query_engine() -> AIQueryEngine:
    """Get or create the AI query engine singleton."""
    return AIQueryEngine()
