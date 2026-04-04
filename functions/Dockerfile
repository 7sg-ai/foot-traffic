# =============================================================================
# Foot Traffic Analyzer - Azure Functions Docker Image
# Deployed to Azure Container Apps (replaces Y1/Consumption App Service Plan)
#
# Uses the official Azure Functions Python 3.11 base image which bundles the
# Functions host runtime — no App Service Plan or quota required.
#
# Pinned to python:3.11 / Debian 12 (Bookworm) for stable ODBC driver support.
# =============================================================================
FROM mcr.microsoft.com/azure-functions/python:4-python3.11

# Install system dependencies + Microsoft ODBC Driver 18 for SQL Server
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        apt-transport-https \
        ca-certificates \
        unixodbc \
        unixodbc-dev \
        # OpenCV headless requires these
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
        libgl1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    # Add Microsoft GPG key
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    # Add Microsoft Debian 12 (Bookworm) package repo
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        -o /etc/apt/sources.list.d/mssql-release.list \
    # Install ODBC driver
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# The Functions base image sets AzureWebJobsScriptRoot=/home/site/wwwroot
# Copy requirements first for layer caching
COPY requirements.txt /home/site/wwwroot/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /home/site/wwwroot/requirements.txt

# Copy function app code
COPY . /home/site/wwwroot

# The Functions host listens on port 80 inside the container.
# Azure Container Apps will route external HTTPS → internal port 80.
EXPOSE 80
