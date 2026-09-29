from pydantic import BaseModel, Field


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class Source(BaseModel):
    source_id: str
    passage_id: str
    document: str
    page: int
    excerpt: str
    score: float
    document_id: str | None = None
    version: str | None = None
    revision: str
    fingerprint: str


class Claim(BaseModel):
    text: str
    source_ids: list[str]


class Answer(BaseModel):
    answer: str
    sources: list[Source]
    grounded: bool
    claims: list[Claim]
    safety_notice: str


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
