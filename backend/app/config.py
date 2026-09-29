from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    embedding_model: str = "nomic-embed-text"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "work_instructions"
    documents_path: str = "/documents"
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    chunk_size: int = Field(default=1400, ge=100, le=16000)
    chunk_overlap: int = Field(default=250, ge=0)
    index_lock_path: str = "/tmp/instruct-locks"

    @model_validator(mode="after")
    def valid_chunk_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP doit être inférieur à CHUNK_SIZE")
        return self

    state_path: str = "/state"
    app_origin: str = "http://localhost:3000"
    cookie_secure: bool = False
    session_seconds: int = Field(default=28800, ge=60, le=604800)
    login_attempts: int = Field(default=5, ge=1, le=100)
    audit_retention_days: int = Field(default=90, ge=1, le=3650)
    max_pdf_bytes: int = Field(default=52428800, ge=1024, le=524288000)

    @model_validator(mode="after")
    def valid_network(self):
        from urllib.parse import urlsplit

        origin = urlsplit(self.app_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.path
            or origin.query
            or origin.fragment
            or origin.username
        ):
            raise ValueError("APP_ORIGIN doit être une origine sans chemin")
        if origin.scheme == "http" and origin.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("HTTPS est requis hors localhost")
        if origin.scheme == "https" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE=true est requis avec HTTPS")
        return self

    min_score: float = 0.35
    top_k: int = 6
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
