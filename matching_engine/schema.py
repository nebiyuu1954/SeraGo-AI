"""
OpenAPI 3.0 schema for SeraGo AI Matching Engine.

Hand-crafted because the views are plain Django function-based views
(not DRF ViewSets), so drf-spectacular's auto-discovery doesn't pick them up.
This gives us full control over the documentation.

Documents the two core features only:
  Feature 1 — POST /webhook/job-published (score job against eligible talents)
  Feature 2 — POST /webhook/application (score application immediately)
plus the batch read endpoints the .NET backend uses to display those scores.
"""

SCHEMA = {
    "openapi": "3.0.3",
    "info": {
        "title": "SeraGo AI — Matching Engine API",
        "description": (
            "AI-powered job-talent matching service for SeraGo.\n\n"
            "## Features\n"
            "- **For You matching**: when a job is published, every talent that has "
            "For You settings + a usable profile is scored 0-100\n"
            "- **Application scoring**: when a talent applies, the application is "
            "scored immediately (0-100) for the recruiter's applications page\n\n"
            "## Authentication\n"
            "Webhook and batch read endpoints require the `X-Api-Key` header "
            "(shared secret with the .NET backend)."
        ),
        "version": "1.0.0",
        "contact": {"name": "SeraGo Team"},
    },
    "servers": [
        {"url": "http://localhost:8001", "description": "Local development"},
    ],
    "tags": [
        {"name": "Health", "description": "Service health checks"},
        {"name": "Webhooks", "description": "Endpoints called by the .NET backend (require X-Api-Key)"},
        {"name": "Scores", "description": "Batch score reads used by the .NET pages"},
    ],
    "paths": {
        # ── Health ──────────────────────────────────────────────────
        "/api/matching/health": {
            "get": {
                "tags": ["Health"],
                "summary": "Service health check",
                "description": "Returns service status and stored vector/score counts.",
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
                                        "for_you_scores": {"type": "integer"},
                                        "application_scores": {"type": "integer"},
                                    },
                                }
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
                "summary": "Feature 1: score published job against eligible talents",
                "description": (
                    "Called by .NET when admin approves a job. Only talents that "
                    "have both For You settings (preferredSectorIds) and a usable "
                    "profile are scored (0-100); everyone else is returned as "
                    "skipped with a reason. Requires `X-Api-Key` header."
                ),
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
        "/api/matching/webhook/application": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Feature 2: score application immediately",
                "description": (
                    "Called by .NET when a talent applies. Scores the application "
                    "(talent snapshot vs job) right away and stores the 0-100 score "
                    "for the recruiter's applications page. Requires `X-Api-Key`."
                ),
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
                    "200": {"description": "Application scored"},
                    "401": {"description": "Missing API key"},
                    "403": {"description": "Invalid API key"},
                },
            }
        },
        # ── Talent-initiated refresh ─────────────────────────────────
        "/api/matching/refresh-for-you": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Score one talent against a list of jobs now (For You refresh)",
                "description": (
                    "Called by .NET when a talent hits the 'Run AI matching' "
                    "button on the For You page. Scores their feed jobs against "
                    "their profile immediately (0-100 each), stores the scores, "
                    "and returns them. Requires `X-Api-Key` header."
                ),
                "operationId": "refreshForYou",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RefreshForYouRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Map of jobId -> freshly computed score payload",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RefreshForYouResponse"},
                            }
                        },
                    },
                    "401": {"description": "Missing API key"},
                    "403": {"description": "Invalid API key"},
                },
            }
        },
        # ── Recruiter-initiated rescore ─────────────────────────────
        "/api/matching/applications/rescore": {
            "post": {
                "tags": ["Webhooks"],
                "summary": "Score stored applications now (recruiter 'Run AI matching')",
                "description": (
                    "Called by .NET when a recruiter hits 'Run AI matching' on "
                    "a job's applicants. Scores each application (0-100) from "
                    "its apply-time profile snapshot vs the job and stores the "
                    "result on ApplicationScore. Requires `X-Api-Key`."
                ),
                "operationId": "applicationsRescore",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ApplicationsRescoreRequest"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Counts of scored / reused / failed applications",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ApplicationsRescoreResponse"},
                            }
                        },
                    },
                    "401": {"description": "Missing API key"},
                    "403": {"description": "Invalid API key"},
                },
            }
        },
        # ── Scores (batch reads) ────────────────────────────────────
        "/api/matching/scores/batch": {
            "post": {
                "tags": ["Scores"],
                "summary": "Batch-fetch talent-job scores (For You feed)",
                "description": (
                    "Returns the stored 0-100 scores for one talent against a list "
                    "of job ids. Used by the .NET For You feed to annotate a page "
                    "of jobs in a single call. Requires `X-Api-Key`."
                ),
                "operationId": "scoresBatch",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["userId"],
                                "properties": {
                                    "userId": {"type": "string"},
                                    "jobIds": {"type": "array", "items": {"type": "string", "format": "uuid"}},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Map of jobId -> score payload",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ScoreBatchResponse"},
                            }
                        },
                    }
                },
            }
        },
        "/api/matching/application-scores/batch": {
            "post": {
                "tags": ["Scores"],
                "summary": "Batch-fetch application scores",
                "description": (
                    "Returns stored 0-100 scores for a list of application ids. "
                    "Used by the .NET recruiter applications page. Requires `X-Api-Key`."
                ),
                "operationId": "applicationScoresBatch",
                "security": [{"apiKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "applicationIds": {"type": "array", "items": {"type": "string", "format": "uuid"}},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Map of applicationId -> score payload",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ApplicationScoreBatchResponse"},
                            }
                        },
                    }
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
            "ApplicationsRescoreRequest": {
                "type": "object",
                "required": ["applications"],
                "properties": {
                    "applications": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["applicationId", "talentProfile", "job"],
                            "properties": {
                                "applicationId": {"type": "string", "format": "uuid"},
                                "talentProfile": {"$ref": "#/components/schemas/TalentProfile"},
                                "job": {"$ref": "#/components/schemas/JobData"},
                            },
                        },
                    },
                },
            },
            "ApplicationsRescoreResponse": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                    "scored": {"type": "integer", "description": "Applications freshly scored"},
                    "cached": {"type": "integer", "description": "Applications whose stored score was already current and reused"},
                    "failed": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "integer"},
                },
            },
            "RefreshForYouRequest": {
                "type": "object",
                "required": ["talent", "jobs"],
                "properties": {
                    "talent": {"$ref": "#/components/schemas/TalentProfile"},
                    "jobs": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/JobData"},
                    },
                },
            },
            "RefreshForYouResponse": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ok", "skipped"]},
                    "reason": {"type": "string", "nullable": True},
                    "scored": {"type": "integer", "description": "Pairs freshly scored this run"},
                    "cached": {"type": "integer", "description": "Pairs whose stored score was already current and reused"},
                    "total": {"type": "integer"},
                    "skipped": {"type": "array", "items": {"type": "object"}},
                    "scores": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/components/schemas/StoredScore"},
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
                    "preferredSectorIds": {
                        "type": "array",
                        "items": {"type": "string", "format": "uuid"},
                        "description": "The talent's For You sector ids (UserSettings.forYou.sectorIds)",
                    },
                },
            },
            "ScoreBatchResponse": {
                "type": "object",
                "properties": {
                    "userId": {"type": "string"},
                    "count": {"type": "integer"},
                    "scores": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/components/schemas/StoredScore"},
                    },
                },
            },
            "ApplicationScoreBatchResponse": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "scores": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/components/schemas/ApplicationScorePayload"},
                    },
                },
            },
            "StoredScore": {
                "type": "object",
                "properties": {
                    "score": {"type": "number", "description": "0-100 compatibility score"},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    "missing": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "object"},
                },
            },
            "ApplicationScorePayload": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "scored", "failed"]},
                    "score": {"type": "number", "nullable": True},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    "missing": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "object"},
                },
            },
        },
    },
}
