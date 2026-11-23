import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  DictCandidate,
  DictConflict,
  EntryRow,
  ImportSummary,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ----------------------------------------------------------------------
// 外部字典导入与候选审核（术语归一专用接口）
// ----------------------------------------------------------------------

export interface DictionaryImportRequest {
  dict_version: string
  source: string
  content?: string
  file_path?: string
  filename?: string
  batch_id?: string
  fail_after_rows?: number
}

export async function importDictionary(payload: DictionaryImportRequest): Promise<ImportSummary> {
  const response = await request('/api/term_normalize/dictionary/import', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error((await response.json()).detail ?? '字典导入失败')
  return await response.json()
}

export async function resumeImport(batchId: string): Promise<ImportSummary> {
  const response = await request(`/api/term_normalize/dictionary/batches/${batchId}/resume`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error((await response.json()).detail ?? '导入恢复失败')
  return await response.json()
}

export async function listCandidates(status?: string): Promise<DictCandidate[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  const response = await request(`/api/term_normalize/dictionary/candidates${query}`)
  if (!response.ok) throw new Error('候选映射读取失败')
  const payload = (await response.json()) as { items: DictCandidate[] }
  return payload.items
}

export async function listConflicts(status?: string): Promise<DictConflict[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  const response = await request(`/api/term_normalize/dictionary/conflicts${query}`)
  if (!response.ok) throw new Error('冲突组读取失败')
  const payload = (await response.json()) as { items: DictConflict[] }
  return payload.items
}

export async function reviewCandidate(
  candidateKey: string,
  action: string,
): Promise<{ ok: boolean; message: string }> {
  const response = await request(
    `/api/term_normalize/dictionary/candidates/${candidateKey}/review`,
    { method: 'POST', body: JSON.stringify({ action }) },
  )
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '候选审核失败')
  return payload
}

export async function resolveConflict(
  conflictKey: string,
  chosenStandard: string,
): Promise<{ ok: boolean; message: string }> {
  const response = await request(
    `/api/term_normalize/dictionary/conflicts/${conflictKey}/resolve`,
    { method: 'POST', body: JSON.stringify({ chosen_standard: chosenStandard }) },
  )
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '冲突裁决失败')
  return payload
}

export async function queryEndpoint(term: string): Promise<Record<string, unknown>> {
  const response = await request(
    `/api/term_normalize/dictionary/endpoint?term=${encodeURIComponent(term)}`,
  )
  if (!response.ok) throw new Error('端点查询失败')
  return await response.json()
}
