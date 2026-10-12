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
    ("projects", re.compile(
        r"^(personal\s+|deployed\s+|academic\s+|key\s+|side\s+|selected\s+)*projects\b", re.I)),
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

#: Locations that should never be mistaken for a job title or profession.
_LOCATION_HINTS = re.compile(
    r"\b(addis\s*abeba|addis\s*ababa|ethiopia|kenya|nairobi|remote|onsite|hybrid|"
    r"hawassa|bahir\s*dar|dire\s*dawa|mekelle|adama|bishoftu|gondar|jimma|"
    r"africa|usa|uk|united\s+states)\b",
    re.I,
)


def _is_location(text: str | None) -> bool:
    """True if text is predominantly a location (e.g. 'Addis Abeba,Ethiopia')."""
    if not text:
        return False
    cleaned = text.strip().lower()
    has_location = bool(_LOCATION_HINTS.search(cleaned))
    if not has_location:
        return False
    has_role_words = bool(re.search(
        r"\b(engineer|developer|manager|lead|director|specialist|officer|analyst|"
        r"designer|assistant|consultant|coordinator|intern|head|architect|technician|"
        r"administrator|accountant|writer|editor|trainer|teacher)\b",
        cleaned,
    ))
    return has_location and not has_role_words


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


def _split_role_header(header: str, headline_fallback: str | None = None) -> tuple[str | None, str | None]:
    """Split a 'Title — Company' style line into (title, company)."""
    raw_tokens = [t.strip(" .:;-–—") for t in _HEADER_SPLIT.split(header) if t.strip(" .:;-–—")]
    if not raw_tokens:
        return (None, None)

    # Separate location tokens so they are never assigned as title or company
    tokens = [t for t in raw_tokens if not _is_location(t)]
    if not tokens:
        tokens = raw_tokens

    if len(tokens) == 1:
        single = tokens[0]
        if _COMPANY_HINTS.search(single):
            return (_clean(headline_fallback), _clean(single))
        return (_clean(single), None)

    first, second = tokens[0], tokens[1]
    first_is_company = bool(_COMPANY_HINTS.search(first))
    second_is_company = bool(_COMPANY_HINTS.search(second))
    if first_is_company and not second_is_company:
        title = _clean(second)
        if _is_location(title) and headline_fallback:
            title = _clean(headline_fallback)
        return (title, _clean(first))
    if second_is_company and not first_is_company:
        title = _clean(first)
        if _is_location(title) and headline_fallback:
            title = _clean(headline_fallback)
        return (title, _clean(second))
    # No hint either way: resumes overwhelmingly lead with the job title.
    title = _clean(first)
    if _is_location(title) and headline_fallback:
        title = _clean(headline_fallback)
    return (title, _clean(second))


def _extract_experience(body: list[str], headline_fallback: str | None = None) -> tuple[list[dict], int]:
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

        title, company = _split_role_header(header_text, headline_fallback=headline_fallback)
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

        # A new education block should only start if:
        # 1. line has an education level keyword (BSc, MSc, etc.) AND current already has a line with a level keyword.
        # 2. current already has both a level keyword and an institution, and line has a new date range or header.
        if current:
            current_has_level = any(_education_level_from_degree(l) for l in current)
            line_has_level = bool(_education_level_from_degree(line))
            current_has_years = any(re.search(r"\b(19|20)\d{2}\b", l) for l in current)
            line_has_years = bool(re.search(r"\b(19|20)\d{2}\b", line))

            if line_has_level and current_has_level:
                blocks.append(current)
                current = [line]
                continue
            if current_has_level and current_has_years and line_has_years and not line_has_level:
                if len(current) >= 2:
                    blocks.append(current)
                    current = [line]
                    continue

        current.append(line)
    if current:
        blocks.append(current)

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

        # Fallback for single-line education entry e.g. "BSc in Computer Science, Addis Ababa University 2021-2025"
        if not institution and degree and len(block) == 1:
            parts = [p.strip() for p in re.split(r",|\s+at\s+|\|", block[0]) if p.strip()]
            for p in parts:
                if _education_level_from_degree(p):
                    degree = p
                elif re.search(r"[A-Za-z]{3}", p) and not re.search(r"\b(19|20)\d{2}\b", p):
                    institution = p

        if not institution or not degree:
            skipped += 1
            continue

        # Clean trailing dates, locations, and separator pipes from degree
        cleaned_degree = re.sub(r"\s*[|•·]\s*.*$", "", degree)
        cleaned_degree = re.sub(r"\s*[-–—]?\s*\b(19|20)\d{2}\b.*$", "", cleaned_degree).strip()
        if cleaned_degree:
            degree = cleaned_degree

        # Clean trailing locations, dates, and separator pipes from institution
        cleaned_inst = re.sub(r"\s*[|•·]\s*.*$", "", institution)
        cleaned_inst = re.sub(r"\s*[-–—]?\s*\b(19|20)\d{2}\b.*$", "", cleaned_inst).strip()
        if cleaned_inst:
            institution = cleaned_inst

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

