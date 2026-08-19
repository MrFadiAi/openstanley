import type { ReactNode } from 'react';
import { Clock, Crosshair, TrendingUp, Users } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { TargetScore } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useApp } from '@/lib/app-context';

/** same color ladder as the algorithm ScoreBadge (≥80 good … <50 bad) */
function scoreVariant(score: number): string {
  if (score >= 80) return 'border-good/50 bg-good/15 text-good';
  if (score >= 65) return 'border-accent/50 bg-accent/15 text-accent2';
  if (score >= 50) return 'border-warn/50 bg-warn/15 text-warn';
  return 'border-bad/50 bg-bad/15 text-bad';
}

const VERDICT_KEY = {
  fresh: 'inbox.tvFresh',
  rising: 'inbox.tvRising',
  warm: 'inbox.tvWarm',
  stale: 'inbox.tvStale',
} as const;

function miniChip(icon: ReactNode, label: string): ReactNode {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full border border-edge bg-panel2 px-2 py-0.5',
        'text-[11px] leading-4 text-muted',
      )}
    >
      {icon}
      {label}
    </span>
  );
}

/**
 * v0.3.8 engage quality gate chips for a reply draft: target score chip with
 * component-breakdown popover + fresh / traction / crowd minis, matching the
 * existing chip language on the approval cards.
 */
export function TargetScoreBadge({ ts }: { ts: TargetScore }) {
  const { t } = useApp();
  const age = ts.age_h;
  const comp = ts.components ?? {};
  const verdict = ts.verdict && VERDICT_KEY[ts.verdict]
    ? t(VERDICT_KEY[ts.verdict])
    : null;

  const chip = (
    <span
      className={cn(
        'inline-flex cursor-help items-center gap-1 rounded-full border px-2 py-0.5',
        'text-[11px] font-semibold leading-4',
        scoreVariant(ts.score),
      )}
    >
      <Crosshair size={10} />
      {t('inbox.targetScore', { n: ts.score })}
      {verdict ? <span className="font-medium opacity-80">· {verdict}</span> : null}
    </span>
  );

  const rows: { label: string; v: number | undefined }[] = [
    { label: t('inbox.tcRecency'), v: comp.recency },
    { label: t('inbox.tcTraction'), v: comp.traction },
    { label: t('inbox.tcAuthor'), v: comp.author },
    { label: t('inbox.tcCrowding'), v: comp.crowding },
    { label: t('inbox.tcFit'), v: comp.fit },
  ];

  return (
    <>
      <Popover>
        <PopoverTrigger asChild>{chip}</PopoverTrigger>
        <PopoverContent className="w-72">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {t('inbox.targetTitle')}
            </span>
            <span className="font-mono text-[12px] font-semibold">{ts.score}/100</span>
          </div>
          <p className="mb-2 text-[11.5px] text-muted">{t('inbox.targetHint')}</p>
          <div className="flex flex-col gap-1">
            {rows.map(
              (r) =>
                r.v !== undefined ? (
                  <div key={r.label} className="flex items-center justify-between gap-2">
                    <span className="text-[12.5px]">{r.label}</span>
                    <span className="font-mono text-[12px] text-muted">
                      {Math.round(r.v * 100)}%
                    </span>
                  </div>
                ) : null,
            )}
          </div>
          {ts.reasons?.length ? (
            <div className="mt-2 border-t border-edge pt-2 text-[11.5px] text-muted">
              {ts.reasons.slice(0, 3).map((r, i) => (
                <div key={i}>· {r}</div>
              ))}
            </div>
          ) : null}
        </PopoverContent>
      </Popover>
      {age !== undefined && age !== null
        ? miniChip(
            <Clock size={10} />,
            age > 24
              ? t('inbox.targetStaleAge', { h: Math.round(age) })
              : t('inbox.targetFresh', { h: Math.max(1, Math.round(age)) }),
          )
        : null}
      {comp.traction !== undefined
        ? miniChip(
            <TrendingUp size={10} />,
            t('inbox.targetTraction', { p: Math.round(comp.traction * 100) }),
          )
        : null}
      {comp.crowding !== undefined
        ? miniChip(
            <Users size={10} />,
            t('inbox.targetCrowd', { p: Math.round(comp.crowding * 100) }),
          )
        : null}
    </>
  );
}
