import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { RefreshCw, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import { api, apiPost, type Strategy } from '@/lib/api';
import { errMsg, hasArabic } from '@/lib/utils';

const HEADERS = [
  'Strategic Goal',
  'Target Audience',
  'Positioning Line',
  'Content Pillars',
  'Core Message',
  'Voice & Style',
  'Cadence & Formats',
];

function isHeader(line: string): boolean {
  const s = line.trim().replace(/:$/, '');
  if (HEADERS.includes(s)) return true;
  return s.length > 0 && s.length <= 42 && line.trim().endsWith(':') && !s.includes('.');
}

export function StrategyPage() {
  const { t } = useApp();
  const [data, setData] = useState<Strategy | null>(null);
  const [loading, setLoading] = useState(true);
  const [regen, setRegen] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const s = await api<Strategy>('strategy');
        if (alive) setData(s);
      } catch {
        if (alive) setData({});
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const generate = (force: boolean): void => {
    setRegen(true);
    toast.info(t('strategy.generating'));
    void (async () => {
      try {
        await apiPost<Strategy>('strategy', {}, force ? { force: 'true' } : undefined);
        const s = await api<Strategy>('strategy');
        setData(s);
        toast.success(t('strategy.generated'));
      } catch (e) {
        toast.error(t('strategy.generateFailed', { msg: errMsg(e) }));
      } finally {
        setRegen(false);
      }
    })();
  };

  const text = data?.text ?? '';
  const rtl = hasArabic(text);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[860px] px-6 py-6">
        <PageHeader
          title={t('strategy.title')}
          subtitle={t('strategy.subtitle')}
          actions={
            <div className="flex gap-2">
              <Button variant="primary" size="sm" onClick={() => generate(false)} disabled={regen}>
                <Sparkles size={13} /> {t('strategy.generate')}
              </Button>
              <Button size="sm" onClick={() => generate(true)} disabled={regen}>
                <RefreshCw size={13} className={regen ? 'animate-spin' : ''} />{' '}
                {t('strategy.regenerate')}
              </Button>
            </div>
          }
        />

        {loading || regen ? (
          <div className="rounded-xl border border-edge bg-panel p-5">
            <Skeleton className="mb-4 h-5 w-40" />
            <Skeleton className="mb-2.5 h-4 w-full" />
            <Skeleton className="mb-2.5 h-4 w-[92%]" />
            <Skeleton className="mb-2.5 h-4 w-[85%]" />
            <Skeleton className="mb-4 h-4 w-[60%]" />
            <Skeleton className="mb-4 h-5 w-48" />
            <Skeleton className="mb-2.5 h-4 w-[88%]" />
            <Skeleton className="h-4 w-[70%]" />
          </div>
        ) : text ? (
          <div
            dir={rtl ? 'rtl' : 'ltr'}
            className="rounded-xl border border-edge bg-panel p-5 text-[14px] leading-[1.7]"
          >
            {text.split('\n').map((line, i) =>
              isHeader(line) ? (
                <div key={i} className="mb-1.5 mt-5 font-bold text-accent2 first:mt-0">
                  {line.replace(/:$/, '')}
                </div>
              ) : (
                <div key={i} className="whitespace-pre-wrap">
                  {line || ' '}
                </div>
              ),
            )}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed border-edge py-12 text-center text-[13px] text-muted">
            {t('strategy.empty')}
          </div>
        )}
      </div>
    </div>
  );
}
