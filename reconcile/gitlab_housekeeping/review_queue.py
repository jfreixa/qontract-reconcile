from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from reconcile.gitlab_housekeeping.labels import compute_mr_label_status
from reconcile.utils.mr.labels import (
    AVS,
    HOLD,
    PENDING_SELF_SERVICE_APPROVAL,
    SAAS_FILE_UPDATE,
    SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE,
    change_owner_label,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class ReviewQueueEligibility:
    """Whether an open MR belongs in the AppSRE review queue right now, and
    why not when it doesn't. Each compute_review_queue_*_eligibility()
    function below mirrors one stage of app_interface_review_queue's
    historical filter chain - first matching skip reason wins, same as the
    original sequence of `continue` statements - so "why isn't MR X in the
    queue" has one place to look instead of a silently dropped iteration.

    Split into three stages instead of one function because each stage
    gates a progressively more expensive GitLab API call (pipelines, then
    assignment/last-action/comments) that the caller should only make if
    the MR survived the cheaper checks first.
    """

    needs_review: bool
    skip_reason: str | None


def _eligible() -> ReviewQueueEligibility:
    return ReviewQueueEligibility(needs_review=True, skip_reason=None)


def _skip(reason: str) -> ReviewQueueEligibility:
    return ReviewQueueEligibility(needs_review=False, skip_reason=reason)


def compute_review_queue_mr_eligibility(
    *,
    labels: Iterable[str],
    is_draft: bool,
    has_commits: bool,
    is_unmergeable: bool,
) -> tuple[ReviewQueueEligibility, bool]:
    """Stage 1: mr-state and label-only checks, nothing here needs an extra
    GitLab API call beyond what's already been fetched for the MR listing.

    Returns the eligibility verdict plus whether an error label overrides
    an otherwise-blocking label condition - the pipeline and reviewer
    stages need that same override to decide their own skip conditions.
    The override value is meaningless (and never read) when the verdict
    here is already a skip.
    """
    if is_draft:
        return _skip("draft"), False
    if not has_commits:
        return _skip("no commits"), False
    if is_unmergeable:
        return _skip("unmergeable"), False

    label_set = frozenset(labels)
    status = compute_mr_label_status(label_set)
    good_to_merge = status.is_good_to_merge
    has_error_label = status.has_error
    overridden_by_error = good_to_merge and has_error_label

    if good_to_merge and not has_error_label:
        return _skip("already good to merge"), overridden_by_error
    if "stale" in label_set:
        return _skip("stale"), overridden_by_error
    if SAAS_FILE_UPDATE in label_set and not has_error_label:
        return _skip("saas file update"), overridden_by_error
    if HOLD in label_set:
        return _skip("bot/hold"), overridden_by_error
    if PENDING_SELF_SERVICE_APPROVAL in label_set and not has_error_label:
        return _skip("pending self-service approval"), overridden_by_error
    if (
        status.is_self_serviceable
        and SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE not in label_set
        # SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE sourced from a role's
        # change-owners-labels config is bundle-derived, so change-owners
        # correctly wraps it with the "change-owner/" prefix (see
        # rebuild_change_owner_labels()) - unlike the unprefixed form set
        # directly by github-username-sync/ldap-users-api.
        and change_owner_label(SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE) not in label_set
        and AVS not in label_set
        and not has_error_label
    ):
        return _skip("self-serviceable, not shown in queue"), overridden_by_error

    return _eligible(), overridden_by_error


def compute_review_queue_pipeline_eligibility(
    *,
    has_runnable_pipelines: bool,
    has_running_pipeline: bool,
    latest_pipeline_succeeded: bool,
    overridden_by_error: bool,
) -> ReviewQueueEligibility:
    """Stage 2: pipeline-state checks, only worth fetching pipelines for an
    MR that survived stage 1."""
    if not has_runnable_pipelines:
        return _skip("no pipelines")
    if has_running_pipeline:
        return _skip("pipeline running")
    if not latest_pipeline_succeeded and not overridden_by_error:
        return _skip("pipeline not successful")
    return _eligible()


def compute_review_queue_reviewer_eligibility(
    *,
    author_is_app_sre: bool,
    is_assigned_by_app_sre: bool,
    is_last_action_by_app_sre: bool,
    last_comment_body: str | None,
    trigger_phrases_regex: str | None,
    overridden_by_error: bool,
) -> ReviewQueueEligibility:
    """Stage 3: author/assignment/last-action checks, only worth fetching
    for an MR that survived stages 1 and 2."""
    if author_is_app_sre and not overridden_by_error:
        return _skip("author is appsre")
    if is_assigned_by_app_sre:
        return _skip("assigned to appsre")
    if is_last_action_by_app_sre and not overridden_by_error:
        # skip only if the last comment isn't a trigger phrase
        if (
            last_comment_body is not None
            and trigger_phrases_regex is not None
            and not re.fullmatch(trigger_phrases_regex, last_comment_body)
        ):
            return _skip("awaiting contributor response")
    return _eligible()
