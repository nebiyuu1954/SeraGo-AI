"""Resume PDF -> structured TalentProfile fields.

Rule-based and deliberately conservative. EVERY field is optional: a field is
returned only when the resume text actually supports it, and anything the
parser is unsure about comes back as ``None`` so the user fills it in by hand.
Nothing is inferred or invented.

Output keys are aligned with the frontend's form shapes (`WorkExperienceEntry`
and `EducationEntry` in ProfileForm.tsx) so the values drop straight into the
form with no mapping layer::

    experience[] = {company, title, startDate, endDate, description}
    education[]  = {level, institution, degree, gpa, startYear, endYear}

Three shape rules exist because the form validates BEFORE it saves (the Yup
schemas in ProfileForm.tsx). Getting them wrong produces a form the user cannot
submit, which is worse than a missing field:

* ``experience[]`` rows are emitted only when all five fields are non-empty.
* ``startDate``/``endDate`` are ``YYYY-MM-DD`` — the form renders them with
  ``<input type="date">``. A current role therefore gets TODAY as its end date
  (a date input cannot represent "Present").
* ``education[]`` rows carry ``level`` from the form's own list
  (``HighSchool|Bachelors|Masters|PhD``) and 4-digit 1970-2099 years. Entries
  whose years cannot be read are skipped rather than emitted half-filled.

``about`` and work ``description`` are rendered by a rich-text editor, so they
are HTML-escaped and wrapped in ``<p>`` — the text comes from an untrusted PDF
and must never be able to inject markup into a profile recruiters will view.

Scope: text-based PDFs only. No OCR. An image-only (scanned) resume yields zero
characters and therefore all-null fields, which the caller reports to the user
as "we couldn't read your resume" rather than as an error.
"""
from __future__ import annotations

import html
import io
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from django.db import connections
from django.db.utils import OperationalError

logger = logging.getLogger(__name__)

#: Download cap, matching the .NET upload limit for resumes (10 MB).
MAX_BYTES = 10 * 1024 * 1024

#: Download timeout for the presigned URL.
DOWNLOAD_TIMEOUT = 30

#: Only the first N pages are read — a resume is never longer, and this bounds
#: the work on a pathological or malicious PDF.
MAX_PAGES = 10

MAX_SKILLS = 50
MAX_EXPERIENCE = 20
MAX_EDUCATION = 10


# ── Section detection ──────────────────────────────────────────────────

