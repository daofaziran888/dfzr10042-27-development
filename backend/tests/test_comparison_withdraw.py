"""洞察工坊 · 对比撤回有效范围测试

覆盖业务主线：先跑真实实验对比和报告计算，再交错撤回，核对报告实际
采用的观察值、下一次报告的合计，以及发布凭证对回；重复确认不得复制
报告或凭证。
"""
from __future__ import annotations

import pytest

from app.models import ComparisonEligibilityBasis
from app.services.experiment_scope import (
    ComparisonScopeError,
    ComparisonWithdrawLedger,
)

CMP = "CMP-0001"
EXP = "EXP-0001"
PERIOD = "2026-11"
A_IDS = ("OBS-AB-A1", "OBS-AB-A2")
B_IDS = ("OBS-AB-B1", "OBS-AB-B2")


@pytest.fixture
def ledger() -> ComparisonWithdrawLedger:
    book = ComparisonWithdrawLedger()
    book.seed_default()
    return book


class TestBaselineReport:
    def test_source_observations_are_fixed(self, ledger):
        rows = ledger.observations_for(CMP, groups=("A", "B"))
        by_group = {}
        for row in rows:
            by_group.setdefault(row.group, []).append(row.value)
        assert by_group == {"A": [12.0, 18.0], "B": [9.0, 11.0]}

    def test_report_reads_both_groups(self, ledger):
        report = ledger.compute_report(comparison_id=CMP, report_period=PERIOD)
        assert report.total == 50
        assert report.group_totals == {"A": 30, "B": 20}
        assert report.used_observation_ids == A_IDS + B_IDS
        assert report.basis is ComparisonEligibilityBasis.CONFIRM

    def test_read_only_b_group_is_20(self, ledger):
        b_total = sum(
            row.value for row in ledger.observations_for(CMP, groups=("B",)))
        assert b_total == 20


class TestInterleavedWithdraw:
    def test_claim_snapshot_is_50(self, ledger):
        claim = ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="claim")
        assert claim.claimed_total == 50
        assert claim.group_totals == {"A": 30, "B": 20}
        assert set(claim.observation_ids) == set(A_IDS + B_IDS)

    def test_confirm_basis_recomputes_to_20(self, ledger):
        ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="confirm")
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        report = ledger.compute_report(
            comparison_id=CMP, report_period=PERIOD, revalidate=True)
        assert report.total == 20
        assert report.group_totals == {"B": 20}
        assert set(report.used_observation_ids) == set(B_IDS)
        assert set(report.excluded_observation_ids) == set(A_IDS)
        assert report.basis is ComparisonEligibilityBasis.CONFIRM
        assert report.conflict is True
        assert "50" in report.conflict_note and "20" in report.conflict_note
        assert report.claim_total_at_claim == 50

    def test_claim_basis_keeps_50(self, ledger):
        ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="claim")
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        report = ledger.compute_report(comparison_id=CMP, report_period=PERIOD)
        assert report.total == 50
        assert report.group_totals == {"A": 30, "B": 20}
        assert report.conflict is True

    def test_next_report_never_takes_a_again(self, ledger):
        ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="confirm")
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        ledger.compute_report(comparison_id=CMP, report_period=PERIOD, revalidate=True)
        nxt = ledger.compute_report(comparison_id=CMP, report_period="2026-12")
        assert nxt.total == 20
        assert set(nxt.used_observation_ids) == set(B_IDS)
        assert set(nxt.excluded_observation_ids) == set(A_IDS)
        assert nxt.group_totals == {"B": 20}

    def test_b_statistics_and_source_data_survive(self, ledger):
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        ledger.compute_report(comparison_id=CMP, report_period=PERIOD)
        b_rows = ledger.observations_for(CMP, groups=("B",))
        assert [row.value for row in b_rows] == [9.0, 11.0]
        assert sum(row.value for row in b_rows) == 20
        # A 的源观察也仍在，只是不再有资格。
        a_rows = ledger.observations_for(CMP, groups=("A",))
        assert [row.value for row in a_rows] == [12.0, 18.0]


