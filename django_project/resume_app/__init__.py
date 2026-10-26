"""
Job Application & Resume Optimization Platform.

Organized by Domain-Driven Design (DDD) Bounded Contexts:
- domain: Core domain primitives, Value Objects, Domain Events, and Event Bus.
- sourcing: Job Sourcing Anti-Corruption Layer (Ports & Adapters) and Ingestion Service.
- pipeline: Career Pipeline state machine, stage boards, and preference ranking.
- optimizer: Multi-agent resume tailoring, ATS/Recruiter judges, and LLM prompt graphs.
- llm_gateway: LLM Provider routing, kill switch, rate limits, and failure policies.
- billing: Commercial plans, subscriptions, quotas, and Stripe webhook handling.
- application: Use Case orchestration services coordinating domain entities and tasks.
"""
