import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/cost'
const GOVERNANCE_ENDPOINT = '/api/governance/impact'
const columns = [
  "账单编号",
  "数据表名",
  "存储大小GB",
  "每日扫描量GB",
  "查询次数",
  "存储成本",
  "计算成本",
  "统计月份"
]
const actions = [
  "生成账单",
  "核对账单",
  "归档账单"
]
const statuses = [
  "草稿",
  "已生成",
  "已核对",
  "已归档"
]
const stats = [
  {
    "label": "成本账单总数",
    "value": 0
  },
  {
    "label": "已核对账单",
    "value": 0
  },
  {
    "label": "本月总成本",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type Impact = {
  数据表名: string
  最近作业编号: string
  最近作业状态: string
  最近结论: string
  累计释放存储GB: number
}

export default function CostView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [impacts, setImpacts] = useState<Record<string, Impact>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('成本账单列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '存储成本列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    // 治理任务的成本联动结论：按数据表名索引，供每行账单引用
    request(GOVERNANCE_ENDPOINT)
      .then((response) => (response.ok ? response.json() : { items: [] }))
      .then((payload: { items?: Impact[] }) => {
        const map: Record<string, Impact> = {}
        for (const item of payload.items ?? []) {
          map[item.数据表名] = item
        }
        setImpacts(map)
      })
      .catch(() => undefined)
  }, [])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('存储成本动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '存储成本操作失败')
    }
  }

  return (
    <section className="page" data-module="cost">
      <header className="page-head">
        <div>
          <h2>存储成本管理</h2>
          <p className="page-desc">追踪各数据表的存储和计算成本，为优化决策提供成本数据支持。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('成本账单登记入口尚未接入审批流')}>
            登记成本账单
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出存储成本清单
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
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>治理联动</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const impact = impacts[String(row['数据表名'] ?? '')]
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>
                  {impact ? (
                    <>
                      <span>
                        {impact.最近作业编号} {impact.最近作业状态}，累计释放 {impact.累计释放存储GB}GB
                      </span>{' '}
                      <Link className="link" to={`/governance?table=${encodeURIComponent(impact.数据表名)}`}>
                        治理计划
                      </Link>
                    </>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无存储成本数据，可先登记成本账单</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条存储成本记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
