# SeraGo-AI

Standalone Django matching service for SeraGo. It implements exactly two features:

## Feature 1 — Job published → For You scores

When the .NET backend approves a job it calls:

```
POST /api/matching/webhook/job-published   (X-Api-Key)
```

The job is scored (0–100) against every talent in `eligibleTalents` that has
**both** For You settings (`preferredSectorIds`) **and** a usable profile.
Talents missing either are returned as `skipped` with a reason — their match
cannot be calculated. Scores are stored in `TalentJobScore`.

The .NET For You feed reads the stored scores in one call:

```
POST /api/matching/scores/batch            (X-Api-Key)
```

## Feature 2 — Application created → application score

When a talent applies, the .NET backend calls:

```
POST /api/matching/webhook/application     (X-Api-Key)
```

The application (talent snapshot + job) is scored **immediately** (0–100) and
stored on `ApplicationScore` so the recruiter's applications page can display
it right away. The recruiter pages read scores via:

```
POST /api/matching/application-scores/batch   (X-Api-Key)
```

## Scoring

`matching_engine/services/scoring.py` (ported from Temp-AI) blends:
embedding similarity (35%) + skill overlap (20%) + experience (15%) +
sector (10%) + location (10%) + work mode (5%) + role (5%), producing a
0–100 score with matched/missing keywords and a full explanation breakdown.

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; use pip on POSIX
cp .env.example .env                            # set DB_* + MATCHING_API_KEY
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py runserver 8001
```

The .NET backend must use the same `MATCHING_API_KEY` and point
`MATCHING_API_URL` at this service.

## Endpoints

- `POST /api/matching/webhook/job-published` — Feature 1
- `POST /api/matching/webhook/application` — Feature 2
- `POST /api/matching/scores/batch` — batch For You scores
- `POST /api/matching/application-scores/batch` — batch application scores
- `GET /api/matching/health` — health check
- `GET /api/matching/docs` — Swagger UI (all endpoints above)
