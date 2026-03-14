from typing import List, Optional

from ninja import Schema


class JobSearchRequest(Schema):
    search_term: str
    location: Optional[str] = None
    site_name: Optional[List[str]] = None
    results_wanted: Optional[int] = 50
    resume_id: Optional[int] = None
    sort: Optional[str] = "focus"  # "focus" = by fit to liked jobs
    llm_provider: Optional[str] = None  # for Matching step; uses configured provider if omitted
    llm_model: Optional[str] = None  # for Matching step; uses provider default if omitted
    track: Optional[str] = None  # "ic" or "mgmt" preference track


class JobPayload(Schema):
    id: int
    title: str
    company_name: str
    location: str
    snippet: str
    url: str
    source: str
    focus_score: Optional[float] = None
    focus_percent: Optional[int] = None
    focus_reason: Optional[List[dict]] = None
    similar_to_disliked_percent: Optional[int] = None
    focus_percent_after_penalty: Optional[int] = None
    preference_margin_percent: Optional[int] = None
    matching_score: Optional[int] = None


class JobDetailPayload(Schema):
    id: int
    title: str
    company_name: str
    location: str
    description: str
    url: str
    source: str


class JobSearchResponse(Schema):
    jobs: List[JobPayload]
    total: int


class MatchRequest(Schema):
    resume_id: Optional[int] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None


class MatchResponse(Schema):
    score: int
    reasoning: str
    thoughts: str
    job_listing_id: int
    resume_id: int


class MarkAppliedRequest(Schema):
    resume_id: int


class KeywordEntry(Schema):
    keyword: str
    resume_id: int


class RunKeywordSearchRequest(Schema):
    entries: List[KeywordEntry]
    location: Optional[str] = None
    site_name: Optional[List[str]] = None
    results_wanted: Optional[int] = 50


class JobMatchPayload(Schema):
    job_listing_id: int
    title: str
    company_name: str
    location: str
    url: str
    keyword: Optional[str] = None
    fit_score: Optional[int] = None
    reasoning: str
    thoughts: str
    analyzed_at: str
    status: str
    resume_id: int


class RunKeywordSearchResponse(Schema):
    results: List[JobMatchPayload]
    errors: List[str]


class AiMatchRequest(Schema):
    resume_id: int
    job_listing_ids: List[int]
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None


class AiMatchResultItem(Schema):
    job_listing_id: int
    score: int
    reasoning: str
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None


class AiMatchResponse(Schema):
    results: List[AiMatchResultItem]
    errors: List[str] = []


class InsightsRequest(Schema):
    job_listing_ids: List[int]
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None


class InsightsResponse(Schema):
    content: str
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None


class ResumeOption(Schema):
    id: int
    uploaded_at: str
    label: str


class DisqualifierAddRequest(Schema):
    phrase: str


class DisqualifierPayload(Schema):
    id: int
    phrase: str

