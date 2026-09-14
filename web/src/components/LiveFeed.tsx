import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Activity, ExternalLink } from 'lucide-react';
import { useApp } from '@/lib/app-context';
import { api } from '@/lib/api';

interface PublishEvent {
  id: number;
  ts: string;
  kind: string;
  x_id: string | null;
  text: string;
}

/** Live panel — publishes appear the moment they ship (owner 2026-09-14:
 * 'I want a live panel when something is published'). Polls every 8s;
 * new publishes pulse in + toast with the X link. */
export function LiveFeed() {
  const { t } = useApp();
  const [events, setEvents] = useState<PublishEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [pulse, setPulse] = useState(false);
  const sinceRef = useRef<string>('');
  const firstRef = useRef(true);

  useEffect(() => {
    const tick = (): void => {
      void (async () => {
        try {
          const r = await api<{ events: PublishEvent[]; now: string }>(
            `activity${sinceRef.current ? `?since=${encodeURIComponent(sinceRef.current)}` : ''}`,
          );
          sinceRef.current = r.now;
          if (r.events.length) {
            setEvents((prev) => [...r.events, ...prev].slice(0, 12));
            if (firstRef.current) {
              firstRef.current = false;
            } else {
              setOpen(true);
              setPulse(true);
              setTimeout(() => setPulse(false), 4000);
              const newest = r.events[0];
              toast.success(t('live.publishedNow', { id: newest.id }), {
                action: newest.x_id
                  ? { label: t('live.view'), onClick: () => window.open(`https://x.com/_/status/${newest.x_id}`, '_blank', 'noopener') }
                  : undefined,
              });
            }
          }
        } catch {
          /* offline → keep polling silently */
        }
      })();
    };
    tick();
    const iv = setInterval(tick, 8000);
    return () => clearInterval(iv);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!events.length) return null;

  return (
    <div className="fixed bottom-4 end-4 z-50 flex flex-col items-end gap-2">
      {open ? (
        <div className="w-80 rounded-xl border border-edge bg-panel/95 p-3 shadow-lg backdrop-blur">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-[12px] font-medium text-accent2">
              <Activity size={12} className={pulse ? 'animate-pulse text-green' : 'text-green'} />
              {t('live.title')}
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[11px] text-ink-3 hover:text-ink"
            >
              {t('live.hide')}
            </button>
          </div>
          <ul className="flex flex-col gap-2">
            {events.map((e) => (
              <li key={e.id} className="flex items-start gap-2 text-[11.5px]">
                <span className="mt-1 size-1.5 shrink-0 rounded-full bg-green" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-ink-2">{e.text}</p>
                  <p className="mt-0.5 flex items-center gap-1.5 text-ink-3">
                    <span className="font-mono">#{e.id}</span>
                    <span className="font-mono uppercase">{e.kind}</span>
                    <span>{e.ts.slice(11, 16)}</span>
                    {e.x_id ? (
                      <a
                        href={`https://x.com/_/status/${e.x_id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-0.5 text-accent2 hover:underline"
                      >
                        X <ExternalLink size={9} />
                      </a>
                    ) : null}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[11.5px] shadow transition-colors ${pulse ? 'border-green bg-green/10' : 'border-edge bg-panel/90 backdrop-blur hover:border-green/50'}`}
        >
          <span className={`size-2 rounded-full ${pulse ? 'animate-pulse bg-green' : 'bg-green/70'}`} />
          {t('live.badge', { n: events.length })}
        </button>
      )}
    </div>
  );
}
