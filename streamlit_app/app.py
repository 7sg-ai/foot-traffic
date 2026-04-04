"""
Foot Traffic Analyzer - Streamlit Dashboard
Main entry point for the Azure-hosted Streamlit application.
Connects to Azure Synapse Analytics for data and Azure OpenAI for NL queries.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from db import get_synapse_client
from ai_query import get_ai_query_engine

# ─── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Foot Traffic Analyzer",
    page_icon="🚶",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": "Foot Traffic Analyzer - Powered by Azure OpenAI & Synapse Analytics",
    },
)

# ─── Custom CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #1e3a5f 0%, #2d5986 100%);
        border-radius: 12px;
        padding: 20px;
        color: white;
        text-align: center;
        box-shadow: 0 4px 15px rgba(0,0,0,0.2);
    }
    .metric-value {
        font-size: 2.5rem;
        font-weight: 700;
        margin: 8px 0;
    }
    .metric-label {
        font-size: 0.9rem;
        opacity: 0.85;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    .section-header {
        font-size: 1.4rem;
        font-weight: 600;
        color: #1e3a5f;
        border-bottom: 3px solid #2d5986;
        padding-bottom: 8px;
        margin: 24px 0 16px 0;
    }
    .stAlert {
        border-radius: 8px;
    }
    div[data-testid="stSidebarContent"] {
        background: linear-gradient(180deg, #0f2744 0%, #1e3a5f 100%);
        color: white;
    }
</style>
""", unsafe_allow_html=True)

logger = logging.getLogger(__name__)


# ─── Sidebar ─────────────────────────────────────────────────────────────────
def render_sidebar() -> dict:
    """Render sidebar filters and return selected values."""
    with st.sidebar:
        st.markdown("## 🚶 Foot Traffic Analyzer")
        st.markdown("---")

        # Time range selector
        st.markdown("### 📅 Time Range")
        time_range = st.selectbox(
            "Select period",
            options=["Last 1 Hour", "Last 6 Hours", "Last 24 Hours", "Last 7 Days", "Last 30 Days", "Custom"],
            index=2,
        )

        start_time = None
        end_time = datetime.now(timezone.utc)

        if time_range == "Last 1 Hour":
            start_time = end_time - timedelta(hours=1)
        elif time_range == "Last 6 Hours":
            start_time = end_time - timedelta(hours=6)
        elif time_range == "Last 24 Hours":
            start_time = end_time - timedelta(hours=24)
        elif time_range == "Last 7 Days":
            start_time = end_time - timedelta(days=7)
        elif time_range == "Last 30 Days":
            start_time = end_time - timedelta(days=30)
        elif time_range == "Custom":
            col1, col2 = st.columns(2)
            with col1:
                start_date = st.date_input("Start date", value=datetime.now().date() - timedelta(days=7))
            with col2:
                end_date = st.date_input("End date", value=datetime.now().date())
            start_time = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_time = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

        st.markdown("### 📹 Video Feeds")

        # Load feeds
        try:
            db = get_synapse_client()
            feeds_df = db.get_feeds_dataframe()
            feed_options = ["All Feeds"] + feeds_df["feed_name"].tolist() if not feeds_df.empty else ["All Feeds"]
            feed_id_map = dict(zip(feeds_df["feed_name"], feeds_df["feed_id"])) if not feeds_df.empty else {}
        except Exception:
            feed_options = ["All Feeds"]
            feed_id_map = {}

        selected_feed = st.selectbox("Select feed", options=feed_options)
        selected_feed_id = feed_id_map.get(selected_feed) if selected_feed != "All Feeds" else None

        st.markdown("---")
        st.markdown("### ⚙️ Settings")
        auto_refresh = st.checkbox("Auto-refresh (5 min)", value=False)
        show_raw_data = st.checkbox("Show raw data tables", value=False)

        st.markdown("---")
        st.markdown(
            "<small style='color: #aaa;'>Data powered by Azure Synapse Analytics<br>"
            "Analysis by Azure OpenAI GPT-4o</small>",
            unsafe_allow_html=True,
        )

    return {
        "start_time": start_time,
        "end_time": end_time,
        "feed_id": selected_feed_id,
        "feed_name": selected_feed,
        "auto_refresh": auto_refresh,
        "show_raw_data": show_raw_data,
    }


