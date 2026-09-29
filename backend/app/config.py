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

    lexical_index_path: str = "./data/lexical"
    retrieval_candidates: int = Field(default=24, ge=1, le=100)
    context_max_chars: int = Field(default=8000, ge=512, le=64000)
    min_score: float = Field(default=0.35, ge=-1, le=1)
    top_k: int = Field(default=4, ge=1, le=20)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
