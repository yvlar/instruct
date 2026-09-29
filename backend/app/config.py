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
    max_context_chars: int = Field(default=3200, ge=200, le=24000)
    max_passage_chars: int = Field(default=1400, ge=100, le=16000)
    max_answer_chars: int = Field(default=1600, ge=100, le=4200)
    max_response_chars: int = Field(default=6000, ge=200, le=24000)
    ollama_num_ctx: int = Field(default=4096, ge=2048, le=32768)
    ollama_num_predict: int = Field(default=768, ge=128, le=4096)

    @model_validator(mode="after")
    def valid_chunk_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP doit être inférieur à CHUNK_SIZE")
        if self.ollama_num_predict >= self.ollama_num_ctx // 2:
            raise ValueError(
                "OLLAMA_NUM_PREDICT doit être inférieur à la moitié de OLLAMA_NUM_CTX"
            )
        return self

    min_score: float = Field(default=0.35, ge=-1, le=1)
    lexical_index_path: str = "./data/lexical"
    retrieval_candidates: int = Field(default=24, ge=1, le=100)
    context_max_chars: int = Field(default=8000, ge=512, le=64000)
    top_k: int = Field(default=4, ge=1, le=20)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
