import json

import pytest
import responses
from requests import HTTPError

from reconcile.utils.quay_api import (
    QuayApi,
    QuayTeamNotFoundError,
)

ORG = "some-org"
BASE_URL = "some.quay.io"
TEAM_NAME = "some-team"


@pytest.fixture
def quay_api() -> QuayApi:
    return QuayApi("some-token", ORG, base_url=BASE_URL)


@responses.activate
def test_create_or_update_team_default_payload(quay_api: QuayApi) -> None:
    responses.add(
        responses.PUT,
        f"https://{BASE_URL}/api/v1/organization/{ORG}/team/{TEAM_NAME}",
        status=200,
    )

    quay_api.create_or_update_team(TEAM_NAME)

    assert responses.calls[0].request.body is not None
    assert json.loads(responses.calls[0].request.body) == {"role": "member"}


@responses.activate
def test_create_or_update_team_with_description(quay_api: QuayApi) -> None:
    responses.add(
        responses.PUT,
        f"https://{BASE_URL}/api/v1/organization/{ORG}/team/{TEAM_NAME}",
        status=200,
    )

    quay_api.create_or_update_team(TEAM_NAME, description="This is a team")

    assert responses.calls[0].request.body is not None
    assert json.loads(responses.calls[0].request.body) == {
        "role": "member",
        "description": "This is a team",
    }


@responses.activate
def test_create_or_update_team_raises(quay_api: QuayApi) -> None:
    responses.add(
        responses.PUT,
        f"https://{BASE_URL}/api/v1/organization/{ORG}/team/{TEAM_NAME}",
        status=400,
    )

    with pytest.raises(HTTPError):
        quay_api.create_or_update_team(TEAM_NAME)


@responses.activate
def test_list_team_members_raises_team_doesnt_exist(quay_api: QuayApi) -> None:
    responses.add(
        responses.GET,
        f"https://{BASE_URL}/api/v1/organization/{ORG}/team/{TEAM_NAME}/"
        f"members?includePending=true",
        status=404,
    )

    with pytest.raises(QuayTeamNotFoundError):
        quay_api.list_team_members(TEAM_NAME)


@responses.activate
def test_list_team_members_raises_other_status_codes(quay_api: QuayApi) -> None:
    responses.add(
        responses.GET,
        f"https://{BASE_URL}/api/v1/organization/{ORG}/team/{TEAM_NAME}/"
        f"members?includePending=true",
        status=401,
    )

    with pytest.raises(HTTPError):
        quay_api.list_team_members(TEAM_NAME)
