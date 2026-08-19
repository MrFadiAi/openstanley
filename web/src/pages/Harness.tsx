import { useCallback, useEffect, useMemo, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { toast } from 'sonner';
import { motion } from 'framer-motion';
import {
  ArrowLeft,
  ChevronRight,
  FlaskConical,
  GitCompareArrows,
  History as HistoryIcon,
  Languages,
  Play,
  ShieldCheck,
  Sparkles,
  TrendingDown,
  TrendingUp,
  Wrench,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import TaskRows, { type TaskRowItem } from '@/components/bui/components/TaskRows';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import {
  compareHarnessRuns,
  getHarnessRun,
  getHarnessRuns,
  startHarnessRun,
  streamHarnessRun,
  type HarnessCompareResponse,
  type HarnessRun,
  type HarnessRunSummary,
} from '@/lib/api';
import type { I18nKey } from '@/lib/i18n';
import { cn, errMsg } from '@/lib/utils';

/* Harness — the eval + quality-measurement desk. Run the five suites (live
 * TaskRows progress over SSE), see per-suite score cards with deltas vs the
 * last run and a weighted total ring; history with rendered reports; a
 * side-by-side compare view; and the A/B brain-lift panel that proves the
 * Brain actually improves the agent. AR/EN + RTL. */

const SUITES: { id: string; icon: LucideIcon; key: I18nKey; weight: string }[] = [
  { id: 'voice', icon: Sparkles, key: 'harness.suite.voice', weight: '.30' },
  { id: 'algorithm', icon: TrendingUp, key: 'harness.suite.algorithm', weight: '.25' },
  { id: 'bilingual', icon: Languages, key: 'harness.suite.bilingual', weight: '.15' },
  { id: 'tools', icon: Wrench, key: 'harness.suite.tools', weight: '.15' },
  { id: 'safety', icon: ShieldCheck, key: 'harness.suite.safety', weight: '.15' },
];

const SUITE_ICON: Record<string, LucideIcon> = Object.fromEntries(
  SUITES.map((s) => [s.id, s.icon]),
);

type View = 'run' | 'history' | 'compare';

interface SuiteState {
  status: 'idle' | 'running' | 'ok';
  score?: number;
  delta?: number | null;
}

function scoreColor(score: number): string {
  if (score >= 80) return 'var(--good)';
  if (score >= 65) return 'var(--accent)';
  if (score >= 50) return 'var(--warn)';
  return 'var(--bad)';
}

function DeltaChip({ delta, label }: { delta?: number | null; label?: string }) {
  if (delta === null || delta === undefined) return null;
  const up = delta >= 0;
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-semibold tabular-nums',
        up ? 'bg-good/15 text-good' : 'bg-bad/15 text-bad',
      )}
    >
      <Icon size={10} />
      {up ? '+' : ''}
      {delta.toFixed(1)}
      {label ? <span className="ms-1 font-normal opacity-70">{label}</span> : null}
    </span>
  );
}

/** weighted-total donut */
function TotalRing({ total, delta }: { total: number | null; delta?: number | null }) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const pct = total === null ? 0 : Math.max(0, Math.min(100, total)) / 100;
  return (
    <div className="relative flex size-[132px] items-center justify-center">
      <svg width={132} height={132} className="-rotate-90">
        <circle cx={66} cy={66} r={r} fill="none" stroke="var(--line)" strokeWidth={9} />
        <motion.circle
          cx={66} cy={66} r={r} fill="none"
          stroke={total === null ? 'var(--line)' : scoreColor(total)}
          strokeWidth={9} strokeLinecap="round"
          initial={false}
          animate={{ strokeDasharray: `${c * pct} ${c}` }}
          transition={{ duration: 0.9, ease: [0.23, 1, 0.32, 1] }}
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span className="text-[26px] font-bold leading-none tabular-nums" dir="ltr">
          {total === null ? '—' : Math.round(total)}
        </span>
        <span className="mt-0.5 text-[10px] uppercase tracking-wider text-muted">/ 100</span>
        {delta !== null && delta !== undefined ? (
          <span className="mt-1"><DeltaChip delta={delta} /></span>
        ) : null}
      </div>
    </div>
  );
}