CANONICAL_SECTORS: list[str] = [
    "Accounting & Finance",
    "Agriculture & Natural Science",
    "Banking & Insurance",
    "Beauty & Grooming",
    "Business & Administration",
    "Customer Service & Support",
    "Design & Creative",
    "Education & Training",
    "Engineering & Construction",
    "Healthcare",
    "Hospitality & Tourism",
    "Human Resources",
    "Legal",
    "Logistics & Transportation",
    "Manufacturing & Production",
    "Media & Communication",
    "Procurement & Supply Chain",
    "Sales & Marketing",
    "Security & Protection",
    "Skilled & General Labor",
    "Social Science & Community",
    "Technology & IT",
]


def _load_sector_names() -> list[str]:
    """Active sector names from the .NET DB's "Sectors" table.

    Read through the read-only "sectors" alias, falling back to "default"
    (same pattern as ai_service.services.classifier). Returns CANONICAL_SECTORS when
    the table cannot be read or is empty.
    """
    alias = "sectors" if "sectors" in connections else "default"
    try:
        with connections[alias].cursor() as cursor:
            cursor.execute(
                'SELECT "Name" FROM "Sectors" WHERE "IsActive" = TRUE ORDER BY "Name"'
            )
            rows = [str(row[0]) for row in cursor.fetchall()]
            if rows:
                return rows
    except (OperationalError, Exception) as exc:  # noqa: BLE001 - best effort
        logger.warning("Could not load sectors for industry matching: %s", exc)
    return list(CANONICAL_SECTORS)


