"""
Matching engine scoring service.

Ported from Temp-AI's matching_engine/services/applicant/scoring.py and adapted
for SeraGo's data model. Uses sentence-transformers for embedding similarity
plus keyword overlap, skill matching, and heuristic bonuses.

Scoring formula:
    score = 0.35 * embedding_similarity
          + 0.20 * skill_overlap
          + 0.15 * experience_match
          + 0.10 * sector_match
          + 0.10 * location_match
          + 0.05 * work_mode_match
          + 0.05 * role_match

All components are in [0, 1], so the final score is in [0, 1] and gets
multiplied by 100 for display.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

try:
    from sklearn.metrics.pairwise import cosine_similarity
except ImportError:
    cosine_similarity = None

logger = logging.getLogger(__name__)

# ── Lazy-loaded sentence-transformer model ────────────────────────────
_model = None


def get_model():
    """Lazily load the sentence-transformer model (loaded once, ~80 MB RAM)."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            from django.conf import settings
            model_name = getattr(settings, "MATCHING_MODEL_NAME", "all-MiniLM-L6-v2")
            _model = SentenceTransformer(model_name)
            logger.info("Loaded sentence-transformer model: %s", model_name)
        except ImportError:
            raise ImportError(
                "sentence-transformers is required for the matching engine. "
                "Install it with: pip install sentence-transformers"
            )
    return _model


# ── Stopwords ─────────────────────────────────────────────────────────
STOPWORDS = frozenset({
    "with", "and", "for", "from", "the", "a", "an", "in", "on", "at",
    "by", "to", "of", "is", "are", "as", "be", "this", "that", "it",
    "was", "were", "has", "have", "had", "but", "or", "not", "so", "if",
    "then", "than", "too", "very", "can", "will", "just", "do", "does",
    "did", "into", "out", "about", "above", "below", "up", "down", "over",
    "under", "again", "further", "once", "here", "there", "when", "where",
    "why", "how", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "only", "own", "same", "also",
    "including", "etc", "etc.", "e.g", "i.e",
})

# ── Education / experience keyword sets ────────────────────────────────
EDUCATION_KEYWORDS = frozenset({
    "bachelor", "bachelors", "master", "masters", "phd", "degree",
    "diploma", "certificate", "b.sc", "m.sc", "b.a", "m.a", "mba",
    "bsc", "msc", "ba", "ma", "associate",
})

EXPERIENCE_KEYWORDS = frozenset({
    "engineer", "developer", "analyst", "designer", "manager", "lead",
    "senior", "junior", "intern", "specialist", "architect", "director",
    "coordinator", "consultant", "officer", "administrator",
})

# Sector keyword stop words — words too generic for sector comparison
_SECTOR_STOP_WORDS = frozenset({
    "and", "or", "the", "of", "for", "in", "at", "to", "a", "an",
    "&", "+", "/", ",",
})


# ── Data classes for text building ─────────────────────────────────────

@dataclass
class TalentProfileData:
    """Structured talent profile data for scoring."""
    user_id: str
    headline: str = ""
    about: str = ""
    skills: list[str] = field(default_factory=list)
    experience_level: str | None = None
    years_of_experience: int | None = None
    desired_roles: list[str] = field(default_factory=list)
    desired_job_types: list[str] = field(default_factory=list)
    preferred_sector_ids: list[str] = field(default_factory=list)
    work_mode: str | None = None
    preferred_locations: list[str] = field(default_factory=list)
    work_experience: str = "[]"  # JSON string
    education_history: str = "[]"  # JSON string
    current_industry: str = ""
    current_profession: str = ""

    @property
    def has_data(self) -> bool:
        """Check if the profile has enough data for meaningful scoring."""
        return bool(
            self.headline
            or self.about
            or self.skills
            or self.desired_roles
            or self.current_industry
            or self.current_profession
        )


