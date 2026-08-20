import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { toast } from 'sonner';
import { Lightbulb, PenLine, RefreshCw, Trash2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/PageHeader';
import { Sparkles } from 'lucide-react';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  type Idea,
  type IdeaBank,
  type IdeaSource,
  type ReplenishResult,
} from '@/lib/api';
import { errMsg } from '@/lib/utils';
import { triggerLoop } from '@/lib/loops';

/** The four mining-chain badges get fixed colors; anything else (e.g. an
 * LLM-written evidence note) stays a plain badge. */
const SRC_VARIANTS: Record<IdeaSource, 'cyan' | 'accent' | 'green' | 'amber'> = {
  scan: 'cyan',
  brain: 'accent',
  study: 'green',
  evergreen: 'amber',
};

const isIdeaSource = (s?: string): s is IdeaSource =>
  !!s && s in SRC_VARIANTS;

interface HookPattern {
  id: number;
  pattern: string;
  why: string;
  example: string;
  added_at?: string;
}

export function IdeasPage() {
  const { t, navigate } = useApp();
  const [ideas, setIdeas] = useState<Idea[] | null>(null);
  const [bank, setBank] = useState<IdeaBank | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [replenishing, setReplenishing] = useState(false);

  const load = (): void => {
    void (async () => {
      try {
        const [list, health] = await Promise.all([
          api<Idea[]>('ideas'),
          api<IdeaBank>('ideas/bank'),
        ]);
        setIdeas(list);
        setBank(health);
      } catch {
        setIdeas([]);
      }
    })();
  };

  useEffect(load, []);

  const [hooksL, setHooks] = useState<HookPattern[] | null>(null);
  const [remixingId, setRemixingId] = useState<number | null>(null);
  const loadHooks = (): void => {
    void (async () => {
      try {
        const r = await api<{ hooks: HookPattern[] }>('hooks');
        setHooks(r.hooks);
      } catch {
        setHooks([]);
      }
    })();
  };
  useEffect(loadHooks, []);
  const remixHook = (id: number): void => {
    setRemixingId(id);
    void (async () => {
      try {
        const r = await apiPost<{ ok: boolean; draft_id: number }>(`hooks/${id}/remix`, {});
        toast.success(t('hooks.remixed', { id: r.draft_id }));
      } catch (e) {
        toast.error(t('hooks.remixFailed', { msg: errMsg(e) }));
      } finally {
        setRemixingId(null);
      }
    })();
  };

  const writeIt = (id: number): void => {
    setBusyId(id);
    void triggerLoop('create', t).then((ok) => {
      setBusyId(null);
      if (ok) {
        toast.success(t('ideas.writing'));
        navigate('inbox');
      }
    });
  };

  const discard = (id: number): void => {
    void (async () => {
      try {
        await apiPost(`ideas/${id}/discard`, {});
        toast.success(t('ideas.discarded'));
        load();
      } catch (e) {
        toast.error(t('ideas.discardFailed', { msg: errMsg(e) }));
      }
    })();
  };

  const replenish = (): void => {
    setReplenishing(true);
    void (async () => {
      try {
        const r = await apiPost<ReplenishResult>('ideas/replenish', {});
        if (r.added > 0) {
          toast.success(t('ideas.replenished', {
            n: r.added,
            sources: (r.sources ?? []).map((s) => t(`ideas.src.${s}`)).join(' · '),
          }));
        } else {
          toast.info(t('ideas.replenishNone'));
        }
        load();
      } catch (e) {
        toast.error(t('ideas.replenishFailed', { msg: errMsg(e) }));
      } finally {
        setReplenishing(false);
      }
    })();
  };

  const count = bank?.count ?? 0;
  const lastAt = bank?.last?.at ?? '';
  const lastWhen = lastAt ? lastAt.slice(0, 16).replace('T', ' ') : '';

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[880px] px-6 py-6">
        <PageHeader title={t('ideas.title')} subtitle={t('ideas.subtitle')} />

        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant={count >= 15 ? 'green' : count >= 8 ? 'amber' : 'red'}>
              {t('ideas.bankHealth')} · {t('ideas.bankCount', { n: count })}
            </Badge>
            {bank && (lastAt ? (
              <Badge>{t('ideas.lastReplenish', {
                n: bank.last?.added ?? 0,
                when: lastWhen,
              })}</Badge>
            ) : (
              <Badge>{t('ideas.neverReplenished')}</Badge>
            ))}
          </div>
          <Button size="sm" onClick={replenish} disabled={replenishing}>
            <RefreshCw size={13} className={replenishing ? 'animate-spin' : ''} />
            {replenishing ? t('ideas.replenishing') : t('ideas.replenishNow')}
          </Button>
        </div>

        {ideas === null ? (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-24" />
            ))}
          </div>
        ) : ideas.length === 0 ? (
          <div className="flex items-center justify-center gap-2 py-12 text-[13px] text-muted">
            <Lightbulb size={15} />
            {t('ideas.empty')}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {ideas.map((idea, i) => (
              <motion.div
                key={idea.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.16, delay: Math.min(i * 0.02, 0.2) }}
                className="rounded-xl border border-edge bg-panel p-4"
              >
                <div className="font-semibold">{idea.title}</div>
                <div className="mt-1 text-[13px] text-muted">{idea.angle}</div>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <Badge variant="accent">{idea.format}</Badge>
                  <Badge variant="green">
                    {t('common.score')} {idea.score.toFixed ? idea.score.toFixed(1) : idea.score}
                  </Badge>
                  {idea.source ? (
                    isIdeaSource(idea.source) ? (
                      <Badge variant={SRC_VARIANTS[idea.source]}>
                        {t(`ideas.src.${idea.source}`)}
                      </Badge>
                    ) : (
                      <Badge>{idea.source}</Badge>
                    )
                  ) : null}
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => writeIt(idea.id)}
                    disabled={busyId !== null}
                  >
                    <PenLine size={13} /> {t('ideas.writeIt')}
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => discard(idea.id)}>
                    <Trash2 size={13} /> {t('common.discard')}
                  </Button>
                </div>
              </motion.div>
            ))}
          </div>
        )}

        {/* — steal this hook — */}
        <div className="mt-8">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-1.5 font-serif text-[19px]">
              <Sparkles size={15} className="text-accent-ink" />
              {t('hooks.title')}
            </h2>
            <span className="font-serif text-[12.5px] italic text-ink-3">
              {hooksL === null ? '' : hooksL.length === 0 ? t('hooks.none') : `${hooksL.length}`}
            </span>
          </div>
          {hooksL !== null && hooksL.length > 0 ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {hooksL.map((h) => (
                <div key={h.id} className="rounded-xl border border-edge bg-panel p-4">
                  <div className="font-serif text-[15px] leading-snug">{h.pattern}</div>
                  <div className="mt-1.5 text-[12.5px] text-muted">{h.why}</div>
                  {h.example ? (
                    <div className="mt-1.5 border-s-2 border-line ps-2 text-[12px] italic text-ink-3">
                      {h.example}
                    </div>
                  ) : null}
                  <div className="mt-3">
                    <Button size="sm" variant="primary" disabled={remixingId !== null}
                            onClick={() => remixHook(h.id)}>
                      {remixingId === h.id ? t('hooks.remixing') : t('hooks.remix')}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
