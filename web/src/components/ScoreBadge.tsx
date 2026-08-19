import { FlaskConical } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { AlgScore } from '@/lib/api';
import { cn } from '@/lib/utils';

function scoreVariant(score: number): string {
  if (score >= 80) return 'border-good/50 bg-good/15 text-good';
  if (score >= 65) return 'border-accent/50 bg-accent/15 text-accent2';
  if (score >= 50) return 'border-warn/50 bg-warn/15 text-warn';
  return 'border-bad/50 bg-bad/15 text-bad';
}

interface ScoreBadgeProps {
  alg: AlgScore | undefined;
  /** compact inline variant (no popover) */
  plain?: boolean;
}

/** Algorithm score 0-100 chip with factor-breakdown popover. */
export function ScoreBadge({ alg, plain }: ScoreBadgeProps) {
  if (!alg) return null;
  const chip = (
    <span
      className={cn(
        'inline-flex cursor-help items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-4',
        scoreVariant(alg.score),
      )}
    >
      <FlaskConical size={10} />
      {alg.score}
    </span>
  );
  if (plain) return chip;
  return (
    <Popover>
      <PopoverTrigger asChild>{chip}</PopoverTrigger>
      <PopoverContent className="w-80">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[12px] font-semibold uppercase tracking-wider text-muted">
            {alg.score}/100 · {alg.grade}
          </span>
        </div>
        {alg.factors?.length ? (
          <div className="flex flex-col gap-1.5">
            {alg.factors.map((f, i) => (
              <div key={i} className="rounded-lg border border-edge bg-panel2/60 px-2.5 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[12.5px] font-medium">{f.name}</span>
                  <span
                    className={cn(
                      'font-mono text-[12px] font-semibold',
                      f.impact >= 0 ? 'text-good' : 'text-bad',
                    )}
                  >
                    {f.impact >= 0 ? `+${f.impact}` : f.impact}
                  </span>
                </div>
                {f.note ? <div className="mt-0.5 text-[11.5px] text-muted">{f.note}</div> : null}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-[12.5px] text-muted">—</p>
        )}
      </PopoverContent>
    </Popover>
  );
}
