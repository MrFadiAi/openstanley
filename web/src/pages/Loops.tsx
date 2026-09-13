import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Play, Repeat, Settings2, Zap } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useApp } from '@/lib/app-context';
import { api, apiPost } from '@/lib/api';

interface Behavior {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}

interface LoopRow {
  name: string;
  last_run: string | null;
  last_status: string | null;
  last_message: string | null;
  next_run: string | null;
}

/** Loops — the autonomous control room: toggle optional behaviors the
 * create loop may use, see + fire the system loops. */
export function LoopsPage() {
  const { t } = useApp();
  const [behaviors, setBehaviors] = useState<Behavior[] | null>(null);
  const [loops, setLoops] = useState<LoopRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const load = (): void => {
    void (async () => {
      try {
        const [b, l] = await Promise.all([
          api<{ behaviors: Behavior[] }>('behaviors'),
          api<{ loops: LoopRow[] }>('loops/status?all=1').catch(() => ({ loops: [] as LoopRow[] })),
        ]);
        setBehaviors(b.behaviors);
        setLoops(l.loops);
      } catch {
        setBehaviors([]);
      }
    })();
  };

  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (b: Behavior): void => {
    setBusy(b.id);
    void (async () => {
      try {
        await apiPost(`behaviors/${b.id}`, { text: b.enabled ? 'false' : 'true' });
        toast.success(b.enabled ? t('loops.disabled', { name: b.name }) : t('loops.enabled', { name: b.name }));
        load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    })();
  };

  const runLoop = (name: string): void => {
    setBusy(`loop-${name}`);
    void (async () => {
      try {
        await apiPost(`loops/${name}`, {});
        toast.success(t('loops.started', { name }));
        setTimeout(load, 2500);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    })();
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="h-full px-6 py-6">
        <div className="mb-7">
          <h1 className="font-serif text-[34px] font-medium leading-none tracking-tight">
            {t('loops.title')}
          </h1>
          <p className="mt-2 font-serif text-[14px] italic text-ink-3">{t('loops.subtitle')}</p>
        </div>

        {/* autonomous behaviors */}
        <section className="mb-8">
          <h2 className="mb-1 flex items-center gap-2 font-serif text-[20px]">
            <Zap size={16} className="text-accent2" />
            {t('loops.behaviorsTitle')}
          </h2>
          <p className="mb-4 text-[12.5px] text-ink-3">{t('loops.behaviorsHint')}</p>
          {behaviors === null ? (
            <div className="grid gap-3 md:grid-cols-2">
              {[1, 2, 3].map((i) => <Skeleton key={i} className="h-28 rounded-xl" />)}
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {behaviors.map((b) => (
                <div
                  key={b.id}
                  className={`rounded-xl border p-4 transition-colors ${b.enabled ? 'border-teal/50 bg-teal/5' : 'border-edge bg-panel'}`}
                >
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="font-medium">{b.name}</span>
                    <button
                      type="button"
                      disabled={busy === b.id}
                      onClick={() => toggle(b)}
                      aria-pressed={b.enabled}
                      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors ${b.enabled ? 'border-teal bg-teal/30' : 'border-edge bg-panel2'}`}
                      title={b.enabled ? t('loops.turnOff') : t('loops.turnOn')}
                    >
                      <span
                        className={`inline-block size-4 rounded-full transition-transform ${b.enabled ? 'translate-x-[22px] bg-teal' : 'translate-x-[3px] bg-ink-3'}`}
                      />
                    </button>
                  </div>
                  <p className="text-[12.5px] leading-relaxed text-muted">{b.description}</p>
                  {b.enabled ? (
                    <Badge variant="green" className="mt-2">{t('loops.active')}</Badge>
                  ) : (
                    <Badge className="mt-2">{t('loops.off')}</Badge>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>

        {/* system loops */}
        <section>
          <h2 className="mb-1 flex items-center gap-2 font-serif text-[20px]">
            <Repeat size={16} className="text-accent2" />
            {t('loops.systemTitle')}
          </h2>
          <p className="mb-4 text-[12.5px] text-ink-3">{t('loops.systemHint')}</p>
          <div className="overflow-hidden rounded-xl border border-edge">
            <table className="w-full text-[12.5px]">
              <thead className="bg-panel2/60 text-start text-ink-3">
                <tr>
                  <th className="px-3 py-2 text-start font-semibold">{t('loops.name')}</th>
                  <th className="px-3 py-2 text-start font-semibold">{t('loops.lastRun')}</th>
                  <th className="px-3 py-2 text-start font-semibold">{t('loops.status')}</th>
                  <th className="px-3 py-2 text-end font-semibold" />
                </tr>
              </thead>
              <tbody>
                {loops.map((l) => (
                  <tr key={l.name} className="border-t border-edge/60">
                    <td className="px-3 py-2 font-medium">{l.name}</td>
                    <td className="px-3 py-2 text-muted">{l.last_run ?? '—'}</td>
                    <td className="px-3 py-2">
                      {l.last_status ? (
                        <Badge variant={l.last_status === 'ok' ? 'green' : 'amber'}>
                          {l.last_status}
                        </Badge>
                      ) : '—'}
                    </td>
                    <td className="px-3 py-2 text-end">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy === `loop-${l.name}` || l.name === 'publish'}
                        onClick={() => runLoop(l.name)}
                        title={l.name === 'publish' ? t('loops.publishNote') : t('loops.runNow')}
                      >
                        <Play size={12} />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 flex items-center gap-1 text-[11.5px] text-ink-3">
            <Settings2 size={10} />
            {t('loops.scheduleNote')}
          </p>
        </section>
      </div>
    </div>
  );
}