function ScoreCard({
  suite, state, delta,
}: { suite: string; state?: SuiteState; delta?: number | null }) {
  const { t } = useApp();
  const Icon = SUITE_ICON[suite] ?? FlaskConical;
  const key = `harness.suite.${suite}` as I18nKey;
  const score = state?.score ?? null;
  return (
    <div
      className="flex flex-col gap-2 rounded-2xl border border-edge bg-surface p-3.5 shadow-card"
      style={{ animation: 'fade-up 450ms cubic-bezier(0.23,1,0.32,1) both' }}
    >
      <div className="flex items-center gap-2">
        <span className="flex size-7 items-center justify-center rounded-lg bg-inset text-accent2">
          <Icon size={14} />
        </span>
        <span className="text-[13px] font-semibold">{t(key)}</span>
        <span className="ms-auto"><DeltaChip delta={delta ?? state?.delta} /></span>
      </div>
      {score === null ? (
        <Skeleton className="h-9 w-20" />
      ) : (
        <div className="flex items-end gap-1.5" dir="ltr">
          <span className="text-[30px] font-bold leading-none tabular-nums" style={{ color: scoreColor(score) }}>
            {Math.round(score)}
          </span>
          <span className="pb-0.5 text-[11px] text-muted">/ 100</span>
        </div>
      )}
      <div className="h-1.5 overflow-hidden rounded-full bg-inset" dir="ltr">
        <motion.div
          className="h-full rounded-full"
          style={{ background: score === null ? 'var(--line)' : scoreColor(score) }}
          initial={false}
          animate={{ width: `${score ?? 0}%` }}
          transition={{ duration: 0.8, ease: [0.23, 1, 0.32, 1] }}
        />
      </div>
    </div>
  );
}

