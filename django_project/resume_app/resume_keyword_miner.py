"""
Extractive keyword / phrase mining for a corpus of job descriptions (no LLM).

ATS-oriented: emphasizes skills/stack collocations; drops job titles, HR filler, and
subsumes redundant shorter n-grams (e.g. keep \"software defined networking\", drop
\"software defined\"). Mining uses role-focused description text only — job title
is not concatenated (still passed into extract_role_description for context).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Sequence, Set, Tuple

from . import embeddings as embedding_module

_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could did do does doing done for from further
    had has have having he her him his how i if in into is it its me more my no nor not of off
    on once only or our ours she should so some such than that the their them then there these
    they this those through to too until up ve very was we were what when where which while who
    whom whose why will with would you your yours about above after again against all am any
    because before below between both each few here itself just may might must out over same
    so under until yet re ll d all any both each few just
    across throughout within among high higher highest
    """.split()
)

_GENERIC_UNIGRAMS = frozenset(
    """
    experience work role job skills must preferred required ability years day
    time including various multiple related well highly strong excellent good great opportunity
    position candidate ideal responsibilities qualifications requirements description summary
    overview duties join us company based remote hybrid onsite full part etc eg e g
    looking seeking hiring passionate proven exceptional outstanding
    """.split()
)

_NOISE_UNIGRAMS = frozenset(
    """
    across throughout within among following per via plus also even ever
    high higher highest low lower large small big best better key core main primary
    various several many some few lots myriad diverse wide deep broad hands
    cross functional fast paced dynamic exciting challenging rewarding
    world class state art cutting edge best-in-class
    teams team group groups together collaboratively collaboration
    technical technically business businesses customer customers product products
    user users client clients stakeholder stakeholders partner partners
    industry industries market markets global enterprise
    distinguished senior junior lead principal director
    level grade band ic individual contributor
    ability abilities capable capacity opportunity opportunities environment
    culture values mission vision purpose impact grow growing growth learn learning
    systems metrics stakeholders
    engineer engineers architect architects developer developers scientist scientists
    manager managers director directors recruiter recruiters
    """.split()
)

_ATS_SHORT_UNIGRAMS = frozenset(
    """
    ai ml nlp llm sql api rpc http tls ssl sso iam etl elt oss iac
    aws gcp azure kpi sla sre devops mlops llms gpu tpm data java rust
    ruby scala spark kafka linux agile scrum oauth ldap grpc rest soap
    json yaml html css git npm
    """.split()
)

_BIGRAM_HEAD_JUNK = frozenset(
    """
    a an the this that these those our your their its we you they it there here
    what which who how when where why all both each every few many most some several such
    any other another every no not only just also even more most less very too
    as at be by do if in is it me my no of on or so to up us we
    am is are was were been being have has had having do does did doing done
    will would shall should may might must can could need needs
    including following regarding related per via across throughout within among
    """.split()
) | _STOPWORDS

_UNIGRAM_CANONICAL = {
    "engineers": "engineer",
    "architects": "architect",
    "developers": "developer",
    "scientists": "scientist",
    "managers": "manager",
    "researchers": "researcher",
    "designers": "designer",
    "analysts": "analyst",
    "consultants": "consultant",
    "programs": "program",
    "services": "service",
    "technologies": "technology",
    "applications": "application",
    "solutions": "solution",
    "platforms": "platform",
    "frameworks": "framework",
    "libraries": "library",
    "databases": "database",
    "pipelines": "pipeline",
    "workloads": "workload",
    "workflows": "workflow",
    "microservices": "microservice",
    "containers": "container",
    "clusters": "cluster",
    "instances": "instance",
    "endpoints": "endpoint",
    "integrations": "integration",
    "deployments": "deployment",
    "releases": "release",
    "features": "feature",
    "initiatives": "initiative",
    "objectives": "objective",
    "outcomes": "outcome",
    "requirements": "requirement",
    "stakeholders": "stakeholder",
}

# Seniority / level tokens that usually indicate a job title, not a skill.
_TITLE_MODIFIERS = frozenset(
    """
    senior junior staff principal distinguished associate entry intern contractor
    sr snr i ii iii iv v vi vii l4 l5 l6 l7 l8 e4 e5 e6 e7 m1 m2 m3 m4 m5
    vp svp avp evp cto cio cfo coo ceo head chief cofounder co-founder founding
    grade band level individual
    """.split()
)

