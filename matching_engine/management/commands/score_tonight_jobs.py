"""
Nightly batch scoring command.

Scores all un-scored talent-job pairs for active users.
Run via: python manage.py score_tonight_jobs

Options:
  --dry-run    Preview what would be scored without actually scoring
  --limit N    Limit to N users (for testing)
  --days N     Only consider users active in last N days (default: 7)
"""

import json
import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from matching_engine.models import (
    JobVector,
    SCORING_VERSION,
    SweepState,
    TalentJobScore,
    TalentProfileVector,
    UserActivity,
)


class Command(BaseCommand):
    help = "Nightly batch: score un-scored talent-job pairs for active users"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be scored without actually scoring",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help="Maximum number of users to process (default: 500)",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Only consider users active in the last N days (default: 7)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        days = options["days"]

        self.stdout.write(f"Nightly batch scoring (dry_run={dry_run}, limit={limit}, days={days})")
        start = time.time()

        # Find active users
        cutoff = timezone.now() - timezone.timedelta(days=days)
        active_users = UserActivity.objects.filter(
            last_active__gte=cutoff,
        ).values_list("user_id", flat=True)[:limit]

        active_user_list = list(active_users)

        if not active_user_list:
            self.stdout.write(self.style.WARNING("No active users found"))
            return

        self.stdout.write(f"Found {len(active_user_list)} active users")

        total_scores = 0
        users_processed = 0

        for user_id in active_user_list:
            # Get talent vector
            talent_vec = TalentProfileVector.objects.filter(
                talent_user_id=user_id
            ).first()

            if not talent_vec:
                continue

            # Find un-scored jobs
            scored_job_ids = TalentJobScore.objects.filter(
                talent_user_id=user_id,
            ).values_list("job_id", flat=True)

            unscored_jobs = JobVector.objects.exclude(job_id__in=scored_job_ids)

            if not unscored_jobs.exists():
                continue

            users_processed += 1

            if dry_run:
                self.stdout.write(
                    f"  User {user_id[:8]}: {unscored_jobs.count()} un-scored jobs"
                )
                total_scores += unscored_jobs.count()
                continue

            # Score each un-scored job using embedding similarity
            try:
                from sklearn.metrics.pairwise import cosine_similarity
            except ImportError:
                self.stdout.write(self.style.ERROR("scikit-learn not installed"))
                return

            for job_vector in unscored_jobs[:50]:  # limit per user
                if talent_vec.embedding and job_vector.embedding:
                    sim = float(
                        cosine_similarity(
                            [talent_vec.embedding], [job_vector.embedding]
                        )[0][0]
                    )
                    score = round(((sim + 1) / 2) * 100, 1)

                    TalentJobScore.objects.update_or_create(
                        talent_user_id=user_id,
                        job_id=job_vector.job_id,
                        defaults={
                            "score": score,
                            "matched": [],
                            "missing": [],
                            "explanation": {"embedding_similarity": round((sim + 1) / 2, 4)},
                            "talent_profile_hash": talent_vec.profile_hash,
                            "job_data_hash": job_vector.job_data_hash,
                            "scoring_version": SCORING_VERSION,
                        },
                    )
                    total_scores += 1

        elapsed = time.time() - start

        # Update sweep state
        if not dry_run:
            SweepState.objects.update_or_create(
                id=1,
                defaults={
                    "last_sweep_at": timezone.now(),
                    "jobs_scored": total_scores,
                    "users_processed": users_processed,
                },
            )

        self.stdout.write(self.style.SUCCESS(
            f"Nightly batch {'preview' if dry_run else 'complete'}: "
            f"{users_processed} users, {total_scores} scores, {elapsed:.1f}s"
        ))