class TestCredentialReconciliation:
    def test_credential_maps_back_to_used_samples(self, ledger):
        ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="confirm")
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        report = ledger.compute_report(
            comparison_id=CMP, report_period=PERIOD, revalidate=True)
        credential = report.credential
        assert credential is not None

        reconciled = ledger.reconcile(credential.credential_id)
        assert reconciled["credential"]["report_id"] == report.report_id
        assert [row["observation_id"] for row in reconciled["observations"]] == list(B_IDS)
        assert reconciled["report"]["total"] == 20
        # 撤回状态流转能对回这份报告和它的凭证。
        assert reconciled["scopes"], "撤回范围应挂上报告对回记录"
        assert reconciled["scopes"][0]["report_ids"] == [report.report_id]
        assert reconciled["scopes"][0]["credential_ids"] == [credential.credential_id]
        transitions = reconciled["scopes"][0]["transitions"]
        assert transitions[-1]["to_status"] == "生效"

    def test_history_result_maps_to_its_own_credential(self, ledger):
        first = ledger.compute_report(comparison_id=CMP, report_period="2026-10")
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        second = ledger.compute_report(comparison_id=CMP, report_period=PERIOD)
        assert first.total == 50
        assert second.total == 20
        assert first.credential.credential_id != second.credential.credential_id
        restored = ledger.reconcile(first.credential.credential_id)
        assert restored["report"]["total"] == 50
        assert set(restored["report"]["used_observation_ids"]) == set(A_IDS + B_IDS)


class TestIdempotence:
    def test_repeat_confirmation_does_not_duplicate(self, ledger):
        ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="confirm")
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        first = ledger.compute_report(
            comparison_id=CMP, report_period=PERIOD, revalidate=True)
        second = ledger.compute_report(
            comparison_id=CMP, report_period=PERIOD, revalidate=True)
        third = ledger.compute_report(
            comparison_id=CMP, report_period=PERIOD, revalidate=True)
        assert first.report_id == second.report_id == third.report_id
        assert (
            first.credential.credential_id
            == second.credential.credential_id
            == third.credential.credential_id
        )
        assert len(ledger.reports_for(CMP)) == 1
        assert len(ledger.credentials) == 1

    def test_one_report_per_period_even_with_new_claim(self, ledger):
        ledger.compute_report(comparison_id=CMP, report_period=PERIOD)
        with pytest.raises(ComparisonScopeError):
            ledger.claim(comparison_id=CMP, report_period=PERIOD)
        again = ledger.compute_report(comparison_id=CMP, report_period=PERIOD)
        assert len(ledger.reports_for(CMP)) == 1

    def test_repeat_claim_returns_same_snapshot(self, ledger):
        first = ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="claim")
        second = ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="confirm")
        assert first.claim_id == second.claim_id
        assert len(ledger.claims_for(CMP)) == 1


class TestScopeLifecycle:
    def test_withdraw_scope_transitions_reconcile(self, ledger):
        ledger.claim(comparison_id=CMP, report_period=PERIOD, basis="confirm")
        scope = ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        assert scope.active
        ledger.compute_report(
            comparison_id=CMP, report_period=PERIOD, revalidate=True)
        ledger.revoke_scope(scope.scope_id, reason="复核后恢复")
        assert not scope.active
        assert [t.to_status for t in scope.transitions] == ["生效", "已撤销"]
        restored = ledger.compute_report(comparison_id=CMP, report_period="2026-12")
        assert restored.total == 50

    def test_cannot_double_withdraw_same_observation(self, ledger):
        ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        with pytest.raises(ComparisonScopeError):
            ledger.open_scope(
                comparison_id=CMP, experiment_id=EXP, group="A",
                observation_ids=A_IDS[:1], reason="重复声明")

    def test_cannot_withdraw_observation_from_other_group(self, ledger):
        with pytest.raises(ComparisonScopeError):
            ledger.open_scope(
                comparison_id=CMP, experiment_id=EXP, group="A",
                observation_ids=B_IDS, reason="组别错配")

    def test_scope_does_not_delete_source_rows(self, ledger):
        scope = ledger.open_scope(
            comparison_id=CMP, experiment_id=EXP, group="A",
            observation_ids=A_IDS, reason="口径不符")
        assert scope.observation_ids == A_IDS
        assert len(ledger.observations_for(CMP)) == 4
