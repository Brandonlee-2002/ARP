"""
Pydantic models for the account research schema v1.0.0.
Each model maps 1:1 to the JSON schema defined in the design phase.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class RefreshCadence(str, Enum):
    DAILY     = "daily"
    WEEKLY    = "weekly"
    MONTHLY   = "monthly"
    ON_DEMAND = "on-demand"


class Confidence(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class OwnershipType(str, Enum):
    PUBLIC     = "public"
    PRIVATE    = "private"
    SUBSIDIARY = "subsidiary"
    JV         = "jv"
    GOVERNMENT = "government"


class LocationType(str, Enum):
    HQ           = "hq"
    REGIONAL_HQ  = "regional_hq"
    FAB          = "fab"
    ASSEMBLY     = "assembly"
    RD_CENTER    = "rd_center"
    DESIGN_CENTER= "design_center"
    SALES_OFFICE = "sales_office"
    WAREHOUSE    = "warehouse"


class LocationStatus(str, Enum):
    ACTIVE            = "active"
    UNDER_CONSTRUCTION= "under_construction"
    RAMPING           = "ramping"
    PLANNED           = "planned"
    CLOSED            = "closed"


class RoleType(str, Enum):
    ECONOMIC_BUYER    = "economic_buyer"
    TECHNICAL_BUYER   = "technical_buyer"
    EXECUTIVE_SPONSOR = "executive_sponsor"
    CHAMPION          = "champion"
    INFLUENCER        = "influencer"
    GATEKEEPER        = "gatekeeper"
    END_USER          = "end_user"
    BLOCKER           = "blocker"


class RelationshipStatus(str, Enum):
    COLD     = "cold"
    KNOWN    = "known"
    WARM     = "warm"
    ACTIVE   = "active"
    CHAMPION = "champion"


class ChipBuyerType(str, Enum):
    FABLESS       = "fabless"
    FAB_LITE      = "fab_lite"
    IDC           = "idc"
    EMS           = "ems"
    OEM_BUYER_ONLY= "oem_buyer_only"


class DataQuality(str, Enum):
    AUDITED  = "audited"
    ESTIMATED= "estimated"
    INFERRED = "inferred"


class SignalType(str, Enum):
    OPPORTUNITY = "opportunity"
    RISK        = "risk"
    WATCH       = "watch"


class Urgency(str, Enum):
    IMMEDIATE  = "immediate"
    NEAR_TERM  = "near_term"
    LONG_TERM  = "long_term"


class SupplierStatus(str, Enum):
    NO_RELATIONSHIP  = "no_relationship"
    EVALUATION       = "evaluation"
    APPROVED_VENDOR  = "approved_vendor"
    ACTIVE_ACCOUNT   = "active_account"
    STRATEGIC_PARTNER= "strategic_partner"


# ---------------------------------------------------------------------------
# Meta section
# ---------------------------------------------------------------------------

class SourceRecord(BaseModel):
    name:             str
    url:              str
    fetched_at:       datetime
    fields_populated: list[str] = Field(default_factory=list)


class FinancialDiscrepancy(BaseModel):
    """A single field where the LLM estimate diverges >20% from the EDGAR audited figure."""
    field:    str
    llm_m:    float   # LLM estimate in USD millions
    edgar_m:  float   # EDGAR audited figure in USD millions
    pct_diff: int     # absolute percentage difference, rounded


class ConfidenceMap(BaseModel):
    overall:      Confidence = Confidence.LOW
    overview:     Confidence = Confidence.LOW
    locations:    Confidence = Confidence.LOW
    stakeholders: Confidence = Confidence.LOW
    economics:    Confidence = Confidence.LOW


class Meta(BaseModel):
    schema_version:  str            = "1.0.0"
    record_id:       str            = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at:      datetime       = Field(default_factory=datetime.utcnow)
    refreshed_at:    datetime       = Field(default_factory=datetime.utcnow)
    refresh_cadence: RefreshCadence = RefreshCadence.WEEKLY
    confidence:      ConfidenceMap  = Field(default_factory=ConfidenceMap)
    sources:         list[SourceRecord] = Field(default_factory=list)
    # Verification metadata (populated by RecordAssembler)
    financial_discrepancies: list[FinancialDiscrepancy] = Field(default_factory=list)
    citation_warnings:       int                        = 0


# ---------------------------------------------------------------------------
# Overview section
# ---------------------------------------------------------------------------

class Ticker(BaseModel):
    symbol:   str
    exchange: str
    alt_otc:  Optional[str] = None


class EmployeeCount(BaseModel):
    value:       int
    approximate: bool = True
    as_of_year:  Optional[int] = None


class Overview(BaseModel):
    legal_name:         str
    doing_business_as:  Optional[str]         = None
    ticker:             Optional[Ticker]       = None
    ownership_type:     OwnershipType          = OwnershipType.PRIVATE
    parent_company:     Optional[str]          = None
    founded_year:       Optional[int]          = None
    website:            str
    industry_verticals: list[str]              = Field(default_factory=list)
    employee_count:     Optional[EmployeeCount]= None
    summary:            str                    = ""
    competitors:        list[str]              = Field(default_factory=list)
    key_products:       list[str]              = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Locations section
# ---------------------------------------------------------------------------

class Location(BaseModel):
    type:         LocationType
    name:         Optional[str]           = None
    address:      Optional[str]           = None
    city:         str
    state_region: Optional[str]           = None
    country:      str
    country_code: str                     = ""
    status:       LocationStatus          = LocationStatus.ACTIVE
    headcount:    Optional[int]           = None
    notes:        Optional[str]           = None


# ---------------------------------------------------------------------------
# Stakeholders section
# ---------------------------------------------------------------------------

class Stakeholder(BaseModel):
    name:                  str
    title:                 str
    department:            Optional[str]            = None
    location:              Optional[str]            = None
    role_type:             RoleType
    role_type_secondary:   list[RoleType]           = Field(default_factory=list)
    influence_score:       float                    = Field(ge=0.0, le=1.0)
    relationship_status:   RelationshipStatus       = RelationshipStatus.COLD
    linkedin_url:          Optional[str]            = None
    email:                 Optional[str]            = None
    notes:                 Optional[str]            = None
    last_contacted:        Optional[date]           = None


# ---------------------------------------------------------------------------
# Economics section
# ---------------------------------------------------------------------------

class Financials(BaseModel):
    fiscal_year:            Optional[int]         = None
    currency:               str                   = "USD"
    revenue_usd_m:          Optional[float]       = None
    revenue_growth_pct:     Optional[float]       = None
    gross_margin_pct:       Optional[float]       = None
    operating_margin_pct:   Optional[float]       = None
    net_income_usd_m:       Optional[float]       = None
    rd_spend_usd_m:         Optional[float]       = None
    rd_pct_revenue:         Optional[float]       = None
    capex_usd_m:            Optional[float]       = None
    market_cap_usd_m:       Optional[float]       = None
    data_quality:           DataQuality           = DataQuality.ESTIMATED


class RevenueSegment(BaseModel):
    name:           str
    revenue_usd_m:  float
    pct_of_total:   float
    growth_pct:     Optional[float] = None
    relevant_to_us: bool            = False


class SemiconductorProfile(BaseModel):
    chip_buyer_type:              Optional[ChipBuyerType] = None
    has_own_fab:                  bool                    = False
    fab_nodes:                    list[str]               = Field(default_factory=list)
    known_chip_suppliers:         list[str]               = Field(default_factory=list)
    known_supply_gaps:            list[str]               = Field(default_factory=list)
    design_win_notes:             Optional[str]           = None
    qualification_cycle:          Optional[str]           = None
    annual_chip_spend_est_usd_m:  Optional[float]         = None


class Economics(BaseModel):
    financials:           Financials             = Field(default_factory=Financials)
    revenue_segments:     list[RevenueSegment]   = Field(default_factory=list)
    semiconductor_profile:SemiconductorProfile   = Field(default_factory=SemiconductorProfile)


# ---------------------------------------------------------------------------
# Account intelligence section
# ---------------------------------------------------------------------------

class OpportunitySignal(BaseModel):
    type:        SignalType
    description: str
    urgency:     Urgency
    source:      Optional[str] = None
    cited:       bool          = False  # True when source is a non-empty URL


class AccountIntelligence(BaseModel):
    opportunity_signals:      list[OpportunitySignal] = Field(default_factory=list)
    recommended_next_action:  Optional[str]           = None
    current_supplier_status:  SupplierStatus          = SupplierStatus.NO_RELATIONSHIP


# ---------------------------------------------------------------------------
# News section
# ---------------------------------------------------------------------------

class NewsItem(BaseModel):
    date:        date
    headline:    str
    summary:     Optional[str] = None
    source_name: str
    url:         str
    tags:        list[str]     = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Root record
# ---------------------------------------------------------------------------

class AccountResearchRecord(BaseModel):
    meta:                Meta
    overview:            Overview
    locations:           list[Location]           = Field(default_factory=list)
    stakeholders:        list[Stakeholder]        = Field(default_factory=list)
    economics:           Economics                = Field(default_factory=Economics)
    account_intelligence:AccountIntelligence      = Field(default_factory=AccountIntelligence)
    news:                list[NewsItem]           = Field(default_factory=list)

    class Config:
        use_enum_values = True