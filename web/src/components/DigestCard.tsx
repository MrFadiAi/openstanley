/**
 * Digest card (Insights page, v0.4.2) — the agent's daily report to its
 * owner: today's digest rendered markdown-lite, a last-7-days history
 * picker, and a send-to-webhook button. RTL-safe (content direction
 * follows the UI language; stored digests render in whatever language
 * they were written in).
 */
import { useCallback, useEffect, useState } from 'react';
import Markdown from 'react-markdown';
import { toast } from 'sonner';
import { Newspaper, Send } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { useApp } from '@/lib/app-context';
import { getDigest, getDigestHistory, sendDigest, type DigestResponse } from '@/lib/api';
import { errMsg } from '@/lib/utils';

export function DigestCard() {
  const { t, lang } = useApp();
  const [digest, setDigest] = useState<DigestResponse | null>(null);
  const [history, setHistory] = useState<string[]>([]);
  const [day, setDay] = useState<string | null>(null); // null = today (live)
  const [busy, setBusy] = useState(false);

  const load = useCallback((d: string | null): void => {
    void (async () => {
      try {
        const [dg, h] = await Promise.all([
          getDigest(d ?? undefined),
          getDigestHistory(7),
        ]);
        setDigest(dg);
        setHistory(h.days);
      } catch (e) {
        toast.error(t('digest.sendFailed', { msg: errMsg(e) }));
      }
    })();
  }, [t]);

  useEffect(() => {
    load(null);
  }, [load]);

  const send = (): void => {
    setBusy(true);
    void (async () => {
      try {
        const r = await sendDigest(day ?? undefined);
        if (r.sent) toast.success(t('digest.sent'));
        else if (r.error) toast.error(t('digest.sendFailed', { msg: r.error }));
        else toast.message(t('digest.notConfigured'));
        load(day);
      } catch (e) {
        toast.error(t('digest.sendFailed', { msg: errMsg(e) }));
      } finally {
        setBusy(false);
      }
    })();
  };

  if (!digest) {
    return <Skeleton className="mb-4 h-[220px]" />;
  }

  const today = new Date().toISOString().slice(0, 10);
  const dir = lang === 'ar' ? 'rtl' : 'ltr';

  return (
    <div className="mb-4 rounded-xl border border-edge bg-panel p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Newspaper size={14} className="text-accent2" />
          <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
            {t('digest.title')}
          </span>
          <span className="font-mono text-[11px] text-muted/70">{digest.day}</span>
          <Badge variant={digest.stored ? 'default' : 'green'}>
            {digest.stored ? t('digest.stored') : t('digest.fresh')}
          </Badge>
        </div>
        <Button variant="default" onClick={send} disabled={busy}>
          <Send size={13} /> {busy ? t('digest.sending') : t('digest.send')}
        </Button>
      </div>

      <div className="text-[12.5px] text-muted">{t('digest.subtitle')}</div>

      {/* history picker — last 7 days with a stored digest + today (live) */}
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        <span className="me-1 text-[11.5px] text-muted">{t('digest.history')}:</span>
        <button
          onClick={() => {
            setDay(null);
            load(null);
          }}
          className={`cursor-pointer rounded-md px-2 py-0.5 font-mono text-[11.5px] transition-colors ${
            day === null ? 'bg-accent text-white' : 'bg-panel2 text-muted hover:text-base'
          }`}
        >
          {t('common.today')}
        </button>
        {history
          .filter((d) => d !== today)
          .map((d) => (
            <button
              key={d}
              onClick={() => {
                setDay(d);
                load(d);
              }}
              className={`cursor-pointer rounded-md px-2 py-0.5 font-mono text-[11.5px] transition-colors ${
                day === d ? 'bg-accent text-white' : 'bg-panel2 text-muted hover:text-base'
              }`}
            >
              {d.slice(5)}
            </button>
          ))}
      </div>

      {/* the report itself — markdown-lite, direction per UI language */}
      <div className="digest-md mt-3" dir={dir}>
        <Markdown
          components={{
            h1: (p) => <h1 className="mb-2 text-[15px] font-bold" {...p} />,
            h2: (p) => (
              <h2 className="mt-3 mb-1.5 border-t border-edge/60 pt-2.5 text-[12px] font-bold text-muted" {...p} />
            ),
            li: (p) => <li className="my-0.5 text-[12.5px] leading-snug text-ink-2" {...p} />,
            p: (p) => <p className="my-1 text-[12.5px] leading-relaxed" {...p} />,
          }}
        >
          {digest.markdown}
        </Markdown>
      </div>
    </div>
  );
}
