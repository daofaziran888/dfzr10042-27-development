"""对比撤回有效范围接口：领取、撤回、报告实算、发布凭证对回。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.models import ComparisonEligibilityBasis
from app.services.experiment_scope import LEDGER, ComparisonScopeError

router = APIRouter(prefix="/api/comparison-scope", tags=["对比撤回有效范围"])

COMPARISON_ID = "CMP-0001"


class RegisterObservationPayload(BaseModel):
    observation_id: str
    experiment_id: str = "EXP-0001"
    group: str
    value: float
    collected_at: str = ""
    source: str = "实验平台"
    status: str = "已完成"
    note: str = ""


class OpenScopePayload(BaseModel):
    experiment_id: str = "EXP-0001"
    group: str
    observation_ids: list[str] = Field(default_factory=list)
    actor: str = "对比负责人"
    reason: str = ""
    claim_id: str = ""


class ClaimPayload(BaseModel):
    report_period: str = "2026-11"
    groups: list[str] = Field(default_factory=lambda: ["A", "B"])
    experiment_id: str | None = "EXP-0001"
    basis: str = ComparisonEligibilityBasis.CONFIRM.value
    actor: str = "对比负责人"


class ReportPayload(BaseModel):
    report_period: str = "2026-11"
    claim_id: str | None = None
    basis: str | None = None
    revalidate: bool = False
    groups: list[str] | None = None
    actor: str = "对比负责人"


def _guard(action):
    try:
        return action()
    except ComparisonScopeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _comparison_state(comparison_id: str) -> dict[str, Any]:
    return {
        "comparison_id": comparison_id,
        "observations": [
            item.to_dict() for item in LEDGER.observations_for(
                comparison_id, completed_only=False)
        ],
        "withdraw_scopes": [
            scope.to_dict() for scope in LEDGER.scopes_for_comparison(comparison_id)
        ],
        "claims": [claim.to_dict() for claim in LEDGER.claims_for(comparison_id)],
        "reports": [report.to_dict() for report in LEDGER.reports_for(comparison_id)],
        "credentials": [
            credential.to_dict()
            for credential in LEDGER.credentials.values()
            if credential.comparison_id == comparison_id
        ],
        "events": [
            event for event in LEDGER.events()
            if event.get("comparison_id", comparison_id) == comparison_id
        ],
    }


@router.get("/{comparison_id}")
def get_comparison(comparison_id: str = COMPARISON_ID) -> dict[str, Any]:
    return _comparison_state(comparison_id)


@router.post("/{comparison_id}/observations")
def register_observation(
    payload: RegisterObservationPayload,
    comparison_id: str = COMPARISON_ID,
) -> dict[str, Any]:
    observation = _guard(lambda: LEDGER.register_observation(
        comparison_id=comparison_id, **payload.model_dump()))
    return {"ok": True, "observation": observation.to_dict()}


@router.post("/{comparison_id}/claim")
def claim_report(
    payload: ClaimPayload,
    comparison_id: str = COMPARISON_ID,
) -> dict[str, Any]:
    claim = _guard(lambda: LEDGER.claim(
        comparison_id=comparison_id,
        report_period=payload.report_period,
        groups=payload.groups,
        experiment_id=payload.experiment_id,
        basis=payload.basis,
        actor=payload.actor,
    ))
    return {"ok": True, "claim": claim.to_dict()}


@router.post("/{comparison_id}/withdraw")
def withdraw_scope(
    payload: OpenScopePayload,
    comparison_id: str = COMPARISON_ID,
) -> dict[str, Any]:
    scope = _guard(lambda: LEDGER.open_scope(
        comparison_id=comparison_id,
        entry_id=0,
        **payload.model_dump(),
    ))
    return {"ok": True, "scope": scope.to_dict()}


@router.post("/scopes/{scope_id}/revoke")
def revoke_scope(scope_id: str, actor: str = Query(default="对比负责人")) -> dict[str, Any]:
    scope = _guard(lambda: LEDGER.revoke_scope(scope_id, actor=actor))
    return {"ok": True, "scope": scope.to_dict()}


@router.post("/{comparison_id}/report")
def compute_report(
    payload: ReportPayload,
    comparison_id: str = COMPARISON_ID,
) -> dict[str, Any]:
    report = _guard(lambda: LEDGER.compute_report(
        comparison_id=comparison_id,
        report_period=payload.report_period,
        claim_id=payload.claim_id,
        basis=payload.basis,
        revalidate=payload.revalidate,
        groups=payload.groups,
        actor=payload.actor,
    ))
    return {"ok": True, "report": report.to_dict()}


@router.get("/credentials/{credential_id}")
def get_credential(credential_id: str) -> dict[str, Any]:
    return _guard(lambda: LEDGER.reconcile(credential_id))