@dataclass
class JobData:
    """Structured job data for scoring."""
    job_id: str
    title: str = ""
    description: str = ""
    company: str = ""
    location: str = ""
    sector_id: str | None = None
    sector_name: str = ""
    experience_level: str | None = None
    job_type: str | None = None
    work_mode: str | None = None
    skills: str = ""  # comma-separated or JSON
    experience_min_years: int | None = None
    experience_max_years: int | None = None

    @property
    def has_data(self) -> bool:
        """Check if the job has enough data for meaningful scoring."""
        return bool(self.title or self.description)


# ── Text builders ──────────────────────────────────────────────────────

def build_talent_text(profile: TalentProfileData) -> str:
    """Serialize a talent profile into a single text string for embedding."""
    parts = [
        profile.headline or "",
        profile.about or "",
        profile.current_industry or "",
        profile.current_profession or "",
    ]

    for skill in profile.skills:
        parts.append(str(skill))

    for role in profile.desired_roles:
        parts.append(str(role))

    if profile.experience_level:
        parts.append(profile.experience_level)

    try:
        work_exp = json.loads(profile.work_experience) if isinstance(profile.work_experience, str) else profile.work_experience
        if isinstance(work_exp, list):
            for exp in work_exp:
                if isinstance(exp, dict):
                    parts.append(f"{exp.get('company', '')} {exp.get('title', '')} {exp.get('description', '')}")
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        edu = json.loads(profile.education_history) if isinstance(profile.education_history, str) else profile.education_history
        if isinstance(edu, list):
            for e in edu:
                if isinstance(e, dict):
                    parts.append(f"{e.get('institution', '')} {e.get('degree', '')} {e.get('field', '')}")
    except (json.JSONDecodeError, TypeError):
        pass

    text = " ".join(str(p) for p in parts if p).strip()
    return text or "No profile data"


def build_job_text(job: JobData) -> str:
    """Serialize a job into a single text string for embedding."""
    parts = [
        job.title or "",
        job.description or "",
        job.company or "",
        job.location or "",
        job.sector_name or "",
    ]

    if job.skills:
        try:
            skills_list = json.loads(job.skills) if isinstance(job.skills, str) else job.skills
            if isinstance(skills_list, list):
                for s in skills_list:
                    parts.append(str(s))
        except (json.JSONDecodeError, TypeError):
            parts.append(job.skills)

    if job.experience_level:
        parts.append(job.experience_level)

    if job.job_type:
        parts.append(job.job_type)

    text = " ".join(str(p) for p in parts if p).strip()
    return text or "No job description"


# ── Keyword extraction ─────────────────────────────────────────────────

def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from text (words > 3 chars, not stopwords)."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9+#.]{2,}", text.lower())
    return [w for w in words if w not in STOPWORDS]


def text_hash(text: str) -> str:
    """SHA-256 hash of text for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Encoding ───────────────────────────────────────────────────────────

def encode_text(text: str) -> list[float]:
    """Encode text into a 384-dim vector using sentence-transformers."""
    model = get_model()
    embedding = model.encode(text)
    return embedding.tolist()


# ── Heuristic scoring components ──────────────────────────────────────

def _compute_experience_score(
    talent: TalentProfileData,
    job: JobData,
    talent_keywords: set[str],
    job_keywords: set[str],
) -> float:
    """Score how well the talent's experience matches the job requirements."""
    score = 0.0

    if talent.experience_level and job.experience_level:
        level_order = {
            "ENTRY": 1, "JUNIOR": 2, "MID": 3, "SENIOR": 4, "LEAD": 5, "EXECUTIVE": 6,
        }
        t_level = level_order.get(talent.experience_level.upper(), 0)
        j_level = level_order.get(job.experience_level.upper(), 0)
        if t_level and j_level:
            diff = abs(t_level - j_level)
            if diff == 0:
                score += 0.5
            elif diff == 1:
                score += 0.3
            elif diff == 2:
                score += 0.1

    if talent.years_of_experience is not None and job.experience_min_years is not None:
        if talent.years_of_experience >= job.experience_min_years:
            score += 0.3
        elif talent.years_of_experience >= job.experience_min_years - 1:
            score += 0.15

    if talent.years_of_experience is not None and job.experience_max_years is not None:
        if talent.years_of_experience <= job.experience_max_years:
            score += 0.2
        elif talent.years_of_experience <= job.experience_max_years + 1:
            score += 0.1

    edu_matches = talent_keywords & EDUCATION_KEYWORDS
    job_req_edu = job_keywords & EDUCATION_KEYWORDS
    if job_req_edu and edu_matches:
        score += 0.2
    elif not job_req_edu:
        score += 0.1

    return min(score, 1.0)


