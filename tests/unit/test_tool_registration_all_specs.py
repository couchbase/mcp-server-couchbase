"""Spec-agnostic coverage of the tool-gating pipeline, over every server.

``tests/unit/test_tool_registration.py`` covers the operational server in
depth (10 tests, all asserting on its own tool names). Rather than rewrite
those to be name-agnostic, this file adds a small, genuinely spec-generic
set of checks and runs them over every ``ServerSpec`` this distribution
ships — so a second server's tool gating gets covered without touching the
existing file.
"""

import pytest
from _all_specs import ALL_SPECS

from cb_mcp.tool_registration import prepare_tools_for_registration


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.id)
def test_read_only_mode_returns_exactly_the_read_only_tools(spec):
    tools, _, _ = prepare_tools_for_registration(
        spec,
        read_only_mode=True,
        disabled_tools=None,
        confirmation_required_tools=None,
    )
    assert {t.__name__ for t in tools} == spec.tools.read_only_tool_names


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.id)
def test_non_read_only_mode_returns_every_tool(spec):
    tools, _, _ = prepare_tools_for_registration(
        spec,
        read_only_mode=False,
        disabled_tools=None,
        confirmation_required_tools=None,
    )
    assert {t.__name__ for t in tools} == spec.tools.all_tool_names


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda spec: spec.id)
def test_disabling_a_tool_removes_it_and_is_reported(spec):
    target = next(iter(spec.tools.all_tool_names))
    tools, _, disabled = prepare_tools_for_registration(
        spec,
        read_only_mode=False,
        disabled_tools=target,
        confirmation_required_tools=None,
    )
    assert target not in {t.__name__ for t in tools}
    assert disabled == {target}
