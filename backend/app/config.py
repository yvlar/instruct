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
    document_state_path: str = ".instruct-state"
    max_pdf_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=1024**3)

    @model_validator(mode="after")
    def valid_chunk_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP doit être inférieur à CHUNK_SIZE")
        return self

    min_score: float = 0.35
    top_k: int = 6
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