def _compute_sector_match(talent: TalentProfileData, job: JobData) -> float:
    """Score sector alignment.

    Compares the talent's currentIndustry (free-text string like 'Technology',
    'Accounting') against the job's sectorName (canonical name like
    'Technology & IT', 'Accounting & Finance').

    Returns:
      1.0  — exact or strong match
      0.75 — partial / keyword overlap
      0.5  — no sector info on either side (neutral)
      0.0  — sectors exist but don't match at all
    """
    talent_industry = (talent.current_industry or "").strip().lower()
    job_sector = (job.sector_name or "").strip().lower()

    # If either side has no sector info, return neutral
    if not talent_industry and not job_sector:
        return 0.5
    if not talent_industry:
        return 0.0
    if not job_sector:
        return 0.5

    # Exact match
    if talent_industry == job_sector:
        return 1.0

    # One contains the other (e.g. 'technology' in 'technology & it')
    if talent_industry in job_sector or job_sector in talent_industry:
        return 1.0

    # Keyword overlap: split on common separators and check
    talent_keywords = set(re.split(r'[&/,+\s]+', talent_industry)) - _SECTOR_STOP_WORDS
    job_keywords = set(re.split(r'[&/,+\s]+', job_sector)) - _SECTOR_STOP_WORDS

    overlap = talent_keywords & job_keywords
    if overlap:
        # Partial match — at least one meaningful word in common
        overlap_ratio = len(overlap) / max(len(talent_keywords), len(job_keywords))
        return max(0.5, 0.5 + overlap_ratio * 0.5)  # range [0.5, 1.0]

    return 0.0


def _compute_location_match(talent: TalentProfileData, job: JobData) -> float:
    """Score location alignment."""
    if not talent.preferred_locations:
        return 0.5
    if not job.location:
        return 0.5

    job_loc = job.location.lower()
    for pref in talent.preferred_locations:
        if pref.lower() in job_loc or job_loc in pref.lower():
            return 1.0
        if pref.lower() == "remote" and "remote" in job_loc:
            return 1.0

    return 0.2


def _compute_work_mode_match(talent: TalentProfileData, job: JobData) -> float:
    """Score work mode alignment (Remote/Onsite/Hybrid)."""
    if not talent.work_mode or not job.work_mode:
        return 0.5
    if talent.work_mode.lower() == job.work_mode.lower():
        return 1.0
    if "hybrid" in (talent.work_mode.lower(), job.work_mode.lower()):
        return 0.7
    return 0.2


def _compute_role_match(talent: TalentProfileData, job: JobData) -> float:
    """Score how well the talent's desired roles match the job title."""
    if not talent.desired_roles:
        return 0.5
    if not job.title:
        return 0.5

    job_title_words = set(job.title.lower().split())
    for role in talent.desired_roles:
        role_words = set(role.lower().split())
        overlap = role_words & job_title_words
        if overlap:
            return 1.0
        for rw in role_words:
            if len(rw) > 3 and rw in job.title.lower():
                return 0.7

    return 0.2


# ── Main scoring function ─────────────────────────────────────────────

@dataclass
class ScoreResult:
    """Result of a compatibility scoring computation."""
    score: float  # 0-100
    matched: list[str]
    missing: list[str]
    explanation: dict[str, Any]


