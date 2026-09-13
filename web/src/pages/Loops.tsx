import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Play, Plus, Repeat, Settings2, Trash2, Zap } from 'lucide-react';
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

interface CustomLoop {
  id: string;
  name: string;
  source: string;
  param: string;
  interval_h: number;
  draft_count: number;
  enabled: boolean;
  last_run: string | null;
  last_result: string | null;
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
  const [custom, setCustom] = useState<CustomLoop[]>([]);
  const [sources, setSources] = useState<Record<string, string>>({});
  const [showNew, setShowNew] = useState(false);
  const [form, setForm] = useState({ name: '', source: 'github_trending', param: '', interval_h: 24, draft_count: 1 });
  const [busy, setBusy] = useState<string | null>(null);

  const load = (): void => {
    void (async () => {
      try {
        const [b, l, c] = await Promise.all([
          api<{ behaviors: Behavior[] }>('behaviors'),
          api<{ loops: LoopRow[] }>('loops/status?all=1').catch(() => ({ loops: [] as LoopRow[] })),
          api<{ loops: CustomLoop[]; sources: Record<string, string> }>('custom-loops').catch(() => ({ loops: [] as CustomLoop[], sources: {} })),
        ]);
        setBehaviors(b.behaviors);
        setLoops(l.loops);
        setCustom(c.loops);
        setSources(c.sources);
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

  const createLoop = (): void => {
    setBusy('new-loop');
    void (async () => {
      try {
        await apiPost('custom-loops', form);
        toast.success(t('loops.created', { name: form.name }));
        setShowNew(false);
        setForm({ name: '', source: 'github_trending', param: '', interval_h: 24, draft_count: 1 });
        load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    })();
  };

  const customAction = (id: string, action: 'toggle' | 'run' | 'delete'): void => {
    setBusy(id + action);
    void (async () => {
      try {
        if (action === 'toggle') {
          await apiPost(`custom-loops/${id}/toggle`, { text: '' });
        } else if (action === 'run') {
          const r = await apiPost<{ note: string }>(`custom-loops/${id}/run`, {});
          toast.info(r.note || t('loops.started', { name: id }));
        } else {
          await fetch(`/api/custom-loops/${id}`, { method: 'DELETE' });
        }
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

        {/* your custom loops */}
        <section className="mb-8">
          <div className="mb-1 flex items-center justify-between">
            <h2 className="flex items-center gap-2 font-serif text-[20px]">
              <Settings2 size={16} className="text-accent2" />
              {t('loops.customTitle')}
            </h2>
            <Button size="sm" onClick={() => setShowNew(!showNew)}>
              <Plus size={13} /> {t('loops.newLoop')}
            </Button>
          </div>
          <p className="mb-4 text-[12.5px] text-ink-3">{t('loops.customHint')}</p>

          {showNew ? (
            <div className="mb-4 rounded-xl border border-edge bg-panel p-4">
              <div className="grid gap-3 md:grid-cols-2">
                <label className="flex flex-col gap-1 text-[12px] text-ink-2">
                  {t('loops.nameLabel')}
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder={t('loops.namePh')}
                    className="rounded-lg border border-edge bg-field px-2.5 py-1.5 text-[13px] text-ink"
                  />
                </label>
                <label className="flex flex-col gap-1 text-[12px] text-ink-2">
                  {t('loops.sourceLabel')}
                  <select
                    value={form.source}
                    onChange={(e) => setForm({ ...form, source: e.target.value })}
                    className="rounded-lg border border-edge bg-field px-2.5 py-1.5 text-[13px] text-ink"
                  >
                    {Object.entries(sources).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </select>
                </label>
                {form.source !== 'github_trending' ? (
                  <label className="flex flex-col gap-1 text-[12px] text-ink-2">
                    {form.source === 'github_user' ? t('loops.userLabel') : t('loops.topicLabel')}
                    <input
                      value={form.param}
                      onChange={(e) => setForm({ ...form, param: e.target.value })}
                      placeholder={form.source === 'github_user' ? 'torvalds' : 'AI agents'}
                      className="rounded-lg border border-edge bg-field px-2.5 py-1.5 text-[13px] text-ink"
                      dir="ltr"
                    />
                  </label>
                ) : null}
                <label className="flex flex-col gap-1 text-[12px] text-ink-2">
                  {t('loops.intervalLabel')}
                  <select
                    value={form.interval_h}
                    onChange={(e) => setForm({ ...form, interval_h: Number(e.target.value) })}
                    className="rounded-lg border border-edge bg-field px-2.5 py-1.5 text-[13px] text-ink"
                  >
                    <option value={6}>{t('loops.every6h')}</option>
                    <option value={12}>{t('loops.every12h')}</option>
                    <option value={24}>{t('loops.every24h')}</option>
                    <option value={168}>{t('loops.everyWeek')}</option>
                  </select>
                </label>
              </div>
              <div className="mt-3 flex gap-2">
                <Button size="sm" disabled={busy === 'new-loop' || !form.name.trim()} onClick={createLoop}>
                  {t('loops.createLoop')}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setShowNew(false)}>
                  {t('common.cancel')}
                </Button>
              </div>
            </div>
          ) : null}

          {custom.length === 0 ? (
            <p className="rounded-xl border border-dashed border-edge py-6 text-center text-[12.5px] text-ink-3">
              {t('loops.noneYet')}
            </p>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {custom.map((l) => (
                <div key={l.id} className={`rounded-xl border p-4 ${l.enabled ? 'border-accent/40 bg-accent/5' : 'border-edge bg-panel'}`}>
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <span className="font-medium">{l.name}</span>
                    <div className="flex items-center gap-1">
                      <Button size="icon-sm" variant="ghost" disabled={busy === l.id + 'run'} onClick={() => customAction(l.id, 'run')} title={t('loops.runNow')}>
                        <Play size={12} />
                      </Button>
                      <Button size="icon-sm" variant="ghost" disabled={busy === l.id + 'toggle'} onClick={() => customAction(l.id, 'toggle')} title={l.enabled ? t('loops.turnOff') : t('loops.turnOn')}>
                        <Zap size={12} className={l.enabled ? 'text-teal' : 'text-ink-3'} />
                      </Button>
                      <Button size="icon-sm" variant="ghost" disabled={busy === l.id + 'delete'} onClick={() => customAction(l.id, 'delete')} title={t('loops.deleteLoop')}>
                        <Trash2 size={12} />
                      </Button>
                    </div>
                  </div>
                  <p className="text-[12px] text-muted">
                    {sources[l.source] || l.source}{l.param ? ` - ${l.param}` : ''}
                  </p>
                  <p className="mt-1 text-[11px] text-ink-3">
                    {t('loops.everyLabel', { n: l.interval_h })} - {l.last_run ? l.last_result || '' : t('loops.neverRun')}
                  </p>
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
