import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { ProviderUsage, UsageWindow } from '../lib/types'

/** Colour by headroom, not by brand: the point is "how close am I to stopping". */
export function meterTone(percent: number): 'ok' | 'warn' | 'hot' {
  if (percent >= 85) return 'hot'
  if (percent >= 60) return 'warn'
  return 'ok'
}

export function resetLabel(resetsAt: number | null): string {
  if (!resetsAt) return ''
  const remaining = resetsAt - Date.now() / 1000
  if (remaining <= 0) return 'resetting'
  const hours = Math.floor(remaining / 3600)
  const minutes = Math.floor((remaining % 3600) / 60)
  if (hours >= 48) return `resets in ${Math.round(hours / 24)}d`
  if (hours >= 1) return `resets in ${hours}h ${minutes}m`
  return `resets in ${minutes}m`
}

export function Meter({ window }: { window: UsageWindow }) {
  const pct = Math.max(0, Math.min(100, window.usedPercent))
  return (
    <div className="meter-row">
      <span className="meter-label">{window.label}</span>
      <div className="meter" role="meter" aria-valuenow={pct} aria-valuemin={0}
           aria-valuemax={100} aria-label={`${window.label} usage`}>
        <div className={`meter-fill ${meterTone(pct)}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="meter-pct mono">{pct.toFixed(0)}%</span>
      <span className="meter-reset faint">{resetLabel(window.resetsAt)}</span>
    </div>
  )
}

export function UsageView() {
  const [providers, setProviders] = useState<ProviderUsage[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (refresh = false) => {
    setBusy(true)
    try {
      const data = await api.usage(refresh)
      setProviders(data.providers)
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = window.setInterval(() => load(), 60_000)
    return () => window.clearInterval(timer)
  }, [load])

  return (
    <div className="main">
      <div className="toolbar">
        <h1>Usage</h1>
        <button className="btn small ghost" style={{ marginLeft: 'auto' }}
                disabled={busy} onClick={() => load(true)}>
          {busy ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>
      <div className="pane-body">
        {error && <div className="notice error">{error}</div>}
        {!providers && !error && <div className="notice info">Loading usage…</div>}
        {providers?.map((p) => <ProviderCard key={p.provider} usage={p} />)}
      </div>
    </div>
  )
}

function ProviderCard({ usage }: { usage: ProviderUsage }) {
  const totals = usage.totals ?? {}
  return (
    <div className="context-section">
      <h3>
        {usage.label}
        {usage.plan && <span className="label-chip" style={{ marginLeft: 8 }}>{usage.plan}</span>}
      </h3>

      {usage.error && <div className="notice error" style={{ margin: '0 0 10px' }}>{usage.error}</div>}

      {usage.windows.length > 0 ? (
        <div className="meters">
          {usage.windows.map((w) => <Meter key={w.label} window={w} />)}
        </div>
      ) : !usage.error && (
        // No windows is not a failure for every provider, so say which it is.
        <div className="faint" style={{ fontSize: 12.5, lineHeight: 1.55 }}>
          {usage.note ?? 'This provider does not report rate-limit windows.'}
        </div>
      )}

      {usage.credits && (usage.credits.balance !== null || usage.credits.resetCreditsAvailable) && (
        <div className="kv" style={{ marginTop: 10 }}>
          <span className="k">Credits</span>
          <span className="v mono">
            {usage.credits.unlimited ? 'unlimited' : (usage.credits.balance ?? '0')}
            {usage.credits.resetCreditsAvailable
              ? ` · ${usage.credits.resetCreditsAvailable} reset credit${usage.credits.resetCreditsAvailable > 1 ? 's' : ''}`
              : ''}
          </span>
        </div>
      )}

      {usage.lastLimitEvent && (
        <div className="notice" style={{ margin: '10px 0 0' }}>
          <strong style={{ color: 'var(--text)' }}>Last limit hit — </strong>
          {usage.lastLimitEvent.text}
          {usage.lastLimitEvent.timestamp && (
            <span className="faint">
              {' '}({new Date(usage.lastLimitEvent.timestamp).toLocaleDateString()})
            </span>
          )}
        </div>
      )}

      {usage.account && (
        <div className="kv" style={{ marginTop: 8 }}>
          <span className="k">Account</span><span className="v">{usage.account}</span>
        </div>
      )}
      {typeof totals.lifetimeTokens === 'number' && (
        <div className="kv">
          <span className="k">Lifetime tokens</span>
          <span className="v mono">{formatTokens(totals.lifetimeTokens)}</span>
        </div>
      )}
      {typeof totals.currentStreakDays === 'number' && (
        <div className="kv">
          <span className="k">Streak</span>
          <span className="v mono">{totals.currentStreakDays}d (best {totals.longestStreakDays}d)</span>
        </div>
      )}
      {typeof totals.totalSessions === 'number' && (
        <div className="kv">
          <span className="k">Sessions / messages</span>
          <span className="v mono">{totals.totalSessions} / {totals.totalMessages}</span>
        </div>
      )}
      {totals.computedAt && (
        // The local cache lags, so the number is only honest with its date attached.
        <div className="kv">
          <span className="k">Counted up to</span>
          <span className="v mono">{totals.computedAt}</span>
        </div>
      )}
      {Array.isArray(totals.dailyTokens) && totals.dailyTokens.length > 1 && (
        <Sparkline points={totals.dailyTokens} />
      )}
    </div>
  )
}

export function formatTokens(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`
  return String(value)
}

function Sparkline({ points }: { points: { date: string; tokens: number }[] }) {
  const max = Math.max(...points.map((p) => p.tokens), 1)
  return (
    <div style={{ marginTop: 12 }}>
      <div className="kv" style={{ marginBottom: 5 }}>
        <span className="k">Recent daily tokens</span>
        <span className="v mono">peak {formatTokens(max)}</span>
      </div>
      <div className="spark">
        {points.map((p) => (
          <div key={p.date} className="spark-bar"
               title={`${p.date}: ${formatTokens(p.tokens)}`}
               style={{ height: `${Math.max(3, (p.tokens / max) * 100)}%` }} />
        ))}
      </div>
    </div>
  )
}
