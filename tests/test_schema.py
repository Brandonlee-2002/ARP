"""
Offline regression tests for account_research.models.schema.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from account_research.extractors.edgar import EdgarFinancials as ExtractorEdgarFinancials
from account_research.models.schema import EdgarFinancials as SchemaEdgarFinancials
from account_research.models.schema import Segment, SegmentSAMEstimate


def test_edgarfinancials_is_re_exported_not_redefined():
    """models.schema.EdgarFinancials must be the SAME class object as
    extractors.edgar.EdgarFinancials — guards the re-export against someone
    'fixing' it into a second, divergent definition."""
    assert SchemaEdgarFinancials is ExtractorEdgarFinancials


def test_segment_enum_has_exactly_four_values():
    assert {s.value for s in Segment} == {"power", "ep", "hpc", "ams"}


def test_segment_sam_estimate_rejects_removed_conn_value():
    with pytest.raises(ValidationError):
        SegmentSAMEstimate(segment="conn")
