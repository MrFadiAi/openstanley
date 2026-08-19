import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Brain,
  CalendarDays,
  FlaskConical,
  Inbox,
  Lightbulb,
  Link2,
  PenLine,
  ScrollText,
  Settings,
  Target,
  TrendingUp,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useApp, type Tab } from '@/lib/app-context';
import type { I18nKey } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { AccountSwitcher } from '@/components/AccountSwitcher';

/*
 * Sidebar — rebuilt on the beautifului SidebarNav grammar (beautifului.dev,
 * MIT © 2026 Shane Levine): workspace row, quick search, accent action,
 * sectioned items with a single gliding highlight + pop-in counts.
 * Keeps every OpenStanley tab, AR/EN labels and RTL.
 */

interface NavItem {
  tab: Tab;
  icon: LucideIcon;
  key: I18nKey;
  section: 'workspace' | 'objects' | 'system';
  count?: 'inbox' | 'ideas';
}

const NAV: NavItem[] = [
  { tab: 'write', icon: PenLine, key: 'nav.write', section: 'workspace' },
  { tab: 'calendar', icon: CalendarDays, key: 'nav.calendar', section: 'workspace' },
  { tab: 'inbox', icon: Inbox, key: 'nav.inbox', section: 'workspace', count: 'inbox' },
  { tab: 'ideas', icon: Lightbulb, key: 'nav.ideas', section: 'objects', count: 'ideas' },
  { tab: 'strategy', icon: Target, key: 'nav.strategy', section: 'objects' },
  { tab: 'brain', icon: Brain, key: 'nav.brain', section: 'objects' },
  { tab: 'insights', icon: TrendingUp, key: 'nav.insights', section: 'objects' },
  { tab: 'harness', icon: FlaskConical, key: 'nav.harness', section: 'objects' },
  { tab: 'connect', icon: Link2, key: 'nav.connect', section: 'system' },
  { tab: 'settings', icon: Settings, key: 'nav.settings', section: 'system' },
  { tab: 'log', icon: ScrollText, key: 'nav.log', section: 'system' },
];

const SECTIONS: { id: NavItem['section']; key: I18nKey }[] = [
  { id: 'workspace', key: 'nav.workspace' },
  { id: 'objects', key: 'nav.objects' },
  { id: 'system', key: 'nav.system' },
];

interface SidebarProps {
  mode: string | null;
  inboxCount: number;
  ideasCount?: number;
}

