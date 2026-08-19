import { useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Heart, MessageCircle, Repeat2, TrendingUp, ExternalLink } from 'lucide-react';
import { AutopilotCard } from '@/components/AutopilotCard';
import { DigestCard } from '@/components/DigestCard';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import type { GrowthResponse, InsightsResponse, TimesResponse, TopPostsResponse } from '@/lib/api';
import { api } from '@/lib/api';
import type { I18nKey } from '@/lib/i18n';

const TOOLTIP_STYLE: React.CSSProperties = {
  background: '#18181f',
  border: '1px solid #26262f',
  borderRadius: 9,
  fontSize: 12,
  color: '#ececf1',
};
const AXIS_TICK = { fill: '#9b9ba7', fontSize: 11 };
const GRID = '#22222b';

const LANG_COLORS: Record<string, string> = {
  en: '#7c6cff',
  ar: '#3fb96d',
  mixed: '#e0a53f',
};

function StatTile({ value, label }: { value: string; label: string }) {
  return (
    <div className="rounded-xl border border-edge bg-panel px-4 py-3">
      <b className="block text-[22px] font-bold leading-tight">{value}</b>
      <small className="text-[12px] text-muted">{label}</small>
    </div>
  );
}

function ChartCard({
  title,
  badge,
  children,
}: {
  title: string;
  badge?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-edge bg-panel p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">
          {title}
        </div>
        {badge}
      </div>
      {children}
    </div>
  );
}

