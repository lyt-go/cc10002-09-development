import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/governance'

const columns = [
  '计划编号',
  '数据表名',
  '分区粒度',
  '预建天数',
  '保留天数',
  '冷热阈值',
  '归档路径',
  '生效日期',
  '冲突原因',
  '状态',
]
const statuses = ['草稿', '已演练', '执行中', '已中断', '已完成', '部分失败']

type Row = Record<string, string | number | null>

type ScopeBucket = { 起始: string; 截止: string; 数量: number; 分区: Array<string | { 分区: string; 归档证据: string }> }
type Scope = {
  基准日期: string
  锚点日期: string
  新建: ScopeBucket
  保留: ScopeBucket
  归档: ScopeBucket
  删除: ScopeBucket
}
type Task = { 分区: string; 操作: string; 结果: string; 说明: string }
type Run = {
  作业编号: string
  作业类型: string
  status: string
  开始时间: string
  结束时间: string
  说明: string
  演练范围: Scope | null
  任务列表: Task[]
  结果统计: Record<string, number>
  演练差异: { 计划外执行?: string[]; 未执行?: string[] }
  成本影响: { 释放存储GB?: number; 估算口径?: string; 联动账单?: string }
}
type PlanDetail = {
  plan: Row
  最近演练: Run | null
  最近作业: Run | null
  作业列表: Run[]
  台账统计: Record<string, number>
  成本影响: { 累计释放存储GB?: number; 账单编号?: string; 当前存储GB?: string; 治理联动?: string }
}
type LedgerRow = Record<string, string | number | null>

