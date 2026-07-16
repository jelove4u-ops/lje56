import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
} from 'recharts'
import {
  RefreshCw,
  Flame,
  Award,
  PieChart,
  TrendingUp,
  BarChart3,
  Sparkles,
  Bell,
  Download,
  AlertTriangle,
  Inbox,
} from 'lucide-react'

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const REFRESH_INTERVAL_MS = 20000
const APPLIANCE_KEYWORDS = ['냉장고', '세탁기', '에어컨', '공기청정기', 'TV']

// Recharts writes these straight onto SVG presentation attributes, so they need
// literal hex values rather than the CSS custom properties defined in index.css.
const CHART_COLORS = {
  grid: '#2e2822',
  axisLine: '#3a322b',
  tickMuted: '#8f8479',
  tickSoft: '#c9beb6',
  brandRed: '#e23a54',
  competitorBlue: '#1e86c4',
}

function parseGroupName(groupName) {
  const [source, category, ...rest] = (groupName || '').split(':')
  return { source, category, keyword: rest.join(':') }
}

async function fetchJson(path) {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) {
    throw new Error(`${path} 요청 실패 (HTTP ${res.status})`)
  }
  return res.json()
}

function buildTopCategory(trends) {
  if (!trends.length) return null
  const latestDate = trends.reduce((max, t) => (t.date > max ? t.date : max), trends[0].date)
  const totals = {}
  trends
    .filter((t) => t.date === latestDate)
    .forEach((t) => {
      const { category } = parseGroupName(t.group_name)
      totals[category] = (totals[category] || 0) + t.ratio
    })
  const entries = Object.entries(totals)
  if (!entries.length) return null
  const grandTotal = entries.reduce((sum, [, v]) => sum + v, 0)
  const [name, value] = entries.sort((a, b) => b[1] - a[1])[0]
  return { name, share: grandTotal > 0 ? Math.round((value / grandTotal) * 1000) / 10 : 0 }
}

function buildLineChartData(trends) {
  const byDate = {}
  trends.forEach((t) => {
    const { keyword } = parseGroupName(t.group_name)
    if (keyword !== 'LG 냉장고' && keyword !== '삼성 냉장고') return
    byDate[t.date] ??= { date: t.date }
    byDate[t.date][keyword === 'LG 냉장고' ? 'LG' : '경쟁사'] = t.ratio
  })
  return Object.values(byDate)
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(-30)
}

function buildApplianceShare(trends) {
  const byKeywordDate = {}
  trends.forEach((t) => {
    const { keyword } = parseGroupName(t.group_name)
    if (!APPLIANCE_KEYWORDS.includes(keyword)) return
    byKeywordDate[keyword] ??= {}
    byKeywordDate[keyword][t.date] ??= []
    byKeywordDate[keyword][t.date].push(t.ratio)
  })

  const latestPerKeyword = Object.entries(byKeywordDate).map(([keyword, dateMap]) => {
    const dates = Object.keys(dateMap).sort()
    const latestDate = dates[dates.length - 1]
    const values = dateMap[latestDate] || []
    const avg = values.reduce((sum, v) => sum + v, 0) / (values.length || 1)
    return { keyword, avg }
  })

  const total = latestPerKeyword.reduce((sum, k) => sum + k.avg, 0)
  return latestPerKeyword
    .map((k) => ({ keyword: k.keyword, share: total > 0 ? Math.round((k.avg / total) * 1000) / 10 : 0 }))
    .sort((a, b) => b.share - a.share)
}