# ─── KPI Cards ───────────────────────────────────────────────────────────────
def render_kpi_cards(summary: dict) -> None:
    """Render top-level KPI metric cards."""
    cols = st.columns(5)

    metrics = [
        ("👥 Total Pedestrians", summary.get("total_pedestrians", 0), ""),
        ("♂️ Male", f"{summary.get('pct_male', 0):.1f}%", f"{summary.get('total_male', 0):,}"),
        ("♀️ Female", f"{summary.get('pct_female', 0):.1f}%", f"{summary.get('total_female', 0):,}"),
        ("💼 Working", f"{summary.get('pct_working', 0):.1f}%", f"{summary.get('total_working', 0):,}"),
        ("📱 Using Phone", f"{summary.get('pct_phone', 0):.1f}%", f"{summary.get('total_phone', 0):,}"),
    ]

    for col, (label, value, delta) in zip(cols, metrics):
        with col:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{label}</div>
                <div class="metric-value">{value}</div>
                <div style="font-size: 0.8rem; opacity: 0.7;">{delta}</div>
            </div>
            """, unsafe_allow_html=True)


# ─── Traffic Timeline ─────────────────────────────────────────────────────────
def render_traffic_timeline(df: pd.DataFrame) -> None:
    """Render pedestrian count over time."""
    st.markdown('<div class="section-header">📈 Pedestrian Traffic Over Time</div>', unsafe_allow_html=True)

    if df.empty:
        st.info("No data available for the selected time range.")
        return

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        subplot_titles=("Total Pedestrian Count", "Gender Split"),
        vertical_spacing=0.08,
        row_heights=[0.6, 0.4],
    )

    # Total count
    fig.add_trace(
        go.Scatter(
            x=df["interval_start"],
            y=df["total_count"],
            mode="lines+markers",
            name="Total",
            line=dict(color="#2d5986", width=2),
            fill="tozeroy",
            fillcolor="rgba(45, 89, 134, 0.15)",
        ),
        row=1, col=1,
    )

    # Gender split
    fig.add_trace(
        go.Bar(x=df["interval_start"], y=df["count_male"], name="Male", marker_color="#3498db"),
        row=2, col=1,
    )
    fig.add_trace(
        go.Bar(x=df["interval_start"], y=df["count_female"], name="Female", marker_color="#e74c3c"),
        row=2, col=1,
    )

    fig.update_layout(
        height=450,
        barmode="stack",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=0, r=0, t=30, b=0),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.05)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.05)")

    st.plotly_chart(fig, use_container_width=True)


# ─── Demographics Charts ──────────────────────────────────────────────────────
def render_demographics(df: pd.DataFrame) -> None:
    """Render demographic breakdown charts."""
    st.markdown('<div class="section-header">👥 Demographic Breakdown</div>', unsafe_allow_html=True)

    if df.empty:
        st.info("No demographic data available.")
        return

    col1, col2, col3 = st.columns(3)

    # Age group distribution
    with col1:
        age_data = {
            "Children (0-12)": df["count_children"].sum(),
            "Teens (13-17)": df["count_teens"].sum(),
            "Young Adults (18-35)": df["count_young_adults"].sum(),
            "Adults (36-60)": df["count_adults"].sum(),
            "Seniors (60+)": df["count_seniors"].sum(),
        }
        age_df = pd.DataFrame(list(age_data.items()), columns=["Age Group", "Count"])
        age_df = age_df[age_df["Count"] > 0]

        fig_age = px.pie(
            age_df,
            values="Count",
            names="Age Group",
            title="Age Distribution",
            color_discrete_sequence=px.colors.sequential.Blues_r,
            hole=0.4,
        )
        fig_age.update_layout(
            height=320,
            margin=dict(l=0, r=0, t=40, b=0),
            showlegend=True,
            legend=dict(font=dict(size=10)),
        )
        st.plotly_chart(fig_age, use_container_width=True)

    # Attire breakdown
    with col2:
        attire_data = {
            "Business": df["count_business_attire"].sum(),
            "Casual": df["count_casual_attire"].sum(),
            "Athletic": df["count_athletic_attire"].sum(),
            "Uniform": df["count_uniform_attire"].sum(),
        }
        attire_df = pd.DataFrame(list(attire_data.items()), columns=["Attire", "Count"])
        attire_df = attire_df[attire_df["Count"] > 0]

        fig_attire = px.bar(
            attire_df,
            x="Attire",
            y="Count",
            title="Attire Type",
            color="Attire",
            color_discrete_sequence=["#2ecc71", "#3498db", "#e74c3c", "#f39c12"],
        )
        fig_attire.update_layout(
            height=320,
            margin=dict(l=0, r=0, t=40, b=0),
            showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_attire, use_container_width=True)

    # Work vs Leisure
    with col3:
        work_data = {
            "Working/Commuting": df["count_working"].sum(),
            "Leisure": df["count_leisure"].sum(),
        }
        work_df = pd.DataFrame(list(work_data.items()), columns=["Category", "Count"])
        work_df = work_df[work_df["Count"] > 0]

        fig_work = px.pie(
            work_df,
            values="Count",
            names="Category",
            title="Work vs Leisure",
            color_discrete_map={
                "Working/Commuting": "#2d5986",
                "Leisure": "#27ae60",
            },
            hole=0.4,
        )
        fig_work.update_layout(
            height=320,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_work, use_container_width=True)


# ─── Activity & Behavior ──────────────────────────────────────────────────────
def render_activity_behavior(df: pd.DataFrame) -> None:
    """Render activity and behavior charts."""
    st.markdown('<div class="section-header">🏃 Activity & Behavior Patterns</div>', unsafe_allow_html=True)

    if df.empty:
        return

    col1, col2 = st.columns(2)

    with col1:
        activity_data = {
            "Walking": df["count_walking"].sum(),
            "Running": df["count_running"].sum(),
            "Standing": df["count_standing"].sum(),
            "Cycling": df["count_cycling"].sum(),
            "Shopping": df["count_shopping"].sum(),
        }
        activity_df = pd.DataFrame(list(activity_data.items()), columns=["Activity", "Count"])
        activity_df = activity_df[activity_df["Count"] > 0].sort_values("Count", ascending=True)

        fig_activity = px.bar(
            activity_df,
            x="Count",
            y="Activity",
            orientation="h",
            title="Activity Breakdown",
            color="Count",
            color_continuous_scale="Blues",
        )
        fig_activity.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=40, b=0),
            plot_bgcolor="rgba(0,0,0,0)",
            coloraxis_showscale=False,
        )
        st.plotly_chart(fig_activity, use_container_width=True)

    with col2:
        behavior_data = {
            "Using Phone": df["count_using_phone"].sum(),
            "Carrying Items": df["count_carrying_items"].sum(),
            "In Groups": df["count_in_groups"].sum(),
        }
        behavior_df = pd.DataFrame(list(behavior_data.items()), columns=["Behavior", "Count"])
        total = df["total_count"].sum()

        if total > 0:
            behavior_df["Percentage"] = (behavior_df["Count"] / total * 100).round(1)

        fig_behavior = px.bar(
            behavior_df,
            x="Behavior",
            y="Percentage" if total > 0 else "Count",
            title="Behavior Indicators (% of total)",
            color="Behavior",
            color_discrete_sequence=["#9b59b6", "#e67e22", "#1abc9c"],
            text="Percentage" if total > 0 else "Count",
        )
        fig_behavior.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig_behavior.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=40, b=0),
            showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_behavior, use_container_width=True)


# ─── Ethnicity Heatmap ────────────────────────────────────────────────────────
def render_ethnicity_breakdown(df: pd.DataFrame) -> None:
    """Render ethnicity breakdown from JSON column."""
    st.markdown('<div class="section-header">🌍 Apparent Ethnicity Distribution</div>', unsafe_allow_html=True)
    st.caption("⚠️ These are VLM visual estimates only — not ground truth. Use for aggregate trend analysis.")

    if df.empty or "ethnicity_breakdown" not in df.columns:
        st.info("No ethnicity data available.")
        return

    import json

    ethnicity_totals: dict[str, int] = {}
    for val in df["ethnicity_breakdown"].dropna():
        try:
            d = json.loads(val) if isinstance(val, str) else val
            for k, v in d.items():
                ethnicity_totals[k] = ethnicity_totals.get(k, 0) + int(v)
        except Exception:
            continue

    if not ethnicity_totals:
        st.info("No ethnicity data in selected range.")
        return

    eth_df = pd.DataFrame(
        list(ethnicity_totals.items()), columns=["Ethnicity", "Count"]
    ).sort_values("Count", ascending=False)

    total = eth_df["Count"].sum()
    eth_df["Percentage"] = (eth_df["Count"] / total * 100).round(1)

    fig = px.bar(
        eth_df,
        x="Ethnicity",
        y="Count",
        color="Percentage",
        color_continuous_scale="Blues",
        title="Apparent Ethnicity (VLM Estimates)",
        text="Percentage",
    )
    fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
    fig.update_layout(
        height=350,
        margin=dict(l=0, r=0, t=40, b=0),
        plot_bgcolor="rgba(0,0,0,0)",
        coloraxis_showscale=False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ─── Hourly Heatmap ───────────────────────────────────────────────────────────
def render_hourly_heatmap(df: pd.DataFrame) -> None:
    """Render a day-of-week × hour-of-day heatmap."""
    st.markdown('<div class="section-header">🗓️ Traffic Heatmap (Day × Hour)</div>', unsafe_allow_html=True)

    if df.empty or len(df) < 10:
        st.info("Not enough data for heatmap. Collect at least a few hours of data.")
        return

    df_copy = df.copy()
    df_copy["hour"] = pd.to_datetime(df_copy["interval_start"]).dt.hour
    df_copy["day_name"] = pd.to_datetime(df_copy["interval_start"]).dt.day_name()

    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    pivot = df_copy.pivot_table(
        values="total_count",
        index="day_name",
        columns="hour",
        aggfunc="mean",
    ).reindex([d for d in day_order if d in df_copy["day_name"].unique()])

    fig = px.imshow(
        pivot,
        labels=dict(x="Hour of Day", y="Day of Week", color="Avg Pedestrians"),
        color_continuous_scale="Blues",
        title="Average Pedestrian Count by Day & Hour",
        aspect="auto",
    )
    fig.update_layout(
        height=350,
        margin=dict(l=0, r=0, t=40, b=0),
    )
    st.plotly_chart(fig, use_container_width=True)


# ─── AI Query Interface ───────────────────────────────────────────────────────
def render_ai_query(filters: dict) -> None:
    """Render the natural language query interface."""
    st.markdown('<div class="section-header">🤖 Ask AI About Your Data</div>', unsafe_allow_html=True)
    st.markdown(
        "Ask questions in plain English. The AI will query Synapse Analytics and interpret the results."
    )

    # Example questions
    with st.expander("💡 Example questions"):
        examples = [
            "What time of day has the most pedestrian traffic?",
            "What percentage of people appear to be working vs leisure?",
            "Which age group is most common during morning rush hour?",
            "How does foot traffic compare between weekdays and weekends?",
            "What is the trend in phone usage over the past week?",
            "Which feed location has the highest traffic volume?",
            "What is the gender split during business hours (9am-5pm)?",
            "Show me the busiest 5-minute intervals in the last 24 hours.",
        ]
        for ex in examples:
            if st.button(f"▶ {ex}", key=f"ex_{ex[:20]}"):
                st.session_state["ai_query"] = ex

    query = st.text_area(
        "Your question:",
        value=st.session_state.get("ai_query", ""),
        placeholder="e.g. What percentage of pedestrians appear to be working during morning hours?",
        height=80,
        key="ai_query_input",
    )

    col1, col2 = st.columns([1, 5])
    with col1:
        submit = st.button("🔍 Ask", type="primary", use_container_width=True)
    with col2:
        if st.button("🗑️ Clear", use_container_width=False):
            st.session_state["ai_query"] = ""
            st.session_state["ai_response"] = None
            st.rerun()

    if submit and query.strip():
        with st.spinner("🤔 Analyzing your question..."):
            try:
                ai_engine = get_ai_query_engine()
                response = ai_engine.query(
                    question=query,
                    start_time=filters["start_time"],
                    end_time=filters["end_time"],
                    feed_id=filters["feed_id"],
                )
                st.session_state["ai_response"] = response
            except Exception as e:
                st.error(f"AI query failed: {e}")
                logger.exception("AI query error: %s", e)

    # Display response
    if st.session_state.get("ai_response"):
        resp = st.session_state["ai_response"]

        st.markdown("#### 💬 Answer")
        st.markdown(resp.get("answer", "No answer generated."))

        if resp.get("sql_query"):
            with st.expander("🔍 SQL Query Used"):
                st.code(resp["sql_query"], language="sql")

        if resp.get("data") is not None and len(resp["data"]) > 0:
            with st.expander("📊 Query Results"):
                result_df = pd.DataFrame(resp["data"])
                st.dataframe(result_df, use_container_width=True)

                # Auto-visualize if numeric columns present
                numeric_cols = result_df.select_dtypes(include="number").columns.tolist()
                if len(numeric_cols) >= 1 and len(result_df) > 1:
                    x_col = result_df.columns[0]
                    y_col = numeric_cols[0]
                    fig = px.bar(result_df, x=x_col, y=y_col, title="Query Result Visualization")
                    fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
                    st.plotly_chart(fig, use_container_width=True)


# ─── Main App ─────────────────────────────────────────────────────────────────
def main():
    # Initialize session state
    if "ai_response" not in st.session_state:
        st.session_state["ai_response"] = None
    if "ai_query" not in st.session_state:
        st.session_state["ai_query"] = ""

    # Render sidebar and get filters
    filters = render_sidebar()

    # Header
    st.title("🚶 Foot Traffic Analyzer")
    st.markdown(
        f"**Analyzing pedestrian demographics** | "
        f"Period: `{filters['start_time'].strftime('%Y-%m-%d %H:%M') if filters['start_time'] else 'All time'}` → "
        f"`{filters['end_time'].strftime('%Y-%m-%d %H:%M')}` | "
        f"Feed: `{filters['feed_name']}`"
    )

    # Load data
    with st.spinner("Loading data from Azure Synapse..."):
        try:
            db = get_synapse_client()
            df = db.get_interval_aggregates_df(
                feed_id=filters["feed_id"],
                start_time=filters["start_time"],
                end_time=filters["end_time"],
            )
            summary = db.get_summary_stats(
                feed_id=filters["feed_id"],
                start_time=filters["start_time"],
                end_time=filters["end_time"],
            )
        except Exception as e:
            st.error(f"⚠️ Failed to connect to Azure Synapse: {e}")
            st.info("Make sure the Synapse SQL pool is online and credentials are configured.")
            df = pd.DataFrame()
            summary = {}

    # Data freshness indicator
    if not df.empty:
        latest = pd.to_datetime(df["interval_start"]).max()
        age_minutes = (datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age_minutes < 10:
            st.success(f"✅ Data is fresh — last update {age_minutes:.0f} minutes ago")
        elif age_minutes < 30:
            st.warning(f"⚠️ Data is {age_minutes:.0f} minutes old")
        else:
            st.error(f"🔴 Data is stale — last update {age_minutes:.0f} minutes ago")
    else:
        st.info("📭 No data found for the selected filters. The analyzer may still be collecting data.")

    # KPI Cards
    render_kpi_cards(summary)

    st.markdown("---")

    # Main charts
    render_traffic_timeline(df)
    render_demographics(df)
    render_activity_behavior(df)
    render_ethnicity_breakdown(df)
    render_hourly_heatmap(df)

    st.markdown("---")

    # AI Query Interface
    render_ai_query(filters)

    st.markdown("---")

    # Raw data table (optional)
    if filters.get("show_raw_data") and not df.empty:
        st.markdown('<div class="section-header">📋 Raw Interval Data</div>', unsafe_allow_html=True)
        display_cols = [
            "interval_start", "feed_name", "total_count",
            "count_male", "count_female", "count_working",
            "pct_male", "pct_female", "pct_working", "avg_confidence_score",
        ]
        available_cols = [c for c in display_cols if c in df.columns]
        st.dataframe(
            df[available_cols].sort_values("interval_start", ascending=False),
            use_container_width=True,
            height=400,
        )

    # Auto-refresh
    if filters.get("auto_refresh"):
        import time
        time.sleep(300)
        st.rerun()


if __name__ == "__main__":
    main()