export default function GovernanceView() {
  const [searchParams] = useSearchParams()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>(() => {
    const table = searchParams.get('table')
    const initial: Record<string, string> = {}
    if (table) {
      initial['数据表名'] = table
    }
    return initial
  })
  const [detail, setDetail] = useState<PlanDetail | null>(null)
  const [selectedRun, setSelectedRun] = useState<Run | null>(null)
  const [interruptAfter, setInterruptAfter] = useState('')
  const [ledger, setLedger] = useState<LedgerRow[]>([])
  const [evidenceForm, setEvidenceForm] = useState({ 数据表名: '', 分区: '', 归档路径: '' })

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('治理计划列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '治理计划列表读取失败')
    }
  }, [filters])

  const reloadLedger = useCallback(async (table?: string) => {
    try {
      const query = table ? `?table=${encodeURIComponent(table)}` : ''
      const response = await request(`${ENDPOINT}/ledger${query}`)
      if (response.ok) {
        const payload = await response.json()
        setLedger(payload.items ?? [])
      }
    } catch {
      // 台账读取失败不阻断页面
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadLedger()
  }, [reload, reloadLedger])

  const stats = useMemo(
    () => [
      { label: '治理计划总数', value: total },
      { label: '已演练', value: rows.filter((row) => row.status === '已演练').length },
      { label: '执行中/已中断', value: rows.filter((row) => row.status === '执行中' || row.status === '已中断').length },
      { label: '已完成', value: rows.filter((row) => row.status === '已完成').length },
    ],
    [rows, total],
  )

  async function postAction(path: string, body: Record<string, unknown>, fallback: string) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify({ values: body }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || payload.detail || fallback)
      }
      setNotice(payload.message || fallback)
      return payload
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : fallback)
      return null
    }
  }

  async function generatePlans() {
    const payload = await postAction(`${ENDPOINT}/generate`, {}, '治理计划生成失败')
    if (payload) {
      await reload()
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('治理计划详情读取失败')
      }
      const payload = (await response.json()) as PlanDetail
      setDetail(payload)
      setSelectedRun(payload.最近作业 ?? payload.最近演练 ?? null)
      setEvidenceForm((prev) => ({ ...prev, 数据表名: String(row['数据表名'] ?? '') }))
      void reloadLedger(String(row['数据表名'] ?? ''))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '治理计划详情读取失败')
    }
  }

  async function runPlanAction(action: 'dryrun' | 'execute' | 'resume', row: Row) {
    const body: Record<string, unknown> = {}
    if (action === 'execute' && interruptAfter.trim()) {
      body.interrupt_after = interruptAfter.trim()
    }
    const payload = await postAction(`${ENDPOINT}/${row.id}/${action}`, body, '治理动作执行失败')
    if (payload) {
      await reload()
      await openDetail(row)
      void reloadLedger(String(row['数据表名'] ?? ''))
    }
  }

  async function registerEvidence() {
    const payload = await postAction(`${ENDPOINT}/evidence`, evidenceForm, '归档证据登记失败')
    if (payload) {
      setEvidenceForm((prev) => ({ ...prev, 分区: '' }))
      void reloadLedger(evidenceForm.数据表名 || undefined)
    }
  }

  function bucketText(bucket: ScopeBucket | undefined) {
    if (!bucket || bucket.数量 === 0) {
      return '无'
    }
    return `${bucket.起始} ~ ${bucket.截止}`
  }

  const dryrunScope = detail?.最近演练?.演练范围 ?? null

  return (
    <section className="page" data-module="governance">
      <header className="page-head">
        <div>
          <h2>治理计划管理</h2>
          <p className="page-desc">
            由已启用的分区方案生成治理计划，先演练看清将新建、将归档、将删除的范围，再正式执行；
            删除前必须存在已持久化的归档完成证据。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void generatePlans()}>
            生成治理计划
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出治理计划清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>数据表名</span>
          <input
            value={filters['数据表名'] ?? ''}
            placeholder="按数据表名检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, 数据表名: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <input
            value={filters['status'] ?? ''}
            placeholder="草稿、已演练、已完成…"
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{row['计划编号'] ?? '—'}</td>
              <td>{row['数据表名'] ?? '—'}</td>
              <td>{row['分区粒度'] ?? '—'}</td>
              <td>{row['预建天数'] ?? '—'}</td>
              <td>{row['保留天数'] ?? '—'}</td>
              <td>{row['冷热阈值'] ?? '—'}</td>
              <td>{row['归档路径'] ?? '—'}</td>
              <td>{row['生效日期'] ?? '—'}</td>
              <td className={row['冲突原因'] && row['冲突原因'] !== '无' ? 'warn-text' : ''}>
                {row['冲突原因'] ?? '—'}
              </td>
              <td>{row.status ?? '—'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void runPlanAction('dryrun', row)}>演练</button>
                <button className="link" type="button" onClick={() => void runPlanAction('execute', row)}>正式执行</button>
                <button className="link" type="button" onClick={() => void runPlanAction('resume', row)}>续做</button>
                <button className="link" type="button" onClick={() => void openDetail(row)}>详情</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无治理计划，可点击「生成治理计划」由已启用的分区方案生成</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="panel">
          <h3>
            计划 {String(detail.plan['计划编号'])} · {String(detail.plan['数据表名'])} · 第{String(detail.plan['版本'])}版 · {String(detail.plan.status)}
          </h3>
          {detail.plan['冲突原因'] && detail.plan['冲突原因'] !== '无' && (
            <p className="warn-text">冲突原因：{String(detail.plan['冲突原因'])}</p>
          )}

          <h4>演练范围{dryrunScope ? `（基准日期 ${dryrunScope.基准日期}，锚点 ${dryrunScope.锚点日期}）` : '（尚未演练）'}</h4>
          <div className="stat-row">
            <article className="stat-card">
              <span className="stat-label">将新建</span>
              <strong className="stat-value">{dryrunScope?.新建.数量 ?? '—'}</strong>
              <span className="stat-label">{bucketText(dryrunScope?.新建)}</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">可归档</span>
              <strong className="stat-value">{dryrunScope?.归档.数量 ?? '—'}</strong>
              <span className="stat-label">{bucketText(dryrunScope?.归档)}</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">仍保留（热）</span>
              <strong className="stat-value">{dryrunScope?.保留.数量 ?? '—'}</strong>
              <span className="stat-label">{bucketText(dryrunScope?.保留)}</span>
            </article>
            <article className="stat-card">
              <span className="stat-label">将删除</span>
              <strong className="stat-value">{dryrunScope?.删除.数量 ?? '—'}</strong>
              <span className="stat-label">{bucketText(dryrunScope?.删除)}</span>
            </article>
          </div>
          {dryrunScope && dryrunScope.删除.数量 > 0 && (
            <details>
              <summary>删除分区证据核验明细</summary>
              <table className="data-table">
                <thead>
                  <tr><th>分区</th><th>归档证据</th></tr>
                </thead>
                <tbody>
                  {dryrunScope.删除.分区.map((item) => {
                    const entry = typeof item === 'string' ? { 分区: item, 归档证据: '—' } : item
                    return (
                      <tr key={entry.分区}>
                        <td>{entry.分区}</td>
                        <td className={entry.归档证据 === '缺失' ? 'error-text' : 'ok-text'}>{entry.归档证据}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </details>
          )}

          <div className="filter-bar">
            <label className="filter-item">
              <span>模拟中断位置（可选）</span>
              <input
                value={interruptAfter}
                placeholder="第 N 个操作后中断"
                onChange={(event) => setInterruptAfter(event.target.value)}
              />
            </label>
            <button className="btn" type="button" onClick={() => void runPlanAction('dryrun', detail.plan)}>重新演练</button>
            <button className="btn primary" type="button" onClick={() => void runPlanAction('execute', detail.plan)}>正式执行</button>
            <button className="btn" type="button" onClick={() => void runPlanAction('resume', detail.plan)}>从失败分区续做</button>
          </div>

          <h4>作业历史</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>作业编号</th><th>类型</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>结论</th><th>分区结果</th>
              </tr>
            </thead>
            <tbody>
              {detail.作业列表.map((run) => (
                <tr key={run.作业编号}>
                  <td>{run.作业编号}</td>
                  <td>{run.作业类型}</td>
                  <td>{run.status}</td>
                  <td>{run.开始时间}</td>
                  <td>{run.结束时间 || '—'}</td>
                  <td>{run.说明}</td>
                  <td>
                    {run.任务列表.length > 0 && (
                      <button className="link" type="button" onClick={() => setSelectedRun(run)}>查看 {run.任务列表.length} 条</button>
                    )}
                  </td>
                </tr>
              ))}
              {detail.作业列表.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>还没有作业记录，先演练再执行</td></tr>
              )}
            </tbody>
          </table>

          {selectedRun && selectedRun.任务列表.length > 0 && (
            <>
              <h4>作业 {selectedRun.作业编号} 分区结果</h4>
              <table className="data-table">
                <thead>
                  <tr><th>分区</th><th>操作</th><th>结果</th><th>说明</th></tr>
                </thead>
                <tbody>
                  {selectedRun.任务列表.map((task) => (
                    <tr key={`${task.操作}-${task.分区}`}>
                      <td>{task.分区}</td>
                      <td>{task.操作}</td>
                      <td className={task.结果 === '成功' ? 'ok-text' : task.结果 === '待执行' ? '' : 'error-text'}>{task.结果}</td>
                      <td>{task.说明 || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {selectedRun.作业类型 === '正式执行' && (
                <p className="page-desc">
                  演练与实际差异：计划外执行 {selectedRun.演练差异?.计划外执行?.length ?? 0} 项
                  {selectedRun.演练差异?.计划外执行?.length ? `（${selectedRun.演练差异.计划外执行.join('、')}）` : ''}
                  ，未执行 {selectedRun.演练差异?.未执行?.length ?? 0} 项
                  {selectedRun.演练差异?.未执行?.length ? `（${selectedRun.演练差异.未执行.join('、')}）` : ''}
                  ；成本影响：释放 {selectedRun.成本影响?.释放存储GB ?? 0}GB（{selectedRun.成本影响?.估算口径 ?? '—'}），联动账单 {selectedRun.成本影响?.联动账单 ?? '—'}
                </p>
              )}
            </>
          )}

          <p className="page-desc">
            台账统计：新建 {detail.台账统计['新建'] ?? 0} · 归档 {detail.台账统计['归档'] ?? 0} · 删除 {detail.台账统计['删除'] ?? 0}
            ；累计释放 {detail.成本影响['累计释放存储GB'] ?? 0}GB
            ，账单 {detail.成本影响['账单编号'] ?? '—'} 当前存储 {detail.成本影响['当前存储GB'] ?? '—'}GB
            。<Link className="link" to="/cost">前往成本页查看联动</Link>
          </p>
        </section>
      )}

      <section className="panel">
        <h3>归档证据台账（已持久化）</h3>
        <form
          className="filter-bar"
          onSubmit={(event) => { event.preventDefault(); void registerEvidence() }}
        >
          <label className="filter-item">
            <span>数据表名</span>
            <input
              value={evidenceForm.数据表名}
              placeholder="如 DWD_ORDER"
              onChange={(event) => setEvidenceForm((prev) => ({ ...prev, 数据表名: event.target.value }))}
            />
          </label>
          <label className="filter-item">
            <span>分区</span>
            <input
              value={evidenceForm.分区}
              placeholder="dt=YYYY-MM-DD"
              onChange={(event) => setEvidenceForm((prev) => ({ ...prev, 分区: event.target.value }))}
            />
          </label>
          <label className="filter-item">
            <span>归档路径</span>
            <input
              value={evidenceForm.归档路径}
              placeholder="/archive/..."
              onChange={(event) => setEvidenceForm((prev) => ({ ...prev, 归档路径: event.target.value }))}
            />
          </label>
          <button className="btn" type="submit">登记归档完成证据</button>
        </form>
        <table className="data-table">
          <thead>
            <tr>
              <th>证据编号</th><th>数据表名</th><th>分区</th><th>类型</th><th>来源</th><th>作业编号</th><th>归档路径</th><th>完成时间</th>
            </tr>
          </thead>
          <tbody>
            {ledger.map((row) => (
              <tr key={String(row['证据编号'])}>
                <td>{row['证据编号'] ?? '—'}</td>
                <td>{row['数据表名'] ?? '—'}</td>
                <td>{row['分区'] ?? '—'}</td>
                <td>{row['类型'] ?? '—'}</td>
                <td>{row['来源'] ?? '—'}</td>
                <td>{row['作业编号'] || '—'}</td>
                <td>{row['归档路径'] || '—'}</td>
                <td>{row['完成时间'] ?? '—'}</td>
              </tr>
            ))}
            {ledger.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无台账记录</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条治理计划</span>
        {notice && <span className="ok-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
