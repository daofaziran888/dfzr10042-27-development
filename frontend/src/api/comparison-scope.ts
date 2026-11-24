/** 对比撤回有效范围的接口封装：领取、撤回、报告实算和发布凭证对回。 */
import { request } from './client'

export type Observation = {
  observation_id: string
  comparison_id: string
  experiment_id: string
  group: string
  value: number
  collected_at: string
  source: string
  status: string
  note: string
}

export type ScopeTransition = {
  sequence: number
  from_status: string
  to_status: string
  at: string
  actor: string
  reason: string
}

export type WithdrawScope = {
  scope_id: string
  comparison_id: string
  experiment_id: string
  group: string
  observation_ids: string[]
  status: string
  active: boolean
  opened_at: string
  actor: string
  reason: string
  claim_id: string
  transitions: ScopeTransition[]
  reconciliations: {
    report_id: string
    credential_id: string
    claim_id: string
    basis: string
    used_observation_ids: string[]
    excluded_observation_ids: string[]
    total: number
    conflict: boolean
    at: string
  }[]
  report_ids: string[]
  credential_ids: string[]
}

export type Claim = {
  claim_id: string
  comparison_id: string
  report_period: string
  basis: string
  groups: string[]
  observation_ids: string[]
  group_totals: Record<string, number>
  claimed_total: number
  claimed_at: string
  status: string
  revalidated: boolean
  conflict: boolean
  conflict_note: string
  delivered_report_id: string
  delivered_credential_id: string
  delivered_total: number | null
}

export type Credential = {
  credential_id: string
  report_id: string
  comparison_id: string
  report_period: string
  digest: string
  issued_at: string
  total: number
  observation_ids: string[]
  basis: string
}

export type ComparisonReport = {
  report_id: string
  comparison_id: string
  report_period: string
  basis: string
  claim_id: string
  used_observation_ids: string[]
  excluded_observation_ids: string[]
  group_totals: Record<string, number>
  group_means: Record<string, number>
  group_counts: Record<string, number>
  total: number
  total_n: number
  control_mean: number | null
  treatment_mean: number | null
  lift: number | null
  p_value: number | null
  significant: boolean | null
  eligibility_notes: string[]
  reconciliation: {
    observation_id: string
    experiment_id: string
    group: string
    value: number
    decision: string
    reason: string
    scope_ids: string[]
  }[]
  withdraw_scope_ids: string[]
  claim_total_at_claim: number | null
  conflict: boolean
  conflict_note: string
  status: string
  version: number
  created_at: string
  credential: Credential | null
}

export type ComparisonScopeState = {
  comparison_id: string
  observations: Observation[]
  withdraw_scopes: WithdrawScope[]
  claims: Claim[]
  reports: ComparisonReport[]
  credentials: Credential[]
  events: { sequence: number; kind: string; at: string; [key: string]: unknown }[]
}

export async function loadComparisonScope(comparisonId: string): Promise<ComparisonScopeState> {
  const response = await request(`/api/comparison-scope/${encodeURIComponent(comparisonId)}`)
  if (!response.ok) throw new Error('对比撤回有效范围读取失败')
  return (await response.json()) as ComparisonScopeState
}

export async function reconcileCredential(credentialId: string): Promise<{
  credential: Credential
  report: ComparisonReport
  observations: Observation[]
  scopes: WithdrawScope[]
}> {
  const response = await request(
    `/api/comparison-scope/credentials/${encodeURIComponent(credentialId)}`,
  )
  if (!response.ok) throw new Error('发布凭证对回失败')
  return await response.json()
}
