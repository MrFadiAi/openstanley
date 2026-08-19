import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Toaster } from 'sonner';
import { Sidebar } from '@/components/Sidebar';
import { CommandPalette } from '@/components/CommandPalette';
import { Skeleton } from '@/components/ui/skeleton';
import { TooltipProvider } from '@/components/ui/tooltip';
import { AppContext, type Tab } from '@/lib/app-context';
import { makeT, type Lang } from '@/lib/i18n';
import { api, apiPost, type Health, type Settings, type Stats, type XStatus } from '@/lib/api';
import { WritePage } from '@/pages/Write';
import { CalendarPage } from '@/pages/Calendar';
import { InboxPage } from '@/pages/Inbox';
import { IdeasPage } from '@/pages/Ideas';
import { StrategyPage } from '@/pages/Strategy';
import { BrainPage } from '@/pages/Brain';
import { HarnessPage } from '@/pages/Harness';
import { ConnectPage } from '@/pages/Connect';
import { SettingsPage } from '@/pages/Settings';
import { LogPage } from '@/pages/Log';

// recharts is heavy — only fetched when the Insights tab opens
const InsightsPage = lazy(() =>
  import('@/pages/Insights').then((m) => ({ default: m.InsightsPage })),
);

const VALID_TABS: Tab[] = [
  'write',
  'calendar',
  'inbox',
  'ideas',
  'strategy',
  'brain',
  'insights',
  'harness',
  'connect',
  'settings',
  'log',
];

function initialTab(): Tab {
  const v = localStorage.getItem('xs.tab');
  return v && VALID_TABS.includes(v as Tab) ? (v as Tab) : 'write';
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [lang, setLangState] = useState<Lang>(() =>
    localStorage.getItem('xs.lang') === 'ar' ? 'ar' : 'en',
  );
  const [dense, setDenseState] = useState<boolean>(() => localStorage.getItem('xs.dense') === '1');
  const [mode, setMode] = useState<string | null>(null);
  const [inboxCount, setInboxCount] = useState(0);
  const [ideasCount, setIdeasCount] = useState(0);

  const navigate = useCallback((next: Tab) => {
    setTab(next);
    localStorage.setItem('xs.tab', next);
  }, []);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    localStorage.setItem('xs.lang', l);
    document.documentElement.lang = l;
    document.documentElement.dir = l === 'ar' ? 'rtl' : 'ltr';
    // best-effort persist on the backend
    void apiPost('settings', { language: l }).catch(() => undefined);
  }, []);

  const setDense = useCallback((d: boolean) => {
    setDenseState(d);
    localStorage.setItem('xs.dense', d ? '1' : '0');
    document.documentElement.classList.toggle('dense', d);
  }, []);

  const t = useMemo(() => makeT(lang), [lang]);

  // sync language from server settings on first load when no local preference exists
  useEffect(() => {
    if (localStorage.getItem('xs.lang')) return;
    let alive = true;
    api<Settings>('settings')
      .then((s) => {
        if (alive && s.language === 'ar') setLang('ar');
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // poll stats + x status for the sidebar mode chip and inbox badge
  useEffect(() => {
    let alive = true;
    const poll = (): void => {
      void (async () => {
        const [statsR, xR] = await Promise.allSettled([
          api<Stats>('stats'),
          api<XStatus>('x/status'),
        ]);
        if (!alive) return;
        if (statsR.status === 'fulfilled') {
          setInboxCount(statsR.value.drafts?.draft ?? 0);
          setIdeasCount(statsR.value.ideas_bank ?? 0);
        }
        if (xR.status === 'fulfilled' && xR.value.mode) setMode(xR.value.mode);
        else if (xR.status === 'rejected') {
          const h = await api<Health>('health').catch(() => null);
          if (alive && h?.mode) setMode(h.mode);
        }
      })();
    };
    poll();
    const id = setInterval(poll, 20000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const page = (() => {
    switch (tab) {
      case 'write':
        return <WritePage />;
      case 'calendar':
        return <CalendarPage />;
      case 'inbox':
        return <InboxPage />;
      case 'ideas':
        return <IdeasPage />;
      case 'strategy':
        return <StrategyPage />;
      case 'brain':
        return <BrainPage />;
      case 'insights':
        return (
          <Suspense
            fallback={
              <div className="mx-auto max-w-[1060px] px-6 py-6">
                <Skeleton className="mb-5 h-8 w-56" />
                <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {Array.from({ length: 4 }, (_, i) => (
                    <Skeleton key={i} className="h-[62px]" />
                  ))}
                </div>
                <Skeleton className="h-64" />
              </div>
            }
          >
            <InsightsPage />
          </Suspense>
        );
      case 'harness':
        return <HarnessPage />;
      case 'connect':
        return <ConnectPage />;
      case 'settings':
        return <SettingsPage />;
      case 'log':
        return <LogPage />;
    }
  })();

  return (
    <AppContext.Provider value={{ lang, setLang, t, dense, setDense, tab, navigate }}>
      <TooltipProvider delayDuration={250}>
      <div className="flex h-screen overflow-hidden bg-bg">
        <Sidebar mode={mode} inboxCount={inboxCount} ideasCount={ideasCount} />
        <main className="relative flex-1 overflow-hidden">
          <AnimatePresence mode="wait">
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.16, ease: 'easeOut' }}
              className="h-full"
            >
              {page}
            </motion.div>
          </AnimatePresence>
        </main>
        <CommandPalette />
        <Toaster
          theme="dark"
          position="bottom-right"
          gap={8}
          toastOptions={{
            style: {
              background: '#18181f',
              border: '1px solid #26262f',
              color: '#ececf1',
              fontSize: '13px',
            },
          }}
        />
      </div>
      </TooltipProvider>
    </AppContext.Provider>
  );
}
