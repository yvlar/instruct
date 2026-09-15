from pydantic import BaseModel, Field


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class Source(BaseModel):
    document: str
    page: int
    excerpt: str
    score: float


class Answer(BaseModel):
    answer: str
    sources: list[Source]
    grounded: bool


class IngestionResult(BaseModel):
    documents: int
    chunks: int

