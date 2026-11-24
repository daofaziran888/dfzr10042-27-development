"""对比撤回有效范围领域服务。

业务主线：
1. 对比负责人先「领取」本期报告（A、B 两组的实际观察），领取时冻结
   观察范围，作为后续资格判定的快照；
2. 领取后、出件前可以「撤回」某个实验组（例如 A）的有效范围——撤回
   不删除源数据，只登记「后续报告不得再取这些观察」；
3. 报告计算读取撤回有效范围：按资格判定时点（领取时确定 / 报告确认
   前再验证）决定本期报告采用哪些观察，并写出核对记录；
4. 每份报告出一件就登记唯一发布凭证，历史结果可凭凭证对回观察值和
   撤回范围；重复确认不会复制报告或凭证。

只有本服务修改撤回状态、领取范围和报告产物，路由层不做业务判断。
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Iterable

from app.models import (
    ABExperiment,
    AggregationMethod,
    ComparisonClaim,
    ComparisonEligibilityBasis,
    ComparisonReport,
    ComparisonWithdrawScope,
    ExperimentObservation,
    ExperimentResult,
    MetricDataSource,
    MetricDef,
    MetricPeriod,
    PublishCredential,
    WithdrawScopeTransition,
)


class ComparisonScopeError(ValueError):
    """撤回范围/领取/报告状态流转被业务规则拒绝。"""


GROUP_ORDER = ("A", "B")


def _stable_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _sorted_groups(groups: Iterable[str]) -> tuple[str, ...]:
    seen = set(groups)
    ordered = [group for group in GROUP_ORDER if group in seen]
    ordered.extend(sorted(seen - set(GROUP_ORDER)))
    return tuple(ordered)


def _total(values: Iterable[float]) -> float:
    """按实际观察聚合合计；保留一位小数避免浮点展示噪声。"""
    return round(sum(float(value) for value in values), 6)


class ComparisonWithdrawLedger:
    """对比撤销/领取/报告的确定性内存账本。

    账本与 store 分开：store 只存列表展示行，账本存真实观察、领取快照、
    撤回范围和报告凭证，便于测试直接驱动领域逻辑。
    """

    def __init__(self) -> None:
        self.observations: dict[str, ExperimentObservation] = OrderedDict()
        self.scopes: dict[str, ComparisonWithdrawScope] = OrderedDict()
        self.claims: dict[str, ComparisonClaim] = OrderedDict()
        self.reports: dict[str, ComparisonReport] = OrderedDict()
        self.credentials: dict[str, PublishCredential] = OrderedDict()
        self._events: list[dict[str, Any]] = []
        self._clock: int = 0

    # ------------------------------------------------------------------
    # 时钟与事件
    # ------------------------------------------------------------------
    def _tick(self, at: str | None = None) -> str:
        if at:
            return at
        self._clock += 1
        return f"2026-11-24T09:{self._clock // 60:02d}:{self._clock % 60:02d}Z"

    def events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]

    def _record(self, kind: str, at: str, **payload: Any) -> dict[str, Any]:
        event = {"sequence": len(self._events) + 1, "kind": kind, "at": at, **payload}
        self._events.append(event)
        return event

    # ------------------------------------------------------------------
    # 观察（源数据）
    # ------------------------------------------------------------------
    def register_observation(
        self,
        *,
        observation_id: str,
        comparison_id: str,
        experiment_id: str,
        group: str,
        value: float,
        collected_at: str = "",
        source: str = "实验平台",
        status: str = "已完成",
        note: str = "",
    ) -> ExperimentObservation:
        existing = self.observations.get(observation_id)
        observation = ExperimentObservation(
            observation_id=observation_id,
            comparison_id=comparison_id,
            experiment_id=experiment_id,
            group=group,
            value=float(value),
            collected_at=collected_at,
            source=source,
            status=status,
            note=note,
        )
        if existing is not None:
            if existing != observation:
                raise ComparisonScopeError(
                    f"观察 {observation_id} 已登记且内容不同，源数据不可改写")
            return existing
        self.observations[observation_id] = observation
        return observation

    def observations_for(
        self,
        comparison_id: str,
        *,
        experiment_id: str | None = None,
        groups: Iterable[str] | None = None,
        completed_only: bool = True,
    ) -> list[ExperimentObservation]:
        group_set = set(groups) if groups is not None else None
        rows = [
            item for item in self.observations.values()
            if item.comparison_id == comparison_id
            and (experiment_id is None or item.experiment_id == experiment_id)
            and (group_set is None or item.group in group_set)
            and (not completed_only or item.status == "已完成")
        ]
        return rows

    # ------------------------------------------------------------------
    # 撤回有效范围
    # ------------------------------------------------------------------
    def _scopes_for(
        self,
        comparison_id: str,
        *,
        active_only: bool = True,
        experiment_id: str | None = None,
        entry_id: int | None = None,
        scope_id: str | None = None,
    ) -> list[ComparisonWithdrawScope]:
        return [
            scope for scope in self.scopes.values()
            if scope.comparison_id == comparison_id
            and (not active_only or scope.active)
            and (experiment_id is None or scope.experiment_id == experiment_id)
            and (entry_id is None or scope.entry_id == entry_id)
            and (scope_id is None or scope.scope_id == scope_id)
        ]

    def open_scope(
        self,
        *,
        comparison_id: str,
        experiment_id: str,
        group: str,
        observation_ids: Iterable[str],
        entry_id: int = 0,
        actor: str = "对比负责人",
        reason: str = "",
        claim_id: str = "",
        at: str | None = None,
    ) -> ComparisonWithdrawScope:
        """登记一条「生效」的撤回范围；同一观察不会被两条生效范围重复覆盖。"""
        ids = tuple(dict.fromkeys(str(x) for x in observation_ids))
        if not ids:
            raise ComparisonScopeError("撤回范围至少要包含一次观察")
        unknown = [oid for oid in ids if oid not in self.observations]
        if unknown:
            raise ComparisonScopeError(f"撤回观察不存在：{'、'.join(unknown)}")
        wrong_group = [
            oid for oid in ids
            if self.observations[oid].group != group
            or self.observations[oid].comparison_id != comparison_id
        ]
        if wrong_group:
            raise ComparisonScopeError(f"观察不属于{group}组本次对比：{'、'.join(wrong_group)}")
        covered = {
            oid
            for scope in self._scopes_for(comparison_id)
            for oid in scope.observation_ids
        }
        duplicated = [oid for oid in ids if oid in covered]
        if duplicated:
            raise ComparisonScopeError(
                f"观察已在生效撤回范围内：{'、'.join(duplicated)}")

        # 该组全部观察都被撤回时，本期不再有资格；这是允许的，只是报告里
        # 该组会缺席。范围本身按观察集合记录，绝不删除源数据。
        now = self._tick(at)
        sequence = len(self.scopes) + 1
        scope = ComparisonWithdrawScope(
            scope_id=f"WD-{comparison_id}-{sequence:04d}",
            comparison_id=comparison_id,
            experiment_id=experiment_id,
            group=group,
            observation_ids=ids,
            status="生效",
            opened_at=now,
            actor=actor,
            reason=reason,
            claim_id=claim_id,
            sequence=sequence,
            entry_id=entry_id,
            transitions=[
                WithdrawScopeTransition(
                    sequence=0, from_status="未生效", to_status="生效",
                    at=now, actor=actor, reason=reason or "撤回该组有效范围",
                )
            ],
        )
        self.scopes[scope.scope_id] = scope
        self._record(
            "撤回生效", now, scope_id=scope.scope_id, comparison_id=comparison_id,
            experiment_id=experiment_id, group=group, observation_ids=list(ids),
            claim_id=claim_id,
        )
        return scope

    def reopen_scope(
        self,
        scope_id: str,
        *,
        actor: str = "对比负责人",
        reason: str = "恢复该组有效范围",
        at: str | None = None,
    ) -> ComparisonWithdrawScope:
        scope = self.scopes.get(scope_id)
        if scope is None:
            raise ComparisonScopeError(f"撤回范围 {scope_id} 不存在")
        if scope.active:
            raise ComparisonScopeError(f"撤回范围 {scope_id} 仍在生效，无需恢复")
        now = self._tick(at)
        scope.status = "生效"
        scope.transitions.append(WithdrawScopeTransition(
            sequence=len(scope.transitions), from_status="已撤销", to_status="生效",
            at=now, actor=actor, reason=reason,
        ))
        self._record("撤回恢复", now, scope_id=scope_id, actor=actor, reason=reason)
        return scope

    def revoke_scope(
        self,
        scope_id: str,
        *,
        actor: str = "对比负责人",
        reason: str = "撤销撤回，恢复该组有效范围",
        at: str | None = None,
    ) -> ComparisonWithdrawScope:
        """撤销一条撤回范围（观察重新有资格）。状态流转为「已撤销」。"""
        scope = self.scopes.get(scope_id)
        if scope is None:
            raise ComparisonScopeError(f"撤回范围 {scope_id} 不存在")
        if not scope.active:
            raise ComparisonScopeError(f"撤回范围 {scope_id} 已撤销，不能重复撤销")
        now = self._tick(at)
        scope.status = "已撤销"
        scope.transitions.append(WithdrawScopeTransition(
            sequence=len(scope.transitions), from_status="生效", to_status="已撤销",
            at=now, actor=actor, reason=reason,
        ))
        self._record("撤回撤销", now, scope_id=scope_id, actor=actor, reason=reason)
        return scope

    def scope_by_entry(self, entry_id: int) -> ComparisonWithdrawScope | None:
        for scope in self.scopes.values():
            if scope.entry_id == entry_id:
                return scope
        return None

    # ------------------------------------------------------------------
    # 领取
    # ------------------------------------------------------------------
    def _latest_claim(self, comparison_id: str, report_period: str) -> ComparisonClaim | None:
        for claim in reversed(list(self.claims.values())):
            if claim.comparison_id == comparison_id and claim.report_period == report_period:
                return claim
        return None

    def claim(
        self,
        *,
        comparison_id: str,
        report_period: str,
        groups: Iterable[str] = GROUP_ORDER,
        experiment_id: str | None = None,
        basis: ComparisonEligibilityBasis | str = ComparisonEligibilityBasis.CONFIRM,
        actor: str = "对比负责人",
        at: str | None = None,
        claim_id: str | None = None,
    ) -> ComparisonClaim:
        """领取本期报告范围：冻结当时有资格的观察和合计。

        同一期已出件后不能再领取（会重复出件）；已领取未出件时重复领取
        返回原领取记录，不复制快照。
        """
        locked = self._report_for_period(comparison_id, report_period)
        if locked is not None:
            raise ComparisonScopeError(
                f"本期报告 {locked.report_id} 已发布，不能再次领取")
        groups = _sorted_groups(groups)
        existing = self._latest_claim(comparison_id, report_period)
        if existing is not None and not existing.delivered_report_id:
            return existing

        normalized = ComparisonEligibilityBasis.normalize(basis)
        eligible = self._eligible_observations(
            comparison_id, experiment_id=experiment_id, groups=groups,
            active_scope_ids=None,
        )
        if not eligible:
            raise ComparisonScopeError("本期没有已完成的观察可领取")
        totals: dict[str, float] = {}
        for group in groups:
            values = [item.value for item in eligible if item.group == group]
            if values:
                totals[group] = _total(values)
        claimed_total = _total(item.value for item in eligible)
        now = self._tick(at)
        sequence = len(self.claims) + 1
        payload = {
            "comparison_id": comparison_id,
            "report_period": report_period,
            "groups": groups,
            "observation_ids": [item.observation_id for item in eligible],
            "sequence": sequence,
        }
        record = ComparisonClaim(
            claim_id=claim_id or _stable_id("CLM", payload),
            comparison_id=comparison_id,
            report_period=report_period,
            basis=normalized,
            groups=groups,
            observation_ids=tuple(item.observation_id for item in eligible),
            group_totals=totals,
            claimed_total=claimed_total,
            claimed_at=now,
            sequence=sequence,
        )
        self.claims[record.claim_id] = record
        self._record(
            "报告领取", now, claim_id=record.claim_id, comparison_id=comparison_id,
            report_period=report_period, basis=normalized.value,
            observation_ids=list(record.observation_ids), claimed_total=claimed_total,
            actor=actor,
        )
        return record

    # ------------------------------------------------------------------
    # 资格判定
    # ------------------------------------------------------------------
    def _eligible_observations(
        self,
        comparison_id: str,
        *,
        experiment_id: str | None,
        groups: Iterable[str] | None,
        active_scope_ids: set[str] | None,
    ) -> list[ExperimentObservation]:
        """仍未撤回的已完成观察。``active_scope_ids=None`` 表示无撤回。"""
        excluded: set[str] = set()
        for scope in self._scopes_for(comparison_id):
            if active_scope_ids is None or scope.scope_id in active_scope_ids:
                excluded.update(scope.observation_ids)
        return [
            item for item in self.observations_for(
                comparison_id, experiment_id=experiment_id, groups=groups)
            if item.observation_id not in excluded
        ]

    def _report_for_period(self, comparison_id: str, report_period: str) -> ComparisonReport | None:
        for report in self.reports.values():
            if report.comparison_id == comparison_id and report.report_period == report_period:
                return report
        return None

    # ------------------------------------------------------------------
    # 报告实算 + 发布凭证
    # ------------------------------------------------------------------
    def compute_report(
        self,
        *,
        comparison_id: str,
        report_period: str,
        claim_id: str | None = None,
        basis: ComparisonEligibilityBasis | str | None = None,
        revalidate: bool = False,
        experiment_id: str | None = None,
        groups: Iterable[str] | None = None,
        actor: str = "对比负责人",
        at: str | None = None,
    ) -> ComparisonReport:
        """对仍有资格的实验重新计算报告指标。

        - 无领取记录：按当前撤回范围实算，全部采用未撤回观察；
        - 有领取记录：按领取时冻结的 ``basis`` 判定。
          ``claim`` 口径保留领取快照（即使随后撤回）；
          ``confirm`` 口径在出件前再验证，撤回的观察不再采用。
        - 重复确认同一期：返回已发布报告和原凭证，绝不复制。
        """
        locked = self._report_for_period(comparison_id, report_period)
        if locked is not None:
            return locked

        claim = self.claims.get(claim_id) if claim_id else self._latest_claim(
            comparison_id, report_period)
        if claim_id and claim is None:
            raise ComparisonScopeError(f"领取记录 {claim_id} 不存在")
        if claim is not None and claim.delivered_report_id:
            return self.reports[claim.delivered_report_id]

        normalized = ComparisonEligibilityBasis.normalize(
            basis if basis is not None else (claim.basis if claim else ComparisonEligibilityBasis.CONFIRM))
        if claim is not None and normalized != claim.basis:
            raise ComparisonScopeError(
                f"资格判定时点与本报告领取时不一致：领取为 {claim.basis.value}，报告要求 {normalized.value}")
        if claim is not None and revalidate and normalized == ComparisonEligibilityBasis.CONFIRM:
            claim.revalidated = True

        target_groups = _sorted_groups(groups) if groups is not None else (
            claim.groups if claim is not None else GROUP_ORDER)

        active_scopes = self._scopes_for(
            comparison_id, experiment_id=experiment_id)
        active_scope_ids = {scope.scope_id for scope in active_scopes}
        active_excluded = {
            oid for scope in active_scopes for oid in scope.observation_ids
        }

        notes: list[str] = []
        reconciliation: list[dict[str, Any]] = []
        claim_ids: tuple[str, ...] = ()

        if claim is None:
            eligible = self._eligible_observations(
                comparison_id, experiment_id=experiment_id, groups=target_groups,
                active_scope_ids=active_scope_ids,
            )
            used_ids = tuple(item.observation_id for item in eligible)
            excluded_ids = tuple(
                sorted(active_excluded & {
                    item.observation_id
                    for item in self.observations_for(
                        comparison_id, experiment_id=experiment_id, groups=target_groups)
                })
            )
            notes.append("未领取，按当前撤回有效范围实算")
        elif normalized == ComparisonEligibilityBasis.CLAIM:
            eligible = [
                self.observations[oid] for oid in claim.observation_ids
                if oid in self.observations and self.observations[oid].group in target_groups
            ]
            used_ids = tuple(item.observation_id for item in eligible)
            excluded_ids = tuple(
                oid for oid in claim.observation_ids
                if oid in active_excluded
            )
            claim_ids = (claim.claim_id,)
            notes.append("领取时确定资格：采用领取快照，撤回不改变本期报告")
            if excluded_ids:
                notes.append(
                    f"已领范围与撤回生效范围冲突：保留领取合计 {claim.claimed_total:g}")
        else:  # CONFIRM
            claim_ids = (claim.claim_id,)
            if claim.revalidated:
                notes.append("报告确认前再验证：按当前撤回有效范围重算")
            eligible = self._eligible_observations(
                comparison_id, experiment_id=experiment_id, groups=target_groups,
                active_scope_ids=active_scope_ids,
            )
            used_ids = tuple(item.observation_id for item in eligible)
            claim_excluded = set(claim.observation_ids) - set(used_ids)
            excluded_ids = tuple(sorted(claim_excluded | (
                active_excluded & set(claim.observation_ids))))
            notes.append("报告确认前再验证：撤回的观察不进入本期报告")
            if claim_excluded:
                notes.append(
                    f"已领范围与撤回生效范围冲突：本期合计 {_total(i.value for i in eligible):g}，"
                    f"领取时合计 {claim.claimed_total:g}")

        used_set = set(used_ids)
        group_totals: dict[str, float] = {}
        group_means: dict[str, float] = {}
        group_counts: dict[str, int] = {}
        for group in target_groups:
            values = [item.value for item in eligible if item.group == group]
            if not values:
                continue
            group_totals[group] = _total(values)
            group_means[group] = round(sum(values) / len(values), 6)
            group_counts[group] = len(values)

        total = _total(item.value for item in eligible)
        total_n = len(eligible)

        control = [item.value for item in eligible if item.group == "A"]
        treatment = [item.value for item in eligible if item.group == "B"]
        control_mean = round(sum(control) / len(control), 6) if control else None
        treatment_mean = round(sum(treatment) / len(treatment), 6) if treatment else None
        lift = None
        p_value = None
        significant = None
        if control and treatment and control_mean:
            metric = MetricDef(
                metric_id=f"{comparison_id}-合计", metric_name="观察合计",
                calculation_formula="SUM(observation)",
                aggregation=AggregationMethod.SUM,
                data_source=MetricDataSource.CLICKHOUSE,
                period=MetricPeriod.DAILY, owner=actor,
                field_name="value", table_name="experiment_observations",
            )
            analyzed = ExperimentResult(
                report_id=f"__{comparison_id}_{report_period}",
                experiment=ABExperiment(
                    experiment_id=experiment_id or comparison_id,
                    experiment_name=comparison_id,
                    groups={"A": 0.5, "B": 0.5},
                    split_ratio={"A": 0.5, "B": 0.5},
                ),
                metric=metric,
                control_data=control,
                treatment_data=treatment,
            ).analyze()
            lift = analyzed.lift
            p_value = analyzed.p_value
            significant = analyzed.significant

        now = self._tick(at)
        report_id = f"RPT-{comparison_id}-{report_period}"
        version = len([r for r in self.reports.values() if r.comparison_id == comparison_id]) + 1

        # 核对记录：每个被考察的观察一行，说明为什么采用/排除。
        considered = list(dict.fromkeys(
            (claim.observation_ids if claim is not None else ())
            + tuple(
                item.observation_id
                for item in self.observations_for(
                    comparison_id, experiment_id=experiment_id, groups=target_groups)
            )
        ))
        for oid in considered:
            observation = self.observations[oid]
            if observation.group not in target_groups:
                continue
            if oid in used_set:
                decision = "采用"
                reason = "未在撤回生效范围内" if oid not in active_excluded else "按领取快照保留"
            else:
                decision = "排除"
                reason = "撤回生效范围覆盖" if oid in active_excluded else "不在领取范围"
            reconciliation.append({
                "observation_id": oid,
                "experiment_id": observation.experiment_id,
                "group": observation.group,
                "value": observation.value,
                "decision": decision,
                "reason": reason,
                "scope_ids": sorted(
                    scope.scope_id for scope in active_scopes
                    if oid in scope.observation_ids
                ),
            })

        conflict = bool(claim is not None and set(claim.observation_ids) & active_excluded)
        conflict_note = ""
        claim_total_at_claim = claim.claimed_total if claim is not None else None
        if conflict:
            claim.conflict = True
            claim.conflict_note = (
                f"已领范围与撤回生效范围冲突：领取时合计 {claim.claimed_total:g}，"
                f"出件实算合计 {total:g}")
            conflict_note = claim.conflict_note

        credit_payload = {
            "report_id": report_id,
            "comparison_id": comparison_id,
            "report_period": report_period,
            "basis": normalized.value,
            "used_observation_ids": list(used_ids),
            "total": total,
        }
        credential = PublishCredential(
            credential_id=_stable_id("CRD", credit_payload),
            report_id=report_id,
            comparison_id=comparison_id,
            report_period=report_period,
            digest=_digest(credit_payload),
            issued_at=now,
            total=total,
            observation_ids=used_ids,
            basis=normalized,
        )
        report = ComparisonReport(
            report_id=report_id,
            comparison_id=comparison_id,
            report_period=report_period,
            basis=normalized,
            claim_id=claim.claim_id if claim is not None else "",
            used_observation_ids=used_ids,
            excluded_observation_ids=excluded_ids,
            group_totals=group_totals,
            group_means=group_means,
            group_counts=group_counts,
            total=total,
            total_n=total_n,
            control_mean=control_mean,
            treatment_mean=treatment_mean,
            lift=lift,
            p_value=p_value,
            significant=significant,
            eligibility_notes=notes,
            reconciliation=reconciliation,
            withdraw_scope_ids=tuple(sorted(active_scope_ids)),
            claim_total_at_claim=claim_total_at_claim,
            conflict=conflict,
            conflict_note=conflict_note,
            status="已发布",
            version=version,
            created_at=now,
            credential=credential,
        )
        self.reports[report_id] = report
        self.credentials[credential.credential_id] = credential

        if claim is not None:
            claim.status = "已出件"
            claim.delivered_report_id = report_id
            claim.delivered_credential_id = credential.credential_id
            claim.delivered_total = total

        for scope in active_scopes:
            scope.reconciliations.append({
                "report_id": report_id,
                "credential_id": credential.credential_id,
                "claim_id": claim.claim_id if claim is not None else "",
                "basis": normalized.value,
                "used_observation_ids": list(used_ids),
                "excluded_observation_ids": list(excluded_ids),
                "total": total,
                "conflict": conflict,
                "at": now,
            })

        self._record(
            "报告发布", now, report_id=report_id, credential_id=credential.credential_id,
            comparison_id=comparison_id, report_period=report_period,
            basis=normalized.value, total=total,
            used_observation_ids=list(used_ids),
            excluded_observation_ids=list(excluded_ids),
            claim_id=claim.claim_id if claim is not None else "",
            actor=actor,
        )
        return report

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def reports_for(self, comparison_id: str) -> list[ComparisonReport]:
        return [r for r in self.reports.values() if r.comparison_id == comparison_id]

    def claims_for(self, comparison_id: str) -> list[ComparisonClaim]:
        return [c for c in self.claims.values() if c.comparison_id == comparison_id]

    def scopes_for_comparison(self, comparison_id: str) -> list[ComparisonWithdrawScope]:
        return [
            scope for scope in self.scopes.values()
            if scope.comparison_id == comparison_id
        ]

    def credential(self, credential_id: str) -> PublishCredential | None:
        return self.credentials.get(credential_id)

    def reconcile(self, credential_id: str) -> dict[str, Any]:
        """凭发布凭证对回报告实算样本与撤回范围。"""
        credential = self.credentials.get(credential_id)
        if credential is None:
            raise ComparisonScopeError(f"发布凭证 {credential_id} 不存在")
        report = self.reports.get(credential.report_id)
        if report is None:
            raise ComparisonScopeError(f"凭证 {credential_id} 指向的报告不存在")
        observations = [
            self.observations[oid].to_dict() for oid in report.used_observation_ids
        ]
        scopes = [
            scope.to_dict() for scope in self.scopes.values()
            if scope.comparison_id == report.comparison_id
            and report.report_id in scope.report_ids
        ]
        return {
            "credential": credential.to_dict(),
            "report": report.to_dict(),
            "observations": observations,
            "scopes": scopes,
        }

    # ------------------------------------------------------------------
    # 种子
    # ------------------------------------------------------------------
    def seed_default(self) -> None:
        """幂等地装载演示对比：A 组两次有效观察 12、18，B 组 9、11。"""
        if self.observations:
            return
        for group, values in (("A", (12.0, 18.0)), ("B", (9.0, 11.0))):
            for index, value in enumerate(values, start=1):
                self.register_observation(
                    observation_id=f"OBS-AB-{group}{index}",
                    comparison_id="CMP-0001",
                    experiment_id="EXP-0001",
                    group=group,
                    value=value,
                    collected_at="2026-11-20",
                    source="实验平台",
                )


LEDGER = ComparisonWithdrawLedger()
LEDGER.seed_default()
