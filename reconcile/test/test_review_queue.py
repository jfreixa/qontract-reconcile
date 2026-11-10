from __future__ import annotations

import pytest

from reconcile.gitlab_housekeeping.review_queue import (
    ReviewQueueEligibility,
    compute_review_queue_mr_eligibility,
    compute_review_queue_pipeline_eligibility,
    compute_review_queue_reviewer_eligibility,
)
from reconcile.utils.mr.labels import (
    AVS,
    HOLD,
    LGTM,
    MERGE_ERROR,
    PENDING_SELF_SERVICE_APPROVAL,
    SAAS_FILE_UPDATE,
    SELF_SERVICEABLE,
    SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE,
    change_owner_label,
)


@pytest.mark.parametrize(
    ("labels", "expected_skip_reason"),
    [
        ([], None),
        ([MERGE_ERROR], None),
        ([LGTM], "already good to merge"),
        ([LGTM, MERGE_ERROR], None),
        (["stale"], "stale"),
        ([SAAS_FILE_UPDATE], "saas file update"),
        ([SAAS_FILE_UPDATE, MERGE_ERROR], None),
        ([HOLD], "bot/hold"),
        ([PENDING_SELF_SERVICE_APPROVAL], "pending self-service approval"),
        ([PENDING_SELF_SERVICE_APPROVAL, MERGE_ERROR], None),
        ([SELF_SERVICEABLE], "self-serviceable, not shown in queue"),
        ([SELF_SERVICEABLE, MERGE_ERROR], None),
        (
            [SELF_SERVICEABLE, SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE],
            None,
        ),
        (
            [
                SELF_SERVICEABLE,
                change_owner_label(SHOW_SELF_SERVICEABLE_IN_REVIEW_QUEUE),
            ],
            None,
        ),
        ([SELF_SERVICEABLE, AVS], None),
    ],
)
def test_compute_review_queue_mr_eligibility_labels(
    labels: list[str], expected_skip_reason: str | None
) -> None:
    eligibility, _ = compute_review_queue_mr_eligibility(
        labels=labels, is_draft=False, has_commits=True, is_unmergeable=False
    )
    assert eligibility.skip_reason == expected_skip_reason
    assert eligibility.needs_review is (expected_skip_reason is None)


@pytest.mark.parametrize(
    ("is_draft", "has_commits", "is_unmergeable", "expected_skip_reason"),
    [
        (True, True, False, "draft"),
        (False, False, False, "no commits"),
        (False, True, True, "unmergeable"),
        (False, True, False, None),
    ],
)
def test_compute_review_queue_mr_eligibility_mr_state(
    is_draft: bool,
    has_commits: bool,
    is_unmergeable: bool,
    expected_skip_reason: str | None,
) -> None:
    eligibility, _ = compute_review_queue_mr_eligibility(
        labels=[],
        is_draft=is_draft,
        has_commits=has_commits,
        is_unmergeable=is_unmergeable,
    )
    assert eligibility.skip_reason == expected_skip_reason


@pytest.mark.parametrize(
    ("labels", "expected_overridden"),
    [
        ([], False),
        ([MERGE_ERROR], False),
        ([LGTM], False),
        ([LGTM, MERGE_ERROR], True),
    ],
)
def test_compute_review_queue_mr_eligibility_overridden_by_error(
    labels: list[str], expected_overridden: bool
) -> None:
    _, overridden_by_error = compute_review_queue_mr_eligibility(
        labels=labels, is_draft=False, has_commits=True, is_unmergeable=False
    )
    assert overridden_by_error is expected_overridden


@pytest.mark.parametrize(
    (
        "has_runnable_pipelines",
        "has_running_pipeline",
        "latest_pipeline_succeeded",
        "overridden_by_error",
        "expected_skip_reason",
    ),
    [
        (False, False, False, False, "no pipelines"),
        (True, True, False, False, "pipeline running"),
        (True, False, False, False, "pipeline not successful"),
        (True, False, False, True, None),
        (True, False, True, False, None),
    ],
)
def test_compute_review_queue_pipeline_eligibility(
    has_runnable_pipelines: bool,
    has_running_pipeline: bool,
    latest_pipeline_succeeded: bool,
    overridden_by_error: bool,
    expected_skip_reason: str | None,
) -> None:
    eligibility = compute_review_queue_pipeline_eligibility(
        has_runnable_pipelines=has_runnable_pipelines,
        has_running_pipeline=has_running_pipeline,
        latest_pipeline_succeeded=latest_pipeline_succeeded,
        overridden_by_error=overridden_by_error,
    )
    assert eligibility.skip_reason == expected_skip_reason
    assert eligibility.needs_review is (expected_skip_reason is None)


@pytest.mark.parametrize(
    (
        "author_is_app_sre",
        "is_assigned_by_app_sre",
        "is_last_action_by_app_sre",
        "last_comment_body",
        "trigger_phrases_regex",
        "overridden_by_error",
        "expected_skip_reason",
    ),
    [
        (True, False, False, None, None, False, "author is appsre"),
        (True, False, False, None, None, True, None),
        (False, True, False, None, None, False, "assigned to appsre"),
        (False, True, False, None, None, True, "assigned to appsre"),
        (
            False,
            False,
            True,
            "please merge",
            r"/lgtm",
            False,
            "awaiting contributor response",
        ),
        (False, False, True, None, r"/lgtm", False, None),
        (False, False, True, "please merge", None, False, None),
        (False, False, True, "/lgtm", r"/lgtm", False, None),
        (False, False, True, "please merge", r"/lgtm", True, None),
        (False, False, False, None, None, False, None),
    ],
)
def test_compute_review_queue_reviewer_eligibility(
    author_is_app_sre: bool,
    is_assigned_by_app_sre: bool,
    is_last_action_by_app_sre: bool,
    last_comment_body: str | None,
    trigger_phrases_regex: str | None,
    overridden_by_error: bool,
    expected_skip_reason: str | None,
) -> None:
    eligibility = compute_review_queue_reviewer_eligibility(
        author_is_app_sre=author_is_app_sre,
        is_assigned_by_app_sre=is_assigned_by_app_sre,
        is_last_action_by_app_sre=is_last_action_by_app_sre,
        last_comment_body=last_comment_body,
        trigger_phrases_regex=trigger_phrases_regex,
        overridden_by_error=overridden_by_error,
    )
    assert eligibility.skip_reason == expected_skip_reason
    assert eligibility.needs_review is (expected_skip_reason is None)


def test_review_queue_eligibility_is_frozen() -> None:
    eligibility = ReviewQueueEligibility(needs_review=True, skip_reason=None)
    with pytest.raises(AttributeError):
        eligibility.needs_review = False  # type: ignore[misc]