# Role nouns at end of phrase → likely a title when combined with modifiers or domain + engineer.
_TITLE_ROLE_ENDINGS = frozenset(
    """
    engineer engineers architect architects developer developers scientist scientists
    manager managers director directors lead leader leaders recruiter recruiters
    specialist specialists consultant consultants analyst analysts designer designers
    coordinator coordinators officer officers president vice chairman chair
    """.split()
)

# Domain-ish words before engineer/architect → still usually a title (not \"software engineering\").
_TITLE_DOMAIN_BEFORE_ENGINEER = frozenset(
    """
    software hardware systems data cloud security platform product application quality
    sales support solutions network networking infrastructure database frontend backend
    fullstack full stack devops mlops reliability site staff web mobile embedded firmware
    graphics game games qa test automation machine learning ai computer research security
    """.split()
)

# Exact multi-word HR / buzz phrases (low ATS skill signal).
_BUZZ_PHRASES = frozenset(
    """
    long term world class proven track record experience building technical vision
    fast paced self starter best in class cutting edge chart your journey
    deliver your impact thought leadership soft skills strong communication
    verbal written attention detail detail oriented
    """.split()
)


_MAX_ROLE_CHARS = 1200
_MAX_OUTPUT_PHRASES = 60
_MIN_BIGRAM_TOKEN_LEN = 2
_MULTIWORD_SHARE = 0.85


def _tokenize(text: str) -> List[str]:
    if not (text or "").strip():
        return []
    return re.findall(r"\w+", text.lower())


def _token_ok_for_ngram(t: str) -> bool:
    if len(t) < _MIN_BIGRAM_TOKEN_LEN or t.isdigit():
        return False
    return True


def _canonical_unigram(t: str) -> str:
    return _UNIGRAM_CANONICAL.get(t, t)


def _unigram_ok_for_ngram_interior(t: str) -> bool:
    if not _token_ok_for_ngram(t):
        return False
    if t in _STOPWORDS:
        return False
    return True


def _trim_ngram_edges(phrase: str) -> str:
    parts = phrase.split()
    while len(parts) > 1 and parts[0] in _BIGRAM_HEAD_JUNK:
        parts = parts[1:]
    while len(parts) > 1 and parts[-1] in _STOPWORDS:
        parts = parts[:-1]
    return " ".join(parts)


def _is_job_title_phrase(phrase: str) -> bool:
    """True if phrase is mostly a job title / leveling line, not a skill to mirror on a resume."""
    p = phrase.strip().lower()
    if not p:
        return True
    if p in _BUZZ_PHRASES:
        return True
    parts = p.split()
    if "distinguished" in parts or "sr" in parts:
        return True
    if parts.count("principal") >= 2:
        return True
    if "principal" in parts and "senior" in parts and len(parts) <= 4:
        return True
    # SDN acronym split into junk trigrams
    if "sdn" in parts and "networking" in parts and "software" not in parts:
        return True
    # Incomplete title lines: "principal software", "staff systems", …
    if len(parts) == 2:
        a, b = parts[0], parts[1]
        if a in _TITLE_MODIFIERS or a in ("distinguished", "staff"):
            if b in _TITLE_DOMAIN_BEFORE_ENGINEER or b in (
                "software",
                "solutions",
                "technologies",
                "products",
                "offerings",
            ):
                return True

    last = parts[-1]
    if last in _TITLE_ROLE_ENDINGS:
        if any(w in _TITLE_MODIFIERS for w in parts[:-1]):
            return True
        if last in ("engineer", "engineers", "architect", "architects"):
            if any(w in _TITLE_DOMAIN_BEFORE_ENGINEER for w in parts[:-1]):
                return True
        if last in ("leader", "leaders"):
            if any(w in ("technical", "technology", "engineering", "product", "thought") for w in parts):
                return True
    return False


