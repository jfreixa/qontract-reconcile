from collections.abc import Callable

import pytest

from reconcile.templating.lib.rendering import PatchRenderer, TemplateData
from reconcile.utils.secret_reader import SecretReader


@pytest.mark.parametrize(
    "fixture_file",
    [
        "patch_simple.yaml",
        "patch_updated.yaml",
        "patch_add_newline_list.yaml",
        "patch_overwrite_newline_list.yaml",
        "patch_overwrite.yaml",
        "patch_overwrite_nested.yaml",
        "patch_ref.yaml",
        "patch_jsonpath_identifier.yaml",
        "patch_jsonpath_identifier_update.yaml",
        "patch_list.yaml",
        "patch_path_template.yaml",
    ],
)
def test_patch_ref_update(
    get_fixture: Callable,
    fixture_file: str,
    secret_reader: SecretReader,
) -> None:
    template, current, expected = get_fixture(fixture_file).values()

    r = PatchRenderer(
        template,
        TemplateData(variables={"bar": "bar", "foo": "foo"}, current=current),
        secret_reader=secret_reader,
    )

    assert r.render_condition()
    assert r.render_output().strip() == expected.strip()


@pytest.mark.parametrize(
    "fixture_file",
    [
        "patch_not_rendering.yaml",
        "patch_not_overwrite.yaml",
    ],
)
def test_patch_not_rendering(
    get_fixture: Callable,
    fixture_file: str,
    secret_reader: SecretReader,
) -> None:
    template, current, _ = get_fixture(fixture_file).values()

    r = PatchRenderer(
        template,
        TemplateData(variables={"bar": "bar", "foo": "foo"}, current=current),
        secret_reader=secret_reader,
    )
    assert not r.render_condition()


@pytest.mark.parametrize(
    "fixture_file",
    [
        "patch_wrong_identifier.yaml",
        "patch_missing_identifier.yaml",
    ],
)
def test_patch_raises(
    get_fixture: Callable,
    fixture_file: str,
    secret_reader: SecretReader,
) -> None:
    template, _, _ = get_fixture(fixture_file).values()

    with pytest.raises(ValueError):
        r = PatchRenderer(
            template,
            TemplateData(variables={"bar": "bar", "foo": "foo"}),
            secret_reader=secret_reader,
        )
        r.render_output()