def compute_score(
    talent: TalentProfileData,
    job: JobData,
    talent_vector: list[float] | None = None,
    job_vector: list[float] | None = None,
) -> ScoreResult:
    """
    Compute a compatibility score between a talent profile and a job posting.

    If vectors are not provided, they will be computed from the text.

    Returns a ScoreResult with score (0-100), matched/missing skills, and
    a detailed explanation of how the score was computed.
    """
    talent_text = build_talent_text(talent)
    job_text = build_job_text(job)

    if talent_vector is None:
        talent_vector = encode_text(talent_text)
    if job_vector is None:
        job_vector = encode_text(job_text)

    # 1. Embedding similarity (cosine)
    if cosine_similarity is not None:
        emb_sim = float(cosine_similarity([talent_vector], [job_vector])[0][0])
    else:
        # Fallback: manual cosine similarity
        import math
        dot = sum(a * b for a, b in zip(talent_vector, job_vector))
        norm_a = math.sqrt(sum(a * a for a in talent_vector))
        norm_b = math.sqrt(sum(b * b for b in job_vector))
        emb_sim = dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

    # Normalize from [-1, 1] to [0, 1]
    emb_sim = (emb_sim + 1) / 2

    # 2. Keyword extraction and overlap
    talent_kw = set(extract_keywords(talent_text))
    job_kw = set(extract_keywords(job_text))

    matched_kw = talent_kw & job_kw
    missing_kw = job_kw - talent_kw

    skill_overlap = len(matched_kw) / max(1, len(job_kw))

    # 3. Experience match
    experience_score = _compute_experience_score(talent, job, talent_kw, job_kw)

    # 4. Sector match
    sector_score = _compute_sector_match(talent, job)

    # 5. Location match
    location_score = _compute_location_match(talent, job)

    # 6. Work mode match
    work_mode_score = _compute_work_mode_match(talent, job)

    # 7. Role match
    role_score = _compute_role_match(talent, job)

    # Weighted final score
    final_score = (
        0.35 * emb_sim
        + 0.20 * skill_overlap
        + 0.15 * experience_score
        + 0.10 * sector_score
        + 0.10 * location_score
        + 0.05 * work_mode_score
        + 0.05 * role_score
    )

    score_100 = round(final_score * 100, 1)

    explanation = {
        "embedding_similarity": round(emb_sim, 4),
        "skill_overlap": round(skill_overlap, 4),
        "experience_score": round(experience_score, 4),
        "sector_score": round(sector_score, 4),
        "location_score": round(location_score, 4),
        "work_mode_score": round(work_mode_score, 4),
        "role_score": round(role_score, 4),
        "final_score": round(final_score, 4),
        "talent_keywords_count": len(talent_kw),
        "job_keywords_count": len(job_kw),
    }

    return ScoreResult(
        score=score_100,
        matched=sorted(matched_kw),
        missing=sorted(missing_kw),
        explanation=explanation,
    )


# ── Batch scoring helpers ─────────────────────────────────────────────

def score_job_against_talents(
    job: JobData,
    talents: list[TalentProfileData],
    job_vector: list[float] | None = None,
) -> list[dict]:
    """Score a single job against multiple talents."""
    job_text = build_job_text(job)
    if job_vector is None:
        job_vector = encode_text(job_text)

    results = []
    for talent in talents:
        if not talent.has_data:
            continue

        talent_text = build_talent_text(talent)
        talent_vector = encode_text(talent_text)

        result = compute_score(talent, job, talent_vector, job_vector)
        results.append({
            "talent_user_id": talent.user_id,
            "job_id": job.job_id,
            "score": result.score,
            "matched": result.matched,
            "missing": result.missing,
            "explanation": result.explanation,
            "profile_hash": text_hash(talent_text),
        })

    return results


def score_talent_against_jobs(
    talent: TalentProfileData,
    jobs: list[JobData],
    talent_vector: list[float] | None = None,
) -> list[dict]:
    """Score a single talent against multiple jobs."""
    talent_text = build_talent_text(talent)
    if talent_vector is None:
        talent_vector = encode_text(talent_text)

    results = []
    for job in jobs:
        if not job.has_data:
            continue

        job_text = build_job_text(job)
        job_vector = encode_text(job_text)

        result = compute_score(talent, job, talent_vector, job_vector)
        results.append({
            "talent_user_id": talent.user_id,
            "job_id": job.job_id,
            "score": result.score,
            "matched": result.matched,
            "missing": result.missing,
            "explanation": result.explanation,
            "profile_hash": text_hash(talent_text),
        })

    return results
