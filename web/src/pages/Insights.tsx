import { useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { CheckCircle2, Eye, Heart, Lock, RefreshCw, Trophy } from 'lucide-react';
import { AutopilotCard } from '@/components/AutopilotCard';
import { DigestCard } from '@/components/DigestCard';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useApp } from '@/lib/app-context';
import { api, apiPost } from '@/lib/api';
import { cn, hasArabic } from '@/lib/utils';

// ---------------- api types (GET /api/insights/overview) ----------------

interface Overview {
  account: { handle: string; followers: number | null; today_impressions: number };
  heatmap: { date: string; value: number }[];
  growth: {
    days: number; total: number; prev_total: number; delta_pct: number | null;
    series: { date: string; impressions: number }[];
  };
  orbit: { handle: string; interactions: number }[];
  milestones: { id: string; label: string; current: number; target: number; unlocked: boolean }[];
  types: {
    text: { count: number; avg_impressions: number; engagement_rate: number };
    image: { count: number; avg_impressions: number; engagement_rate: number };
    leader: 'text' | 'image'; ratio: number | null;
  };
  best: {
    x_id: string; text: string; created_at: string; likes: number;
    impressions: number; engagement_rate: number; image: string | null;
  }[];
}

const TOOLTIP_STYLE: React.CSSProperties = {
  background: '#18181f', border: '1px solid #26262f', borderRadius: 9,
  fontSize: 12, color: '#ececf1',
};
const AXIS_TICK = { fill: '#9b9ba7', fontSize: 11 };
const GRID = '#22222b';

const fmtCompact = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M`
  : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k`
  : String(n);

/** purple ladder for the heatmap — quartiles of the year's max day */
function heatLevel(v: number, max: number): string {
  if (v <= 0) return 'bg-transparent border border-line/50';
  const q = v / Math.max(max, 1);
  if (q > 0.75) return 'bg-accent';
  if (q > 0.5) return 'bg-accent/70';
  if (q > 0.25) return 'bg-accent/45';
  return 'bg-accent/25';
}

// ---------------- section 1 · performance ----------------

