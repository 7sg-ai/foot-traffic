"""
Shared configuration and settings for Azure Functions.
Reads from environment variables (populated from Key Vault references in Azure).
"""
import os
from dataclasses import dataclass, field
from typing import Optional  # kept for _settings singleton type hint


@dataclass
class Settings:
    # Azure OpenAI — optional at construction time; validated lazily when VLMAnalyzer is used
    openai_endpoint: str = field(default_factory=lambda: os.environ.get("AZURE_OPENAI_ENDPOINT", ""))
    openai_api_key: str = field(default_factory=lambda: os.environ.get("AZURE_OPENAI_API_KEY", ""))
    # gpt-5.3-chat: vision-capable (text + image)
    openai_deployment: str = field(default_factory=lambda: os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.3-chat"))
    # 2025-01-01-preview supports gpt-4o 2024-11-20 (retires 2026-10-01)
    openai_api_version: str = field(default_factory=lambda: os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"))

    # Azure Storage
    storage_connection_string: str = field(default_factory=lambda: os.environ["STORAGE_CONNECTION_STRING"])
    storage_account_name: str = field(default_factory=lambda: os.environ.get("STORAGE_ACCOUNT_NAME", ""))
    frames_container: str = "video-frames"
    results_container: str = "analysis-results"

    # Azure Service Bus
    service_bus_connection_string: str = field(
        default_factory=lambda: os.environ.get("SERVICE_BUS_CONNECTION_STRING", "")
    )
    frame_analysis_queue: str = "frame-analysis"

    # Azure Synapse / SQL
    synapse_server: str = field(default_factory=lambda: os.environ["SYNAPSE_SERVER"])
    synapse_database: str = field(default_factory=lambda: os.environ["SYNAPSE_DATABASE"])
    synapse_username: str = field(default_factory=lambda: os.environ["SYNAPSE_USERNAME"])
    synapse_password: str = field(default_factory=lambda: os.environ["SYNAPSE_PASSWORD"])

    # Analysis settings
    frames_per_interval: int = field(default_factory=lambda: int(os.environ.get("FRAMES_PER_INTERVAL", "5")))
    max_persons_per_frame: int = field(default_factory=lambda: int(os.environ.get("MAX_PERSONS_PER_FRAME", "20")))
    confidence_threshold: float = field(
        default_factory=lambda: float(os.environ.get("CONFIDENCE_THRESHOLD", "0.6"))
    )

    @property
    def synapse_connection_string(self) -> str:
        """Build ODBC connection string for Synapse."""
        return (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={self.synapse_server};"
            f"DATABASE={self.synapse_database};"
            f"UID={self.synapse_username};"
            f"PWD={self.synapse_password};"
            f"Encrypt=yes;"
            f"TrustServerCertificate=no;"
            f"Connection Timeout=30;"
        )


# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
