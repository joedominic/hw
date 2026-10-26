from datetime import datetime
from typing import Any, List, Optional

from ninja import Schema


class JobSearchRequest(Schema):
    search_term: str
    location: Optional[str] = None
    site_name: Optional[List[str]] = None
    results_wanted: Optional[int] = 50
    resume_id: Optional[int] = None
    sort: Optional[str] = "match"  # "match"/"focus" (% match), "latest"/"freshness" (date posted)
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
    source_display: Optional[str] = None
    focus_score: Optional[float] = None
    focus_percent: Optional[int] = None
    focus_reason: Optional[List[dict]] = None
    similar_to_disliked_percent: Optional[int] = None
    focus_percent_after_penalty: Optional[int] = None
    preference_margin_percent: Optional[int] = None
    matching_score: Optional[int] = None
    interview_probability: Optional[int] = None
    interview_reasoning: Optional[str] = None
    interview_status: Optional[str] = None  # "pending" | "short_jd" when no interview_probability
    posted_at: Optional[datetime] = None  # when the job was posted on the source board
    fetched_at: Optional[datetime] = None  # when we first ingested the job into the app
    optimized_resume_id: Optional[int] = None  # latest OptimizedResume for Applying board (pipeline-linked)
    optimizer_user_resume_id: Optional[int] = None  # UserResume id for "Open optimizer" prefill (Applying board)
    pipeline_entry_id: Optional[int] = None  # PipelineEntry id for Done-board interview prep
    has_interview_prep: Optional[bool] = None
    is_saved: Optional[bool] = False
    is_liked: Optional[bool] = False


class JobDetailPayload(Schema):
    id: int
    title: str
    company_name: str
    location: str
    description: str
    description_char_count: int = 0
    url: str
    source: str
    external_id: str = ""
    posted_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    raw_json: Optional[Any] = None


class FetchJobDescriptionRequest(Schema):
    url: str
    job_listing_id: Optional[int] = None


class FetchJobDescriptionResponse(Schema):
    description: str
    title: Optional[str] = None
    company_name: Optional[str] = None
    location: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    job_listing_id: Optional[int] = None


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
    track: Optional[str] = None


class JobMatchPayload(Schema):
    job_listing_id: int
    title: str
    company_name: str
    location: str
    url: str
    keyword: Optional[str] = None
    fit_score: Optional[int] = None
    reasoning: str
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


class PipelineResumeSummaryAggregate(Schema):
    hard_skills: List[str]
    methodologies: List[str]
    soft_skills: List[str]
    business_outcomes: List[str] = []
    domain_scale: List[str] = []
    action_verbs: List[str] = []


class PipelineResumeSummaryStartRequest(Schema):
    track: str
    llm_provider: str
    model: str
    max_jobs: Optional[int] = None


class PipelineResumeSummaryStartResponse(Schema):
    run_id: str
    total_jobs: int
    track: str


class PipelineResumeSummaryStatusResponse(Schema):
    track: str
    run_id: str
    provider: str = ""
    model: str
    phase: str
    processed_count: int
    total_jobs: int
    message: Optional[str] = None
    error: Optional[str] = None
    aggregated: Optional[PipelineResumeSummaryAggregate] = None


class PipelineResumeSummaryStopRequest(Schema):
    track: str
    run_id: str


class ResumeOption(Schema):
    id: int
    uploaded_at: str
    label: str
    track: Optional[str] = ""


class DisqualifierAddRequest(Schema):
    phrase: str


class DisqualifierPayload(Schema):
    id: int
    phrase: str


class InterviewPrepTypeInfo(Schema):
    interview_type: str
    content: str = ""
    markdown: str = ""
    generated_at: Optional[str] = None


class InterviewPrepGenerateRequest(Schema):
    interview_type: str = "recruiter"
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None


class InterviewPrepResponse(Schema):
    interview_type: str = "recruiter"
    content: str = ""
    markdown: str = ""
    generated_at: Optional[str] = None
    available_types: List[str] = []
    types_data: dict[str, Any] = {}
    provider: str = ""
    model: str = ""
    prompt: Optional[str] = None


class InterviewPrepSaveRequest(Schema):
    interview_prep: str
    interview_type: Optional[str] = None