#: Section headers we can anchor to. Deliberately narrow: a false positive
#: cuts a section in half, so we match only the common spellings.
_SECTIONS = (
    ("about", re.compile(
        r"^(summary|professional summary|about(\s+me)?|profile|objective|"
        r"career objective|career summary|personal statement)\b", re.I)),
    ("skills", re.compile(
        r"^(skills|key skills|technical skills|skills\s*(&|and)\s*"
        r"technologies|technologies|core competencies|core skills|"
        r"competencies|tools(\s*(&|and)\s*technologies)?|expertise)\b", re.I)),
    ("experience", re.compile(
        r"^(experience|work experience|professional experience|employment|"
        r"employment history|work history|career history|relevant experience)\b",
        re.I)),
    ("education", re.compile(
        r"^(education|educational background|academic background|academics|"
        r"qualifications|education(\s*(&|and)\s*)training)\b", re.I)),
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_ALT = "|".join(_MONTHS)

#: One end of a date range: "2020", "Jan 2020", "01/2020", "Present".
_POINT = (
    rf"(?:{_MONTH_ALT})[a-z]*\.?\s+\d{{4}}"   # Jan 2020
    rf"|\d{{1,2}}/\d{{4}}"                    # 01/2020
    rf"|\d{{4}}"                              # 2020
)

_DATE_RANGE = re.compile(
    rf"(?P<start>{_POINT})\s*(?:-|–|—|to|until)\s*(?P<end>{_POINT}|present|"
    rf"current|now|to\s+date|today)",
    re.I,
)

_PRESENT = re.compile(r"^(present|current|now|to\s+date|today)$", re.I)

#: Leading bullet characters stripped from list lines.
_BULLET = re.compile(r"^\s*[•·▪◦*\-–—]\s*")

#: Lines that are contact details, not content.
_CONTACT = re.compile(
    r"(@|https?://|www\.|linkedin\.com|github\.com|\+?\d[\d\s().\-]{6,})", re.I)

_SKILL_SPLIT = re.compile(r"[,;|•·\u2022\t]|\s{2,}")

#: Words that hint a header token is the employer rather than the job title.
_COMPANY_HINTS = re.compile(
    r"\b(ltd|limited|inc|llc|plc|corp|corporation|company|co|group|holdings|"
    r"technolog(?:y|ies)|solutions|systems|software|labs?|bank|insurance|"
    r"university|college|hospital|ngo|foundation|agency|consulting|partners|"
    r"studio|media|networks?|academy|institute|enterprises?|trading|"
    r"industries|pharma|hotel|airlines?|microfinance|telecom|ministry|"
    r"bureau|authority|school|cent(?:er|re))\b", re.I)

_HEADER_SPLIT = re.compile(r"\s+(?:at|@)\s+|\s*[|•·]\s*|\s+[—–]\s+|\s+-\s+|\s*,\s*")


# ── Text helpers ───────────────────────────────────────────────────────

def _to_html(text: str) -> str:
    """Escape untrusted text and wrap it in paragraphs for the rich editor."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    if not paragraphs:
        return ""
    return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; treat blank as missing."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _parse_point(token: str | None) -> tuple[int, int, int] | None:
    """Parse one end of a date range into (year, month, day)."""
    if not token:
        return None
    token = token.strip().lower()

    if _PRESENT.match(token):
        today = date.today()
        return (today.year, today.month, today.day)

    # "Jan 2020" / "January 2020"
    m = re.match(rf"({_MONTH_ALT})[a-z]*\.?\s+(\d{{4}})", token)
    if m:
        return (int(m.group(2)), _MONTHS[m.group(1)], 1)

    # "01/2020"
    m = re.match(r"(\d{1,2})/(\d{4})", token)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            return (int(m.group(2)), month, 1)

    # "2020"
    m = re.search(r"(\d{4})", token)
    if m:
        year = int(m.group(1))
        if 1970 <= year <= 2099:
            return (year, 1, 1)

    return None


def _iso(point: tuple[int, int, int] | None) -> str:
    """(year, month, day) -> 'YYYY-MM-DD' (empty when unparsed)."""
    if point is None:
        return ""
    year, month, day = point
    return f"{year:04d}-{month:02d}-{day:02d}"


def _months_between(start: tuple[int, int, int], end: tuple[int, int, int]) -> int:
    span = (end[0] * 12 + end[1]) - (start[0] * 12 + start[1])
    return max(0, span)


def _is_section_header(line: str) -> tuple[str, bool]:
    """Return (section_key, is_header) for one line."""
    candidate = line.strip().rstrip(":").strip()
    if not candidate or len(candidate) > 60:
        return ("", False)
    for key, pattern in _SECTIONS:
        if pattern.match(candidate):
            return (key, True)
    return ("", False)


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Bucket lines by section. Text before the first header is 'header'."""
    sections: dict[str, list[str]] = {"headline": []}
    current = "headline"
    for line in lines:
        key, is_header = _is_section_header(line)
        if is_header:
            current = key
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


# ── Field rules ────────────────────────────────────────────────────────

def _extract_headline(headline_lines: list[str]) -> str | None:
    """First non-contact line after the name block.

    Skips the name itself (usually the first line) only when a second line is
    available to use instead, and never returns a contact line.
    """
    candidates = [ln.strip() for ln in headline_lines if ln.strip()]
    non_contact = [ln for ln in candidates if not _CONTACT.search(ln)]
    if len(non_contact) >= 2:
        return _clean(non_contact[1])
    if len(non_contact) == 1:
        return _clean(non_contact[0])
    return None


def _extract_about(body: list[str]) -> str | None:
    text = "\n".join(ln.strip() for ln in body if ln.strip())
    text = _clean_block(text)
    if not text:
        return None
    return _to_html(text) or None


def _clean_block(text: str) -> str:
    """Strip bullet markers and blank lines, then re-join a section's lines."""
    kept = []
    for raw in text.split("\n"):
        line = _BULLET.sub("", raw).strip()
        if not line:
            continue
        kept.append(line)
    return "\n".join(kept)


def _extract_skills(body: list[str]) -> list[str]:
    text = " ".join(ln.strip() for ln in body if ln.strip())
    text = _BULLET.sub("", text)
    found: list[str] = []
    seen: set[str] = set()
    for chunk in _SKILL_SPLIT.split(text):
        skill = " ".join(chunk.split()).strip(" .:;")
        if not skill or len(skill) > 40 or len(skill) < 2:
            continue
        lowered = skill.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        found.append(skill)
        if len(found) >= MAX_SKILLS:
            break
    return found


def _split_role_header(header: str) -> tuple[str | None, str | None]:
    """Split a 'Title — Company' style line into (title, company)."""
    tokens = [t.strip(" .:;-–—") for t in _HEADER_SPLIT.split(header) if t.strip(" .:;-–—")]
    if not tokens:
        return (None, None)
    if len(tokens) == 1:
        return (_clean(tokens[0]), None)

    first, second = tokens[0], tokens[1]
    first_is_company = bool(_COMPANY_HINTS.search(first))
    second_is_company = bool(_COMPANY_HINTS.search(second))
    if first_is_company and not second_is_company:
        return (_clean(second), _clean(first))
    if second_is_company and not first_is_company:
        return (_clean(first), _clean(second))
    # No hint either way: resumes overwhelmingly lead with the job title.
    return (_clean(first), _clean(second))


def _extract_experience(body: list[str]) -> tuple[list[dict], int]:
    """Parse role blocks. Returns (rows, skipped_count).

    Every emitted row has all five fields non-empty so it satisfies the form's
    Yup schema; anything incomplete is dropped and counted instead.
    """
    lines = [ln.strip() for ln in body]
    if not lines:
        return ([], 0)

    # A role starts on a line containing a date range. When the range is alone
    # on its line (very common), the header is the preceding line.
    anchors: list[tuple[int, str, int]] = []  # (header_index, header_text, line_index)
    for idx, line in enumerate(lines):
        m = _DATE_RANGE.search(line)
        if not m:
            continue
        before = (line[:m.start()] + line[m.end():]).strip(" .:;,|—-–")
        if len(before) >= 2:
            anchors.append((idx, before, idx))
        elif idx > 0 and lines[idx - 1]:
            anchors.append((idx - 1, lines[idx - 1], idx))

    if not anchors:
        return ([], 0)

    rows: list[dict] = []
    skipped = 0
    today = date.today()

    for position, (header_idx, header_text, date_idx) in enumerate(anchors):
        range_match = _DATE_RANGE.search(lines[date_idx])
        if not range_match:
            skipped += 1
            continue

        start = _parse_point(range_match.group("start"))
        end_raw = range_match.group("end")
        end = _parse_point(end_raw)
        if start is None or end is None:
            skipped += 1
            continue
        if _PRESENT.match(end_raw.strip().lower()):
            end = (today.year, today.month, today.day)
        if end < start:
            end = start

        title, company = _split_role_header(header_text)
        if not title or not company:
            skipped += 1
            continue

        # Body runs to the next role's header, or the end of the section.
        next_header = anchors[position + 1][0] if position + 1 < len(anchors) else len(lines)
        blocks = []
        for line in lines[date_idx + 1:next_header]:
            if not line or _is_section_header(line)[1]:
                continue
            blocks.append(_BULLET.sub("", line).strip())
        # Fall back to the (real, extracted) header line so `description` is
        # never empty — the form requires it, and inventing text is not an option.
        description = _to_html("\n".join(b for b in blocks if b)) or _to_html(header_text)
        if not description:
            skipped += 1
            continue

        rows.append({
            "company": company,
            "title": title,
            "startDate": _iso(start),
            "endDate": _iso(end),
            "description": description,
        })
        if len(rows) >= MAX_EXPERIENCE:
            break

    return (rows, skipped)


def _education_level_from_degree(degree: str) -> str | None:
    """Map a degree string onto the form's EDUCATION_LEVELS list."""
    text = degree.lower()
    if re.search(r"\b(ph\.?d|doctorate|doctoral|d\.?phil)\b", text):
        return "PhD"
    if re.search(r"\b(master|masters|msc|m\.?sc|mba|m\.?a|mph|llm|meng|m\.?eng|"
                 r"postgraduate)\b", text):
        return "Masters"
    if re.search(r"\b(bachelor|bachelors|bsc|b\.?sc|ba|b\.?a|beng|b\.?eng|llb|"
                 r"btech|b\.?tech|undergraduate)\b", text):
        return "Bachelors"
    # The form has no Diploma band; a diploma sits closest to HighSchool.
    if re.search(r"\b(diploma|high school|secondary|hsc|grade 12|tvte)\b", text):
        return "HighSchool"
    return None


def _extract_education(body: list[str]) -> tuple[list[dict], int]:
    """Parse education entries. Returns (rows, skipped_count)."""
    lines = [ln.strip() for ln in body if ln.strip()]
    if not lines:
        return ([], 0)

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        # A new institution/degree line right after a year line starts a block.
        if current and re.search(r"\b(19|20)\d{2}\b", current[-1]) and not re.search(
                r"\b(19|20)\d{2}\b", line) and re.search(r"[A-Za-z]{3}", line):
            if _education_level_from_degree(line) or len(line) < 60:
                blocks.append(current)
                current = [line]
                continue
        current.append(line)
    if current:
        blocks.append(current)
    # Single-block section: fall back to one entry per year-bearing line.
    if len(blocks) == 1 and sum(bool(re.search(r"\b(19|20)\d{2}\b", b)) for b in blocks[0]) > 1:
        blocks = [[b] for b in blocks[0]]

    rows: list[dict] = []
    skipped = 0
    for block in blocks:
        text = " ".join(block)
        years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
        years = [y for y in years if 1970 <= y <= 2099]
        if not years:
            skipped += 1
            continue
        start_year, end_year = min(years), max(years)

        level = _education_level_from_degree(text)
        if level is None:
            skipped += 1
            continue

        # Institution = the longest alphabetical line in the block; degree =
        # the line that carried the level keyword (falling back to the header).
        institution = None
        degree = None
        for line in block:
            if _education_level_from_degree(line):
                degree = degree or line
            elif re.search(r"[A-Za-z]{4}", line) and not re.search(r"\b(19|20)\d{2}\b", line):
                if institution is None or len(line) > len(institution):
                    institution = line
        degree = degree or block[0]
        institution = institution or (degree if degree != block[0] else None)
        if not institution or not degree:
            skipped += 1
            continue

        rows.append({
            "level": level,
            "institution": _clean(institution) or "",
            "degree": _clean(degree) or "",
            "gpa": "",
            "startYear": f"{start_year:04d}",
            "endYear": f"{end_year:04d}",
        })
        if len(rows) >= MAX_EDUCATION:
            break

    return (rows, skipped)


def _experience_level_from_years(years: int | None) -> str | None:
    """Bucket total years into the TalentProfile ExperienceLevel enum."""
    if years is None:
        return None
    if years < 1:
        return "Entry"
    if years < 2:
        return "Junior"
    if years < 5:
        return "Mid"
    if years < 8:
        return "Senior"
    return "Lead"


# ── Industry (best effort against the shared Sectors table) ────────────

def _load_sector_names() -> list[str]:
    """Active sector names from the .NET DB's "Sectors" table.

    Read through the read-only "sectors" alias, falling back to "default"
    (same pattern as ai_service.services.classifier). Returns [] when the table
    cannot be read — industry is best-effort and must never fail a parse.
    """
    alias = "sectors" if "sectors" in connections else "default"
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute(
                'SELECT "Name" FROM "Sectors" WHERE "IsActive" = TRUE ORDER BY "Name"'
            )
            return [str(row[0]) for row in cursor.fetchall()]
    except (OperationalError, Exception) as exc:  # noqa: BLE001 - best effort
        logger.warning("Could not load sectors for industry matching: %s", exc)
        return []


def _extract_industry(text: str, sectors: list[str]) -> str | None:
    """Match the resume text against sector names by shared significant words."""
    if not text or not sectors:
        return None
    words = {
        w for w in re.findall(r"[a-z]{4,}", text.lower())
        if w not in _STOPWORDS
    }
    if not words:
        return None

    best_name, best_score, required = None, 0, 1
    for name in sectors:
        name_words = {
            w for w in re.findall(r"[a-z]{4,}", name.lower())
            if w not in _STOPWORDS
        }
        if not name_words:
            continue
        score = len(words & name_words)
        if score > best_score:
            best_name, best_score = name, score
            # A one-word sector name ("Healthcare") matches on that word, but a
            # multi-word one needs at least two, or a single generic word would
            # decide the category on its own. A wrong industry is worse than
            # null — the user fills in what the resume doesn't support.
            required = 1 if len(name_words) == 1 else 2
    if best_name and best_score >= required:
        return best_name
    return None


_STOPWORDS = {
    "with", "from", "that", "this", "have", "will", "your", "their", "them",
    "they", "work", "working", "experience", "years", "year", "using", "used",
    "team", "teams", "also", "more", "most", "other", "into", "over", "such",
    "skills", "ability", "including", "include", "responsible", "responsibilities",
    "management", "development", "developed", "various", "across", "within",
    "general", "services", "service", "business", "company", "level", "strong",
}


# ── PDF extraction ─────────────────────────────────────────────────────

def fetch_pdf(url: str, timeout: int = DOWNLOAD_TIMEOUT, max_bytes: int = MAX_BYTES) -> bytes:
    """Download a PDF from a (presigned) URL with a hard size cap."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "SeraGo-AI/1.0 (+resume-parser)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(f"PDF is larger than the {max_bytes // (1024 * 1024)}MB limit")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"PDF is larger than the {max_bytes // (1024 * 1024)}MB limit")
        return data


def extract_text(pdf_bytes: bytes, max_pages: int = MAX_PAGES) -> tuple[str, int]:
    """Extract text page by page. Returns (text, pages_read)."""
    import pdfplumber  # lazy: keeps the app importable without the dependency

    chunks: list[str] = []
    pages_read = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:max_pages]:
            pages_read += 1
            try:
                chunks.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001 - one bad page must not kill the parse
                logger.warning("Could not read page %d of resume: %s", pages_read, exc)
    return ("\n".join(chunks), pages_read)


def extract_fields(text: str, sectors: list[str] | None = None) -> dict:
    """Run the field rules over extracted text. Pure function (testable)."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    sections = _split_sections(lines)

    experience, skipped_experience = _extract_experience(sections.get("experience", []))
    education, skipped_education = _extract_education(sections.get("education", []))

    headline = _extract_headline(sections.get("headline", []))
    about = _extract_about(sections.get("about", []))
    skills = _extract_skills(sections.get("skills", []))

    # Total years = sum of the parsed role spans (overlaps are not merged —
    # a slight over-count is preferable to inventing a number).
    total_months = 0
    for row in experience:
        start = _parse_point(row["startDate"][:7].replace("-", "/")) or _parse_point(row["startDate"])
        end = _parse_point(row["endDate"][:7].replace("-", "/")) or _parse_point(row["endDate"])
        if start and end:
            total_months += _months_between(start, end)
    years = (total_months // 12) if experience else None

    current_profession = _clean(experience[0]["title"]) if experience else None

    industry_source = " ".join(filter(None, [
        headline, current_profession, " ".join(skills),
        " ".join(row["title"] for row in experience),
        " ".join(row["company"] for row in experience),
    ]))
    current_industry = _extract_industry(industry_source, sectors or [])

    return {
        "headline": headline,
        "about": about,
        "skills": skills,
        "experience": experience,
        "education": education,
        "currentProfession": current_profession,
        "currentIndustry": current_industry,
        "experienceLevel": _experience_level_from_years(years),
        "yearsOfExperience": years,
        "skippedExperience": skipped_experience,
        "skippedEducation": skipped_education,
    }


def _fields_found(profile: dict) -> int:
    return sum(
        1 for key in (
            "headline", "about", "skills", "experience", "education",
            "currentProfession", "currentIndustry", "experienceLevel",
        )
        if profile.get(key)
    )


def parse_resume_url(url: str) -> dict:
    """Download, read and parse a resume PDF.

    Always returns a JSON-safe dict. A read failure is reported with
    ``success: True`` and zero characters — the caller shows the user a plain
    "we couldn't read your resume" message, not an error state.
    """
    started = time.monotonic()
    empty_profile = {
        "headline": None, "about": None, "skills": [], "experience": [],
        "education": [], "currentProfession": None, "currentIndustry": None,
        "experienceLevel": None, "yearsOfExperience": None,
        "skippedExperience": 0, "skippedEducation": 0,
    }

    try:
        pdf_bytes = fetch_pdf(url)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
        logger.warning("Resume download failed: %s", exc)
        return {
            "success": True, "charsExtracted": 0, "pagesRead": 0,
            "fieldsFound": 0, "profile": empty_profile, "reason": "download_failed",
        }

    try:
        text, pages_read = extract_text(pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - unreadable/encrypted/scanned PDFs
        logger.warning("Resume text extraction failed: %s", exc)
        return {
            "success": True, "charsExtracted": 0, "pagesRead": 0,
            "fieldsFound": 0, "profile": empty_profile, "reason": "extract_failed",
        }

    chars = len(text.strip())
    if chars == 0:
        # Image-only / scanned PDF. Expected, not an error.
        logger.info("Resume yielded no text (scanned or image-only PDF)")
        return {
            "success": True, "charsExtracted": 0, "pagesRead": pages_read,
            "fieldsFound": 0, "profile": empty_profile, "reason": "no_text",
        }

    sectors = _load_sector_names()
    profile = extract_fields(text, sectors)
    found = _fields_found(profile)

    logger.info(
        "Resume parsed: pages=%d chars=%d fields_found=%d "
        "(experience=%d skipped=%d, education=%d skipped=%d, skills=%d, "
        "level=%s, industry=%s) in %dms",
        pages_read, chars, found,
        len(profile["experience"]), profile["skippedExperience"],
        len(profile["education"]), profile["skippedEducation"],
        len(profile["skills"]), profile["experienceLevel"],
        profile["currentIndustry"],
        int((time.monotonic() - started) * 1000),
    )

    return {
        "success": True,
        "charsExtracted": chars,
        "pagesRead": pages_read,
        "fieldsFound": found,
        "profile": profile,
        "reason": None if found else "no_fields",
    }
