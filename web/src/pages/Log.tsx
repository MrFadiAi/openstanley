import { useEffect, useState } from 'react';
import { ScrollText } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import type { LogEntry } from '@/lib/api';
import { api } from '@/lib/api';
import { cn } from '@/lib/utils';

function levelVariant(level: string): 'green' | 'amber' | 'red' | 'default' {
  if (level === 'ok' || level === 'success') return 'green';
  if (level === 'warn' || level === 'warning') return 'amber';
  if (level === 'error') return 'red';
  return 'default';
}

export function LogPage() {
  const { t, lang } = useApp();
  const [rows, setRows] = useState<LogEntry[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = (): void => {
      void (async () => {
        try {
          const r = await api<LogEntry[]>('log?limit=120');
          if (alive) setRows(r);
        } catch {
          if (alive) setRows([]);
        }
      })();
    };
    load();
    const id = setInterval(load, 10000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const fmt = (ts: string): string => {
    const d = new Date(ts);
    return Number.isNaN(d.getTime())
      ? ts
      : d.toLocaleString(lang === 'ar' ? 'ar' : 'en-GB', {
          day: '2-digit',
          month: 'short',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        });
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1060px] px-6 py-6">
        <PageHeader title={t('log.title')} subtitle={t('log.subtitle')} />

        {rows === null ? (
          <Skeleton className="h-72" />
        ) : rows.length === 0 ? (
          <div className="flex items-center justify-center gap-2 py-12 text-[13px] text-muted">
            <ScrollText size={15} />
            {t('log.empty')}
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-edge bg-panel">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b border-edge text-start text-[11px] uppercase tracking-wider text-muted">
                  <th className="px-4 py-2.5 text-start font-semibold">{t('log.time')}</th>
                  <th className="px-4 py-2.5 text-start font-semibold">{t('log.level')}</th>
                  <th className="px-4 py-2.5 text-start font-semibold">{t('log.loop')}</th>
                  <th className="px-4 py-2.5 text-start font-semibold">{t('log.message')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr
                    key={i}
                    className={cn(
                      'border-b border-edge/60',
                      i % 2 === 1 && 'bg-panel2/30',
                    )}
                  >
                    <td className="whitespace-nowrap px-4 py-2 font-mono text-[11.5px] text-muted">
                      {fmt(r.ts)}
                    </td>
                    <td className="px-4 py-2">
                      <Badge variant={levelVariant(r.level)}>{r.level}</Badge>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 font-mono text-[12px] text-accent2">
                      {r.loop}
                    </td>
                    <td className="px-4 py-2">{r.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
