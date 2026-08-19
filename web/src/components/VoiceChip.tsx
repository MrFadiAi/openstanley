import { Mic, Wrench } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { VoiceCheckMeta } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useApp } from '@/lib/app-context';

/** green ≥ threshold default (75), amber in the borderline zone (55+), red below */
function scoreVariant(score: number): string {
  if (score >= 75) return 'border-good/50 bg-good/15 text-good';
  if (score >= 55) return 'border-warn/50 bg-warn/15 text-warn';
  return 'border-bad/50 bg-bad/15 text-bad';
}

interface VoiceChipProps {
  voice: VoiceCheckMeta | undefined | null;
  /** compact inline variant (no popover) */
  plain?: boolean;
}

/**
 * v0.4.0 voice-lock chip: mic icon + score, colored by verdict, popover with
 * the violations (and a "fixed by voice lock" note when the rewrite won).
 */
export function VoiceChip({ voice, plain }: VoiceChipProps) {
  const { t } = useApp();
  if (!voice || voice.checked !== true) return null;

  const chip = (
    <span
      className={cn(
        'inline-flex cursor-help items-center gap-1 rounded-full border px-2 py-0.5',
        'text-[11px] font-semibold leading-4',
        scoreVariant(voice.score),
      )}
    >
      <Mic size={10} />
      {t('voice.chip', { n: voice.score })}
      {voice.fixed ? <Wrench size={9} className="opacity-80" /> : null}
    </span>
  );
  if (plain) return chip;

  return (
    <Popover>
      <PopoverTrigger asChild>{chip}</PopoverTrigger>
      <PopoverContent className="w-72">
        <div className="mb-1 flex items-center justify-between">
          <span className="text-[12px] font-semibold uppercase tracking-wider text-muted">
            {t('voice.lockTitle')}
          </span>
          <span className="font-mono text-[12px] font-semibold">{voice.score}/100</span>
        </div>
        <p className="mb-2 text-[11.5px] text-muted">{t('voice.lockHint')}</p>
        {voice.fixed ? (
          <div className="mb-2 rounded-lg border border-good/40 bg-good/10 px-2.5 py-1.5 text-[11.5px] font-medium text-good">
            {t('voice.fixed')}
          </div>
        ) : null}
        {voice.violations?.length ? (
          <div className="flex flex-col gap-1 border-t border-edge pt-2 text-[11.5px] text-muted">
            {voice.violations.slice(0, 5).map((v, i) => (
              <div key={i}>· {v}</div>
            ))}
          </div>
        ) : (
          <div className="border-t border-edge pt-2 text-[11.5px] text-good">
            {t('voice.clean')}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