def _bigrams_trigrams_for_doc(tokens: List[str]) -> Set[str]:
    raw: set[str] = set()
    n = len(tokens)
    for i in range(n - 1):
        a, b = tokens[i], tokens[i + 1]
        if not _unigram_ok_for_ngram_interior(a) or not _unigram_ok_for_ngram_interior(b):
            continue
        if a in _BIGRAM_HEAD_JUNK:
            continue
        if a in _STOPWORDS and b in _STOPWORDS:
            continue
        raw.add(f"{a} {b}")
    for i in range(n - 2):
        a, b, c = tokens[i], tokens[i + 1], tokens[i + 2]
        if not (
            _unigram_ok_for_ngram_interior(a)
            and _unigram_ok_for_ngram_interior(b)
            and _unigram_ok_for_ngram_interior(c)
        ):
            continue
        if a in _BIGRAM_HEAD_JUNK:
            continue
        if sum(1 for x in (a, b, c) if x in _STOPWORDS) >= 2:
            continue
        raw.add(f"{a} {b} {c}")
    out: set[str] = set()
    for p in raw:
        cleaned = _trim_ngram_edges(p)
        if len(cleaned.split()) < 2:
            continue
        if _is_job_title_phrase(cleaned):
            continue
        if cleaned in _BUZZ_PHRASES:
            continue
        out.add(cleaned)
    return out


def _strict_unigrams_for_doc(tokens: List[str]) -> Set[str]:
    out: set[str] = set()
    for t in tokens:
        if not _token_ok_for_ngram(t):
            continue
        if t in _STOPWORDS or t in _GENERIC_UNIGRAMS or t in _NOISE_UNIGRAMS:
            continue
        c = _canonical_unigram(t)
        if c in _STOPWORDS or c in _GENERIC_UNIGRAMS or c in _NOISE_UNIGRAMS:
            continue
        if c in _ATS_SHORT_UNIGRAMS or len(c) >= 5:
            out.add(c)
    return out


def _mining_text(title: str, description: str) -> str:
    """
    Role-focused body only. Do not prepend the job title — titles dominate n-grams with
    \"Principal / Distinguished / Software Engineer\" spam.
    """
    return (
        embedding_module.extract_role_description(
            description or "", title or "", max_chars=_MAX_ROLE_CHARS
        )
        or ""
    ).strip()


def _phrase_subsumed_by_kept(phrase: str, kept: List[str]) -> bool:
    """True if phrase is a strict contiguous token substring of an already-kept longer phrase."""
    pad = f" {phrase} "
    for k in kept:
        if phrase == k:
            return False
        if len(k) > len(phrase) and pad in f" {k} ":
            return True
    return False


def mine_keywords_from_jobs(
    jobs: Sequence[Tuple[str, str]],
    *,
    max_phrases: int = _MAX_OUTPUT_PHRASES,
) -> List[Dict[str, Any]]:
    if not jobs:
        return []

    total_docs = len(jobs)
    df_multi: dict[str, int] = defaultdict(int)
    df_uni: dict[str, int] = defaultdict(int)

    for title, description in jobs:
        text = _mining_text(title, description)
        tokens = _tokenize(text)
        for p in _bigrams_trigrams_for_doc(tokens):
            df_multi[p] += 1
        for u in _strict_unigrams_for_doc(tokens):
            df_uni[u] += 1

    if not df_multi and not df_uni:
        return []

    def n_words(s: str) -> int:
        return len(s.split())

    # Prefer longer, more specific phrases first, then frequency (reduces \"software defined\" when
    # \"software defined networking\" exists).
    multi_candidates = sorted(
        df_multi.items(),
        key=lambda kv: (-len(kv[0]), -n_words(kv[0]), -kv[1], kv[0]),
    )

    multi_cap = max(1, int(max_phrases * _MULTIWORD_SHARE))
    selected: List[Tuple[str, int]] = []
    kept_multi_keys: List[str] = []
    covered_unigrams: set[str] = set()

    for phrase, doc_count in multi_candidates:
        if len(selected) >= multi_cap:
            break
        if _is_job_title_phrase(phrase):
            continue
        if phrase in _BUZZ_PHRASES:
            continue
        if _phrase_subsumed_by_kept(phrase, kept_multi_keys):
            continue
        kept_multi_keys.append(phrase)
        for u in phrase.split():
            if _token_ok_for_ngram(u):
                covered_unigrams.add(_canonical_unigram(u))
        selected.append((phrase, doc_count))

    uni_sorted = sorted(df_uni.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    for u, doc_count in uni_sorted:
        if len(selected) >= max_phrases:
            break
        if _canonical_unigram(u) in covered_unigrams or u in covered_unigrams:
            continue
        selected.append((u, doc_count))
        covered_unigrams.add(u)
        covered_unigrams.add(_canonical_unigram(u))

    out: List[Dict[str, Any]] = []
    for phrase, doc_count in selected:
        out.append(
            {
                "phrase": phrase,
                "doc_count": doc_count,
                "job_fraction": round(doc_count / total_docs, 4) if total_docs else 0.0,
            }
        )
    return out