function HeatmapCard({ data }: { data: Overview['heatmap'] }) {
  const { t, lang } = useApp();
  const total = useMemo(() => data.reduce((n, d) => n + d.value, 0), [data]);
  const max = useMemo(() => Math.max(1, ...data.map((d) => d.value)), [data]);
  // group into 7-row columns starting Sunday — GitHub layout
  const weeks: Overview['heatmap'][] = [];
  let week: Overview['heatmap'] = [];
  data.forEach((d) => {
    const dow = new Date(`${d.date}T00:00:00`).getDay();
    if (week.length === 0 && dow > 0) {
      for (let i = 0; i < dow; i++) week.push({ date: '', value: -1 }); // pad
    }
    week.push(d);
    if (week.length === 7) { weeks.push(week); week = []; }
  });
  if (week.length) weeks.push(week);
  const monthLabel = (w: Overview['heatmap']): string => {
    const first = w.find((d) => d.value >= 0);
    if (!first) return '';
    return new Date(`${first.date}T00:00:00`).toLocaleDateString(
      lang === 'ar' ? 'ar' : 'en-GB', { month: 'short' });
  };

  return (
    <div className="rounded-2xl border border-line bg-panel p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="font-serif text-[16px]">{t('insights2.heatmapTitle')}</h3>
        <span className="font-mono text-[12px] text-muted">
          {t('insights2.heatmapTotal', { n: fmtCompact(total) })}
        </span>
      </div>
      <div className="overflow-x-auto pb-1">
        <div className="flex gap-[3px]" style={{ direction: 'ltr' }}>
          {weeks.map((w, i) => (
            <div key={i} className="flex flex-col gap-[3px]">
              <span className="h-3 text-[8.5px] leading-3 text-ink-3">
                {i % 4 === 0 ? monthLabel(w) : ''}
              </span>
              {w.map((d, j) => (
                <div
                  key={j}
                  title={d.value >= 0 ? `${d.date} · ${d.value}` : ''}
                  className={cn('size-[10px] rounded-[2px]', heatLevel(d.value, max))}
                />
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function GrowthCard({ growth }: { growth: Overview['growth'] }) {
  const { t } = useApp();
  const delta = growth.delta_pct;
  return (
    <div className="rounded-2xl border border-line bg-panel p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-serif text-[16px]">{t('insights2.growthTitle')}</h3>
        <div className="flex items-center gap-2">
          <span className="font-serif text-[22px] leading-none">{fmtCompact(growth.total)}</span>
          {delta !== null ? (
            <Badge variant={delta >= 0 ? 'green' : 'default'}
                   className={delta >= 0 ? '' : 'text-bad'}>
              {delta >= 0 ? '▲' : '▼'} {Math.abs(delta)}%
            </Badge>
          ) : null}
        </div>
      </div>
      <div className="h-44" style={{ direction: 'ltr' }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={growth.series} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="date" tick={AXIS_TICK} tickLine={false} axisLine={false}
                   minTickGap={48} tickFormatter={(v: string) => v.slice(5)} />
            <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} width={36}
                   tickFormatter={(v: number) => fmtCompact(v)} />
            <RTooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => [v, 'impressions']} />
            <Line type="monotone" dataKey="impressions" stroke="var(--accent)"
                  strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-[11.5px] text-muted">{t('insights2.vsPrev')}</p>
    </div>
  );
}

// ---------------- section 2 · audience ----------------

function OrbitCard({ orbit }: { orbit: Overview['orbit'] }) {
  const { t } = useApp();
  const maxN = Math.max(1, ...orbit.map((o) => o.interactions));
  return (
    <div className="rounded-2xl border border-line bg-panel p-4">
      <h3 className="mb-3 font-serif text-[16px]">{t('insights2.orbitTitle')}</h3>
      {orbit.length === 0 ? (
        <p className="py-6 text-center text-[12.5px] leading-relaxed text-muted">
          {t('insights2.orbitEmpty')}
        </p>
      ) : (
        <div className="flex flex-wrap items-end justify-center gap-4 py-2">
          {orbit.map((o) => {
            const size = 44 + Math.round((o.interactions / maxN) * 36);
            return (
              <div key={o.handle} className="flex w-20 flex-col items-center gap-1">
                <div
                  title={t('insights2.interactions', { n: o.interactions })}
                  style={{ width: size, height: size }}
                  className="flex items-center justify-center rounded-full border border-accent/50 bg-accent-tint font-mono text-[11px] text-accent-ink"
                >
                  {o.interactions}
                </div>
                <span className="w-full truncate text-center text-[11px] text-muted">
                  @{o.handle}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function MilestonesCard({ milestones }: { milestones: Overview['milestones'] }) {
  const { t } = useApp();
  return (
    <div className="rounded-2xl border border-line bg-panel p-4">
      <h3 className="mb-3 font-serif text-[16px]">{t('insights2.milestones')}</h3>
      <div className="grid grid-cols-5 gap-2">
        {milestones.map((m) => (
          <div
            key={m.id}
            title={m.unlocked ? m.label : `${m.label} — ${m.current}/${m.target}`}
            className={cn(
              'flex flex-col items-center gap-1.5 rounded-xl border px-1.5 py-3 text-center',
              m.unlocked ? 'border-accent/40 bg-accent-tint' : 'border-line bg-inset opacity-60',
            )}
          >
            {m.unlocked ? (
              <Trophy size={16} className="text-accent-ink" />
            ) : (
              <Lock size={14} className="text-ink-3" />
            )}
            <span className={cn('text-[10.5px] leading-tight', m.unlocked ? 'text-ink' : 'text-muted')}>
              {m.label}
            </span>
            {!m.unlocked ? (
              <span className="font-mono text-[9.5px] text-ink-3">
                {fmtCompact(m.current)}/{fmtCompact(m.target)}
              </span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function TypeCard({ types }: { types: Overview['types'] }) {
  const { t } = useApp();
  const buckets = [
    { key: 'text', label: t('insights2.text'), d: types.text },
    { key: 'image', label: t('insights2.images'), d: types.image },
  ];
  return (
    <div className="rounded-2xl border border-line bg-panel p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="font-serif text-[16px]">{t('insights2.typeTitle')}</h3>
        {types.ratio ? (
          <span className="font-mono text-[11.5px] text-accent-ink">
            {t('insights2.leadsBy', {
              a: types.leader === 'text' ? t('insights2.text') : t('insights2.images'),
              b: types.leader === 'text' ? t('insights2.images') : t('insights2.text'),
              n: types.ratio,
            })}
          </span>
        ) : null}
      </div>
      <div className="grid grid-cols-2 gap-3">
        {buckets.map((b) => (
          <div key={b.key} className={cn(
            'rounded-xl border p-3',
            types.leader === b.key ? 'border-accent/40 bg-accent-tint' : 'border-line bg-inset',
          )}>
            <div className="mb-1 font-serif text-[15px]">{b.label}</div>
            <div className="font-serif text-[24px] leading-none">
              {b.d.count ? fmtCompact(b.d.avg_impressions) : '—'}
            </div>
            <div className="mt-1 text-[11px] text-muted">{t('insights2.avgImpressions')}</div>
            <div className="mt-2 font-mono text-[11px] text-ink-2">
              {t('insights2.engRate')}: {b.d.engagement_rate ? `${(b.d.engagement_rate * 100).toFixed(1)}%` : '—'}
              {' · '}{b.d.count}
            </div>
          </div>
        ))}
      </div>
      {types.image.count === 0 ? (
        <p className="mt-2 text-[11.5px] text-muted">{t('insights2.noImagesYet')}</p>
      ) : null}
    </div>
  );
}

// ---------------- section 3 · curation ----------------

function BestCard({ post }: { post: Overview['best'][number] }) {
  const { t } = useApp();
  const [state, setState] = useState<'idle' | 'busy' | 'queued'>('idle');
  const [draftId, setDraftId] = useState<number | null>(null);

  const repost = (): void => {
    setState('busy');
    void (async () => {
      try {
        const r = await apiPost<{ ok: boolean; draft_id: number }>(
          `posts/${post.x_id}/repost`, {});
        setDraftId(r.draft_id);
        setState('queued');
      } catch {
        setState('idle');
      }
    })();
  };

  return (
    <div className="mb-3 break-inside-avoid rounded-2xl border border-line bg-panel p-3.5">
      <div className="mb-2 font-mono text-[10.5px] text-ink-3">{post.created_at}</div>
      <p dir={hasArabic(post.text) ? 'rtl' : 'ltr'}
         className="whitespace-pre-wrap text-[13px] leading-relaxed">
        {post.text}
      </p>
      <div className="mt-2.5 flex items-center gap-3 font-mono text-[11px] text-muted">
        <span className="inline-flex items-center gap-1"><Heart size={11} />{post.likes}</span>
        <span className="inline-flex items-center gap-1"><Eye size={11} />{fmtCompact(post.impressions)}</span>
        <span>{(post.engagement_rate * 100).toFixed(1)}%</span>
      </div>
      <div className="mt-2.5">
        {state === 'queued' ? (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-good">
            <CheckCircle2 size={13} />
            {t('insights2.reposted', { id: draftId ?? '…' })}
          </span>
        ) : (
          <Button size="sm" variant="primary" disabled={state === 'busy'} onClick={repost}>
            {state === 'busy' ? <RefreshCw size={12} className="animate-spin" /> : null}
            {t('insights2.repost')}
          </Button>
        )}
      </div>
    </div>
  );
}

// ---------------- page ----------------

export function InsightsPage() {
  const { t } = useApp();
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        setData(await api<Overview>('insights/overview'));
      } catch {
        setData(null);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return (
    <div className="h-full overflow-y-auto">
      <div className="h-full px-6 py-6">
        <div className="mb-6">
          <h1 className="font-serif text-[34px] font-medium leading-none tracking-tight">
            {t('insights2.title')}
          </h1>
          <p className="mt-2 font-serif text-[14px] italic text-ink-3">{t('insights2.subtitle')}</p>
        </div>

        {loading ? (
          <div className="grid grid-cols-2 gap-3">
            {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-40 rounded-2xl" />)}
          </div>
        ) : !data ? (
          <p className="py-16 text-center text-[13px] text-muted">{t('common.error', { msg: '' })}</p>
        ) : (
          <div className="flex flex-col gap-6">

            {/* — 1 · performance overview — */}
            <section className="flex flex-col gap-3">
              <h2 className="font-serif text-[19px]">{t('insights2.overview')}</h2>
              <div className="grid grid-cols-3 gap-3">
                <div className="rounded-2xl border border-line bg-panel p-4">
                  <div className="mb-1 font-mono text-[11px] text-accent-ink">
                    X · @{data.account.handle}
                  </div>
                  <div className="font-serif text-[30px] leading-none">
                    {fmtCompact(data.account.today_impressions)}
                  </div>
                  <div className="mt-1 text-[11.5px] text-muted">{t('insights2.todayImpressions')}</div>
                  <div className="mt-3 font-mono text-[11px] text-ink-2">
                    {data.account.followers ?? '—'} {t('insights2.followers')}
                  </div>
                </div>
                <div className="col-span-2 rounded-2xl border border-line bg-panel p-4">
                  <h3 className="mb-2.5 font-serif text-[16px]">{t('insights2.integrations')}</h3>
                  <div className="flex flex-wrap gap-2">
                    <span className="inline-flex items-center gap-1.5 rounded-full border border-good/40 bg-green-tint px-3 py-1.5 text-[12px] text-good">
                      <CheckCircle2 size={12} /> X · {t('insights2.connected')}
                    </span>
                    {['LinkedIn', 'Instagram', 'Threads', 'YouTube'].map((p) => (
                      <span key={p}
                            className="inline-flex items-center gap-1.5 rounded-full border border-line px-3 py-1.5 text-[12px] text-ink-3">
                        {p}
                        <Badge className="px-1 py-0 text-[9.5px]">{t('insights2.comingSoon')}</Badge>
                      </span>
                    ))}
                  </div>
                </div>
              </div>
              <HeatmapCard data={data.heatmap} />
              <GrowthCard growth={data.growth} />
            </section>

            {/* — 2 · audience & content — */}
            <section className="flex flex-col gap-3">
              <h2 className="font-serif text-[19px]">{t('insights2.audience')}</h2>
              <div className="grid grid-cols-2 gap-3">
                <OrbitCard orbit={data.orbit} />
                <div className="flex flex-col gap-3">
                  <MilestonesCard milestones={data.milestones} />
                  <TypeCard types={data.types} />
                </div>
              </div>
            </section>

            {/* — 3 · curation — */}
            <section className="flex flex-col gap-3">
              <h2 className="font-serif text-[19px]">{t('insights2.curation')}</h2>
              {data.best.length === 0 ? (
                <p className="rounded-2xl border border-line bg-panel py-10 text-center text-[12.5px] text-muted">
                  {t('insights2.bestEmpty')}
                </p>
              ) : (
                <div className="columns-1 gap-3 md:columns-2 xl:columns-3">
                  {data.best.map((p) => <BestCard key={p.x_id} post={p} />)}
                </div>
              )}
            </section>

            {/* — standing panels — */}
            <section className="grid grid-cols-2 gap-3">
              <AutopilotCard />
              <DigestCard />
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
