"""
Async pipeline resume summary: batched LLM extraction over job descriptions.

Uses LangChain providers from llm_factory (same as the resume optimizer). Persists progress under
MEDIA_ROOT/pipeline_llm_extract/{track}/{run_id}/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Keys expected in each batch JSON (also used for aggregation and NDJSON rows).
SKILL_ARRAY_KEYS = (
    "hard_skills",
    "methodologies",
    "soft_skills",
    "business_outcomes",
    "domain_scale",
    "action_verbs",
)

SYSTEM_PROMPT = """You are an expert Technical Recruiter and Data Engineer. 
Your task is to analyze a batch of Job Descriptions and extract high-signal keywords into a strict JSON object.

OUTPUT RULES (CRITICAL):
- Your entire response must be ONE JSON object only: no preamble, no markdown code fences, no chain-of-thought, no analysis.
- Do not use tags such as <thinking> or <think>. Do not explain your reasoning.

EXTRACTION & FORMATTING RULES:
1. DISCRETE ITEMS ONLY: Never include commas or "and" inside an extracted item. Split lists into separate array elements. (e.g., instead of ["python, aws, c++"], output ["python", "aws", "c++"]).
2. LENGTH LIMIT: No extracted item can be longer than 3 words. 
3. BASE VERB FORMS: Convert all verbs and outcomes to their base/root form to prevent duplicates (e.g., output "reduce latency" instead of "reduced latency" or "reduction in latency"). 
4. NO INSTRUCTIONS IN OUTPUT: Do not output any of these prompt instructions or examples in the final JSON. 
5. NO CROSS-CONTAMINATION: "problem solving" and "collaboration" belong ONLY in soft_skills. Do not put them in hard_skills.

CATEGORIZATION DEFINITIONS:
- hard_skills: Strict technical tools, languages, platforms (e.g., python, aws, databricks).
- methodologies: Engineering and architecture processes (e.g., agile, ci cd, distributed systems).
- soft_skills: Interpersonal traits (e.g., leadership, adaptability).
- business_outcomes: High-level goals (e.g., cost optimization, latency reduction).
- domain_scale: Industry context (e.g., fintech, petabyte scale).
- action_verbs: Past-tense verbs showing leadership/execution (e.g., architected, spearheaded).

OUTPUT SCHEMA (Return ONLY this JSON, with empty arrays if nothing is found):
{
  "hard_skills": [],
  "methodologies": [],
  "soft_skills": [],
  "business_outcomes": [],
  "domain_scale": [],
  "action_verbs": []
}
"""

STOP_FILENAME = "stop_signal.flag"
RUN_META_NAME = "run_meta.json"
PROGRESS_NAME = "progress_state.json"
SKILLS_NAME = "extracted_skills.json"
CONSOLIDATED_SKILLS_NAME = "consolidated_skills.json"

CONSOLIDATION_SYSTEM_PROMPT = """You consolidate resume keyword lists for a job search pipeline.

INPUT is one JSON object with keys: hard_skills, methodologies, soft_skills, business_outcomes, domain_scale, action_verbs.
Each value is an array of strings (already lowercased) merged from many job postings.

TASK: Within each array ONLY, merge obvious duplicates and near-duplicates (e.g. "architect", "architected", "architecting" -> one canonical phrase such as "architect"; "code review" and "code reviews" -> "code reviews").

