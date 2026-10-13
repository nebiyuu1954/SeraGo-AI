from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ai_service.providers import LlmError, LlmRouter

logger = logging.getLogger("ai_service.cover_letter")


@dataclass
class CoverLetterRequest:
    job_title: str
    company: str = "Hiring Team"
    job_description: str = ""
    talent_profile: dict[str, Any] = field(default_factory=dict)
    tone: str = "professional"  # professional, enthusiastic, concise
    custom_notes: str = ""

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> CoverLetterRequest:
        if not isinstance(data, dict):
            raise ValueError("Payload must be a JSON object")

        job_title = str(data.get("jobTitle") or data.get("job_title") or "").strip()
        if not job_title:
            raise ValueError("jobTitle is required")

        company = str(data.get("company") or "Hiring Team").strip()
        job_description = str(data.get("jobDescription") or data.get("job_description") or "").strip()
        talent_profile = data.get("talentProfile") or data.get("talent_profile") or {}
        if not isinstance(talent_profile, dict):
            talent_profile = {}

        tone = str(data.get("tone") or "professional").strip()
        custom_notes = str(data.get("customNotes") or data.get("custom_notes") or "").strip()

        return cls(
            job_title=job_title,
            company=company,
            job_description=job_description,
            talent_profile=talent_profile,
            tone=tone,
            custom_notes=custom_notes,
        )


@dataclass
class CoverLetterResult:
    cover_letter: str
    key_highlights: list[str]
    subject_line: str
    provider_used: str
    model_used: str
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverLetter": self.cover_letter,
            "keyHighlights": self.key_highlights,
            "subjectLine": self.subject_line,
            "providerUsed": self.provider_used,
            "modelUsed": self.model_used,
            "latencyMs": self.latency_ms,
        }


def _build_system_prompt(tone: str) -> str:
    return f"""You are an elite executive career coach and professional cover letter writer.
Your task is to write an authentic, compelling, and tailored cover letter for a job applicant.

Guidelines:
1. Tone: {tone.capitalize()} yet natural and confident.
2. Structure:
   - Engaging opening: State the role and why this specific opportunity and company align with the candidate's background.
   - Body paragraph(s): Connect the candidate's concrete past accomplishments, skills, and projects directly to the responsibilities and challenges in the job description.
   - Closing paragraph: Professional wrap-up with a polite call to action for an interview.
3. Authenticity: Avoid robotic, generic clichés (do NOT use "I am thrilled to throw my hat in the ring" or "I believe I am the ideal candidate"). Write as an articulate, seasoned human professional.
4. Accuracy: Reference ONLY skills, projects, and experiences that are supported by the provided candidate profile. Do not invent past employers or degrees.

Output format:
Respond with ONLY a valid JSON object matching this schema:
{{
  "subjectLine": "Application for <Job Title> - <Candidate Name>",
  "coverLetter": "Full formatted cover letter text with proper paragraph line breaks...",
  "keyHighlights": [
    "Key strength 1 that addresses a job requirement",
    "Key strength 2 with relevant metric or technology",
    "Key strength 3 demonstrating cultural/domain fit"
  ]
}}
"""


def _build_user_prompt(req: CoverLetterRequest) -> str:
    p = req.talent_profile
    full_name = p.get("fullName") or p.get("name") or "Applicant"
    headline = p.get("headline") or p.get("title") or ""
    summary = p.get("summary") or p.get("about") or ""
    skills = p.get("skills") or []
    if isinstance(skills, list):
        skills_str = ", ".join(str(s) for s in skills)
    else:
        skills_str = str(skills)

    exp_lines = []
    for e in p.get("experience") or []:
        if isinstance(e, dict):
            comp = e.get("company", "")
            title = e.get("title", "")
            desc = e.get("description", "")
            exp_lines.append(f"- {title} at {comp}: {desc}")

    edu_lines = []
    for ed in p.get("education") or []:
        if isinstance(ed, dict):
            inst = ed.get("institution", "")
            deg = ed.get("degree", "")
            edu_lines.append(f"- {deg} ({inst})")

    notes_section = f"\nCustom candidate instructions:\n{req.custom_notes}" if req.custom_notes else ""

    return f"""Candidate Profile:
Name: {full_name}
Professional Title: {headline}
Summary: {summary}
Core Skills: {skills_str}
Work Experience:
{"\n".join(exp_lines) if exp_lines else "Not specified"}
Education:
{"\n".join(edu_lines) if edu_lines else "Not specified"}{notes_section}

Target Job Opportunity:
Role Title: {req.job_title}
Company: {req.company}
Job Description:
{req.job_description if req.job_description else "General requirements for this role."}

Generate the custom cover letter JSON now.
"""


def generate_cover_letter(request: CoverLetterRequest) -> CoverLetterResult:
    """Generate a tailored cover letter using the configured AI service router (Ollama -> Gemini fallback)."""
    system_prompt = _build_system_prompt(request.tone)
    user_prompt = _build_user_prompt(request)

    t0 = time.perf_counter()
    llm_resp = LlmRouter.execute_chat_json(
        service_name="cover_letter",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.7,
        max_tokens=2500,
        timeout_seconds=180,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    parsed = llm_resp.parsed_json or {}
    cover_letter = parsed.get("coverLetter") or llm_resp.content or ""
    key_highlights = parsed.get("keyHighlights") or []
    if not isinstance(key_highlights, list):
        key_highlights = [str(key_highlights)]

    full_name = request.talent_profile.get("fullName") or "Applicant"
    subject_line = parsed.get("subjectLine") or f"Application for {request.job_title} - {full_name}"

    return CoverLetterResult(
        cover_letter=cover_letter,
        key_highlights=key_highlights,
        subject_line=subject_line,
        provider_used=llm_resp.provider,
        model_used=llm_resp.model,
        latency_ms=latency_ms,
    )
