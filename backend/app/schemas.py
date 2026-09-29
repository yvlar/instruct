from pydantic import BaseModel, Field


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class Source(BaseModel):
    passage_id: str
    document: str
    page: int
    excerpt: str
    score: float


class Answer(BaseModel):
    answer: str
    sources: list[Source]
    grounded: bool


class IngestionError(BaseModel):
    document: str
    code: str


class IngestionResult(BaseModel):
    documents: int
    chunks: int

    added: int = 0
    modified: int = 0
    unchanged: int = 0
    deleted: int = 0
    failed: int = 0
    errors: list[IngestionError] = Field(default_factory=list)
    cleanup_pending: bool = False