RULES:
1. Output a single JSON object with exactly those six keys. Every string must be lowercase.
2. Do not add new concepts, tools, or buzzwords that are not clearly covered by the input lists.
3. Do not move items between categories; respect the same meaning of each key as in the extraction step.
4. Prefer concise, resume-style phrases. Remove clear junk only if it is generic fluff, not if it is a weak but real skill.
5. Output only JSON, no markdown fences."""



class UserRequestedStop(Exception):
    """Stop flag or interrupt during batch processing."""


def pipeline_extract_root() -> Path:
    from django.conf import settings

    return Path(settings.MEDIA_ROOT) / "pipeline_llm_extract"


def run_directory(track_slug: str, run_id: str) -> Path:
    return pipeline_extract_root() / track_slug / run_id


def _hash_entry_ids(entry_ids: Sequence[int]) -> str:
    raw = ",".join(str(i) for i in entry_ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_tiktoken_encoding_for_model(model: str):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        if model.startswith("gpt-4") or model.startswith("gpt-3.5") or "gpt-4o" in model:
            return tiktoken.get_encoding("cl100k_base")
        return tiktoken.get_encoding("cl100k_base")


def estimate_message_tokens(model: str, system_text: str, user_text: str, overhead: int = 8) -> int:
    enc = get_tiktoken_encoding_for_model(model)
    return len(enc.encode(system_text)) + len(enc.encode(user_text)) + overhead


def load_run_meta(run_dir: Path) -> Dict[str, Any]:
    p = run_dir / RUN_META_NAME
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_progress(run_dir: Path) -> Dict[str, Any]:
    p = run_dir / PROGRESS_NAME
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_run_error(run_dir: Path, message: str) -> None:
    try:
        prog = load_progress(run_dir)
    except Exception:
        prog = {}
    prog.update(
        {
            "phase": "error",
            "error": message,
            "message": message,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    write_progress(run_dir, prog)


def write_progress(run_dir: Path, data: Dict[str, Any]) -> None:
    p = run_dir / PROGRESS_NAME
    data = {**data, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(p)


def append_skills_batch(run_dir: Path, batch_record: Dict[str, Any]) -> None:
    path = run_dir / SKILLS_NAME
    line = json.dumps(batch_record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def stop_requested(run_dir: Path) -> bool:
    return (run_dir / STOP_FILENAME).exists()


def touch_stop_signal(run_dir: Path) -> None:
    (run_dir / STOP_FILENAME).touch()


def read_ndjson_skills(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / SKILLS_NAME
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping bad NDJSON line in %s", path)
    return rows


def effective_pipeline_batch_size(provider: str) -> int:
    """Batch JD count per LLM call. Ollama Local defaults to 1 so the UI advances per job."""
    from django.conf import settings

    base = max(1, int(getattr(settings, "PIPELINE_LLM_BATCH_SIZE", 5)))
    p = (provider or "").strip()
    if p == "Ollama Local":
        return max(1, int(getattr(settings, "PIPELINE_LLM_BATCH_SIZE_OLLAMA_LOCAL", 1)))
    return base


def skill_array_occ_and_batch_freq(
    rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, Counter], Dict[str, Counter]]:
    """
    Per category: total list-slot occurrences, and in how many batch rows the term appears
    at least once (spread across job-description batches).
    """
    occ: Dict[str, Counter] = {k: Counter() for k in SKILL_ARRAY_KEYS}
    batches: Dict[str, Counter] = {k: Counter() for k in SKILL_ARRAY_KEYS}
    for r in rows:
        for key in SKILL_ARRAY_KEYS:
            seen_in_row: set[str] = set()
            for x in r.get(key) or []:
                if isinstance(x, str) and x.strip():
                    t = x.strip().lower()
                    occ[key][t] += 1
                    seen_in_row.add(t)
            for t in seen_in_row:
                batches[key][t] += 1
    return occ, batches


def skill_array_counters(rows: List[Dict[str, Any]]) -> Dict[str, Counter]:
    """Total occurrence counts per category (same as first half of ``skill_array_occ_and_batch_freq``)."""
    occ, _ = skill_array_occ_and_batch_freq(rows)
    return occ


def aggregate_skill_arrays(
    rows: List[Dict[str, Any]],
    *,
    min_count: int = 1,
    counters: Optional[Dict[str, Counter]] = None,
    batch_freq: Optional[Dict[str, Counter]] = None,
) -> Dict[str, List[str]]:
    """
    Merge batch rows: sort by total occurrences (desc), then batch spread (desc), then alpha.
    Drops terms with total count strictly below ``min_count`` (default keeps all).
    """
    if counters is None or batch_freq is None:
        occ, bf = skill_array_occ_and_batch_freq(rows)
        if counters is None:
            counters = occ
        if batch_freq is None:
            batch_freq = bf
    mc = max(1, int(min_count))
    out: Dict[str, List[str]] = {}
    for key in SKILL_ARRAY_KEYS:
        pairs = [
            (t, counters[key][t], batch_freq[key][t])
            for t in counters[key]
            if counters[key][t] >= mc
        ]
        pairs.sort(key=lambda x: (-x[1], -x[2], x[0]))
        out[key] = [t for t, _, _ in pairs]
    return out


def _overlap_rank_scores(
    phrase: str, occ: Counter, batch_freq: Counter
) -> Tuple[int, int]:
    """Best occurrence and batch spread among exact match and plausible substring overlaps."""
    p = phrase.strip().lower()
    best_o = occ.get(p, 0)
    best_b = batch_freq.get(p, 0)
    if len(p) < 3:
        return best_o, best_b
    for t in occ:
        if t == p or len(t) < 4:
            continue
        if t in p or p in t:
            if occ[t] > best_o:
                best_o = occ[t]
            if batch_freq[t] > best_b:
                best_b = batch_freq[t]
    return best_o, best_b


def rank_consolidated_by_counters(
    consolidated: Dict[str, List[str]],
    counters: Dict[str, Counter],
    *,
    min_count: int,
    batch_freq: Dict[str, Counter],
) -> Dict[str, List[str]]:
    """
    Re-order consolidated phrases using occurrence + batch spread; substring overlap when exact
    counts are zero. Drops terms whose exact string occurred 1..min_count-1 when min_count>1.
    """
    mc = max(1, int(min_count))
    out: Dict[str, List[str]] = {}
    for key in SKILL_ARRAY_KEYS:
        occ = counters[key]
        bf = batch_freq[key]
        items = _normalize_token_list(consolidated.get(key))
        scored: List[Tuple[int, int, int, str]] = []
        for t in items:
            exact = occ.get(t, 0)
            if mc > 1 and 0 < exact < mc:
                continue
            bo, bb = _overlap_rank_scores(t, occ, bf)
            scored.append((bo, bb, exact, t))
        scored.sort(key=lambda x: (-x[0], -x[1], -x[2], x[3]))
        out[key] = [x[3] for x in scored]
    return out


def _truncate_for_consolidation(
    aggregated: Dict[str, List[str]], max_per_key: int
) -> Tuple[Dict[str, List[str]], bool]:
    if max_per_key <= 0:
        return aggregated, False
    truncated = False
    out: Dict[str, List[str]] = {}
    for k in SKILL_ARRAY_KEYS:
        items = list(aggregated.get(k) or [])
        if len(items) > max_per_key:
            truncated = True
            items = items[:max_per_key]
        out[k] = items
    return out, truncated


def _build_consolidation_user_content(aggregated: Dict[str, List[str]]) -> str:
    body = {k: list(aggregated.get(k) or []) for k in SKILL_ARRAY_KEYS}
    return "INPUT:\n" + json.dumps(body, ensure_ascii=False, indent=2)


def _write_consolidated_skills(run_dir: Path, data: Dict[str, List[str]]) -> None:
    p = run_dir / CONSOLIDATED_SKILLS_NAME
    tmp = p.with_suffix(".tmp")
    payload = {k: list(data.get(k) or []) for k in SKILL_ARRAY_KEYS}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def maybe_run_pipeline_consolidation(
    run_dir: Path,
    *,
    provider: str,
    api_key: str,
    model: str,
    http_max_attempts: int,
) -> None:
    """Single LLM pass after all batches: semantic de-dupe. Best-effort; leaves NDJSON aggregate if it fails."""
    from django.conf import settings

    if not getattr(settings, "PIPELINE_LLM_CONSOLIDATE", True):
        return
    rows = read_ndjson_skills(run_dir)
    raw = aggregate_skill_arrays(rows, min_count=1) if rows else None
    if not raw or not any(raw.get(k) for k in SKILL_ARRAY_KEYS):
        return
    max_per = max(0, int(getattr(settings, "PIPELINE_LLM_CONSOLIDATE_MAX_ITEMS_PER_KEY", 400)))
    payload, truncated = _truncate_for_consolidation(raw, max_per)
    if truncated:
        logger.info(
            "Consolidation input capped at %s items per key (PIPELINE_LLM_CONSOLIDATE_MAX_ITEMS_PER_KEY)",
            max_per,
        )
    user_content = _build_consolidation_user_content(payload)
    try:
        content, _, _ = llm_call_with_retries(
            provider=provider,
            api_key=api_key,
            model=model,
            user_content=user_content,
            system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
            max_attempts=max(1, int(http_max_attempts)),
        )
        merged = _skills_from_llm_text(content)
        _write_consolidated_skills(run_dir, merged)
        logger.info("Pipeline resume summary: wrote %s", run_dir / CONSOLIDATED_SKILLS_NAME)
    except Exception as e:
        logger.warning("Pipeline skill consolidation failed (API shows raw merge): %s", e)


def read_run_status(track_slug: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Load meta + progress + aggregated NDJSON skills for API polling."""
    from django.conf import settings

    run_dir = run_directory(track_slug, run_id)
    if not run_dir.is_dir():
        return None
    meta = load_run_meta(run_dir)
    prog = load_progress(run_dir)
    rows = read_ndjson_skills(run_dir)
    min_kw = max(1, int(getattr(settings, "PIPELINE_LLM_KEYWORD_MIN_COUNT", 1)))
    if rows:
        occ, bf = skill_array_occ_and_batch_freq(rows)
    else:
        occ = {k: Counter() for k in SKILL_ARRAY_KEYS}
        bf = {k: Counter() for k in SKILL_ARRAY_KEYS}
    aggregated = (
        aggregate_skill_arrays(rows, min_count=min_kw, counters=occ, batch_freq=bf)
        if rows
        else None
    )
    phase = str(prog.get("phase", "unknown"))
    if aggregated and phase == "completed":
        cpath = run_dir / CONSOLIDATED_SKILLS_NAME
        if cpath.is_file():
            try:
                with open(cpath, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    consolidated = {k: _normalize_token_list(data.get(k)) for k in SKILL_ARRAY_KEYS}
                    aggregated = rank_consolidated_by_counters(
                        consolidated, occ, min_count=min_kw, batch_freq=bf
                    )
            except Exception as e:
                logger.warning("Could not load consolidated skills %s: %s", cpath, e)
    return {
        "track": str(meta.get("track", track_slug)),
        "run_id": run_id,
        "provider": str(meta.get("provider") or "OpenAI"),
        "model": str(meta.get("model", "")),
        "phase": phase,
        "processed_count": int(prog.get("processed_count", 0)),
        "total_jobs": int(prog.get("total_jobs", meta.get("total_jobs", 0))),
        "message": prog.get("message"),
        "error": prog.get("error"),
        "aggregated": aggregated,
    }


def build_user_message_for_jobs(
    jobs: Sequence[Tuple[str, str]], start_index: int
) -> str:
    parts = []
    for i, (title, desc) in enumerate(jobs):
        idx = start_index + i + 1
        t = (title or "").strip() or "Untitled"
        d = (desc or "").strip() or "(no description)"
        parts.append(f"--- Job {idx} ---\nTitle: {t}\n\n{d}")
    return "\n\n".join(parts)


_USER_JSON_TAIL = (
    "\n\nRespond with a single JSON object only with keys: "
    + ", ".join(SKILL_ARRAY_KEYS)
    + " (each value is an array of strings). Use lowercase for every string. No markdown code fences."
)

_STRICT_JSON_RETRY_TAIL = (
    "\n\nCRITICAL: Reply with ONLY valid JSON for this schema. "
    "No other characters before or after the JSON object. "
    "Do not include thinking tags or prose."
)


_REASONING_STRIP_PATTERNS = (
    # Common "reasoning" / chain-of-thought wrappers (incl. API-leaked redacted blocks)
    re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE),
    re.compile(r"<thinking>[\s\S]*?</thinking>", re.IGNORECASE),
    re.compile(r"<reasoning>[\s\S]*?</reasoning>", re.IGNORECASE),
)