function downloadCsv(rows) {
  const header = ['키워드', '카테고리', '최근 3일 평균', '급증 지수']
  const lines = [
    header.join(','),
    ...rows.map((r) => [r.keyword, r.category, r.recentAvg, r.spikeScore].join(',')),
  ]
  // Leading BOM so Excel opens the UTF-8 Korean text correctly.
  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `lcoup-direct-keywords-${new Date().toISOString().slice(0, 10)}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-charcoal-600 bg-charcoal-800 px-3 py-2 text-xs shadow-lg">
      <p className="mb-1 text-cream-500">{label}</p>
      {payload.map((p) => (
        <p key={p.dataKey} className="flex items-center gap-2" style={{ color: p.color }}>
          <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: p.color }} />
          {p.name}: {typeof p.value === 'number' ? p.value.toFixed(1) : p.value}
        </p>
      ))}
    </div>
  )
}

function StatCard({ icon: Icon, label, value, sublabel }) {
  return (
    <div className="flex items-start gap-4 rounded-2xl border border-charcoal-700 bg-charcoal-900 p-5">
      <div className="shrink-0 rounded-xl bg-brand-red-soft p-3 text-brand-red">
        <Icon className="h-5 w-5" />
      </div>
      <div className="min-w-0">
        <p className="text-sm text-cream-500">{label}</p>
        <p className="mt-1 truncate text-2xl font-bold text-cream-50">{value}</p>
        {sublabel && <p className="mt-1 text-xs text-cream-500">{sublabel}</p>}
      </div>
    </div>
  )
}

function Card({ title, icon: Icon, action, children }) {
  return (
    <div className="rounded-2xl border border-charcoal-700 bg-charcoal-900 p-5">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="h-4 w-4 text-brand-red" />}
          <h3 className="font-semibold text-cream-50">{title}</h3>
        </div>
        {action}
      </div>
      {children}
    </div>
  )
}

function EmptyState({ text }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-charcoal-700 py-10 text-center">
      <Inbox className="h-6 w-6 text-cream-500" />
      <p className="text-xs text-cream-500">{text}</p>
    </div>
  )
}

const STATUS_META = {
  checking: { label: '연결 확인 중', dotClass: 'bg-cream-500' },
  connected: { label: '실시간 동기화', dotClass: 'bg-status-good' },
  error: { label: '연결 끊김', dotClass: 'bg-status-critical' },
}

export default function App() {
  const [trends, setTrends] = useState([])
  const [spikes, setSpikes] = useState([])
  const [compare, setCompare] = useState(null)
  const [alerts, setAlerts] = useState([])
  const [status, setStatus] = useState('checking')
  const [error, setError] = useState(null)
  const [lastSyncedAt, setLastSyncedAt] = useState(null)
  const [isRefreshing, setIsRefreshing] = useState(false)

  const refreshAll = useCallback(async () => {
    setIsRefreshing(true)
    try {
      const [trendsRes, spikesRes, compareRes, alertsRes] = await Promise.all([
        fetchJson('/api/trends'),
        fetchJson('/api/analysis/spikes'),
        fetchJson('/api/analysis/compare'),
        fetchJson('/api/alerts/pending'),
      ])
      setTrends(trendsRes)
      setSpikes(spikesRes.spikes || [])
      setCompare(compareRes)
      setAlerts(alertsRes.alerts || [])
      setStatus('connected')
      setError(null)
      setLastSyncedAt(new Date())
    } catch (err) {
      setStatus('error')
      setError(err.message || '백엔드 연결에 실패했습니다.')
    } finally {
      setIsRefreshing(false)
    }
  }, [])

  useEffect(() => {
    refreshAll()
    const id = setInterval(refreshAll, REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [refreshAll])

  const topCategory = useMemo(() => buildTopCategory(trends), [trends])
  const lineChartData = useMemo(() => buildLineChartData(trends), [trends])
  const applianceShare = useMemo(() => buildApplianceShare(trends), [trends])

  const recommended = useMemo(
    () =>
      spikes.map((s) => {
        const { keyword, category } = parseGroupName(s.group_name)
        return { keyword, category, recentAvg: s.recent_avg, spikeScore: s.spike_score }
      }),
    [spikes],
  )

  const lgShare = useMemo(() => {
    const entry = compare?.comparison?.find((c) => c.keyword.includes('LG'))
    return entry?.share_pct ?? null
  }, [compare])

  const statusMeta = STATUS_META[status]

  return (
    <div className="min-h-screen bg-charcoal-950 text-cream-50">
      <header className="sticky top-0 z-10 border-b border-charcoal-700 bg-charcoal-950/90 px-4 py-4 backdrop-blur sm:px-6">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-red font-bold text-white">
              L
            </div>
            <div>
              <h1 className="text-lg font-semibold leading-tight text-cream-50">L-Coup Direct</h1>
              <p className="text-xs text-cream-500">가전 트렌드 인텔리전스</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 rounded-full border border-charcoal-700 bg-charcoal-900 px-3 py-1.5">
              <span className={`h-2 w-2 rounded-full ${statusMeta.dotClass} ${status === 'connected' ? 'animate-pulse' : ''}`} />
              <span className="text-xs font-medium text-cream-300">{statusMeta.label}</span>
              {lastSyncedAt && (
                <span className="hidden text-xs text-cream-500 sm:inline">
                  · {lastSyncedAt.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={refreshAll}
              disabled={isRefreshing}
              className="inline-flex items-center gap-1.5 rounded-full border border-charcoal-700 bg-charcoal-900 p-2 text-cream-300 transition-colors hover:border-brand-red hover:text-brand-red disabled:opacity-50"
              aria-label="새로고침"
            >
              <RefreshCw className={`h-4 w-4 ${isRefreshing ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-8 px-4 py-8 sm:px-6">
        {error && (
          <div className="flex items-center gap-2 rounded-xl border border-status-critical/40 bg-status-critical/10 px-4 py-3 text-sm text-status-critical">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>
              {error} — 백엔드({API_BASE})가 실행 중인지, POST /api/sync로 데이터를 적재했는지 확인하세요.
            </span>
          </div>
        )}

        {/* Section 1: 핵심 지표 카드 */}
        <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <StatCard
            icon={PieChart}
            label="가전 클릭 점유율 Top 카테고리"
            value={topCategory ? topCategory.name : '데이터 없음'}
            sublabel={topCategory ? `전체 검색 지수의 ${topCategory.share}%` : 'POST /api/sync로 데이터를 적재하세요'}
          />
          <StatCard
            icon={Flame}
            label="오늘 감지된 급상승 키워드"
            value={`${spikes.length}건`}
            sublabel="최근 3일 평균이 14일 baseline 대비 2σ 이상 급증"
          />
          <StatCard
            icon={Award}
            label="LG 브랜드 점유율 스코어"
            value={lgShare !== null ? `${lgShare}%` : '데이터 없음'}
            sublabel="LG 냉장고 vs 삼성 냉장고, 최근 30일 기준"
          />
        </section>

        {/* Section 2: 차트 영역 */}
        <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card title="LG vs 경쟁사 검색 트렌드 (최근 30일)" icon={TrendingUp}>
            {lineChartData.length === 0 ? (
              <EmptyState text="LG/경쟁사 트렌드 데이터가 아직 없습니다." />
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <LineChart data={lineChartData} margin={{ top: 4, right: 12, left: -12, bottom: 0 }}>
                  <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="3 3" vertical={false} />
                  <XAxis
                    dataKey="date"
                    tick={{ fill: CHART_COLORS.tickMuted, fontSize: 11 }}
                    tickLine={false}
                    axisLine={{ stroke: CHART_COLORS.axisLine }}
                    tickFormatter={(d) => d.slice(5)}
                  />
                  <YAxis tick={{ fill: CHART_COLORS.tickMuted, fontSize: 11 }} tickLine={false} axisLine={false} width={36} />
                  <Tooltip content={<ChartTooltip />} />
                  <Legend wrapperStyle={{ fontSize: 12, color: CHART_COLORS.tickSoft }} />
                  <Line type="monotone" dataKey="LG" name="LG" stroke={CHART_COLORS.brandRed} strokeWidth={2} dot={false} connectNulls />
                  <Line
                    type="monotone"
                    dataKey="경쟁사"
                    name="경쟁사"
                    stroke={CHART_COLORS.competitorBlue}
                    strokeWidth={2}
                    dot={false}
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </Card>

          <Card title="세부 가전군 클릭 점유율" icon={BarChart3}>
            {applianceShare.length === 0 ? (
              <EmptyState text="가전군별 트렌드 데이터가 아직 없습니다." />
            ) : (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={applianceShare} layout="vertical" margin={{ top: 4, right: 24, left: 8, bottom: 0 }}>
                  <CartesianGrid stroke={CHART_COLORS.grid} strokeDasharray="3 3" horizontal={false} />
                  <XAxis
                    type="number"
                    unit="%"
                    tick={{ fill: CHART_COLORS.tickMuted, fontSize: 11 }}
                    tickLine={false}
                    axisLine={{ stroke: CHART_COLORS.axisLine }}
                  />
                  <YAxis
                    type="category"
                    dataKey="keyword"
                    tick={{ fill: CHART_COLORS.tickSoft, fontSize: 12 }}
                    tickLine={false}
                    axisLine={false}
                    width={72}
                  />
                  <Tooltip content={<ChartTooltip />} />
                  <Bar dataKey="share" name="점유율" fill={CHART_COLORS.brandRed} radius={[0, 4, 4, 0]} barSize={18} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Card>
        </section>

        {/* Section 3: 액션 센터 */}
        <section className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <Card
              title="쿠팡 상품명 등록 추천 키워드"
              icon={Sparkles}
              action={
                <button
                  type="button"
                  onClick={() => downloadCsv(recommended)}
                  disabled={recommended.length === 0}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-charcoal-600 px-3 py-1.5 text-xs font-medium text-cream-300 transition-colors hover:border-brand-red hover:text-brand-red disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Download className="h-3.5 w-3.5" />
                  CSV 다운로드
                </button>
              }
            >
              {recommended.length === 0 ? (
                <EmptyState text="현재 감지된 급상승 키워드가 없습니다." />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-charcoal-700 text-left text-cream-500">
                        <th className="py-2 pr-4 font-medium">키워드</th>
                        <th className="py-2 pr-4 font-medium">카테고리</th>
                        <th className="py-2 pr-4 text-right font-medium">최근 3일 평균</th>
                        <th className="py-2 text-right font-medium">급증 지수</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recommended.map((r) => (
                        <tr key={`${r.category}-${r.keyword}`} className="border-b border-charcoal-800 last:border-0">
                          <td className="py-2.5 pr-4 font-medium text-cream-50">{r.keyword}</td>
                          <td className="py-2.5 pr-4 text-cream-500">{r.category}</td>
                          <td className="py-2.5 pr-4 text-right tabular-nums text-cream-300">{r.recentAvg}</td>
                          <td className="py-2.5 text-right tabular-nums">
                            <span className="inline-flex items-center rounded-full bg-brand-red-soft px-2 py-0.5 text-xs font-semibold text-brand-red">
                              x{r.spikeScore}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>

          <Card title="카카오 MCP 알림 브릿지" icon={Bell}>
            <div className="mb-4 flex items-start gap-2 rounded-lg border border-charcoal-700 bg-charcoal-800 px-3 py-2">
              <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-status-warning" />
              <p className="text-xs leading-relaxed text-cream-300">
                브릿지 대기 중 — 카카오 MCP가 연결된 Claude 세션이{' '}
                <code className="text-cream-50">/api/alerts/pending</code>을 폴링해 발송합니다.
              </p>
            </div>
            {alerts.length === 0 ? (
              <EmptyState text="대기 중인 트렌드 경보가 없습니다." />
            ) : (
              <ul className="space-y-2">
                {alerts.map((a) => (
                  <li key={a.id} className="rounded-lg border border-charcoal-700 bg-charcoal-800 p-3">
                    <div className="mb-1 flex items-center justify-between">
                      <span className="text-sm font-semibold text-cream-50">{a.category}</span>
                      <span className="text-xs font-semibold text-status-warning">+{a.increase_rate}%</span>
                    </div>
                    <p className="whitespace-pre-line text-xs text-cream-500">{a.message}</p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </section>
      </main>
    </div>
  )
}
