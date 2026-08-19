import { useEffect, useState } from 'react';
import {
  Brain,
  CalendarDays,
  FlaskConical,
  Globe,
  Inbox,
  Lightbulb,
  Link2,
  PenLine,
  Rows3,
  ScrollText,
  Settings,
  Target,
  TrendingUp,
  Zap,
} from 'lucide-react';
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import { useApp, type Tab } from '@/lib/app-context';
import type { I18nKey } from '@/lib/i18n';
import { LOOP_NAMES, triggerLoop } from '@/lib/loops';

const PAGES: { tab: Tab; label: I18nKey; icon: React.ElementType }[] = [
  { tab: 'write', label: 'nav.write', icon: PenLine },
  { tab: 'calendar', label: 'nav.calendar', icon: CalendarDays },
  { tab: 'inbox', label: 'nav.inbox', icon: Inbox },
  { tab: 'ideas', label: 'nav.ideas', icon: Lightbulb },
  { tab: 'strategy', label: 'nav.strategy', icon: Target },
  { tab: 'brain', label: 'nav.brain', icon: Brain },
  { tab: 'insights', label: 'nav.insights', icon: TrendingUp },
  { tab: 'harness', label: 'nav.harness', icon: FlaskConical },
  { tab: 'connect', label: 'nav.connect', icon: Link2 },
  { tab: 'settings', label: 'nav.settings', icon: Settings },
  { tab: 'log', label: 'nav.log', icon: ScrollText },
];

export function CommandPalette() {
  const { t, navigate, lang, setLang, dense, setDense } = useApp();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <CommandDialog open={open} onOpenChange={setOpen} title={t('palette.placeholder')}>
      <CommandInput placeholder={t('palette.placeholder')} />
      <CommandList>
        <CommandEmpty>{t('palette.noResults')}</CommandEmpty>
        <CommandGroup heading={t('palette.pages')}>
          {PAGES.map(({ tab, label, icon: Icon }) => (
            <CommandItem
              key={tab}
              value={`${t(label)} ${tab}`}
              onSelect={() => {
                navigate(tab);
                setOpen(false);
              }}
            >
              <Icon size={15} className="text-muted" />
              {t(label)}
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandGroup heading={t('palette.loops')}>
          {LOOP_NAMES.map((name) => (
            <CommandItem
              key={name}
              value={`${t(`loops.${name}`)} ${name}`}
              onSelect={() => {
                setOpen(false);
                void triggerLoop(name, t);
              }}
            >
              <Zap size={15} className="text-muted" />
              {t(`loops.${name}`)}
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandGroup heading={t('palette.preferences')}>
          <CommandItem
            value="language"
            onSelect={() => {
              setLang(lang === 'en' ? 'ar' : 'en');
              setOpen(false);
            }}
          >
            <Globe size={15} className="text-muted" />
            {t('palette.toggleLang', { lang: lang === 'en' ? t('settings.ar') : t('settings.en') })}
          </CommandItem>
          <CommandItem
            value="compact"
            onSelect={() => {
              setDense(!dense);
              setOpen(false);
            }}
          >
            <Rows3 size={15} className="text-muted" />
            {dense ? t('palette.toggleCompactOff') : t('palette.toggleCompactOn')}
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
