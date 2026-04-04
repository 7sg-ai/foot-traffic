# Foot Traffic Analyzer

An Azure-native application that analyzes publicly available video feeds with pedestrian traffic, categorizes demographics using a Vision Language Model (VLM), stores data in Azure Synapse Analytics, and provides a Streamlit frontend for analysis and inquiry.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Azure Cloud                                  │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │  Azure Timer │    │   Azure      │    │  Azure Computer      │  │
│  │  Function    │───▶│   Function   │───▶│  Vision / GPT-4V     │  │
│  │  (Scheduler) │    │  (Analyzer)  │    │  (VLM Analysis)      │  │
│  └──────────────┘    └──────┬───────┘    └──────────────────────┘  │
│                             │                                        │
│                             ▼                                        │
│                    ┌──────────────────┐                             │
│                    │  Azure Blob      │                             │
│                    │  Storage         │                             │
│                    │  (Frame Cache)   │                             │
│                    └────────┬─────────┘                             │
│                             │                                        │
│                             ▼                                        │
│                    ┌──────────────────┐                             │
│                    │  Azure Synapse   │                             │
│                    │  Analytics       │                             │
│                    │  (Data Warehouse)│                             │
│                    └────────┬─────────┘                             │
│                             │                                        │
│                             ▼                                        │
│                    ┌──────────────────┐                             │
│                    │  Azure Container │                             │
│                    │  Apps            │                             │
│                    │  (Streamlit UI)  │                             │
│                    └──────────────────┘                             │
│                                                                      │
│  Supporting Services:                                                │
│  - Azure Key Vault (Secrets)                                        │
│  - Azure Monitor / App Insights (Observability)                     │
│  - Azure Container Registry (Docker Images)                         │
│  - Azure Service Bus (Message Queue)                                │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Video Ingestion & Analysis (Azure Functions)
- **Timer-triggered function**: Runs every 5 minutes to capture frames from public video feeds
- **Frame analyzer**: Uses Azure OpenAI GPT-4 Vision to analyze pedestrian demographics
- **Demographics captured**: Gender, estimated age group, apparent ethnicity, clothing style (working/casual), activity type

### 2. Data Storage (Azure Synapse Analytics)
- Dedicated SQL pool for structured demographic data
- 5-minute interval aggregations
- Historical trend analysis capabilities

### 3. Frontend (Streamlit on Azure Container Apps)
- Real-time dashboard with demographic breakdowns
- Natural language query interface via Azure OpenAI
- Time-series visualizations
- Comparative analysis tools

## Prerequisites

- Azure CLI installed and authenticated
- Azure subscription with sufficient quota
- Python 3.11+
- Docker (for local testing)

## Quick Start

### 1. Clone and Configure

```bash
git clone <repo-url>
cd foot-traffic
cp .env.example .env
# Edit .env with your values
```

### 2. Deploy Infrastructure

```bash
cd infrastructure
./deploy.sh
```

### 3. Configure Video Feeds

Edit `config/video_feeds.json` with your public camera URLs.

### 4. Deploy Application

```bash
./scripts/deploy-all.sh
```

## Project Structure

```
foot-traffic/
├── infrastructure/          # Bicep IaC templates
│   ├── main.bicep
│   ├── modules/
│   └── deploy.sh
├── functions/               # Azure Functions
│   ├── video_analyzer/
│   ├── frame_processor/
│   └── host.json
├── streamlit_app/           # Streamlit frontend
│   ├── app.py
│   ├── pages/
│   └── Dockerfile
├── shared/                  # Shared utilities
│   ├── models.py
│   └── db_client.py
├── config/                  # Configuration files
│   └── video_feeds.json
├── scripts/                 # Deployment scripts
│   └── deploy-all.sh
└── .github/                 # CI/CD workflows
    └── workflows/
```

## Environment Variables

See `.env.example` for all required environment variables.

## Security

- All secrets stored in Azure Key Vault
- Managed Identity used for service-to-service authentication
- Network isolation via VNet integration
- RBAC for all Azure resources

## Ethical Considerations

This application analyzes publicly available video feeds. Ensure compliance with:
- Local privacy laws and regulations (GDPR, CCPA, etc.)
- Camera operator terms of service
- Data retention policies
- Anonymization requirements

All demographic data is aggregated and anonymized - no individual tracking occurs.
