<template>
  <section class="compare-scope" data-testid="comparison-scope-panel">
    <header class="scope-head">
      <div>
        <h3>对比撤回有效范围</h3>
        <p class="scope-desc">
          领取时冻结 A、B 两组观察；撤回只收窄有效范围，不改写 B 的统计与源数据。
        </p>
      </div>
      <button class="btn" type="button" :disabled="loading" @click="reload">刷新范围</button>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div class="scope-grid">
      <article v-for="group in groupCards" :key="group.group" class="scope-card">
        <span class="scope-label">{{ group.group }} 组 {{ group.activeScope ? '（已撤回）' : '' }}</span>
        <strong class="scope-value">{{ group.total }}</strong>
        <span class="scope-meta">{{ group.values.join(' + ') }} · {{ group.count }} 次观察</span>
      </article>
      <article class="scope-card">
        <span class="scope-label">本期报告合计</span>
        <strong class="scope-value">{{ latestReport ? latestReport.total : '—' }}</strong>
        <span class="scope-meta">{{ latestReport ? latestReport.used_observation_ids.join('、') : '尚未出件' }}</span>
      </article>
    </div>

    <div v-if="claims.length" class="scope-block">
      <h4>领取记录</h4>
      <table class="data-table">
        <thead>
          <tr><th>领取编号</th><th>资格时点</th><th>已领合计</th><th>出件合计</th><th>冲突</th></tr>
        </thead>
        <tbody>
          <tr v-for="claim in claims" :key="claim.claim_id">
            <td>{{ claim.claim_id }}</td>
            <td>{{ claim.basis === 'claim' ? '领取时确定' : '报告确认前再验证' }}</td>
            <td>{{ claim.claimed_total }}</td>
            <td>{{ claim.delivered_total ?? '—' }}</td>
            <td>{{ claim.conflict ? claim.conflict_note : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="withdrawScopes.length" class="scope-block">
      <h4>撤回状态流转</h4>
      <table class="data-table">
        <thead>
          <tr><th>范围</th><th>组</th><th>状态</th><th>流转</th><th>对回报告 / 凭证</th></tr>
        </thead>
        <tbody>
          <tr v-for="scope in withdrawScopes" :key="scope.scope_id">
            <td>{{ scope.scope_id }}</td>
            <td>{{ scope.group }}</td>
            <td>{{ scope.status }}</td>
            <td>
              {{ scope.transitions.map((item) => `${item.from_status}→${item.to_status}`).join('；') }}
            </td>
            <td>
              {{ scope.report_ids.join('、') || '—' }} / {{ scope.credential_ids.join('、') || '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="latestReport" class="scope-block">
      <h4>报告核对记录</h4>
      <p v-for="note in latestReport.eligibility_notes" :key="note" class="scope-meta">{{ note }}</p>
      <table class="data-table">
        <thead>
          <tr><th>观察</th><th>组</th><th>值</th><th>判定</th><th>原因</th><th>撤回范围</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in latestReport.reconciliation" :key="item.observation_id">
            <td>{{ item.observation_id }}</td>
            <td>{{ item.group }}</td>
            <td>{{ item.value }}</td>
            <td>{{ item.decision }}</td>
            <td>{{ item.reason }}</td>
            <td>{{ item.scope_ids.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="latestReport.credential" class="scope-meta">
        发布凭证 {{ latestReport.credential.credential_id }} · 摘要
        {{ latestReport.credential.digest }}
        <button class="link" type="button" @click="reconcile(latestReport!.credential!.credential_id)">
          凭凭证对回
        </button>
      </p>
      <p v-if="credentialCheck" class="scope-meta">{{ credentialCheck }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  loadComparisonScope,
  reconcileCredential,
  type Claim,
  type ComparisonReport,
  type ComparisonScopeState,
  type Observation,
  type WithdrawScope,
} from '@/api/comparison-scope'

const props = defineProps<{ comparisonId?: string }>()
const comparisonId = props.comparisonId ?? 'CMP-0001'

const state = ref<ComparisonScopeState | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const credentialCheck = ref('')

const observations = computed<Observation[]>(() => state.value?.observations ?? [])
const claims = computed<Claim[]>(() => state.value?.claims ?? [])
const withdrawScopes = computed<WithdrawScope[]>(() => state.value?.withdraw_scopes ?? [])
const reports = computed<ComparisonReport[]>(() => state.value?.reports ?? [])
const latestReport = computed<ComparisonReport | null>(() => {
  const list = reports.value
  return list.length ? list[list.length - 1] : null
})

const groupCards = computed(() =>
  ['A', 'B'].map((group) => {
    const rows = observations.value.filter((item) => item.group === group)
    return {
      group,
      count: rows.length,
      values: rows.map((item) => item.value),
      total: rows.reduce((sum, item) => sum + item.value, 0),
      activeScope: withdrawScopes.value.some(
        (scope) => scope.group === group && scope.active,
      ),
    }
  }),
)

async function reload() {
  loading.value = true
  errorMessage.value = ''
  credentialCheck.value = ''
  try {
    state.value = await loadComparisonScope(comparisonId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取失败'
  } finally {
    loading.value = false
  }
}

async function reconcile(credentialId: string) {
  errorMessage.value = ''
  try {
    const payload = await reconcileCredential(credentialId)
    credentialCheck.value =
      `凭证 ${payload.credential.credential_id} 对回报告 ${payload.report.report_id}，` +
      `采用 ${payload.observations.map((item) => item.observation_id).join('、')}，` +
      `合计 ${payload.report.total}`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '凭证对回失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.compare-scope {
  margin-top: 24px;
  padding: 16px;
  border: 1px solid var(--border, #d8dde6);
  border-radius: 8px;
}
.scope-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.scope-desc,
.scope-meta {
  color: #5b6472;
  font-size: 12px;
}
.scope-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 12px 0;
}
.scope-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  background: #f7f9fc;
  border-radius: 6px;
}
.scope-label {
  color: #5b6472;
  font-size: 12px;
}
.scope-value {
  font-size: 22px;
}
.scope-block {
  margin-top: 16px;
}
</style>