/** suite highlights for the TaskRows dropdown */
function suiteDetailRows(run: HarnessRun | null, suite: string): { label: string; meta: string }[] {
  const det = run?.results.find((r) => r.suite === suite)?.details ?? {};
  const out: { label: string; meta: string }[] = [];
  if (det.error) out.push({ label: 'error', meta: det.error.slice(0, 60) });
  if (typeof det.mean_voice_match === 'number') {
    out.push({ label: 'voice-match', meta: `${det.mean_voice_match}%` });
    out.push({ label: 'style distance', meta: `${det.mean_style_distance ?? '—'}` });
  }
  if (typeof det.mean === 'number') {
    out.push({ label: 'mean', meta: `${det.mean}` });
    out.push({ label: 'strong ≥65', meta: `${det.pct_strong}%` });
    out.push({ label: 'weak <35', meta: `${det.pct_weak}%` });
  }
  if (Array.isArray(det.cases)) {
    const ok = det.cases.filter((c) => c.detected === c.requested).length;
    out.push({ label: 'language checks', meta: `${ok}/${det.cases.length}` });
  }
  if (Array.isArray(det.scenarios)) {
    const ok = det.scenarios.filter((s) => s.passed).length;
    out.push({ label: 'tool scenarios', meta: `${ok}/${det.scenarios.length}` });
  }
  if (det.checks) {
    const ok = Object.values(det.checks).filter((c) => c.passed).length;
    out.push({ label: 'safety checks', meta: `${ok}/${Object.keys(det.checks).length}` });
    if (det.fail_closed) out.push({ label: 'fail-closed', meta: 'armed' });
  }
  return out;
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

export function HarnessPage() {
  const { t } = useApp();
  const [view, setView] = useState<View>('run');
  const [selected, setSelected] = useState<Set<string>>(new Set(SUITES.map((s) => s.id)));
  const [ab, setAb] = useState(false);
  const [running, setRunning] = useState(false);
  const [suiteStates, setSuiteStates] = useState<Record<string, SuiteState>>({});
  const [total, setTotal] = useState<number | null>(null);
  const [totalDelta, setTotalDelta] = useState<number | null>(null);
  const [deltas, setDeltas] = useState<Record<string, number>>({});
  const [lift, setLift] = useState<Record<string, number> | null>(null);
  const [lastRun, setLastRun] = useState<HarnessRun | null>(null);

  const [runs, setRuns] = useState<HarnessRunSummary[] | null>(null);
  const [detail, setDetail] = useState<HarnessRun | null>(null);
  const [cmpA, setCmpA] = useState(0);
  const [cmpB, setCmpB] = useState(0);
  const [cmp, setCmp] = useState<HarnessCompareResponse | null>(null);

  const loadRuns = useCallback(async () => {
    try {
      const r = await getHarnessRuns();
      setRuns(r.runs);
      return r.runs;
    } catch (e) {
      setRuns([]);
      return [];
    }
  }, []);

  // initial load: history + prefill the cards with the latest manual run
  useEffect(() => {
    void (async () => {
      const list = await loadRuns();
      const latest = list.find((r) => r.status === 'done' && r.label === 'manual');
      if (latest) void refreshFromRun(latest.id);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshFromRun = useCallback(async (id: number) => {
    try {
      const run = await getHarnessRun(id);
      setLastRun(run);
      if (run.total !== null && run.total !== undefined) setTotal(run.total);
      setDeltas(run.deltas ?? {});
      setTotalDelta(run.deltas?.total ?? null);
      setSuiteStates(Object.fromEntries(
        run.results.map((r) => [r.suite, { status: 'ok', score: r.score }]),
      ));
    } catch {
      /* prefill is best-effort */
    }
  }, []);

  const toggleSuite = (id: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const startRun = async () => {
    if (running || selected.size === 0) return;
    setRunning(true);
    setLift(null);
    setSuiteStates(Object.fromEntries([...selected].map((s) => [s, { status: 'idle' }])));
    try {
      const { run_id } = await startHarnessRun([...selected], ab);
      await streamHarnessRun(run_id, {
        onSuiteStart: (e) =>
          setSuiteStates((cur) => ({ ...cur, [e.suite]: { status: 'running' } })),
        onSuiteDone: (e) =>
          setSuiteStates((cur) => ({
            ...cur,
            [e.suite]: { status: 'ok', score: e.score, delta: e.delta },
          })),
        onDone: (e) => {
          setTotal(e.total);
          setDeltas(e.deltas ?? {});
          setTotalDelta(e.deltas?.total ?? null);
          if (e.regression_notes?.length) toast.warning(t('harness.regression'));
          void refreshFromRun(e.run_id);
          void loadRuns();
        },
        onAbDone: (e) => {
          setLift(e.lift);
          void refreshFromRun(e.with_brain_run_id);
          void loadRuns();
        },
        onError: (m) => toast.error(t('harness.runFailed', { msg: m })),
      });
    } catch (e) {
      toast.error(t('harness.startFailed', { msg: errMsg(e) }));
    } finally {
      setRunning(false);
    }
  };

  const tasks: TaskRowItem[] = useMemo(
    () =>
      [...selected].map((id) => {
        const st = suiteStates[id] ?? { status: 'idle' };
        return {
          key: id,
          label: t(`harness.suite.${id}` as I18nKey),
          status: running && st.status === 'idle' ? 'idle' : st.status,
          amount: st.score !== undefined ? `${st.score.toFixed(1)}` : undefined,
          pill: st.delta !== undefined && st.delta !== null ? <DeltaChip delta={st.delta} /> : undefined,
          details: suiteDetailRows(lastRun, id),
        } as TaskRowItem;
      }),
    [selected, suiteStates, running, lastRun, t],
  );

  const openDetail = async (id: number) => {
    try {
      setDetail(await getHarnessRun(id));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const runCompare = async () => {
    if (!cmpA || !cmpB || cmpA === cmpB) return;
    try {
      setCmp(await compareHarnessRuns(cmpA, cmpB));
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  // ---------------- history detail drill-in ----------------
  if (detail) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-[860px] px-6 py-6">
          <Button variant="ghost" size="sm" className="mb-4" onClick={() => setDetail(null)}>
            <ArrowLeft size={14} className="me-1.5 rtl:rotate-180" />
            {t('harness.back')}
          </Button>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-accent-tint px-2 py-0.5 text-[11px] font-semibold text-accent-ink">
              #{detail.id} · {detail.label}
            </span>
            <span className="text-[12px] text-muted">{shortDate(detail.ts)}</span>
            {detail.total !== null ? (
              <span className="ms-auto flex items-center gap-2">
                <span className="text-[22px] font-bold tabular-nums" dir="ltr" style={{ color: scoreColor(detail.total) }}>
                  {Math.round(detail.total)}
                </span>
                <DeltaChip delta={detail.deltas?.total ?? null} />
              </span>
            ) : null}
          </div>
          <div className="mb-5 grid grid-cols-2 gap-2.5 sm:grid-cols-5">
            {detail.results.map((r) => (
              <ScoreCard key={r.suite} suite={r.suite} state={{ status: 'ok', score: r.score }} delta={detail.deltas?.[r.suite] ?? null} />
            ))}
          </div>
          <div className="rounded-2xl border border-edge bg-surface p-5">
            <h3 className="mb-3 text-[13px] font-semibold uppercase tracking-wider text-muted">
              {t('harness.report')}
            </h3>
            <Markdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: (p) => <h1 className="mb-3 text-[18px] font-bold" {...p} />,
                h2: (p) => <h2 className="mt-5 mb-2 border-t border-edge pt-4 text-[14px] font-bold" {...p} />,
                p: (p) => <p className="my-2 text-[13px] leading-relaxed text-ink-2" {...p} />,
                li: (p) => <li className="my-1 text-[13px] text-ink-2" {...p} />,
                hr: () => <hr className="my-4 border-edge" />,
                blockquote: (p) => <blockquote className="my-2 border-s-2 border-accent/50 ps-3 text-[12.5px] italic text-muted" {...p} />,
                table: (p) => <table className="my-3 w-full text-[12.5px]" {...p} />,
                th: (p) => <th className="border-b border-edge px-2 py-1.5 text-start font-semibold" {...p} />,
                td: (p) => <td className="border-b border-edge/60 px-2 py-1.5 tabular-nums" {...p} />,
              }}
            >
              {detail.report_md ?? '—'}
            </Markdown>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1060px] px-6 py-6">
        <PageHeader
          title={t('harness.title')}
          subtitle={t('harness.subtitle')}
          actions={
            <div className="flex items-center gap-1 rounded-control bg-inset p-1">
              {(['run', 'history', 'compare'] as View[]).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  className={cn(
                    'flex items-center gap-1.5 rounded-[7px] px-2.5 py-1 text-[12.5px] font-medium transition-colors',
                    view === v ? 'bg-surface text-ink shadow-hairline' : 'text-ink-3 hover:text-ink-2',
                  )}
                >
                  {v === 'run' ? <Play size={12} /> : v === 'history' ? <HistoryIcon size={12} /> : <GitCompareArrows size={12} />}
                  {t(v === 'run' ? 'harness.run' : v === 'history' ? 'harness.history' : 'harness.compare')}
                </button>
              ))}
            </div>
          }
        />

        {view === 'run' && (
          <div className="flex flex-col gap-5">
            {/* suite toggles + run controls */}
            <div className="rounded-2xl border border-edge bg-surface p-4 shadow-card">
              <div className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted">
                {t('harness.suites')}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {SUITES.map((s) => {
                  const on = selected.has(s.id);
                  const Icon = s.icon;
                  return (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => toggleSuite(s.id)}
                      disabled={running}
                      className={cn(
                        'flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-[12.5px] font-medium transition-all active:scale-[0.96]',
                        on
                          ? 'border-accent/50 bg-accent/15 text-accent2'
                          : 'border-edge bg-inset text-ink-3 hover:text-ink-2',
                      )}
                    >
                      <Icon size={12} />
                      {t(s.key)}
                      <span className="text-[10px] font-normal opacity-60" dir="ltr">{s.weight}</span>
                    </button>
                  );
                })}
                <div className="ms-auto flex items-center gap-4">
                  <label className="flex cursor-pointer items-center gap-2">
                    <Switch checked={ab} onCheckedChange={setAb} disabled={running} />
                    <span className="flex flex-col">
                      <span className="text-[12.5px] font-medium">{t('harness.ab')}</span>
                      <span className="text-[10.5px] text-muted">{t('harness.abHint')}</span>
                    </span>
                  </label>
                  <Button onClick={startRun} disabled={running || selected.size === 0}>
                    <FlaskConical size={13} className="me-1.5" />
                    {running ? t('harness.running') : t('harness.run')}
                  </Button>
                </div>
              </div>
            </div>

            {/* live progress */}
            {(running || suiteStates['voice']?.status === 'ok' || suiteStates['safety']?.status === 'ok') && (
              <TaskRows tasks={tasks} />
            )}

            {/* A/B brain-lift panel */}
            {lift && (
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                className="rounded-2xl border border-good/40 bg-good/5 p-4"
              >
                <div className="mb-2.5 flex items-center gap-2">
                  <Sparkles size={14} className="text-good" />
                  <span className="text-[13px] font-bold">{t('harness.brainLift')}</span>
                  <span className="text-[11.5px] text-muted">({t('harness.abHint')})</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(lift)
                    .filter(([k]) => k !== 'total')
                    .map(([k, v]) => (
                      <span key={k} className="flex items-center gap-1.5 rounded-full bg-surface px-2.5 py-1 text-[12px] shadow-hairline">
                        {t(`harness.suite.${k}` as I18nKey)}
                        <b className={cn('tabular-nums', v > 0 ? 'text-good' : v < 0 ? 'text-bad' : 'text-muted')} dir="ltr">
                          {v > 0 ? '+' : ''}{v.toFixed(1)}
                        </b>
                      </span>
                    ))}
                  <span className="ms-auto flex items-center gap-1.5 rounded-full bg-surface px-3 py-1 text-[12.5px] font-bold shadow-hairline">
                    {t('harness.total')}
                    <b className={cn('tabular-nums', (lift.total ?? 0) > 0 ? 'text-good' : 'text-bad')} dir="ltr">
                      {(lift.total ?? 0) > 0 ? '+' : ''}{(lift.total ?? 0).toFixed(1)}
                    </b>
                  </span>
                </div>
              </motion.div>
            )}

            {/* score cards + total ring */}
            <div className="flex flex-col gap-4 lg:flex-row">
              <div className="flex size-fit rounded-2xl border border-edge bg-surface p-4 shadow-card">
                <TotalRing total={total} delta={totalDelta} />
              </div>
              <div className="grid flex-1 grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
                {SUITES.map((s) => (
                  <ScoreCard
                    key={s.id}
                    suite={s.id}
                    state={suiteStates[s.id]}
                    delta={deltas[s.id] ?? suiteStates[s.id]?.delta ?? null}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

        {view === 'history' && (
          <div className="overflow-hidden rounded-2xl border border-edge bg-surface shadow-card">
            {runs === null ? (
              <div className="p-4"><Skeleton className="h-40" /></div>
            ) : runs.length === 0 ? (
              <div className="p-8 text-center text-[13px] text-muted">{t('harness.noRuns')}</div>
            ) : (
              <table className="w-full text-[12.5px]">
                <thead>
                  <tr className="border-b border-edge text-[11px] uppercase tracking-wider text-muted">
                    <th className="px-3 py-2.5 text-start font-semibold">#</th>
                    <th className="px-3 py-2.5 text-start font-semibold">{t('harness.history')}</th>
                    <th className="px-3 py-2.5 text-start font-semibold">{t('harness.total')}</th>
                    <th className="px-3 py-2.5 text-start font-semibold">Δ</th>
                    <th className="px-3 py-2.5 text-end font-semibold" />
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => (
                    <tr
                      key={r.id}
                      onClick={() => openDetail(r.id)}
                      className="cursor-pointer border-b border-edge/50 transition-colors last:border-0 hover:bg-inset"
                    >
                      <td className="px-3 py-2.5 font-mono text-muted" dir="ltr">{r.id}</td>
                      <td className="px-3 py-2.5">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium">{shortDate(r.ts)}</span>
                          <span className={cn(
                            'rounded-full px-1.5 py-0.5 text-[10.5px] font-semibold',
                            r.label === 'manual' ? 'bg-accent-tint text-accent-ink'
                              : r.label === 'ab:pair' ? 'bg-good/15 text-good'
                              : 'bg-inset text-muted',
                          )}>
                            {r.label}
                          </span>
                          {r.real_llm ? (
                            <span className="rounded-full bg-warn/15 px-1.5 py-0.5 text-[10.5px] font-semibold text-warn">real</span>
                          ) : null}
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        {r.total === null || r.total === undefined ? (
                          <span className="text-muted">{r.status === 'running' ? '…' : '—'}</span>
                        ) : (
                          <span className="font-bold tabular-nums" dir="ltr" style={{ color: scoreColor(r.total) }}>
                            {Math.round(r.total)}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2.5"><DeltaChip delta={r.suites?.total ?? null} /></td>
                      <td className="px-3 py-2.5 text-end text-muted">
                        <ChevronRight size={14} className="rtl:rotate-180 inline-block" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {view === 'compare' && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-end gap-3 rounded-2xl border border-edge bg-surface p-4 shadow-card">
              {([['a', cmpA, setCmpA, 'harness.baseline'], ['b', cmpB, setCmpB, 'harness.newer']] as const).map(
                ([side, val, set, labelKey]) => (
                  <label key={side} className="flex flex-col gap-1">
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                      {side.toUpperCase()} · {t(labelKey)}
                    </span>
                    <select
                      value={val}
                      onChange={(e) => set(Number(e.target.value))}
                      className="h-9 min-w-[190px] rounded-control border border-edge bg-inset px-2 text-[12.5px] outline-none"
                    >
                      <option value={0}>—</option>
                      {(runs ?? []).map((r) => (
                        <option key={r.id} value={r.id}>
                          #{r.id} · {shortDate(r.ts)} · {r.label} · {r.total ?? '—'}
                        </option>
                      ))}
                    </select>
                  </label>
                ),
              )}
              <Button className="ms-auto" disabled={!cmpA || !cmpB || cmpA === cmpB} onClick={runCompare}>
                <GitCompareArrows size={13} className="me-1.5" />
                {t('harness.compare')}
              </Button>
            </div>

            {cmp ? (
              <div className="rounded-2xl border border-edge bg-surface p-4 shadow-card">
                <div className="mb-3 flex flex-wrap items-center gap-3 text-[12px] text-muted">
                  <span dir="ltr">A #{cmp.a.id}</span>
                  <ChevronRight size={12} className="rtl:rotate-180" />
                  <span dir="ltr">B #{cmp.b.id}</span>
                  <span className="ms-auto flex items-center gap-2 text-[13px] font-bold text-ink">
                    {t('harness.total')}
                    <DeltaChip delta={cmp.total_delta} />
                  </span>
                </div>
                <div className="flex flex-col gap-2.5">
                  {Object.entries(cmp.suites).map(([suite, row]) => (
                    <div key={suite} className="flex items-center gap-3">
                      <span className="w-24 shrink-0 text-[12.5px] font-medium">
                        {t(`harness.suite.${suite}` as I18nKey)}
                      </span>
                      <div className="flex flex-1 items-center gap-2" dir="ltr">
                        <span className="w-10 text-end font-mono text-[12px] text-muted tabular-nums">
                          {row.a ?? '—'}
                        </span>
                        <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-inset">
                          {row.b !== null ? (
                            <motion.div
                              className="absolute inset-y-0 rounded-full"
                              style={{ background: scoreColor(row.b ?? 0) }}
                              initial={false}
                              animate={{ left: 0, width: `${row.b ?? 0}%` }}
                              transition={{ duration: 0.7, ease: [0.23, 1, 0.32, 1] }}
                            />
                          ) : null}
                        </div>
                        <span className="w-10 font-mono text-[12px] font-bold tabular-nums">
                          {row.b ?? '—'}
                        </span>
                      </div>
                      <DeltaChip delta={row.delta} />
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="rounded-2xl border border-dashed border-edge p-8 text-center text-[13px] text-muted">
                {t('harness.pickTwo')}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
