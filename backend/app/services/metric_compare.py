"""指标对比领域服务：在通用状态流转之上承载报告领取与撤回有效范围。

对比负责人在「领取」本期报告后、出件前可能撤回某个实验组的有效范围。
领取和撤回的真实数据在 :mod:`app.services.experiment_scope` 的账本里，
这里的动作把状态变化与账本联动：撤回必须挂到具体撤回范围上，恢复
必须撤销一条还在生效的范围，领取/出件不得重复。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.experiment_scope import LEDGER, ComparisonScopeError

COMPARISON_ID = "CMP-0001"
DEFAULT_EXPERIMENT_ID = "EXP-0001"
DEFAULT_PERIOD = "2026-11"


def _period(entry: dict) -> str:
    return str(entry.get("对比周期") or DEFAULT_PERIOD)


class MetricCompareService(BaseModuleService):
    MODULE = "metric_compare"
    ENTITY = "对比方案"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["方案编号", "主指标", "对比指标", "对比周期", "基准值", "当前值", "变化幅度", "对比状态"]
    REQUIRED_FIELDS = ["方案编号", "主指标", "对比指标"]
    STATUS_ORDER = ["待计算", "已领取", "计算中", "已撤回", "已完成", "数据缺失"]
    ACTION_RULES = {
        "启动计算": "计算中",
        "领取报告": "已领取",
        "撤回范围": "已撤回",
        "恢复范围": "已领取",
        "重算对比": "已完成",
        "标记缺失": "数据缺失",
    }
    NEGATIVE_ACTIONS = ["撤回范围", "标记缺失"]

    def _active_scopes(self, entry_id: int):
        return [
            scope for scope in LEDGER.scopes_for_comparison(COMPARISON_ID)
            if scope.active and scope.entry_id == entry_id
        ]

    def run_action(self, entry_id: int, action: str) -> tuple[dict | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "领取报告":
            try:
                claim = LEDGER.claim(
                    comparison_id=COMPARISON_ID,
                    report_period=_period(entry),
                    basis=str(entry.get("资格时点") or "confirm"),
                )
            except ComparisonScopeError as error:
                return None, str(error)
            entry.update({
                "status": "已领取",
                "pending": False,
                "abnormal": False,
                "领取编号": claim.claim_id,
                "资格时点": claim.basis.value,
                "已领合计": claim.claimed_total,
            })
            return entry, f"{self.ENTITY}已领取本期报告，已领合计 {claim.claimed_total:g}"

        if action == "撤回范围":
            if self._active_scopes(entry_id):
                return None, "本期该组已有生效的撤回范围，不能重复撤回"
            pending = LEDGER.observations_for(
                COMPARISON_ID, experiment_id=DEFAULT_EXPERIMENT_ID, groups=("A",))
            try:
                scope = LEDGER.open_scope(
                    comparison_id=COMPARISON_ID,
                    experiment_id=DEFAULT_EXPERIMENT_ID,
                    group="A",
                    observation_ids=[item.observation_id for item in pending],
                    entry_id=entry_id,
                    reason="对比负责人撤回 A 组有效范围",
                )
            except ComparisonScopeError as error:
                return None, str(error)
            entry.update({
                "status": "已撤回",
                "pending": True,
                "abnormal": True,
                "撤回范围": scope.scope_id,
                "撤回组": scope.group,
                "撤回状态": scope.status,
            })
            return entry, f"{self.ENTITY}已撤回 {scope.group} 组有效范围（{scope.scope_id}）"

        if action == "恢复范围":
            scopes = self._active_scopes(entry_id)
            if not scopes:
                return None, "本期没有生效中的撤回范围可恢复"
            scope = LEDGER.revoke_scope(scopes[-1].scope_id, reason="对比负责人恢复该组有效范围")
            entry.update({
                "status": "已领取",
                "pending": False,
                "abnormal": False,
                "撤回状态": scope.status,
            })
            return entry, f"{self.ENTITY}已恢复 {scope.group} 组有效范围（{scope.scope_id}）"

        if action == "重算对比":
            report = LEDGER.compute_report(
                comparison_id=COMPARISON_ID,
                report_period=_period(entry),
                revalidate=True,
                actor="对比负责人",
            )
            scopes = LEDGER.scopes_for_comparison(COMPARISON_ID)
            entry.update({
                "status": "已完成",
                "pending": False,
                "abnormal": False,
                "本次合计": report.total,
                "采用观察": "、".join(report.used_observation_ids),
                "发布凭证": report.credential.credential_id if report.credential else "",
                "撤回状态": "、".join(s.status for s in scopes) or "未撤回",
            })
            return entry, (
                f"{self.ENTITY}已按撤回有效范围出件，合计 {report.total:g}，"
                f"凭证 {entry['发布凭证']}")

        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict:
        snapshot = super().domain_snapshot()
        scopes = LEDGER.scopes_for_comparison(COMPARISON_ID)
        reports = LEDGER.reports_for(COMPARISON_ID)
        snapshot.update({
            "withdraw_active": sum(1 for scope in scopes if scope.active),
            "withdraw_total": len(scopes),
            "reports": len(reports),
            "credentials": len([
                report for report in reports if report.credential is not None
            ]),
            "latest_total": reports[-1].total if reports else 0.0,
        })
        return snapshot