export function Sidebar({ mode, inboxCount, ideasCount }: SidebarProps) {
  const { t, tab, navigate } = useApp();
  const [query, setQuery] = useState('');
  const [hovered, setHovered] = useState<string | null>(null);
  const [box, setBox] = useState<{ top: number; height: number } | null>(null);
  const navRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const counts = { inbox: inboxCount, ideas: ideasCount ?? 0 };
  const items = useMemo(
    () =>
      query.trim()
        ? NAV.filter((n) => t(n.key).toLowerCase().includes(query.trim().toLowerCase()))
        : NAV,
    [query, t],
  );

  /* one highlight glides between rows instead of per-row backgrounds */
  useLayoutEffect(() => {
    const container = navRef.current;
    const target = itemRefs.current[hovered ?? tab];
    if (!container || !target) return;
    const containerRect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    setBox({
      top: targetRect.top - containerRect.top,
      height: targetRect.height,
    });
  }, [hovered, tab, items.length]);

  return (
    <aside className="flex h-full w-[236px] min-w-[236px] flex-col border-edge bg-panel p-2 sm:border-e">
      {/* workspace row: brand + mode chip, account switcher beneath (v0.5.0) */}
      <div className="mb-1.5 flex w-full items-center gap-2.5 rounded-control p-1.5">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-[8px] bg-accent text-[13px] font-extrabold text-white">
          X
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-semibold leading-tight text-ink">
            X<span className="text-accent">OpenStanley</span>
          </span>
          <span className="block truncate font-mono text-[10.5px] leading-tight text-ink-3">
            {mode
              ? mode === 'dryrun'
                ? t('mode.dryrun')
                : t('mode.live', { mode: mode.toUpperCase() })
              : '…'}
          </span>
        </span>
      </div>
      <div className="mb-2">
        <AccountSwitcher />
      </div>

      {/* quick search */}
      <label className="mb-1.5 flex h-8 items-center gap-2 rounded-control bg-inset px-2.5 shadow-hairline">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round">
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.3-4.3" />
        </svg>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('nav.search')}
          className="min-w-0 flex-1 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
        />
        <kbd className="flex size-4.5 items-center justify-center rounded-[5px] bg-surface text-[10px] text-ink-3 shadow-hairline">
          /
        </kbd>
      </label>

      {/* accent action */}
      <button
        type="button"
        onClick={() => navigate('write')}
        className="mb-2 flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-[13px]
          font-medium text-accent transition-[background-color,transform] duration-100 hover:bg-accent-tint active:scale-[0.96]"
      >
        <span className="min-w-0 flex-1 truncate text-start">{t('nav.newPost')}</span>
        <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-accent text-white">
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
        </span>
      </button>

      {/* items */}
      <div
        ref={navRef}
        onMouseLeave={() => setHovered(null)}
        className="relative flex flex-1 flex-col gap-2 overflow-y-auto"
      >
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-0 rounded-[7px] bg-hover"
          style={{
            top: box?.top ?? 0,
            height: box?.height ?? 0,
            opacity: box ? 1 : 0,
            transition:
              'top 220ms cubic-bezier(0.23,1,0.32,1), height 220ms cubic-bezier(0.23,1,0.32,1), opacity 150ms ease',
          }}
        />
        {SECTIONS.map((section) => {
          const sectionItems = items.filter((item) => item.section === section.id);
          if (sectionItems.length === 0) return null;
          return (
            <div key={section.id}>
              <div className="px-2 pt-1 pb-1 text-[10.5px] font-medium uppercase tracking-[0.08em] text-ink-3">
                {t(section.key)}
              </div>
              <div className="flex flex-col gap-px">
                {sectionItems.map((item) => {
                  const isActive = item.tab === tab;
                  const Icon = item.icon;
                  const count = item.count ? counts[item.count] : 0;
                  return (
                    <button
                      key={item.tab}
                      ref={(el) => {
                        itemRefs.current[item.tab] = el;
                      }}
                      type="button"
                      onMouseEnter={() => setHovered(item.tab)}
                      onFocus={() => setHovered(item.tab)}
                      onBlur={() => setHovered(null)}
                      onClick={() => navigate(item.tab)}
                      aria-current={isActive ? 'page' : undefined}
                      className="group relative z-10 flex w-full items-center gap-2 rounded-[7px] px-2 py-1.5 text-start
                        transition-[color,transform] duration-150 active:scale-[0.96]"
                    >
                      <Icon
                        size={13}
                        strokeWidth={2}
                        className={cn('shrink-0', isActive ? 'text-ink' : 'text-ink-3')}
                      />
                      <span
                        className={cn(
                          'min-w-0 flex-1 truncate text-[13px] transition-colors duration-150',
                          isActive ? 'font-medium text-ink' : 'text-ink-2',
                        )}
                      >
                        {t(item.key)}
                      </span>
                      {count > 0 && (
                        <span
                          key={count}
                          className={cn(
                            'flex h-4.5 min-w-4.5 items-center justify-center rounded-full px-1 text-[10.5px] font-semibold tabular-nums',
                            isActive
                              ? 'bg-surface text-ink-2 shadow-hairline'
                              : 'bg-accent-tint text-accent-ink',
                          )}
                          style={{ animation: 'pop-in 250ms cubic-bezier(0.23,1,0.32,1) both' }}
                        >
                          {count}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