SECTOR_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "Technology & IT": [
        "software", "developer", "programmer", "programming", "web", "frontend", "front-end",
        "backend", "back-end", "fullstack", "full-stack", "full stack", "engineer", "engineering",
        "computer", "computing", "code", "coding", "it", "ict", "devops", "cloud", "database",
        "sql", "python", "javascript", "typescript", "react", "angular", "vue", "node", "django",
        "dotnet", ".net", "net 8", "c#", "java", "c++", "golang", "php", "html", "css", "api",
        "rest", "restful", "docker", "kubernetes", "aws", "azure", "git", "linux", "cybersecurity",
        "network", "sysadmin", "machine learning", "artificial intelligence", "data science",
        "hilcoe", "microfrontends", "microfrontend", "microservices", "system architect", "tech",
        "iis", "nginx", "webpack", "monorepo", "mediatr", "dbeaver", "swagger", "postgresql",
        "postman", "orm", "entity framework", "jwt", "agile", "scrum", "qa automation", "sdk",
    ],
    "Accounting & Finance": [
        "accountant", "accounting", "auditor", "audit", "tax", "taxation", "financial", "finance",
        "bookkeeper", "bookkeeping", "payroll", "ledger", "cpa", "acca", "ifrs", "gaap", "treasury",
        "financial analyst", "budget", "cost control", "reconciliation", "accounts payable", "accounts receivable",
    ],
    "Banking & Insurance": [
        "bank", "banking", "credit", "loan", "mortgage", "underwriter", "underwriting", "insurance",
        "actuary", "actuarial", "claims", "teller", "branch manager", "microfinance", "interest rate",
        "forex", "risk management", "credit analysis", "liquidity",
    ],
    "Healthcare": [
        "doctor", "physician", "nurse", "nursing", "medical", "medicine", "hospital", "clinic",
        "clinical", "patient", "pharmacy", "pharmacist", "health", "healthcare", "pediatric",
        "surgeon", "surgery", "laboratory", "radiology", "dental", "dentist", "therapy", "therapist",
        "midwife", "pathology", "optometry", "public health", "epidemiology",
    ],
    "Engineering & Construction": [
        "civil engineer", "mechanical engineer", "electrical engineer", "structural engineer",
        "construction", "contractor", "site engineer", "architect", "architecture", "surveyor",
        "autocad", "building", "hvac", "plumbing", "quantity surveyor", "draftsman", "structural design",
        "mep", "concrete", "heavy machinery",
    ],
    "Education & Training": [
        "teacher", "teaching", "lecturer", "professor", "instructor", "pedagogy", "curriculum",
        "school teacher", "tutor", "tutoring", "education", "trainer", "academic", "principal",
        "kindergarten", "classroom", "lesson plan", "student assessment", "faculty",
    ],
    "Legal": [
        "lawyer", "attorney", "legal", "paralegal", "advocate", "prosecutor", "judge", "litigation",
        "solicitor", "juris", "law", "counsel", "compliance officer", "contract law", "court",
        "arbitration", "intellectual property", "notary",
    ],
    "Human Resources": [
        "human resources", "hr", "recruiter", "recruitment", "talent acquisition", "onboarding",
        "employee relations", "people operations", "hr manager", "personnel", "talent management",
        "performance appraisal", "staffing", "labor law",
    ],
    "Sales & Marketing": [
        "sales", "marketing", "advertisement", "advertising", "digital marketing", "seo", "sem",
        "social media marketing", "brand", "branding", "sales representative", "account executive",
        "business development", "telemarketing", "b2b", "b2c", "merchandising", "promoter",
        "lead generation", "market research", "campaign",
    ],
    "Design & Creative": [
        "graphic designer", "ui designer", "ux designer", "ui/ux", "creative director", "animator",
        "animation", "video editor", "illustrator", "photographer", "fashion designer", "art director",
        "visual designer", "motion graphics", "figma", "photoshop", "after effects", "typography",
    ],
    "Logistics & Transportation": [
        "logistics", "transport", "transportation", "fleet", "driver", "warehouse", "warehousing",
        "freight", "cargo", "shipping", "distribution", "customs clearing", "dispatch", "import export",
        "supply chain coordinator", "transit", "freight forwarding",
    ],
    "Procurement & Supply Chain": [
        "procurement", "purchasing", "supply chain", "sourcing", "vendor management", "inventory control",
        "buyer", "purchase order", "tender", "supplier evaluation", "material management",
    ],
    "Customer Service & Support": [
        "customer service", "call center", "customer support", "help desk", "client service",
        "customer care", "receptionist", "front desk", "guest service", "inbound", "outbound", "ticketing",
    ],
    "Business & Administration": [
        "administration", "administrative", "executive assistant", "office manager", "operations manager",
        "general manager", "management", "secretary", "clerk", "business analyst", "project management",
        "office administration", "records management", "data entry",
    ],
    "Media & Communication": [
        "journalist", "journalism", "reporter", "news", "editor", "editorial", "broadcasting",
        "media", "communications", "public relations", "pr specialist", "content creator", "copywriter",
        "broadcaster", "producer", "press release",
    ],
    "Hospitality & Tourism": [
        "hotel", "hospitality", "restaurant", "waiter", "waitress", "chef", "cook", "culinary",
        "bartender", "catering", "tourism", "tour guide", "travel agent", "housekeeping", "concierge",
        "resort", "food and beverage", "guest relations",
    ],
    "Manufacturing & Production": [
        "manufacturing", "factory", "production line", "assembly line", "plant operator",
        "quality control", "qa/qc", "industrial engineering", "fabrication", "machinist", "cnc",
        "process engineer", "maintenance engineer",
    ],
    "Agriculture & Natural Science": [
        "agriculture", "agronomist", "agronomy", "farming", "farmer", "crop", "livestock",
        "veterinary", "veterinarian", "botany", "forestry", "horticulture", "soil science",
        "biology", "chemist", "chemistry", "environmental science", "irrigation", "agribusiness",
    ],
    "Security & Protection": [
        "security guard", "surveillance", "safety officer", "patrol", "loss prevention",
        "security officer", "cctv operator", "bodyguard", "access control", "physical security", "fire safety",
    ],
    "Beauty & Grooming": [
        "barber", "hair stylist", "hairdresser", "salon", "spa", "makeup artist", "esthetician",
        "cosmetology", "beautician", "manicurist", "skincare", "massage therapist",
    ],
    "Skilled & General Labor": [
        "carpenter", "plumber", "electrician", "welder", "mason", "laborer", "cleaner", "janitor",
        "maintenance technician", "mechanic", "painter", "handyman", "facility maintenance",
    ],
    "Social Science & Community": [
        "social work", "social worker", "community development", "sociology", "humanitarian",
        "counselor", "counseling", "psychology", "psychologist", "anthropology", "community outreach",
        "human rights", "child protection", "social welfare", "civil society", "ngo worker", "community mobilization",
    ],
}