export function InsightsPage() {
  const { t, lang } = useApp();
  const [data, setData] = useState<InsightsResponse | null>(null);
  const [growth, setGrowth] = useState<GrowthResponse | null>(null);
  const [top, setTop] = useState<TopPostsResponse | null>(null);
  const [times, setTimes] = useState<TimesResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    void (async () => {
      // insights (scan-time snapshots) + the three growth-analytics endpoints
      // (real metrics ground truth) — all settle independently
      const [ins, gr, tp, tm] = await Promise.allSettled([
        api<InsightsResponse>('insights'),
        api<GrowthResponse>('analytics/growth?days=14'),
        api<TopPostsResponse>('analytics/top?limit=10&days=30'),
        api<TimesResponse>('analytics/times?days=60'),
      ]);
      if (!alive) return;
      setData(ins.status === 'fulfilled' ? ins.value : null);
      if (gr.status === 'fulfilled') setGrowth(gr.value);
      if (tp.status === 'fulfilled') setTop(tp.value);
      if (tm.status === 'fulfilled') setTimes(tm.value);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, []);

  const dayKeys: I18nKey[] = ['insights.d0', 'insights.d1', 'insights.d2', 'insights.d3', 'insights.d4', 'insights.d5', 'insights.d6'];

  const heat = useMemo(() => {
    const map = new Map<string, number>();
    let max = 0;
    for (const c of data?.hours_heatmap ?? []) {
      map.set(`${c.day}-${c.hour}`, c.value);
      if (c.value > max) max = c.value;
    }
    return { map, max };
  }, [data]);

  // growth chart rows: followers carry-forward + eng rate as %
  const growthData = useMemo(
    () =>
      (growth?.series ?? []).map((d) => ({
        date: d.date.slice(5),
        followers: d.followers,
        ratePct:
          d.avg_engagement_rate == null ? null : +(d.avg_engagement_rate * 100).toFixed(2),
      })),
    [growth],
  );

  const hasGrowth =
    growthData.length > 0 &&
    (growthData.some((d) => (d.followers ?? 0) > 0) || (growth?.total_posts ?? 0) > 0);

  // best hours: REAL own-post performance once we have >=20 posts, else heuristic
  const bestHoursData = useMemo(() => {
    if (times?.source === 'real') return times.hours;
    return data?.best_hours ?? [];
  }, [times, data]);
  const bestHoursReal = times?.source === 'real';

  const hasData =
    (data?.engagement_over_time?.length ?? 0) > 0 ||
    (data?.best_hours?.length ?? 0) > 0 ||
    (data?.format_performance?.length ?? 0) > 0 ||
    (data?.language_mix?.length ?? 0) > 0 ||
    hasGrowth ||
    (top?.posts?.length ?? 0) > 0;

  if (loading) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-[1060px] px-6 py-6">
          <Skeleton className="mb-5 h-8 w-56" />
          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-[62px]" />
            ))}
          </div>
          <Skeleton className="mb-4 h-64" />
          <div className="grid gap-4 md:grid-cols-2">
            <Skeleton className="h-56" />
            <Skeleton className="h-56" />
          </div>
        </div>
      </div>
    );
  }

  if (!data || !hasData) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-[1060px] px-6 py-6">
          <PageHeader title={t('insights.title')} subtitle={t('insights.subtitle')} />
          <AutopilotCard />
          <DigestCard />
          <div className="flex items-center justify-center gap-2 py-16 text-[13px] text-muted">
            <TrendingUp size={15} />
            {t('insights.empty')}
          </div>
        </div>
      </div>
    );
  }

  const s = data.summary;
  const fmt = (n: number): string => n.toLocaleString(lang === 'ar' ? 'ar-EG' : 'en-GB');

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1060px] px-6 py-6">
        <PageHeader title={t('insights.title')} subtitle={t('insights.subtitle')} />

        {/* autopilot — OpenStanley runs itself; publish stays approval-gated */}
        <AutopilotCard />

        {/* daily digest — OpenStanley's report to its owner */}
        <DigestCard />

        {/* summary tiles */}
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile value={fmt(s.total_impressions)} label={t('insights.impressions')} />
          <StatTile value={fmt(s.total_engagement)} label={t('insights.engagement')} />
          <StatTile value={`${(s.avg_engagement_rate * 100).toFixed(2)}%`} label={t('insights.engRate')} />
          <StatTile
            value={fmt(data.engagement_over_time.reduce((n, p) => n + p.posts, 0))}
            label={t('insights.posts')}
          />
        </div>

        {/* growth — real followers + engagement rate over days */}
        {hasGrowth ? (
          <div className="mb-4">
            <ChartCard
              title={t('insights.growth')}
              badge={
                growth?.followers_delta != null ? (
                  <Badge variant={growth.followers_delta >= 0 ? 'green' : 'red'}>
                    {growth.followers_delta >= 0 ? '+' : ''}
                    {fmt(growth.followers_delta)} {t('insights.followers')}
                  </Badge>
                ) : undefined
              }
            >
              <div className="mb-2 flex items-baseline gap-2 text-[12px] text-muted">
                <span className="inline-flex items-center gap-1">
                  <TrendingUp size={12} />
                  {t('insights.followers')}:
                </span>
                <span dir="ltr" className="font-semibold text-fg">
                  {fmt(growth?.followers_start ?? 0)} → {fmt(growth?.followers_end ?? 0)}
                </span>
                <span className="text-muted/70">({growth?.days ?? 0}d)</span>
              </div>
              <div className="h-[220px]" dir="ltr">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={growthData} margin={{ top: 4, right: 4, left: -8, bottom: 0 }}>
                    <defs>
                      <linearGradient id="gFol" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#e0a53f" stopOpacity={0.4} />
                        <stop offset="100%" stopColor="#e0a53f" stopOpacity={0.03} />
                      </linearGradient>
                      <linearGradient id="gRate" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#7c6cff" stopOpacity={0.45} />
                        <stop offset="100%" stopColor="#7c6cff" stopOpacity={0.03} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: GRID }} />
                    <YAxis yAxisId="f" tick={AXIS_TICK} tickLine={false} axisLine={false} />
                    <YAxis yAxisId="r" orientation="right" tick={AXIS_TICK} tickLine={false} axisLine={false} />
                    <RTooltip contentStyle={TOOLTIP_STYLE} cursor={{ stroke: '#e0a53f', strokeOpacity: 0.3 }} />
                    <Area
                      yAxisId="f"
                      type="monotone"
                      dataKey="followers"
                      name={t('insights.followers')}
                      stroke="#e0a53f"
                      strokeWidth={2}
                      fill="url(#gFol)"
                      connectNulls
                    />
                    <Area
                      yAxisId="r"
                      type="monotone"
                      dataKey="ratePct"
                      name={t('insights.engRatePct')}
                      stroke="#7c6cff"
                      strokeWidth={2}
                      fill="url(#gRate)"
                      connectNulls
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
          </div>
        ) : null}

        {/* engagement over time */}
        {data.engagement_over_time.length > 0 ? (
          <div className="mb-4">
            <ChartCard title={t('insights.overTime')}>
              <div className="h-[230px]" dir="ltr">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.engagement_over_time} margin={{ top: 4, right: 8, left: -14, bottom: 0 }}>
                    <defs>
                      <linearGradient id="gImp" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#7c6cff" stopOpacity={0.45} />
                        <stop offset="100%" stopColor="#7c6cff" stopOpacity={0.03} />
                      </linearGradient>
                      <linearGradient id="gEng" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#3fb96d" stopOpacity={0.5} />
                        <stop offset="100%" stopColor="#3fb96d" stopOpacity={0.03} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: GRID }} />
                    <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} />
                    <RTooltip contentStyle={TOOLTIP_STYLE} cursor={{ stroke: '#7c6cff', strokeOpacity: 0.3 }} />
                    <Area
                      type="monotone"
                      dataKey="impressions"
                      name={t('insights.impressionsLabel')}
                      stroke="#7c6cff"
                      strokeWidth={2}
                      fill="url(#gImp)"
                    />
                    <Area
                      type="monotone"
                      dataKey="engagement"
                      name={t('insights.engagementLabel')}
                      stroke="#3fb96d"
                      strokeWidth={2}
                      fill="url(#gEng)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
          </div>
        ) : null}

        <div className="mb-4 grid gap-4 md:grid-cols-2">
          {/* best hours — real own-post data when available, heuristic below 20 posts */}
          {bestHoursData.length > 0 ? (
            <ChartCard
              title={t('insights.bestHours')}
              badge={
                <Badge variant={bestHoursReal ? 'green' : 'default'}>
                  {bestHoursReal ? t('insights.realData') : t('insights.heuristicData')}
                </Badge>
              }
            >
              <div className="h-[190px]" dir="ltr">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={bestHoursData} margin={{ top: 4, right: 4, left: -22, bottom: 0 }}>
                    <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="hour" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: GRID }} />
                    <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} />
                    <RTooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: '#7c6cff', fillOpacity: 0.08 }} />
                    <Bar dataKey="avg_engagement" name={t('insights.avgEng')} fill="#a89bff" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              {/* why the scheduler would pick these — same reason strings as the Calendar chips */}
              {times?.reasons && Object.keys(times.reasons).length > 0 ? (
                <div className="mt-2 flex flex-col gap-0.5 border-t border-edge/60 pt-2">
                  {Object.entries(times.reasons)
                    .sort(([a], [b]) => Number(a) - Number(b))
                    .map(([h, r]) => (
                      <div key={h} className="truncate text-[11px] text-muted" title={r}>
                        <span className="font-mono text-accent2">{h.padStart(2, '0')}:00</span>
                        {' — '}
                        {r}
                      </div>
                    ))}
                </div>
              ) : null}
            </ChartCard>
          ) : null}

          {/* format performance */}
          {data.format_performance.length > 0 ? (
            <ChartCard title={t('insights.formats')}>
              <div className="h-[190px]" dir="ltr">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    data={data.format_performance}
                    layout="vertical"
                    margin={{ top: 4, right: 12, left: 8, bottom: 0 }}
                  >
                    <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
                    <XAxis type="number" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: GRID }} />
                    <YAxis
                      type="category"
                      dataKey="format"
                      tick={AXIS_TICK}
                      tickLine={false}
                      axisLine={false}
                      width={82}
                    />
                    <RTooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: '#4fc3f7', fillOpacity: 0.08 }} />
                    <Bar dataKey="avg_engagement" name={t('insights.avgEng')} fill="#4fc3f7" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
          ) : null}
        </div>

        {/* heatmap + language mix */}
        <div className="mb-4 grid gap-4 md:grid-cols-[1fr_auto]">
          {data.hours_heatmap.length > 0 ? (
            <ChartCard title={t('insights.heatmap')}>
              <div className="overflow-x-auto" dir="ltr">
                <div className="min-w-[560px]">
                  {dayKeys.map((dk, day) => (
                    <div key={day} className="mb-1 flex items-center gap-1.5">
                      <span className="w-9 shrink-0 text-[10.5px] text-muted">{t(dk)}</span>
                      <div className="flex flex-1 gap-[3px]">
                        {Array.from({ length: 24 }, (_, hour) => {
                          const v = heat.map.get(`${day}-${hour}`) ?? 0;
                          const alpha = heat.max > 0 ? 0.05 + 0.95 * (v / heat.max) : 0.05;
                          return (
                            <div
                              key={hour}
                              title={`${t(dk)} ${hour}:00 — ${v}`}
                              className="h-4 flex-1 rounded-[3px]"
                              style={{ backgroundColor: `rgba(124, 108, 255, ${alpha})` }}
                            />
                          );
                        })}
                      </div>
                    </div>
                  ))}
                  <div className="ms-[42px] mt-1 flex gap-[3px]">
                    {Array.from({ length: 24 }, (_, h) => (
                      <span key={h} className="flex-1 text-center text-[9px] text-muted/70">
                        {h % 3 === 0 ? h : ''}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </ChartCard>
          ) : null}

          {data.language_mix.length > 0 ? (
            <ChartCard title={t('insights.langMix')}>
              <div className="flex h-[190px] items-center gap-5" dir="ltr">
                <ResponsiveContainer width={170} height="100%">
                  <PieChart>
                    <Pie
                      data={data.language_mix}
                      dataKey="count"
                      nameKey="language"
                      innerRadius={40}
                      outerRadius={68}
                      paddingAngle={3}
                      strokeWidth={0}
                    >
                      {data.language_mix.map((e) => (
                        <Cell key={e.language} fill={LANG_COLORS[e.language] ?? '#9b9ba7'} />
                      ))}
                    </Pie>
                    <RTooltip contentStyle={TOOLTIP_STYLE} />
                  </PieChart>
                </ResponsiveContainer>
                <div className="flex flex-col gap-2">
                  {data.language_mix.map((e) => (
                    <span key={e.language} className="flex items-center gap-2 text-[12.5px]">
                      <i
                        className="h-2.5 w-2.5 rounded-full"
                        style={{ backgroundColor: LANG_COLORS[e.language] ?? '#9b9ba7' }}
                      />
                      {e.language} — {e.count}
                    </span>
                  ))}
                </div>
              </div>
            </ChartCard>
          ) : null}
        </div>

        {/* top posts — ranked by follower-normalized engagement rate */}
        {top && top.posts.length > 0 ? (
          <div className="mb-4">
            <ChartCard title={t('insights.topPosts')}>
              <div className="flex flex-col gap-1.5">
                {top.posts.map((p) => (
                  <div
                    key={p.x_id ?? `${p.rank}`}
                    className="flex items-center gap-3 rounded-lg border border-edge/60 bg-panel2/40 px-3 py-2"
                  >
                    <span className="w-5 shrink-0 text-center text-[13px] font-bold text-muted">
                      {p.rank}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] leading-snug">{p.text}</div>
                      <div className="mt-0.5 flex items-center gap-3 text-[11px] text-muted">
                        <span className="inline-flex items-center gap-1">
                          <Heart size={10} className="text-bad" /> {fmt(p.likes)}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Repeat2 size={10} /> {fmt(p.reposts)}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <MessageCircle size={10} /> {fmt(p.replies)}
                        </span>
                      </div>
                    </div>
                    <span
                      className="shrink-0 text-[12px] font-semibold text-good"
                      title={t('insights.rate')}
                    >
                      {(p.rate * 100).toFixed(2)}%
                    </span>
                    {p.url ? (
                      <a
                        href={p.url}
                        target="_blank"
                        rel="noreferrer"
                        title={t('insights.openPost')}
                        className="shrink-0 text-muted transition-colors hover:text-fg"
                      >
                        <ExternalLink size={13} />
                      </a>
                    ) : null}
                  </div>
                ))}
              </div>
            </ChartCard>
          </div>
        ) : null}

        {/* best post */}
        {s.best_post?.text ? (
          <div className="rounded-xl border border-edge bg-panel p-4">
            <div className="mb-2 flex items-center gap-2">
              <Badge variant="green">{t('insights.bestPost')}</Badge>
              <span className="inline-flex items-center gap-3 text-[12px] text-muted">
                <span className="inline-flex items-center gap-1">
                  <Heart size={12} className="text-bad" /> {fmt(s.best_post.likes)}
                </span>
                <span className="inline-flex items-center gap-1">
                  <MessageCircle size={12} /> {fmt(s.best_post.replies)}
                </span>
              </span>
            </div>
            <div className="whitespace-pre-wrap text-[14px] leading-relaxed">{s.best_post.text}</div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
