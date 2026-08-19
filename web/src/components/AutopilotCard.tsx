/**
 * Autopilot card (Insights page) — the "OpenStanley runs itself" control panel.
 * Toggle (confirm-gated) + interval selector + phase indicator with last-tick
 * age + ticks count + error ring (last 5) + force-tick button.
 * Publish is never on autopilot: the approval gate note is part of the card.
 */
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Play, ShieldCheck, TriangleAlert } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { useApp } from '@/lib/app-context';
import {
  forceAutopilotTick,
  getAutopilot,
  setAutopilot,
  type AutopilotState,
} from '@/lib/api';
import { errMsg } from '@/lib/utils';
import type { I18nKey, TFn } from '@/lib/i18n';

const INTERVALS = [15, 30, 45, 60, 90];

function phaseLabel(phase: string | null, t: TFn): string {
  const key = `autopilot.phase.${phase ?? ''}` as I18nKey;
  return phase ? t(key) : '—';
}

function ageLabel(iso: string | null, t: TFn, now: number): string {
  if (!iso) return t('autopilot.never');
  const mins = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60000));
  if (mins < 60) return t('autopilot.ageMin', { n: mins });
  return t('autopilot.ageHour', { n: Math.round(mins / 60) });
}

export function AutopilotCard() {
  const { t } = useApp();
  const [st, setSt] = useState<AutopilotState | null>(null);
  const [failed, setFailed] = useState(false); // initial fetch failed (retriable)
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());

  const refresh = (): void => {
    void (async () => {
      try {
        setSt(await getAutopilot());
        setFailed(false);
      } catch {
        setFailed(true); // only fatal while we have nothing to show (st null)
      }
    })();
  };

  useEffect(refresh, []);
  // keep the last-tick age live and pull scheduler-driven state changes
  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now());
      refresh();
    }, 30_000);
    return () => clearInterval(id);
  }, []);

  const toggleOn = (v: boolean): void => {
    if (v) {
      setConfirmOpen(true); // enabling self-driving needs an explicit yes
      return;
    }
    void (async () => {
      setBusy(true);
      try {
        setSt(await setAutopilot(false));
        toast.success(t('autopilot.stoppedToast'));
      } catch (e) {
        toast.error(errMsg(e));
      } finally {
        setBusy(false);
      }
    })();
  };

  const confirmEnable = (): void => {
    setConfirmOpen(false);
    void (async () => {
      setBusy(true);
      try {
        setSt(await setAutopilot(true));
        toast.success(t('autopilot.startedToast'));
      } catch (e) {
        toast.error(errMsg(e));
      } finally {
        setBusy(false);
      }
    })();
  };

  const changeInterval = (mins: string): void => {
    void (async () => {
      try {
        setSt(await setAutopilot(st?.enabled ?? false, Number(mins)));
      } catch (e) {
        toast.error(errMsg(e));
      }
    })();
  };

  const runTick = (): void => {
    setBusy(true);
    void (async () => {
      try {
        const r = await forceAutopilotTick();
        setSt(r.state);
        if (r.ok) toast.success(t('autopilot.tickDone', { phase: r.phase }));
        else toast.warning(t('autopilot.tickFailed', { msg: r.error ?? '?' }));
      } catch (e) {
        toast.error(t('autopilot.tickFailed', { msg: errMsg(e) }));
      } finally {
        setBusy(false);
      }
    })();
  };

  const nextKey = (phase: string | null): I18nKey => {
    const order = ['study', 'create', 'engage', 'learn'];
    const idx = phase ? order.indexOf(phase) : -1;
    return `autopilot.phase.${order[(idx + 1) % order.length]}` as I18nKey;
  };

  if (!st) {
    if (failed) {
      return (
        <div className="mb-4 flex items-center gap-2 rounded-xl border border-edge bg-panel p-4 text-[12.5px] text-bad">
          <TriangleAlert size={14} />
          {t('autopilot.title')}: {t('common.error', { msg: 'unreachable' })}
          <Button variant="ghost" className="ml-auto" onClick={refresh}>
            {t('common.retry')}
          </Button>
        </div>
      );
    }
    return <Skeleton className="mb-4 h-[104px]" />;
  }
  const schedulerDown = st.enabled && !st.job_active;

  return (
    <div className="mb-4 rounded-xl border border-edge bg-panel p-4">
      <div className="flex flex-wrap items-center gap-3">
        {/* header + toggle */}
        <div className="min-w-[220px] flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
              {t('autopilot.title')}
            </span>
            {st.enabled && !schedulerDown ? (
              <span className="relative flex size-2">
                <span className="absolute inline-flex size-2 animate-ping rounded-full bg-good/70" />
                <span className="relative inline-flex size-2 rounded-full bg-good" />
              </span>
            ) : null}
          </div>
          <div className={`text-[12.5px] ${schedulerDown ? 'text-warn' : 'text-muted'}`}>
            {schedulerDown
              ? t('autopilot.noJob')
              : st.enabled
                ? t('autopilot.subtitle')
                : t('autopilot.off')}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-[11.5px] text-muted">{t('autopilot.interval')}</span>
          <Select value={String(st.interval_min)} onValueChange={changeInterval}>
            <SelectTrigger className="h-8 w-[104px]" aria-label={t('autopilot.interval')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {INTERVALS.map((m) => (
                <SelectItem key={m} value={String(m)}>
                  {t('autopilot.minutes', { n: m })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Switch
          checked={st.enabled}
          onCheckedChange={toggleOn}
          disabled={busy}
          aria-label={t('autopilot.title')}
        />
      </div>

      {/* phase row */}
      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px] text-muted">
        <span className="flex items-center gap-1.5">
          {t('autopilot.phase')}:
          <Badge variant={st.enabled ? 'accent' : 'default'}>
            {phaseLabel(st.phase, t)}
          </Badge>
        </span>
        <span className="flex items-center gap-1.5">
          {t('autopilot.nextPhase')}:
          <b className="font-medium text-base">{t(nextKey(st.phase))}</b>
        </span>
        <span>
          {t('autopilot.lastTick')}: <b className="font-medium text-base">{ageLabel(st.last_tick, t, now)}</b>
        </span>
        <span>
          {t('autopilot.ticks')}: <b className="font-medium text-base tabular-nums">{st.ticks.toLocaleString()}</b>
        </span>
        <Button variant="default" className="ml-auto" onClick={runTick} disabled={busy}>
          <Play size={13} /> {busy ? t('autopilot.running') : t('autopilot.runNow')}
        </Button>
      </div>

      {/* error ring */}
      <div className="mt-2 text-[12px]">
        <span className="text-muted">{t('autopilot.errors')}: </span>
        {st.errors.length === 0 ? (
          <span className="text-good">{t('autopilot.noErrors')}</span>
        ) : (
          <ul className="mt-1 space-y-0.5">
            {st.errors.map((e, i) => (
              <li key={`${i}-${e}`} className="flex items-start gap-1.5 text-bad">
                <span className="mt-[6px] size-1.5 shrink-0 rounded-full bg-bad" />
                <span className="min-w-0 break-words">{e}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-3 flex items-center gap-1.5 border-t border-edge pt-2.5 text-[11.5px] text-muted">
        <ShieldCheck size={13} className="shrink-0 text-good" />
        {t('autopilot.gate')}
      </div>

      {/* enable confirmation */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogTitle>{t('autopilot.title')}</DialogTitle>
          <DialogDescription>{t('autopilot.confirmEnable')}</DialogDescription>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button variant="primary" onClick={confirmEnable}>
              {t('common.approve')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
