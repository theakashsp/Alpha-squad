"""
Central settings object – loaded once at startup.
All other modules import `settings` from here.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    neon_database_url: str

    # Mappls
    mappls_api_key: str
    mappls_secret: str

    # Server
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    log_level: str = "info"

    # AI thresholds
    vision_confidence_threshold: float = 0.80
    acoustic_confidence_threshold: float = 0.85

    # Green-wave
    green_wave_lead_seconds: int = 30
    signal_search_radius_meters: int = 800   # wider net so ambient routes trigger
    signal_restore_after_seconds: int = 60   # restore if ambulance is > this ETA away

    # CORS
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
