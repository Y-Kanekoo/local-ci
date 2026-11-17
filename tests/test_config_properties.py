"""parse_stages のプロパティベーステスト（hypothesis）。

例示ベースでは拾いにくい「順序・重複・空白への不変性」と
「未知の値では必ず ValueError」を、生成した入力全体で確認する。
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from local_ci.config import STAGES, parse_stages

stage_lists = st.lists(st.sampled_from(STAGES), min_size=1, max_size=len(STAGES))


@given(stage_lists)
def test_parse_stages_accepts_any_combination(stages: list[str]) -> None:
    assert parse_stages(",".join(stages)) == set(stages)


@given(stage_lists)
def test_parse_stages_ignores_order_duplicates_and_spaces(stages: list[str]) -> None:
    noisy = ", ".join([*stages, *stages, stages[0]])
    assert parse_stages(noisy) == set(stages)


@given(st.lists(st.sampled_from(["foo", "bar", "stage1", "prod"]), min_size=1))
def test_parse_stages_rejects_unknown_values(values: list[str]) -> None:
    with pytest.raises(ValueError):
        parse_stages(",".join(values))


def test_parse_stages_empty_input() -> None:
    assert parse_stages(None) is None
    assert parse_stages("") is None
