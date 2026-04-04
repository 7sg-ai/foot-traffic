"""
Query Page - Natural language query interface with full SQL transparency.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_query import get_ai_query_engine
from db import get_synapse_client

st.set_page_config(
    page_title="AI Query | Foot Traffic",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 Natural Language Query")
st.markdown(
    "Ask questions about your foot traffic data in plain English. "
    "The AI generates SQL, executes it against Azure Synapse, and interprets the results."
)

# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Query Context")
    time_range = st.selectbox(
        "Time range",
        ["Last 1 Hour", "Last 6 Hours", "Last 24 Hours", "Last 7 Days", "Last 30 Days"],
        index=2,
    )
    time_map = {
        "Last 1 Hour": 1,
        "Last 6 Hours": 6,
        "Last 24 Hours": 24,
        "Last 7 Days": 168,
        "Last 30 Days": 720,
    }
    hours_back = time_map[time_range]
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours_back)

    db = get_synapse_client()
    feeds_df = db.get_feeds_dataframe()
    feed_options = ["All Feeds"] + (feeds_df["feed_name"].tolist() if not feeds_df.empty else [])
    feed_id_map = dict(zip(feeds_df["feed_name"], feeds_df["feed_id"])) if not feeds_df.empty else {}
    selected_feed = st.selectbox("Feed", feed_options)
    feed_id = feed_id_map.get(selected_feed) if selected_feed != "All Feeds" else None

    st.markdown("---")
    st.markdown("### 💡 Query Tips")
    st.markdown("""
    - Be specific about time periods
    - Ask for comparisons (e.g., "compare weekday vs weekend")
    - Request percentages or totals
    - Ask about trends over time
    - Combine demographics (e.g., "working adults")
    """)

# ─── Query History ────────────────────────────────────────────────────────────
if "query_history" not in st.session_state:
    st.session_state["query_history"] = []

# ─── Preset Questions ─────────────────────────────────────────────────────────
st.markdown("### 📋 Quick Questions")

preset_cols = st.columns(4)
presets = [
    ("🕐 Peak Hours", "What are the top 5 busiest hours of the day by average pedestrian count?"),
    ("👔 Work Patterns", "What percentage of pedestrians appear to be working vs leisure by hour of day?"),
    ("📱 Phone Usage", "How does phone usage vary by age group and time of day?"),
    ("🌍 Demographics", "Show me the demographic breakdown by gender and age group for the selected period."),
    ("📈 Trends", "Is pedestrian traffic increasing or decreasing over the selected time period?"),
    ("🏃 Activities", "What are the most common activities observed and how do they vary by time?"),
    ("👥 Groups", "What percentage of pedestrians are in groups vs alone?"),
    ("🎯 Attire", "What is the breakdown of attire types and how does it correlate with working status?"),
]

for i, (label, question) in enumerate(presets):
    col = preset_cols[i % 4]
    with col:
        if st.button(label, key=f"preset_{i}", use_container_width=True):
            st.session_state["current_query"] = question

# ─── Query Input ──────────────────────────────────────────────────────────────
st.markdown("### 💬 Your Question")

query = st.text_area(
    "Ask anything about your foot traffic data:",
    value=st.session_state.get("current_query", ""),
    placeholder="e.g. What is the average number of pedestrians per 5-minute interval during morning rush hour (7-9am)?",
    height=100,
    key="query_input",
)

col1, col2, col3 = st.columns([1, 1, 6])
with col1:
    submit = st.button("🔍 Ask AI", type="primary", use_container_width=True)
with col2:
    clear = st.button("🗑️ Clear", use_container_width=True)

if clear:
    st.session_state["current_query"] = ""
    st.rerun()

# ─── Execute Query ────────────────────────────────────────────────────────────
if submit and query.strip():
    with st.spinner("🤔 Generating SQL and querying Synapse Analytics..."):
        try:
            ai_engine = get_ai_query_engine()
            response = ai_engine.query(
                question=query,
                start_time=start_time,
                end_time=end_time,
                feed_id=feed_id,
            )

            # Add to history
            st.session_state["query_history"].insert(0, {
                "question": query,
                "response": response,
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            })

            # Keep only last 10
            st.session_state["query_history"] = st.session_state["query_history"][:10]

        except Exception as e:
            st.error(f"Query failed: {e}")

# ─── Display Results ──────────────────────────────────────────────────────────
if st.session_state["query_history"]:
    latest = st.session_state["query_history"][0]
    resp = latest["response"]

    st.markdown("---")
    st.markdown(f"### 💬 Answer  <small style='color: gray;'>({latest['timestamp']} UTC)</small>", unsafe_allow_html=True)
    st.markdown(f"**Question:** _{latest['question']}_")
    st.markdown("")

    if resp.get("error"):
        st.error(f"Error: {resp['error']}")
    else:
        st.success(resp.get("answer", "No answer generated."))

    # SQL Query
    if resp.get("sql_query"):
        with st.expander("🔍 View Generated SQL", expanded=False):
            st.code(resp["sql_query"], language="sql")
            st.caption("This SQL was generated by Azure OpenAI and executed against Azure Synapse Analytics.")

    # Results table and chart
    if resp.get("data") and len(resp["data"]) > 0:
        result_df = pd.DataFrame(resp["data"])

        col1, col2 = st.columns([1, 1])

        with col1:
            st.markdown("#### 📊 Query Results")
            st.dataframe(result_df, use_container_width=True, height=300)
            st.caption(f"{len(result_df)} rows returned")

        with col2:
            st.markdown("#### 📈 Visualization")
            numeric_cols = result_df.select_dtypes(include="number").columns.tolist()
            str_cols = result_df.select_dtypes(include="object").columns.tolist()

            if numeric_cols and len(result_df) > 1:
                x_col = str_cols[0] if str_cols else result_df.columns[0]
                y_col = st.selectbox("Y-axis metric:", numeric_cols, key="viz_y")
                chart_type = st.radio("Chart type:", ["Bar", "Line", "Scatter"], horizontal=True, key="viz_type")

                if chart_type == "Bar":
                    fig = px.bar(result_df, x=x_col, y=y_col, color_discrete_sequence=["#2d5986"])
                elif chart_type == "Line":
                    fig = px.line(result_df, x=x_col, y=y_col, line_shape="spline")
                else:
                    fig = px.scatter(result_df, x=x_col, y=y_col)

                fig.update_layout(
                    height=280,
                    margin=dict(l=0, r=0, t=20, b=0),
                    plot_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Not enough numeric data to visualize.")

# ─── Query History ────────────────────────────────────────────────────────────
if len(st.session_state["query_history"]) > 1:
    st.markdown("---")
    with st.expander(f"📜 Query History ({len(st.session_state['query_history'])} queries)"):
        for i, item in enumerate(st.session_state["query_history"][1:], start=2):
            st.markdown(f"**{i}. [{item['timestamp']}]** {item['question']}")
            if item["response"].get("answer"):
                st.markdown(f"   _{item['response']['answer'][:200]}..._")
            st.markdown("---")

# ─── Direct SQL Editor ────────────────────────────────────────────────────────
st.markdown("---")
with st.expander("🛠️ Direct SQL Editor (Advanced)"):
    st.warning("⚠️ Direct SQL access. Queries are read-only against Synapse Analytics.")

    default_sql = """SELECT TOP 20
    f.feed_name,
    ia.interval_start,
    ia.total_count,
    ia.pct_male,
    ia.pct_female,
    ia.pct_working
FROM traffic.interval_aggregates ia
JOIN traffic.video_feeds f ON f.feed_id = ia.feed_id
WHERE ia.processing_status = 'complete'
  AND ia.interval_start >= DATEADD(HOUR, -24, GETUTCDATE())
ORDER BY ia.interval_start DESC"""

    custom_sql = st.text_area("SQL Query:", value=default_sql, height=200, key="custom_sql")

    if st.button("▶ Execute SQL", key="exec_sql"):
        with st.spinner("Executing..."):
            try:
                db = get_synapse_client()
                result_df = db.execute_custom_query(custom_sql)
                st.success(f"Query returned {len(result_df)} rows")
                st.dataframe(result_df, use_container_width=True)
            except Exception as e:
                st.error(f"SQL Error: {e}")