def _extract_suggested_sectors(text: str, sectors: list[str], max_count: int = 1) -> list[str]:
    """Score sector names against resume text using domain keywords first, then fallback to word matches."""
    if not text or not sectors:
        return []
    lower_text = text.lower()
    scored: list[tuple[str, int]] = []

    # 1. High-precision domain ontology keywords
    for name in sectors:
        keywords = SECTOR_DOMAIN_KEYWORDS.get(name) or []
        domain_score = 0
        for kw in keywords:
            if " " in kw or "." in kw or "#" in kw or "+" in kw or "-" in kw:
                if kw in lower_text:
                    domain_score += 4 if " " in kw else 3
            else:
                if re.search(rf"\b{re.escape(kw)}\b", lower_text):
                    domain_score += 2
        if domain_score > 0:
            scored.append((name, domain_score))

    if scored:
        scored.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in scored[:max_count]]

    # 2. Fallback to naive word overlap
    words = {
        w for w in re.findall(r"[a-z]{4,}", lower_text)
        if w not in _STOPWORDS
    }
    if not words:
        return []

    for name in sectors:
        name_words = {
            w for w in re.findall(r"[a-z]{4,}", name.lower())
            if w not in _STOPWORDS
        }
        if not name_words:
            continue
        score = len(words & name_words)
        required = 1 if len(name_words) == 1 else 2
        if score >= required:
            scored.append((name, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in scored[:max_count]]


def _extract_industry(text: str, sectors: list[str]) -> str | None:
    """Match the resume text against sector names by domain ontology or shared words."""
    suggested = _extract_suggested_sectors(text, sectors, max_count=1)
    return suggested[0] if suggested else None


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

    headline = _extract_headline(sections.get("headline", []))
    about = _extract_about(sections.get("about", []))
    skills = _extract_skills(sections.get("skills", []))

    experience, skipped_experience = _extract_experience(
        sections.get("experience", []), headline_fallback=headline
    )
    education, skipped_education = _extract_education(sections.get("education", []))

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
    if (not current_profession or _is_location(current_profession)) and headline:
        current_profession = headline

    for row in experience:
        if _is_location(row.get("title")) and headline:
            row["title"] = headline

    industry_source = " ".join(filter(None, [
        headline, current_profession, " ".join(skills),
        " ".join(row["title"] for row in experience),
        " ".join(row["company"] for row in experience),
    ]))
    all_sectors = sectors or CANONICAL_SECTORS
    suggested_sectors = _extract_suggested_sectors(industry_source, all_sectors, max_count=1)
    current_industry = suggested_sectors[0] if suggested_sectors else None

    return {
        "headline": headline,
        "about": about,
        "skills": skills,
        "experience": experience,
        "education": education,
        "currentProfession": current_profession,
        "currentIndustry": current_industry,
        "suggestedSectors": suggested_sectors[:1],
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


def _extract_fields_ai(text: str, sectors: list[str]) -> dict | None:
    """Extract fields using the AI client."""
    from ai_service.ai_client import AiClient

    schema = """{
  "headline": "string or null",
  "about": "string (html <p> tags, merge broken lines) or null",
  "skills": ["string (clean single technologies, split items like 'HTML/CSS' into two strings)"],
  "experience": [
    {
      "company": "string",
      "title": "string (job role only, NEVER a city/location like 'Addis Abeba')",
      "startDate": "YYYY-MM-DD",
      "endDate": "YYYY-MM-DD (use today's date for 'Present')",
      "description": "string (html <p> tags, merge sentences that wrap across lines)"
    }
  ],
  "education": [
    {
      "level": "HighSchool|Certificate|Bachelors|Masters|PhD",
      "institution": "string",
      "degree": "string",
      "gpa": "string or null",
      "startYear": "YYYY",
      "endYear": "YYYY"
    }
  ],
  "currentProfession": "string (job role only, NEVER a city/location) or null",
  "currentIndustry": "string (must be from the sector list) or null",
  "suggestedSectors": ["string (choose ONLY the 1 single highest/most relevant sector from the sector list for their For You job feed)"],
  "experienceLevel": "Entry|Junior|Mid|Senior|Lead or null (STRICTLY based on total professional work experience years)",
  "yearsOfExperience": "integer or null (total sum of professional work experience years ONLY. NEVER count university/education years)"
}"""

    system_prompt = f"""You are an expert resume parsing AI. Extract the candidate's details from the provided resume text.
Return ONLY a valid JSON object matching this exact schema and follow the inline instructions:
{schema}

- Sector list for currentIndustry and suggestedSectors: {", ".join(sectors)}
- Pick ONLY the 1 single highest/most relevant sector from the sector list for suggestedSectors (to populate their 'For You' job feed).
- CRITICAL for title and currentProfession: NEVER use city names, locations, or countries (e.g. "Addis Abeba", "Ethiopia", "Nairobi", "Remote") as a job title or currentProfession. If the role header has "Company | City", the title is the candidate's professional headline or role (e.g. "Full Stack Web Developer"), NOT the city.
- CRITICAL for suggestedSectors and currentIndustry: Select the single most relevant sector based on the candidate's core technical skills, education, and profession (e.g., software engineering, web development, programming, IT belongs to "Technology & IT"). Do NOT choose "Social Science & Community" just because a company name contains "Social" or a university degree contains "Science".
- CRITICAL for yearsOfExperience and experienceLevel: Count ONLY real professional WORK EXPERIENCE (sum of employment roles in the experience section). NEVER count university degrees, college, or education study periods as years of work experience! For example, if someone studied at university from 2021-2025 and worked from 2025-2026, their work experience is 1 year (Junior), NOT 5 or 6 years (Senior).
- If a field is missing, use null or empty array.
- Education entries only require institution and year; set others to null if missing.

EXAMPLE INPUT:
John Doe
Software Engineer
About: I love coding.
Skills: Python, HTML/CSS
Experience:
Apple Inc
01/2022 - Present
- Wrote code
Education:
MIT 
BSc Computer Science 2016-2020
Coursera 
Frontend Certificate 2021

EXAMPLE OUTPUT:
{{
  "headline": "Software Engineer",
  "about": "<p>I love coding.</p>",
  "skills": ["Python", "HTML", "CSS"],
  "experience": [
    {{
      "company": "Apple Inc",
      "title": "Software Engineer",
      "startDate": "2022-01-01",
      "endDate": "{date.today().isoformat()}",
      "description": "<p>- Wrote code</p>"
    }}
  ],
  "education": [
    {{
      "level": "Bachelors",
      "institution": "MIT",
      "degree": "BSc Computer Science",
      "gpa": null,
      "startYear": "2016",
      "endYear": "2020"
    }},
    {{
      "level": "Certificate",
      "institution": "Coursera",
      "degree": "Frontend Certificate",
      "gpa": null,
      "startYear": "2021",
      "endYear": "2021"
    }}
  ],
  "currentProfession": "Software Engineer",
  "currentIndustry": "Technology & IT",
  "suggestedSectors": ["Technology & IT"],
  "experienceLevel": "Mid",
  "yearsOfExperience": 4
}}
"""
    try:
        client = AiClient()
        parsed = client.chat_json(system_prompt, text, timeout_seconds=150)
        
        education = parsed.get("education") or []
        if isinstance(education, list):
            for edu in education:
                if isinstance(edu, dict):
                    lvl = str(edu.get("level") or "").lower()
                    if "bachelor" in lvl:
                        edu["level"] = "Bachelors"
                    elif "master" in lvl:
                        edu["level"] = "Masters"
                    elif "certif" in lvl:
                        edu["level"] = "Certificate"

        headline = parsed.get("headline")
        current_profession = parsed.get("currentProfession")
        if (not current_profession or _is_location(current_profession)) and headline:
            current_profession = headline

        experience = parsed.get("experience") or []
        if isinstance(experience, list):
            for exp in experience:
                if isinstance(exp, dict):
                    if str(exp.get("endDate")).lower() == "present":
                        exp["endDate"] = date.today().isoformat()
                    if _is_location(str(exp.get("title") or "")) and headline:
                        exp["title"] = headline

        raw_sectors = parsed.get("suggestedSectors")
        suggested: list[str] = []
        if isinstance(raw_sectors, list):
            for s in raw_sectors:
                if isinstance(s, str) and s in sectors and s not in suggested:
                    suggested.append(s)
                if len(suggested) >= 1:
                    break

        curr_ind = parsed.get("currentIndustry")
        if not suggested and curr_ind and curr_ind in sectors:
            suggested.append(curr_ind)

        # Validate sector suggestion with high-precision domain keywords
        text_pool = " ".join(filter(None, [
            headline, current_profession,
            " ".join(parsed.get("skills") or []),
            " ".join(str(e.get("title", "")) for e in experience if isinstance(e, dict)),
        ]))
        domain_suggested = _extract_suggested_sectors(text_pool, sectors, max_count=1)
        if domain_suggested:
            suggested = domain_suggested

        # Compute real years of professional experience strictly from work experience (NOT education)
        total_months = 0
        if isinstance(experience, list):
            for exp in experience:
                if isinstance(exp, dict):
                    s_str = str(exp.get("startDate") or "")
                    e_str = str(exp.get("endDate") or "")
                    start = _parse_point(s_str[:7].replace("-", "/")) or _parse_point(s_str)
                    end = _parse_point(e_str[:7].replace("-", "/")) or _parse_point(e_str)
                    if start and end and end >= start:
                        total_months += _months_between(start, end)

        if experience:
            final_years = (total_months // 12) if total_months >= 12 else (1 if total_months >= 6 else 0)
            final_level = _experience_level_from_years(final_years)
        else:
            final_years = parsed.get("yearsOfExperience")
            final_level = parsed.get("experienceLevel") or _experience_level_from_years(final_years)

        # Ensure base keys exist with safe types
        return {
            "headline": headline,
            "about": parsed.get("about"),
            "skills": parsed.get("skills") or [],
            "experience": experience,
            "education": education,
            "currentProfession": current_profession,
            "currentIndustry": suggested[0] if suggested else curr_ind,
            "suggestedSectors": suggested[:1],
            "experienceLevel": final_level,
            "yearsOfExperience": final_years,
            "skippedExperience": 0, 
            "skippedEducation": 0,
        }
    except Exception as exc:
        logger.warning("AI resume extraction failed: %s", exc)
        return None


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
        "suggestedSectors": [],
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
    
    # Try AI first, fallback to regex rules
    profile = _extract_fields_ai(text, sectors)
    if not profile:
        logger.info("Falling back to regex resume parser")
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
