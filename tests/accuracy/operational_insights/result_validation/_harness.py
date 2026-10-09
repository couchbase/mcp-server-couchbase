"""Shared driver for the Operational Insights result-validation files.

A thin re-export of the operational tier's harness: ``assert_result_case``
is entirely server-agnostic (it takes the client factory, agent and judge as
arguments), so there is nothing OI-specific to fork. Importing it through
this module keeps the OI family files reading the same as their operational
counterparts (``from ._harness import assert_result_case``) instead of
reaching across directories.
"""

from __future__ import annotations

from ...result_validation._harness import assert_result_case

__all__ = ["assert_result_case"]