def _strip_pipeline_reasoning_noise(content: str) -> str:
    """Remove common model reasoning / thinking wrappers so JSON extraction can succeed."""
    if not content or not isinstance(content, str):
        return ""
    t = content.replace("\r\n", "\n").replace("\r", "\n")
    for rx in _REASONING_STRIP_PATTERNS:
        t = rx.sub("", t)
    # Unclosed <think>… (stream ended mid-block): if JSON appears later, keep from first '{'.
    if re.search(r"<redacted_thinking\s*>", t, re.I) and not re.search(
        r"</redacted_thinking\s*>", t, re.I
    ):
        m = re.search(r"<redacted_thinking\s*>", t, re.I)
        if m:
            tail = t[m.end() :].lstrip()
            br = tail.find("{")
            t = tail[br:] if br >= 0 else ""
    return t.strip()


def _normalize_token_list(items: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(items, list):
        return out
    for x in items:
        if x is None:
            continue
        s = str(x).strip().lower()
        if s:
            out.append(s)
    return out


def _skills_from_llm_text(content: str) -> Dict[str, List[str]]:
    raw = (content or "").strip()
    text = _strip_pipeline_reasoning_noise(raw)
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            from .parsers import _extract_json_object, _try_json_dict_lenient

            data = _try_json_dict_lenient(text)
            if not isinstance(data, dict):
                data = _extract_json_object(
                    text, accept_pipeline_skill_keys=True
                )
    if not isinstance(data, dict):
        logger.warning(
            "Pipeline LLM returned non-JSON (first 800 chars raw): %r",
            raw[:800] if raw else "",
        )
        raise ValueError("Model response is not a JSON object")
    return {k: _normalize_token_list(data.get(k)) for k in SKILL_ARRAY_KEYS}


def _empty_skills_dict() -> Dict[str, List[str]]:
    return {k: [] for k in SKILL_ARRAY_KEYS}


@dataclass
class _MinuteBucket:
    """Fixed calendar minute TPM tracking."""

    minute_epoch: int = -1
    tokens_used: int = 0

    def _current_minute() -> int:
        return int(time.time()) // 60

    def reset_if_new_minute(self) -> None:
        m = int(time.time()) // 60
        if m != self.minute_epoch:
            self.minute_epoch = m
            self.tokens_used = 0

    def can_use(self, max_tpm: int, n: int) -> bool:
        self.reset_if_new_minute()
        return self.tokens_used + n <= max_tpm

    def record_usage(self, n: int) -> None:
        self.reset_if_new_minute()
        self.tokens_used += max(0, n)

    def seconds_until_next_minute(self) -> float:
        now = time.time()
        return max(0.0, 60.0 - (now % 60.0))


def _sleep_for_rpm(last_request_end: float, rpm: int) -> None:
    if rpm <= 0:
        return
    min_interval = 60.0 / float(rpm)
    elapsed = time.time() - last_request_end
    wait = min_interval - elapsed
    if wait > 0:
        time.sleep(wait)


def _bind_json_mode(llm: Any, provider: str) -> Tuple[Any, bool]:
    """Return (runnable, True) when native JSON mode is enabled."""
    try:
        # Groq: langchain_groq forwards bind(model_kwargs=...) in a way the client rejects.
        if provider in ("OpenAI", "OpenRouter"):
            return llm.bind(model_kwargs={"response_format": {"type": "json_object"}}), True
        if provider == "Google AI Studio":
            return llm.bind(model_kwargs={"response_mime_type": "application/json"}), True
    except Exception as e:
        logger.debug("pipeline JSON bind not used for %s: %s", provider, e)
    return llm, False


def llm_invoke_pipeline_batch(
    *,
    provider: str,
    api_key: str,
    model: str,
    user_content: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> Tuple[str, int, int]:
    """
    Single stateless chat turn: system prompt + this batch’s user text only.
    No prior JD batches or assistant messages are sent (including for Ollama Local),
    so context length and KV cache are per-call, not cumulative across the pipeline run.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from .agents import _normalize_token_usage
    from .llm_factory import get_llm

    llm = get_llm(provider, api_key, model)
    llm, native_json = _bind_json_mode(llm, provider)
    user_text = user_content if native_json else (user_content + _USER_JSON_TAIL)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_text),
    ]
    response = llm.invoke(messages)
    raw = getattr(response, "content", None) or ""
    if isinstance(raw, list):
        raw = "".join(
            c.get("text", c) if isinstance(c, dict) else str(c) for c in raw
        )
    text = str(raw).strip()
    prompt_for_usage = f"{system_prompt}\n\n{user_text}"
    tu = _normalize_token_usage(
        response, prompt_text=prompt_for_usage, response_content=text
    )
    in_tok = int(tu.get("input_tokens") or 0)
    out_tok = int(tu.get("output_tokens") or 0)
    return text, in_tok, max(1, in_tok + out_tok)


def llm_call_with_retries(
    *,
    provider: str,
    api_key: str,
    model: str,
    user_content: str,
    system_prompt: str = SYSTEM_PROMPT,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 120.0,
) -> Tuple[str, int, int]:
    """Retry only on 429 or 5xx. ``max_attempts`` is total tries (default 3 = initial + 2 retries)."""
    max_attempts = max(1, int(max_attempts))
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return llm_invoke_pipeline_batch(
                provider=provider,
                api_key=api_key,
                model=model,
                user_content=user_content,
                system_prompt=system_prompt,
            )
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            status = getattr(e, "status_code", None)
            if status is None:
                status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = (
                status == 429
                or "429" in msg
                or "rate" in msg
                or (status is not None and 500 <= int(status) < 600)
            )
            if not retryable:
                raise
            if attempt >= max_attempts - 1:
                logger.warning(
                    "LLM giving up after %s attempts (%s)", max_attempts, e
                )
                raise
            delay = min(max_delay, base_delay * (2**attempt) + random.uniform(0, 0.5))
            logger.warning(
                "LLM transient error (attempt %s/%s): %s — retry in %.1fs",
                attempt + 1,
                max_attempts,
                e,
                delay,
            )
            time.sleep(delay)
    if last_err:
        raise last_err
    raise RuntimeError("llm_call_with_retries exhausted")


def run_pipeline_llm_extraction(
    run_dir: Path,
    *,
    provider: str,
    api_key: str,
    jobs: Sequence[Tuple[str, str]],
    model: str,
    batch_size: int,
    max_tokens_per_minute: int,
    requests_per_minute: int,
    http_max_attempts: int = 3,
) -> None:
    """
    Process jobs (already sliced). Idempotent resume via progress_state.json.
    """
    try:
        from .models import AppAutomationSettings
    except Exception:
        AppAutomationSettings = None  # type: ignore

    meta_path = run_dir / RUN_META_NAME
    if not meta_path.exists():
        raise FileNotFoundError(f"Missing {RUN_META_NAME}")

    bucket = _MinuteBucket()
    last_request_end = 0.0
    progress_path = run_dir / PROGRESS_NAME
    if not progress_path.exists():
        raise FileNotFoundError(f"Missing {PROGRESS_NAME}")

    prog = load_progress(run_dir)
    last_done = int(prog.get("last_processed_index", -1))
    phase = str(prog.get("phase", "running"))

    if phase in ("completed", "stopped", "error"):
        logger.info("Run %s already terminal: %s", run_dir, phase)
        return

    total = len(jobs)
    if total == 0:
        write_progress(
            run_dir,
            {
                "last_processed_index": -1,
                "phase": "completed",
                "processed_count": 0,
                "total_jobs": 0,
                "error": None,
                "message": None,
            },
        )
        _finalize_meta(run_dir)
        return

    def kill_switch() -> bool:
        if AppAutomationSettings is None:
            return False
        try:
            return bool(AppAutomationSettings.get_solo().stop_llm_requests)
        except Exception:
            return False

    def save_stopped(message: str) -> None:
        write_progress(
            run_dir,
            {
                "last_processed_index": last_done,
                "phase": "stopped",
                "processed_count": last_done + 1 if last_done >= 0 else 0,
                "total_jobs": total,
                "error": None,
                "message": message,
            },
        )
        _finalize_meta(run_dir, completed=False)

    idx = last_done + 1

    while idx < total:
        if stop_requested(run_dir):
            logger.info("Stopped by user (flag).")
            save_stopped("Stopped by user")
            return
        if kill_switch():
            logger.info("LLM requests disabled (AppAutomationSettings).")
            save_stopped("LLM requests disabled in settings")
            return

        batch_end = min(idx + batch_size, total) - 1

        def process_range(start_i: int, end_i: int) -> None:
            nonlocal last_request_end, idx, last_done
            chunk = jobs[start_i : end_i + 1]
            if not chunk:
                return
            user_text = build_user_message_for_jobs(chunk, start_i)
            est = estimate_message_tokens(model, SYSTEM_PROMPT, user_text)

            if max_tokens_per_minute > 0 and not bucket.can_use(max_tokens_per_minute, est):
                sleep_s = bucket.seconds_until_next_minute()
                if sleep_s > 0.05:
                    logger.info("TPM budget: sleeping %.2fs until next minute window", sleep_s)
                    time.sleep(sleep_s)
                bucket.reset_if_new_minute()

            if max_tokens_per_minute > 0 and est > max_tokens_per_minute:
                if len(chunk) > 1:
                    mid = start_i + (end_i - start_i) // 2
                    process_range(start_i, mid)
                    process_range(mid + 1, end_i)
                    return
                while max_tokens_per_minute > 0 and not bucket.can_use(max_tokens_per_minute, est):
                    sleep_s = bucket.seconds_until_next_minute()
                    logger.info("Single large batch: waiting %.2fs for TPM headroom", sleep_s)
                    time.sleep(max(sleep_s, 1.0))
                    bucket.reset_if_new_minute()

            _sleep_for_rpm(last_request_end, requests_per_minute)

            if stop_requested(run_dir):
                raise UserRequestedStop()
            if kill_switch():
                raise RuntimeError("KILL_SWITCH")

            try:
                hb = load_progress(run_dir)
                hb["message"] = (
                    f"Calling LLM for job descriptions {start_i + 1}–{end_i + 1} of {total} "
                    f"({len(chunk)} in this batch; local models may take several minutes)…"
                )
                write_progress(run_dir, hb)
            except Exception:
                logger.debug("pipeline heartbeat progress write skipped", exc_info=True)

            content, prompt_tokens, _total_tok = llm_call_with_retries(
                provider=provider,
                api_key=api_key,
                model=model,
                user_content=user_text,
                max_attempts=http_max_attempts,
            )
            last_request_end = time.time()
            actual_prompt = prompt_tokens or est
            bucket.record_usage(actual_prompt)

            try:
                skills = _skills_from_llm_text(content)
            except ValueError:
                from django.conf import settings as dj_settings

                parse_retry = bool(
                    getattr(dj_settings, "PIPELINE_LLM_JSON_PARSE_RETRY", True)
                )
                empty_fallback = bool(
                    getattr(
                        dj_settings,
                        "PIPELINE_LLM_USE_EMPTY_SKILLS_AFTER_RETRIES",
                        True,
                    )
                )
                if parse_retry:
                    logger.warning(
                        "Pipeline batch JSON parse failed (jobs %s–%s); retrying with strict JSON-only tail.",
                        start_i + 1,
                        end_i + 1,
                    )
                    content2, prompt_tokens2, _total2 = llm_call_with_retries(
                        provider=provider,
                        api_key=api_key,
                        model=model,
                        user_content=user_text + _STRICT_JSON_RETRY_TAIL,
                        max_attempts=http_max_attempts,
                    )
                    last_request_end = time.time()
                    bucket.record_usage(prompt_tokens2 or est)
                    try:
                        skills = _skills_from_llm_text(content2)
                    except ValueError:
                        if empty_fallback:
                            logger.error(
                                "Pipeline batch still not JSON after retry; using empty skill arrays."
                            )
                            skills = _empty_skills_dict()
                        else:
                            raise
                elif empty_fallback:
                    logger.error(
                        "Pipeline batch JSON parse failed; using empty skill arrays."
                    )
                    skills = _empty_skills_dict()
                else:
                    raise
            batch_record = {
                "start_index": start_i,
                "end_index": end_i,
                **{k: skills[k] for k in SKILL_ARRAY_KEYS},
            }
            append_skills_batch(run_dir, batch_record)
            last_done = end_i
            idx = end_i + 1
            write_progress(
                run_dir,
                {
                    "last_processed_index": last_done,
                    "phase": "running",
                    "processed_count": last_done + 1,
                    "total_jobs": total,
                    "error": None,
                    "message": None,
                },
            )

        try:
            process_range(idx, batch_end)
        except UserRequestedStop:
            logger.info("Stopped by user (flag) during batch.")
            save_stopped("Stopped by user")
            return
        except RuntimeError as e:
            if str(e) == "KILL_SWITCH":
                save_stopped("LLM requests disabled in settings")
                return
            raise

    from django.conf import settings as dj_settings

    consolidate_on = bool(getattr(dj_settings, "PIPELINE_LLM_CONSOLIDATE", True))
    if consolidate_on:
        prog_fin = load_progress(run_dir)
        prog_fin.update(
            {
                "last_processed_index": total - 1,
                "phase": "consolidating",
                "processed_count": total,
                "total_jobs": total,
                "error": None,
                "message": "Consolidating keywords (semantic deduplication)…",
            }
        )
        write_progress(run_dir, prog_fin)
        maybe_run_pipeline_consolidation(
            run_dir,
            provider=provider,
            api_key=api_key,
            model=model,
            http_max_attempts=http_max_attempts,
        )
    prog_fin = load_progress(run_dir)
    prog_fin.update(
        {
            "last_processed_index": total - 1,
            "phase": "completed",
            "processed_count": total,
            "total_jobs": total,
            "error": None,
            "message": None,
        }
    )
    write_progress(run_dir, prog_fin)
    _finalize_meta(run_dir)


def _finalize_meta(run_dir: Path, completed: bool = True) -> None:
    meta = load_run_meta(run_dir)
    meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["success"] = completed
    p = run_dir / RUN_META_NAME
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    tmp.replace(p)


def write_initial_run_files(
    run_dir: Path,
    *,
    track: str,
    provider: str,
    model: str,
    max_jobs: Optional[int],
    entry_ids: Sequence[int],
    jobs: Sequence[Tuple[str, str]],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "track": track,
        "provider": provider,
        "model": model,
        "max_jobs": max_jobs,
        "total_jobs": len(entry_ids),
        "entry_ids": list(entry_ids),
        "entry_ids_hash": _hash_entry_ids(entry_ids),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completed_at": None,
        "success": None,
    }
    with open(run_dir / RUN_META_NAME, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    prog = {
        "last_processed_index": -1,
        "phase": "running",
        "processed_count": 0,
        "total_jobs": len(entry_ids),
        "entry_ids_hash": meta["entry_ids_hash"],
        "error": None,
        "message": None,
    }
    write_progress(run_dir, prog)

    skills_path = run_dir / SKILLS_NAME
    if skills_path.exists():
        skills_path.unlink()


def fetch_jobs_ordered(entry_ids: Sequence[int]) -> List[Tuple[str, str]]:
    """Return (title, description) tuples in the same order as entry_ids."""
    from .models import PipelineEntry

    id_list = [int(x) for x in entry_ids]
    if not id_list:
        return []
    rows = {
        e.id: e
        for e in PipelineEntry.objects.filter(id__in=id_list).select_related("job_listing")
    }
    out: List[Tuple[str, str]] = []
    for eid in id_list:
        e = rows.get(eid)
        if not e:
            logger.warning("Pipeline entry %s missing; skipping in extraction order.", eid)
            continue
        jl = e.job_listing
        title = (jl.title or "").strip()
        desc = jl.description or ""
        out.append((title, desc))
    return out


def resolve_provider_api_key(provider: str) -> Optional[str]:
    """Decrypt stored key for provider, else same env fallbacks as ``llm_factory.get_llm``."""
    from django.conf import settings

    from .crypto import decrypt_api_key
    from .models import LLMProviderConfig

    cfg = LLMProviderConfig.objects.filter(provider=provider).exclude(encrypted_api_key="").first()
    if cfg and cfg.encrypted_api_key:
        try:
            return decrypt_api_key(cfg.encrypted_api_key)
        except Exception:
            pass

    if provider == "OpenAI":
        key = getattr(settings, "OPENAI_API_KEY", None)
        return str(key).strip() if key else None
    if provider == "Anthropic":
        key = getattr(settings, "ANTHROPIC_API_KEY", None)
        return str(key).strip() if key else None
    if provider == "Groq":
        key = getattr(settings, "GROQ_API_KEY", None)
        return str(key).strip() if key else None
    if provider == "Google AI Studio":
        key = getattr(settings, "GOOGLE_API_KEY", None)
        return str(key).strip() if key else None
    if provider == "Ollama Cloud":
        key = getattr(settings, "OLLAMA_API_KEY", None)
        return str(key).strip() if key else None
    if provider == "Ollama Local":
        key = getattr(settings, "OLLAMA_LOCAL_HOST", None) or getattr(settings, "OLLAMA_HOST", None)
        return str(key).strip() if key else None
    if provider == "OpenRouter":
        key = getattr(settings, "OPENROUTER_API_KEY", None)
        return str(key).strip() if key else None
    return None


def resolve_openai_api_key() -> Optional[str]:
    """Backward-compatible alias for OpenAI-only callers."""
    return resolve_provider_api_key("OpenAI")
