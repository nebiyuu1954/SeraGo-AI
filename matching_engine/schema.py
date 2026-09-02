"""
OpenAPI 3.0 schema for SeraGo AI Matching Engine.

Hand-crafted because the views are plain Django function-based views
(not DRF ViewSets), so drf-spectacular's auto-discovery doesn't pick them up.
This gives us full control over the documentation.
"""

SCHEMA = {
    "openapi": "3.0.3",
    "info": {
        "title": "SeraGo AI — Matching Engine API",
        "description": (
            "AI-powered job-talent matching service for SeraGo.\n\n"
            "## Features\n"
            "- **Sector Classification**: 4-layer classification (alias → keyword → embedding → LLM)\n"
            "- **Job-Talent Scoring**: Embedding similarity + heuristic scoring (0-100)\n"
            "- **For You Feed**: Ranked job recommendations with staleness detection\n"
            "- **Deferred Application Scoring**: Nightly batch + recruiter on-demand\n\n"
            "## Authentication\n"
            "Webhook endpoints require `X-Api-Key` header (shared secret with .NET backend).\n"
            "Read endpoints and sector classification are public."
        ),
        "version": "1.0.0",
        "contact": {"name": "SeraGo Team"},
    },
    "servers": [
        {"url": "http://localhost:8001", "description": "Local development"},
    ],
    "tags": [
        {"name": "Health", "description": "Service health checks"},
        {"name": "Sector Classification", "description": "4-layer job sector classification"},
        {"name": "Webhooks", "description": "Endpoints called by the .NET backend (require X-Api-Key)"},
        {"name": "Matching", "description": "On-demand matching and score queries"},
        {"name": "Scores", "description": "Read compatibility scores"},
    ],
    "paths": {
        # ── Health ──────────────────────────────────────────────────
        "/api/matching/health": {
            "get": {
                "tags": ["Health"],
                "summary": "Service health check",
                "description": "Returns service status, vector counts, active users, and last sweep time.",
                "operationId": "health",
                "responses": {
                    "200": {
                        "description": "Health status",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string", "example": "ok"},
                                        "service": {"type": "string", "example": "matching_engine"},
                                        "scoring_version": {"type": "string", "example": "1.0.0"},
                                        "talent_vectors": {"type": "integer"},
                                        "job_vectors": {"type": "integer"},
                                        "scores": {"type": "integer"},
                                        "active_users": {"type": "integer"},
                                        "last_sweep": {"type": "string", "nullable": True},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/matching/classify-sector/health": {
            "get": {
                "tags": ["Health"],
                "summary": "Sector classifier health check",
                "operationId": "classifySectorHealth",
                "responses": {
                    "200": {
                        "description": "Classifier status",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "canonical_sectors": {"type": "integer"},
                                        "sector_names": {"type": "array", "items": {"type": "string"}},
                                        "embedding_model_loaded": {"type": "boolean"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        # ── Sector Classification ───────────────────────────────────
        "/api/matching/classify-sector": {
            "post": {
                "tags": ["Sector Classification"],
                "summary": "Classify job into canonical sector",
                "description": (
                    "4-layer classification:\n"
                    "1. **Alias match** — exact lookup against SectorAliases (free, <1ms)\n"
                    "2. **Keyword rules** — hardcoded title rules (free, <1ms)\n"
                    "3. **Embedding similarity** — sentence-transformers cosine sim (free, ~15ms)\n"
                    "4. **LLM fallback** — GPT-4o-mini for ambiguous cases (~$0.000015)"
                ),
                "operationId": "classifySector",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ClassifySectorRequest"},
                            "examples": {
                                "with_raw_sector": {
                                    "summary": "With raw sector from scraper",
                                    "value": {
                                        "title": "Senior Software Engineer",
                                        "rawSector": "Software Design & Development",
                                    },
                                },
                                "without_raw_sector": {
                                    "summary": "Without raw sector (keyword/embedding layers)",
                                    "value": {"title": "Registered Nurse"},
                                },
                            },
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Classification result",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ClassifySectorResponse"},
                                "examples": {
                                    "success": {
                                        "value": {
                                            "sectorId": "a0000000-0000-0000-0000-000000000001",
                                            "sectorName": "Technology & IT",
                                            "sectorSlug": "technology-it",
                                            "confidence": 1.0,
                                            "method": "alias",
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        # ── Webhooks ────────────────────────────────────────────────
        "/api/matching/webhook/job-published": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Score platform job against eligible talents",
                "description": "Called by .NET when admin approves a job. Requires `X-Api-Key` header.",
                "operationId": "webhookJobPublished",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/JobPublishedRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Scores computed"},
                    "401": {"description": "Missing API key"},
                    "403": {"description": "Invalid API key"},
                },
            }
        },
        "/api/matching/webhook/scraped-job": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Score scraped job against active talents",
                "description": "Enqueues async scoring for hot-sector scraped jobs. Requires `X-Api-Key`.",
                "operationId": "webhookScrapedJob",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ScrapedJobRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Task queued"},
                    "401": {"description": "Missing API key"},
                },
            }
        },
        "/api/matching/webhook/application": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Store application for deferred scoring",
                "description": "Stores application data for nightly batch or recruiter on-demand scoring. Requires `X-Api-Key`.",
                "operationId": "webhookApplication",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ApplicationRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Application queued"},
                    "401": {"description": "Missing API key"},
                },
            }
        },
        "/api/matching/webhook/talent-updated": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Invalidate scores on profile update",
                "description": "Only recomputes hash if scoring-relevant fields changed. Requires `X-Api-Key`.",
                "operationId": "webhookTalentUpdated",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TalentUpdatedRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Scores invalidated"},
                    "401": {"description": "Missing API key"},
                },
            }
        },
        "/api/matching/webhook/talent-login": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Trigger background scoring on talent login",
                "description": "Updates activity status and enqueues scoring for un-scored jobs. Requires `X-Api-Key`.",
                "operationId": "webhookTalentLogin",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TalentLoginRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Activity updated"},
                    "401": {"description": "Missing API key"},
                },
            }
        },
        # ── Matching ────────────────────────────────────────────────
        "/api/matching/on-demand-match": {
            "post": {
                "tags": ["Matching"],
                "summary": "On-demand match: compute scores for uncached jobs",
                "description": "Called when talent clicks 'AI Match' button. Returns cached + recomputed scores.",
                "operationId": "onDemandMatch",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/OnDemandMatchRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Scored jobs list",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/OnDemandMatchResponse"},
                            }
                        },
                    }
                },
            }
        },
        "/api/matching/score-application": {
            "post": {
                "tags": ["Matching"],
                "summary": "Score application on-demand (recruiter)",
                "description": "Called when recruiter clicks 'Match with AI'. Returns cached or freshly computed score.",
                "operationId": "scoreApplication",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["applicationId"],
                                "properties": {
                                    "applicationId": {"type": "string", "format": "uuid"}
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Application score"},
                    "404": {"description": "Application not found"},
                },
            }
        },
        # ── Scores ──────────────────────────────────────────────────
        "/api/matching/for-you/{user_id}": {
            "get": {
                "tags": ["Scores"],
                "summary": "Get ranked job recommendations",
                "description": "Returns top 50 scored jobs sorted by score, with staleness flags.",
                "operationId": "forYou",
                "parameters": [
                    {
                        "name": "user_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Talent user ID (GUID)",
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Ranked jobs",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ForYouResponse"},
                            }
                        },
                    }
                },
            }
        },
        "/api/matching/talent-score/{user_id}/{job_id}": {
            "get": {
                "tags": ["Scores"],
                "summary": "Get score for a specific talent-job pair",
                "operationId": "talentJobScore",
                "parameters": [
                    {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
                ],
                "responses": {
                    "200": {
                        "description": "Score details",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ScoreResponse"},
                            }
                        },
                    },
                    "404": {"description": "Score not found"},
                },
            }
        },
        "/api/matching/application-score/{application_id}": {
            "get": {
                "tags": ["Scores"],
                "summary": "Get score for a specific application",
                "operationId": "applicationScore",
                "parameters": [
                    {"name": "application_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
                ],
                "responses": {
                    "200": {
                        "description": "Application score info",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ApplicationScoreResponse"},
                            }
                        },
                    },
                    "404": {"description": "Application not found"},
                },
            }
        },
    },
    "components": {
        "securitySchemes": {
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Api-Key",
                "description": "Shared secret with .NET backend",
            }
        },
        "schemas": {
            "ClassifySectorRequest": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Job title"},
                    "rawSector": {"type": "string", "description": "Raw sector name from source website"},
                    "description": {"type": "string", "description": "Job description (optional, improves accuracy)"},
                },
            },
            "ClassifySectorResponse": {
                "type": "object",
                "properties": {
                    "sectorId": {"type": "string", "nullable": True},
                    "sectorName": {"type": "string", "nullable": True},
                    "sectorSlug": {"type": "string", "nullable": True},
                    "confidence": {"type": "number", "format": "float"},
                    "method": {"type": "string", "enum": ["alias", "keyword", "embedding", "llm", "none"]},
                },
            },
            "JobPublishedRequest": {
                "type": "object",
                "required": ["job"],
                "properties": {
                    "job": {"$ref": "#/components/schemas/JobData"},
                    "eligibleTalents": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/TalentProfile"},
                    },
                },
            },
            "ScrapedJobRequest": {
                "type": "object",
                "required": ["job"],
                "properties": {
                    "job": {"$ref": "#/components/schemas/JobData"},
                    "activeTalents": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/TalentProfile"},
                    },
                },
            },
            "ApplicationRequest": {
                "type": "object",
                "required": ["applicationId", "talentProfile", "job"],
                "properties": {
                    "applicationId": {"type": "string", "format": "uuid"},
                    "talentProfile": {"$ref": "#/components/schemas/TalentProfile"},
                    "job": {"$ref": "#/components/schemas/JobData"},
                },
            },
            "TalentUpdatedRequest": {
                "type": "object",
                "required": ["talentProfile"],
                "properties": {
                    "talentProfile": {"$ref": "#/components/schemas/TalentProfile"},
                    "previousProfile": {"$ref": "#/components/schemas/TalentProfile"},
                },
            },
            "TalentLoginRequest": {
                "type": "object",
                "required": ["userId"],
                "properties": {
                    "userId": {"type": "string"},
                    "talentProfile": {"$ref": "#/components/schemas/TalentProfile"},
                },
            },
            "OnDemandMatchRequest": {
                "type": "object",
                "required": ["userId", "talentProfile"],
                "properties": {
                    "userId": {"type": "string"},
                    "talentProfile": {"$ref": "#/components/schemas/TalentProfile"},
                },
            },
            "OnDemandMatchResponse": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "scored": {"type": "integer"},
                    "fresh": {"type": "integer"},
                    "recomputed": {"type": "integer"},
                    "elapsed_seconds": {"type": "number"},
                    "jobs": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ScoredJob"},
                    },
                },
            },
            "ForYouResponse": {
                "type": "object",
                "properties": {
                    "userId": {"type": "string"},
                    "count": {"type": "integer"},
                    "staleCount": {"type": "integer"},
                    "jobs": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ScoredJob"},
                    },
                },
            },
            "ScoredJob": {
                "type": "object",
                "properties": {
                    "jobId": {"type": "string", "format": "uuid"},
                    "score": {"type": "number", "description": "0-100 compatibility score"},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    "missing": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "object"},
                    "fresh": {"type": "boolean", "description": "Whether the score is still valid"},
                },
            },
            "ScoreResponse": {
                "type": "object",
                "properties": {
                    "talentUserId": {"type": "string"},
                    "jobId": {"type": "string", "format": "uuid"},
                    "score": {"type": "number"},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    "missing": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "object"},
                    "scoringVersion": {"type": "string"},
                },
            },
            "ApplicationScoreResponse": {
                "type": "object",
                "properties": {
                    "applicationId": {"type": "string", "format": "uuid"},
                    "status": {"type": "string", "enum": ["pending", "scored", "failed"]},
                    "score": {"type": "number", "nullable": True},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    "missing": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "object"},
                },
            },
            "JobData": {
                "type": "object",
                "properties": {
                    "jobId": {"type": "string", "format": "uuid"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "sectorId": {"type": "string", "format": "uuid"},
                    "sectorName": {"type": "string"},
                    "experienceLevel": {"type": "string"},
                    "jobType": {"type": "string"},
                    "workMode": {"type": "string"},
                    "skills": {"type": "string"},
                    "experienceMinYears": {"type": "integer"},
                    "experienceMaxYears": {"type": "integer"},
                },
            },
            "TalentProfile": {
                "type": "object",
                "properties": {
                    "userId": {"type": "string"},
                    "headline": {"type": "string"},
                    "about": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "experienceLevel": {"type": "string"},
                    "yearsOfExperience": {"type": "integer"},
                    "desiredRoles": {"type": "array", "items": {"type": "string"}},
                    "desiredJobTypes": {"type": "array", "items": {"type": "string"}},
                    "currentIndustry": {"type": "string"},
                    "currentProfession": {"type": "string"},
                    "workMode": {"type": "string"},
                    "preferredLocations": {"type": "array", "items": {"type": "string"}},
                    "workExperience": {"type": "array"},
                    "educationHistory": {"type": "array"},
                },
            },
        },
    },
}
