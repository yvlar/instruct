from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    embedding_model: str = "nomic-embed-text"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "work_instructions"
    documents_path: str = "/documents"
    min_score: float = 0.35
    top_k: int = 6
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

