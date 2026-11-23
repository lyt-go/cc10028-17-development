<template>
  <section class="page" data-module="term-dictionary">
    <header class="page-head">
      <div>
        <h2>外部字典导入</h2>
        <p class="page-desc">
          上传外部词表生成导入术语候选映射；同简称冲突进入待裁决，无冲突候选可独立生效，
          同义词审核后作为关系抽取的新端点。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">候选总数</span>
        <strong class="stat-value">{{ candidates.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待裁决冲突候选</span>
        <strong class="stat-value">{{ countByStatus('待裁决') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已生效候选</span>
        <strong class="stat-value">{{ countByStatus('已生效') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">冲突组</span>
        <strong class="stat-value">{{ conflicts.length }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="handleImport">
      <label class="filter-item">
        <span>字典版本</span>
        <input v-model="form.dict_version" data-testid="dict-version" required placeholder="如 dict-v2026.11" />
      </label>
      <label class="filter-item">
        <span>来源</span>
        <input v-model="form.source" data-testid="dict-source" required placeholder="如 外部机构词表" />
      </label>
      <label class="filter-item">
        <span>字典文件（CSV/JSONL）</span>
        <input data-testid="dict-file" type="file" accept=".csv,.jsonl,.txt" @change="handleFile" />
      </label>
      <button class="btn primary" data-testid="dict-import" type="submit" :disabled="uploading || !form.content">
        {{ uploading ? '导入中…' : '导入字典' }}
      </button>
    </form>

    <div v-if="lastImport" class="stat-card" data-testid="import-summary" style="margin-bottom: 12px">
      <strong>批次 {{ lastImport.batch_id }}</strong>
      <span style="margin-left: 12px">状态：{{ lastImport.status }}</span>
      <span style="margin-left: 12px">进度：{{ lastImport.processed_rows }}/{{ lastImport.total_rows }}</span>
      <span style="margin-left: 12px">无冲突 {{ lastImport.clean_candidates }} 条 / 冲突 {{ lastImport.conflict_candidates }} 条</span>
      <button
        v-if="lastImport.status === 'interrupted'"
        class="btn"
        style="margin-left: 12px"
        data-testid="resume-import"
        type="button"
        @click="handleResume"
      >
        恢复导入
      </button>
    </div>

    <h3>冲突组（同简称指向多个标准词，裁决前旧端点继续可查）</h3>
    <table class="data-table" data-testid="conflict-table">
      <thead>
        <tr>
          <th>简称</th>
          <th>候选标准词</th>
          <th>状态</th>
          <th>裁决选择</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="conflict in conflicts" :key="conflict.conflict_key" :data-status="conflict.status">
          <td>{{ conflict.alias }}</td>
          <td>{{ conflict.candidate_standards.join(' / ') }}</td>
          <td>{{ conflict.status }}</td>
          <td>
            <select
              v-if="conflict.status === '待裁决'"
              :value="verdicts[conflict.conflict_key] ?? conflict.candidate_standards[0]"
              :data-testid="`verdict-${conflict.conflict_key}`"
              @change="setVerdict(conflict.conflict_key, ($event.target as HTMLSelectElement).value)"
            >
              <option v-for="standard in conflict.candidate_standards" :key="standard" :value="standard">
                {{ standard }}
              </option>
            </select>
            <span v-else>{{ conflict.chosen_standard }}</span>
          </td>
          <td>
            <button
              v-if="conflict.status === '待裁决'"
              class="link"
              type="button"
              :data-testid="`resolve-${conflict.conflict_key}`"
              @click="handleResolve(conflict)"
            >
              提交裁决
            </button>
          </td>
        </tr>
        <tr v-if="!conflicts.length">
          <td colspan="5" class="empty-state">暂无冲突组</td>
        </tr>
      </tbody>
    </table>

    <h3 style="margin-top: 16px">导入术语候选映射</h3>
    <table class="data-table" data-testid="candidate-table">
      <thead>
        <tr>
          <th>原词（简称）</th>
          <th>标准术语</th>
          <th>字典版本</th>
          <th>来源</th>
          <th>实体身份</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="candidate in candidates" :key="candidate.candidate_key" :data-status="candidate.status">
          <td>{{ candidate.original_term }}</td>
          <td>{{ candidate.standard_term }}</td>
          <td>{{ candidate.dict_version }}</td>
          <td>{{ candidate.source }}</td>
          <td>{{ candidate.identity_id }}</td>
          <td>{{ candidate.status }}</td>
          <td class="row-actions">
            <button
              v-if="candidate.status === '待生效'"
              class="link"
              type="button"
              :data-testid="`review-${candidate.candidate_key}`"
              @click="handleReview(candidate.candidate_key, '确认生效')"
            >
              确认生效
            </button>
            <button
              v-if="candidate.status === '待生效' || candidate.status === '已生效'"
              class="link"
              type="button"
              @click="handleReview(candidate.candidate_key, '标记废弃')"
            >
              标记废弃
            </button>
          </td>
        </tr>
        <tr v-if="!candidates.length">
          <td colspan="7" class="empty-state">暂无候选映射，请先上传字典文件</td>
        </tr>
      </tbody>
    </table>

    <h3 style="margin-top: 16px">端点查询（争议期间旧端点仍可查询）</h3>
    <form class="filter-bar" @submit.prevent="handleQuery">
      <label class="filter-item">
        <span>术语</span>
        <input v-model="queryTerm" data-testid="endpoint-query-input" placeholder="输入简称或标准词" />
      </label>
      <button class="btn" data-testid="endpoint-query" type="submit">查询端点</button>
    </form>
    <pre v-if="endpointResult" data-testid="endpoint-result" style="background: #fff; padding: 12px; border-radius: 8px">{{ JSON.stringify(endpointResult, null, 2) }}</pre>

    <footer class="page-foot">
      <span v-if="message" class="error-text">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  importDictionary,
  listCandidates,
  listConflicts,
  queryEndpoint,
  resolveConflict,
  resumeImport,
  reviewCandidate,
} from '@/api/backend-service'
import type { DictCandidate, DictConflict, ImportSummary } from '@/data/types'

const candidates = ref<DictCandidate[]>([])
const conflicts = ref<DictConflict[]>([])
const lastImport = ref<ImportSummary | null>(null)
const endpointResult = ref<Record<string, unknown> | null>(null)
const queryTerm = ref('')
const message = ref('')
const uploading = ref(false)
const verdicts = ref<Record<string, string>>({})
const form = ref({ dict_version: '', source: '', content: '', filename: '' })

function countByStatus(status: string): number {
  return candidates.value.filter((item) => item.status === status).length
}

function setVerdict(key: string, value: string): void {
  verdicts.value[key] = value
}

function handleFile(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    form.value.content = String(reader.result ?? '')
    form.value.filename = file.name
  }
  reader.readAsText(file, 'utf-8')
}

async function refresh(): Promise<void> {
  const [candidateRows, conflictRows] = await Promise.all([listCandidates(), listConflicts()])
  candidates.value = candidateRows
  conflicts.value = conflictRows
}

async function handleImport(): Promise<void> {
  message.value = ''
  uploading.value = true
  try {
    lastImport.value = await importDictionary({
      dict_version: form.value.dict_version,
      source: form.value.source,
      content: form.value.content,
      filename: form.value.filename,
    })
    await refresh()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '字典导入失败'
  } finally {
    uploading.value = false
  }
}

async function handleResume(): Promise<void> {
  if (!lastImport.value) return
  message.value = ''
  try {
    lastImport.value = await resumeImport(lastImport.value.batch_id)
    await refresh()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '导入恢复失败'
  }
}

async function handleReview(key: string, action: string): Promise<void> {
  message.value = ''
  try {
    const result = await reviewCandidate(key, action)
    message.value = result.message
    await refresh()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '候选审核失败'
  }
}

async function handleResolve(conflict: DictConflict): Promise<void> {
  message.value = ''
  try {
    const chosen = verdicts.value[conflict.conflict_key] ?? conflict.candidate_standards[0]
    const result = await resolveConflict(conflict.conflict_key, chosen)
    message.value = result.message
    await refresh()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '冲突裁决失败'
  }
}

async function handleQuery(): Promise<void> {
  message.value = ''
  try {
    endpointResult.value = await queryEndpoint(queryTerm.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '端点查询失败'
  }
}

onMounted(refresh)
</script>
