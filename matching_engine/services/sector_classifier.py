"""
Sector Classifier — 4-layer classification for scraped job titles.

Layers (executed in order, stops at first confident match):
  1. Alias match       — exact lookup against SectorAliases table (free, <1ms)
  2. Keyword rules     — hardcoded rules ported from C# SectorNormalizer (free, <1ms)
  3. Embedding sim     — sentence-transformers cosine similarity (free, ~15ms)
  4. LLM fallback      — GPT-4o-mini for ambiguous cases (~$0.000015, ~500ms)

Usage:
    classifier = SectorClassifier()
    result = classifier.classify("Senior Software Engineer", raw_sector="Software Design & Development")
    # result.sector_id = UUID, result.sector_name = "Technology & IT", result.confidence = 0.95

The classifier reads canonical sectors from the shared PostgreSQL database
(the same one the .NET backend uses for the Sectors/SectorAlias tables).
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Result dataclass ──────────────────────────────────────────────────


@dataclass
class SectorClassification:
    """Result of sector classification."""
    sector_id: str  # UUID of the canonical sector
    sector_name: str  # Display name, e.g. "Technology & IT"
    confidence: float  # 0.0 - 1.0
    method: str  # "alias" | "keyword" | "embedding" | "llm"
    sector_slug: str = ""  # URL-safe slug, e.g. "technology-it"


# ── Keyword rules (ported from C# SectorNormalizer.cs) ───────────────
# Ordered most-specific first. First match wins.

_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("technology-it", [
        "software", "developer", "programmer", "frontend", "backend",
        "full stack", "flutter", "react", "angular", "vue", "devops",
        "cloud engineer", "machine learning", "artificial intelligence",
        "data scientist", "data engineer", "data analyst", "data science",
        "database", "it support", "system administrator", "network",
        "cybersecurity", "qa automation", "qa engineer", "software tester",
        "computer science", "computer engineer", "genai", "llm",
        "business intelligence", "bi developer", "mobile developer",
        "app developer", "web developer", "wordpress", "informatics",
        "data encoder", "tech", "it officer", "computer",
    ]),
    ("healthcare", [
        "nurse", "doctor", "physician", "medical", "health", "pharmacist",
        "pharmacy", "laboratory", "lab technician", "nutritionist", "dentist",
        "midwife", "clinical", "hospital", "nursing",
    ]),
    ("engineering-construction", [
        "engineer", "engineering", "civil", "mechanical", "electrical",
        "architect", "construction", "surveyor", "site supervisor",
        "sanitary", "architectural", "automotive", "maintenance engineer",
        "geologist",
    ]),
    ("accounting-finance", [
        "accountant", "accounting", "finance", "financial", "auditor",
        "audit", "tax", "bookkeeper", "cashier", "payroll", "budget",
    ]),
    ("banking-insurance", [
        "banking", "bank", "insurance",
    ]),
    ("legal", [
        "lawyer", "legal", "attorney", "advocate", "compliance", "judge",
    ]),
    ("education-training", [
        "teacher", "teaching", "tutor", "instructor", "lecturer",
        "professor", "principal", "school", "education", "trainer",
        "mentor", "academic", "library",
    ]),
    ("human-resources", [
        "human resource", "hr officer", "hr manager", "recruiter",
        "recruitment", "talent acquisition",
    ]),
    ("procurement-supply-chain", [
        "procurement", "purchasing", "supply chain",
    ]),
    ("logistics-transportation", [
        "driver", "transport", "logistics", "delivery", "courier",
        "dispatch", "truck", "vehicle",
    ]),
    ("media-communication", [
        "journalist", "reporter", "editor", "media", "writer", "content",
        "communication", "public relations", "broadcast", "photographer",
    ]),
    ("design-creative", [
        "designer", "graphic", "creative", "artist", "fashion",
        "illustrator", "ui/ux", "animation", "video editor",
    ]),
    ("hospitality-tourism", [
        "cook", "chef", "waiter", "waitress", "hospitality", "hotel",
        "tourism", "bartender", "barista", "housekeeping",
    ]),
    ("customer-service-support", [
        "customer service", "call center", "support", "receptionist",
        "front desk", "help desk",
    ]),
    ("manufacturing-production", [
        "manufacturing", "production", "factory", "machine operator",
        "assembly", "industrial", "quality control",
    ]),
    ("agriculture-natural-science", [
        "agriculture", "agronomist", "farm", "biology", "biologist",
        "chemistry", "chemist", "physics", "mathematician", "science",
        "veterinary", "agro",
    ]),
    ("social-science-community", [
        "social worker", "sociology", "psychologist", "history",
        "community",
    ]),
    ("security-protection", [
        "security", "guard", "safety", "protection",
    ]),
    ("sales-marketing", [
        "sales", "marketing", "promotion", "business development",
        "account manager", "merchandiser",
    ]),
    ("business-administration", [
        "project manager", "manager", "management", "administrator",
        "operations", "executive", "secretary", "office", "director",
        "coordinator", "supervisor", "team leader",
    ]),
    ("skilled-general-labor", [
        "janitor", "cleaner", "plumber", "carpenter", "welder", "mason",
        "electrician", "laborer",
    ]),
]


# ── Sector Classifier ─────────────────────────────────────────────────


class SectorClassifier:
    """4-layer sector classification for scraped jobs.

    Reads canonical sectors from the shared PostgreSQL database (the same
    Sectors/SectorAlias tables the .NET backend manages).

    Layers:
      1. Alias match       — exact normalized string lookup
      2. Keyword rules     — hardcoded title keyword matching
      3. Embedding sim     — sentence-transformers cosine similarity
      4. LLM fallback      — GPT-4o-mini for ambiguous cases
    """

    def __init__(self):
        self._sectors_cache: Optional[list[dict]] = None
        self._sector_vectors: Optional[list] = None  # pre-encoded embeddings
        self._embedding_model = None

    def _get_sectors(self) -> list[dict]:
        """Load canonical sectors + aliases from the shared PostgreSQL DB.

        Returns list of dicts:
          [{ id, name, slug, aliases: ["lowercase alias", ...] }, ...]

        Falls back to seed data if the DB is unavailable (local dev).
        Cached after first call (sectors change rarely).
        """
        if self._sectors_cache is not None:
            return self._sectors_cache

        try:
            from django.db import connection

            with connection.cursor() as cursor:
                # Read active sectors
                cursor.execute("""
                    SELECT s."Id", s."Name", s."Slug"
                    FROM "Sectors" AS s
                    WHERE s."IsActive" = true
                    ORDER BY s."Name"
                """)
                rows = cursor.fetchall()

                if not rows:
                    raise ValueError("No sectors in DB")

                sectors = []
                for sector_id, name, slug in rows:
                    # Read aliases for this sector
                    cursor.execute("""
                        SELECT sa."Alias"
                        FROM "SectorAliases" AS sa
                        WHERE sa."SectorId" = %s
                    """, [sector_id])
                    aliases = [row[0].strip().lower() for row in cursor.fetchall()]

                    sectors.append({
                        "id": str(sector_id),
                        "name": name,
                        "slug": slug,
                        "aliases": aliases,
                    })

                self._sectors_cache = sectors
                logger.info("Loaded %d canonical sectors from DB", len(sectors))
                return sectors

        except Exception as exc:
            logger.warning("DB unavailable, using seed sector data: %s", exc)
            self._sectors_cache = self._get_seed_sectors()
            return self._sectors_cache

    @staticmethod
    def _get_seed_sectors() -> list[dict]:
        """Fallback seed data matching the .NET SectorSeedData.cs.

        Used when the shared PostgreSQL is unavailable (local dev/testing).
        Sector IDs match the seeded UUIDs so they work in production too.
        """
        return [
            {"id": "a0000000-0000-0000-0000-000000000001", "name": "Technology & IT", "slug": "technology-it",
             "aliases": ["information technology", "technology", "tech", "it", "ict",
                          "it support", "computer science and information technology",
                          "it, computer science and software engineering",
                          "software design & development", "data science & analytics",
                          "software", "computer science"]},
            {"id": "a0000000-0000-0000-0000-000000000002", "name": "Accounting & Finance", "slug": "accounting-finance",
             "aliases": ["accounting & finance", "accounting", "finance",
                          "accounting and finance", "economics", "tax", "audit", "bookkeeping"]},
            {"id": "a0000000-0000-0000-0000-000000000003", "name": "Banking & Insurance", "slug": "banking-insurance",
             "aliases": ["banking and insurance", "banking", "bank", "insurance",
                          "insurance and investment"]},
            {"id": "a0000000-0000-0000-0000-000000000004", "name": "Sales & Marketing", "slug": "sales-marketing",
             "aliases": ["sales & promotion", "sales", "marketing",
                          "sales and marketing", "business sales and marketing",
                          "marketing management", "digital marketing", "promotion"]},
            {"id": "a0000000-0000-0000-0000-000000000005", "name": "Healthcare", "slug": "healthcare",
             "aliases": ["healthcare", "health care", "health care management",
                          "public health", "nursing", "pharmaceutical", "pharmacy",
                          "medicine", "medical"]},
            {"id": "a0000000-0000-0000-0000-000000000006", "name": "Education & Training", "slug": "education-training",
             "aliases": ["teaching & education", "education", "teaching",
                          "tutoring, training & mentorship", "language and literature",
                          "education management", "tutoring", "training"]},
            {"id": "a0000000-0000-0000-0000-000000000007", "name": "Engineering & Construction", "slug": "engineering-construction",
             "aliases": ["engineering", "construction & civil engineering", "civil engineering",
                          "mechanical & electrical engineering",
                          "chemical & biomedical engineering",
                          "environmental, mining & energy engineering",
                          "manufacturing engineering", "architectural engineering",
                          "automotive engineering", "sanitary engineering",
                          "biomedical engineering", "electrical engineering",
                          "architecture & urban planning", "construction skilled worker",
                          "mechanical engineering", "planning", "water and sanitation",
                          "automotive"]},
            {"id": "a0000000-0000-0000-0000-000000000008", "name": "Human Resources", "slug": "human-resources",
             "aliases": ["human resource & talent management", "human resource and recruitment",
                          "human resource administration", "human resources", "hr",
                          "recruitment", "human resource", "talent management"]},
            {"id": "a0000000-0000-0000-0000-000000000009", "name": "Business & Administration", "slug": "business-administration",
             "aliases": ["business", "business administration",
                          "business administration & operations",
                          "business and administration",
                          "secretarial, admin and clerical", "secretarial & office management",
                          "management", "development and project management",
                          "advisory & consultancy", "brokerage & case closing",
                          "business management", "administration", "office management",
                          "secretarial"]},
            {"id": "a0000000-0000-0000-0000-00000000000a", "name": "Manufacturing & Production", "slug": "manufacturing-production",
             "aliases": ["manufacturing & production", "manufacturing",
                          "manufacturing management", "fmcg and manufacturing",
                          "woodwork & carpentry", "production"]},
            {"id": "a0000000-0000-0000-0000-00000000000b", "name": "Logistics & Transportation", "slug": "logistics-transportation",
             "aliases": ["transportation & delivery", "transportation & logistics",
                          "transportation management", "transportation",
                          "logistics & supply chain",
                          "logistics, transport and supply chain", "logistics",
                          "warehouse, supply chain and distribution"]},
            {"id": "a0000000-0000-0000-0000-00000000000c", "name": "Procurement & Supply Chain", "slug": "procurement-supply-chain",
             "aliases": ["purchasing & procurement",
                          "supply chain & purchasing management",
                          "procurement", "supply chain", "purchasing"]},
            {"id": "a0000000-0000-0000-0000-00000000000d", "name": "Media & Communication", "slug": "media-communication",
             "aliases": ["media & entertainment", "media and communication",
                          "multimedia content production", "documentation & writing",
                          "translation & transcription", "media", "journalism",
                          "communication", "writing"]},
            {"id": "a0000000-0000-0000-0000-00000000000e", "name": "Design & Creative", "slug": "design-creative",
             "aliases": ["creative art & design",
                          "fashion / clothing & textile design", "design",
                          "graphic design", "creative", "fashion design",
                          "creative arts", "ui/ux design", "fashion"]},
            {"id": "a0000000-0000-0000-0000-00000000000f", "name": "Customer Service & Support", "slug": "customer-service-support",
             "aliases": ["customer service & care", "retail & office support",
                          "customer service", "support", "call center", "reception"]},
            {"id": "a0000000-0000-0000-0000-000000000010", "name": "Hospitality & Tourism", "slug": "hospitality-tourism",
             "aliases": ["hospitality & tourism",
                          "food & drink preparation / service", "hospitality",
                          "tourism", "chef", "catering", "hotel"]},
            {"id": "a0000000-0000-0000-0000-000000000011", "name": "Agriculture & Natural Science", "slug": "agriculture-natural-science",
             "aliases": ["agriculture", "agricultural science", "natural science",
                          "natural sciences", "chemistry", "physics", "microbiology",
                          "mathematics", "nutrition", "biology"]},
            {"id": "a0000000-0000-0000-0000-000000000012", "name": "Legal", "slug": "legal",
             "aliases": ["law & legal advocacy", "legal services", "legal", "law",
                          "advocacy"]},
            {"id": "a0000000-0000-0000-0000-000000000013", "name": "Social Science & Community", "slug": "social-science-community",
             "aliases": ["social sciences and community service", "social science",
                          "social work", "community service", "history", "sociology",
                          "psychology"]},
            {"id": "a0000000-0000-0000-0000-000000000014", "name": "Security & Protection", "slug": "security-protection",
             "aliases": ["security", "protection", "guard", "safety"]},
            {"id": "a0000000-0000-0000-0000-000000000015", "name": "Skilled & General Labor", "slug": "skilled-general-labor",
             "aliases": ["low and medium skilled worker",
                          "service industry skilled worker",
                          "janitorial & office services", "general labor",
                          "cleaner", "maintenance"]},
        ]

    def _get_embedding_model(self):
        """Lazily load the sentence-transformer model."""
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                from django.conf import settings
                model_name = getattr(settings, "MATCHING_MODEL_NAME", "all-MiniLM-L6-v2")
                self._embedding_model = SentenceTransformer(model_name)
                logger.info("Loaded embedding model for sector classification: %s", model_name)
            except ImportError:
                logger.warning("sentence-transformers not installed — embedding layer disabled")
                return None
        return self._embedding_model

    def _ensure_sector_vectors(self):
        """Pre-encode all sector names + aliases as embedding vectors."""
        if self._sector_vectors is not None:
            return

        model = self._get_embedding_model()
        if model is None:
            return

        sectors = self._get_sectors()
        if not sectors:
            return

        # Build text for each sector: "Name: alias1, alias2, alias3"
        texts = []
        for s in sectors:
            parts = [s["name"]]
            if s["aliases"]:
                parts.extend(s["aliases"])
            texts.append(": ".join(parts))

        embeddings = model.encode(texts, show_progress_bar=False)
        self._sector_vectors = [
            {"sector": s, "embedding": emb}
            for s, emb in zip(sectors, embeddings)
        ]
        logger.info("Pre-encoded %d sector vectors", len(self._sector_vectors))

    # ── Layer 1: Alias match ──────────────────────────────────────────

    def _classify_alias(self, raw_sector: str) -> Optional[SectorClassification]:
        """Exact match against SectorAliases table."""
        if not raw_sector:
            return None

        normalized = raw_sector.strip().lower()
        sectors = self._get_sectors()

        for s in sectors:
            if normalized in s["aliases"]:
                return SectorClassification(
                    sector_id=s["id"],
                    sector_name=s["name"],
                    confidence=1.0,
                    method="alias",
                    sector_slug=s["slug"],
                )
        return None

    # ── Layer 2: Keyword rules ────────────────────────────────────────

    def _classify_keywords(self, title: str) -> Optional[SectorClassification]:
        """Keyword-based classification (ported from C# SectorNormalizer).

        Uses word-boundary matching for short keywords (< 5 chars) to avoid
        false positives (e.g. 'tech' matching 'technician').
        """
        if not title:
            return None

        text = title.lower()
        sectors = self._get_sectors()

        for slug, keywords in _KEYWORD_RULES:
            for keyword in keywords:
                # Word-boundary match for short keywords to avoid false positives
                if len(keyword) < 5:
                    if not re.search(r'\b' + re.escape(keyword) + r'\b', text):
                        continue
                else:
                    if keyword not in text:
                        continue

                # Find the matching sector
                for s in sectors:
                    if s["slug"] == slug:
                        return SectorClassification(
                            sector_id=s["id"],
                            sector_name=s["name"],
                            confidence=0.8,
                            method="keyword",
                            sector_slug=s["slug"],
                        )
        return None

    # ── Layer 3: Embedding similarity ─────────────────────────────────

    def _classify_embedding(self, title: str, description: str = "") -> Optional[SectorClassification]:
        """Embedding-based classification using sentence-transformers."""
        model = self._get_embedding_model()
        if model is None:
            return None

        self._ensure_sector_vectors()
        if not self._sector_vectors:
            return None

        # Encode the job title (+ optional description for more context)
        text = title
        if description:
            # Truncate description to keep encoding fast
            text = f"{title}. {description[:200]}"

        job_embedding = model.encode([text], show_progress_bar=False)[0]

        # Cosine similarity against all sector vectors
        from sklearn.metrics.pairwise import cosine_similarity
        import numpy as np

        sector_embeddings = np.array([sv["embedding"] for sv in self._sector_vectors])
        similarities = cosine_similarity([job_embedding], sector_embeddings)[0]

        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[0][best_idx])

        # Normalize from [-1, 1] to [0, 1]
        confidence = (best_score + 1) / 2

        if confidence < 0.6:
            # Too ambiguous — fall through to LLM
            return None

        best_sector = self._sector_vectors[best_idx]["sector"]
        return SectorClassification(
            sector_id=best_sector["id"],
            sector_name=best_sector["name"],
            confidence=round(confidence, 4),
            method="embedding",
            sector_slug=best_sector["slug"],
        )

    # ── Layer 4: LLM fallback ─────────────────────────────────────────

    def _classify_llm(self, title: str, description: str = "",
                      raw_sector: str = "") -> Optional[SectorClassification]:
        """LLM-based classification for ambiguous cases."""
        from django.conf import settings

        # Pick the best available LLM provider
        provider = getattr(settings, "LLM_PROVIDER", "openai")
        api_key = ""
        model = ""
        base_url = ""

        if provider == "openai":
            api_key = getattr(settings, "OPENAI_API_KEY", "")
            model = getattr(settings, "OPENAI_MODEL", "gpt-4o")
        elif provider == "groq":
            api_key = getattr(settings, "GROQ_API_KEY", "")
            model = getattr(settings, "GROQ_MODEL", "llama-3.1-70b-versatile")
            base_url = "https://api.groq.com/openai/v1"
        elif provider == "openrouter":
            api_key = getattr(settings, "OPENROUTER_API_KEY", "")
            model = getattr(settings, "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
            base_url = getattr(settings, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        elif provider == "huggingface":
            api_key = getattr(settings, "HUGGINGFACE_API_KEY", "")
            model = getattr(settings, "HUGGINGFACE_MODEL", "meta-llama/Llama-2-7b-chat-hf")
            base_url = "https://api-inference.huggingface.co/v1"

        if not api_key:
            logger.warning("No LLM API key configured — LLM layer disabled")
            return None

        # Build the sector list for the prompt
        sectors = self._get_sectors()
        sector_names = [s["name"] for s in sectors]

        prompt = f"""Classify this job into exactly ONE of the following sectors.

Job title: {title}
{f"Description: {description[:300]}" if description else ""}
{f"Raw sector from source: {raw_sector}" if raw_sector else ""}

Sectors: {json.dumps(sector_names)}

Return ONLY a JSON object with no explanation:
{{"sector_name": "<exact sector name from the list>", "confidence": 0.0-1.0}}

If none match well, pick the closest one but set confidence below 0.5."""

        try:
            import urllib.request

            url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 100,
            }).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            content = data["choices"][0]["message"]["content"].strip()

            # Parse JSON from response (handle markdown code blocks)
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            result = json.loads(content)
            sector_name = result.get("sector_name", "")
            confidence = float(result.get("confidence", 0.5))

            # Find the matching sector
            for s in sectors:
                if s["name"].lower() == sector_name.lower():
                    return SectorClassification(
                        sector_id=s["id"],
                        sector_name=s["name"],
                        confidence=round(confidence, 4),
                        method="llm",
                        sector_slug=s["slug"],
                    )

            logger.warning("LLM returned unknown sector: %s", sector_name)
            return None

        except Exception as exc:
            logger.warning("LLM classification failed: %s", exc)
            return None

    # ── Main classify method ──────────────────────────────────────────

    def classify(self, title: str, raw_sector: str = "",
                 description: str = "") -> Optional[SectorClassification]:
        """Classify a job into a canonical sector.

        Executes layers in order, stops at first confident match:
          1. Alias match (confidence: 1.0)
          2. Keyword rules (confidence: 0.8)
          3. Embedding similarity (confidence: 0.6-1.0)
          4. LLM fallback (confidence: variable)

        Args:
            title: Job title (required)
            raw_sector: Raw sector name from the source website (optional)
            description: Job description for more context (optional)

        Returns:
            SectorClassification or None if all layers fail
        """
        # Layer 1: Alias match
        result = self._classify_alias(raw_sector)
        if result:
            logger.debug("Layer 1 (alias) matched: %s", result.sector_name)
            return result

        # Layer 2: Keyword rules
        result = self._classify_keywords(title)
        if result:
            logger.debug("Layer 2 (keyword) matched: %s", result.sector_name)
            return result

        # Layer 3: Embedding similarity
        result = self._classify_embedding(title, description)
        if result:
            logger.debug("Layer 3 (embedding) matched: %s (conf: %.2f)",
                         result.sector_name, result.confidence)
            return result

        # Layer 4: LLM fallback
        result = self._classify_llm(title, description, raw_sector)
        if result:
            logger.debug("Layer 4 (LLM) matched: %s (conf: %.2f)",
                         result.sector_name, result.confidence)
            return result

        logger.warning("All 4 layers failed for title: %s", title)
        return None


# ── Singleton (reuse across requests) ─────────────────────────────────

_classifier_instance: Optional[SectorClassifier] = None


def get_sector_classifier() -> SectorClassifier:
    """Get or create the singleton classifier instance."""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = SectorClassifier()
    return _classifier_instance
