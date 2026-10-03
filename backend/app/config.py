from typing import Literal

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
    document_state_path: str = ""
    max_pdf_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=1024**3)
    max_context_chars: int = Field(default=3200, ge=200, le=24000)
    max_passage_chars: int = Field(default=1400, ge=100, le=16000)
    max_answer_chars: int = Field(default=1600, ge=100, le=4200)
    max_response_chars: int = Field(default=6000, ge=200, le=24000)
    ollama_num_ctx: int = Field(default=4096, ge=2048, le=32768)
    ollama_num_predict: int = Field(default=768, ge=128, le=4096)

    # OLLAMA_NUM_PREDICT remains the backwards-compatible Fast budget.
    ollama_reflection_num_predict: int = Field(default=1536, ge=128, le=8192)
    ollama_think_support: Literal["auto", "boolean", "none"] = "auto"
    ollama_keep_alive_seconds: int = Field(default=120, ge=0, le=3600)
    ask_timeout_seconds: float = Field(default=180, ge=1, le=240)
    ask_concurrency: int = Field(default=1, ge=1, le=4)
    ask_queue_size: int = Field(default=2, ge=0, le=16)
    ask_queue_timeout_seconds: float = Field(default=15, ge=0.1, le=60)

    @model_validator(mode="after")
    def valid_chunk_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP doit être inférieur à CHUNK_SIZE")
        if (
            max(self.ollama_num_predict, self.ollama_reflection_num_predict)
            >= self.ollama_num_ctx // 2
        ):
            raise ValueError(
                "Les budgets de génération doivent être inférieurs à la moitié de OLLAMA_NUM_CTX"
            )
        return self

    state_path: str = "/state"
    app_origin: str = "http://localhost:3000"
    cookie_secure: bool = False
    session_seconds: int = Field(default=28800, ge=60, le=604800)
    login_attempts: int = Field(default=5, ge=1, le=100)
    audit_retention_days: int = Field(default=90, ge=1, le=3650)

    @model_validator(mode="after")
    def valid_network(self):
        from pathlib import Path
        from urllib.parse import urlsplit

        if not self.document_state_path:
            self.document_state_path = str(Path(self.state_path) / "document-manager")
        if not self.lexical_index_path:
            self.lexical_index_path = str(Path(self.state_path) / "lexical")

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

    min_score: float = Field(default=0.35, ge=-1, le=1)
    lexical_index_path: str = ""
    retrieval_candidates: int = Field(default=24, ge=1, le=100)
    context_max_chars: int = Field(default=8000, ge=512, le=64000)
    top_k: int = Field(default=4, ge=1, le=20)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
