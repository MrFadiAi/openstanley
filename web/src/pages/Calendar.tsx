import { useEffect, useMemo, useState } from 'react';
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from '@dnd-kit/core';
import { toast } from 'sonner';
import { CalendarDays, ChevronLeft, ChevronRight, Sparkles } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  type CalendarItem,
  type CalendarResponse,
  type SmartSlotChip,
} from '@/lib/api';
import { cn, dateKey, hasArabic } from '@/lib/utils';

const KIND_BAR: Record<string, string> = {
  post: 'border-s-accent',
  reply: 'border-s-warn',
  quote: 'border-s-cyan',
};

/** timestamp of the last completed drag — suppresses the trailing click */
const lastDragEnd = { at: 0 };

interface Cell {
  key: string;
  date: Date;
  inPeriod: boolean;
  isToday: boolean;
}

function mondayOnOrBefore(d: Date): Date {
  const m = new Date(d);
  m.setHours(0, 0, 0, 0);
  m.setDate(m.getDate() - ((m.getDay() + 6) % 7));
  return m;
}

function buildCells(view: 'month' | 'weeks', offset: number): Cell[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const todayKey = dateKey(today);
  const cells: Cell[] = [];
  if (view === 'weeks') {
    const start = mondayOnOrBefore(today);
    start.setDate(start.getDate() + offset * 7);
    for (let i = 0; i < 14; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + i);
      cells.push({ key: dateKey(d), date: d, inPeriod: true, isToday: dateKey(d) === todayKey });
    }
    return cells;
  }
  const first = new Date(today.getFullYear(), today.getMonth() + offset, 1);
  const start = mondayOnOrBefore(first);
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    cells.push({
      key: dateKey(d),
      date: d,
      inPeriod: d.getMonth() === first.getMonth(),
      isToday: dateKey(d) === todayKey,
    });
  }
  return cells;
}

function CalItemChip({ item, solid }: { item: CalendarItem; solid?: boolean }) {
  return (
    <div
      dir={hasArabic(item.text) ? 'rtl' : 'ltr'}
      className={cn(
        'mb-1 cursor-grab select-none overflow-hidden rounded-md border-s-2 bg-panel2 px-1.5 py-1 text-[11.5px] leading-tight text-base/95 active:cursor-grabbing',
        item.state === 'published' ? 'border-s-good' : KIND_BAR[item.kind] ?? 'border-s-accent',
        solid && 'shadow-lg shadow-black/40',
      )}
    >
      <span className="me-1 font-mono text-[10px] text-muted">{item.time}</span>
      <span className="line-clamp-2">{item.text}</span>
    </div>
  );
}

function DraggableItem({ item }: { item: CalendarItem }) {
  const { t, lang } = useApp();
  const [open, setOpen] = useState(false);
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: item.id,
  });

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <div
          ref={setNodeRef}
          {...attributes}
          {...listeners}
          onClick={() => {
            if (Date.now() - lastDragEnd.at > 180) setOpen(true);
          }}
          title={t('calendar.dragHint')}
          className={cn(isDragging && 'opacity-35')}
        >
          <CalItemChip item={item} />
        </div>
      </PopoverAnchor>
      <PopoverContent className="w-80">
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <Badge variant={item.state === 'published' ? 'green' : 'accent'}>{item.state}</Badge>
          <Badge>{item.kind}</Badge>
          <Badge>{t('calendar.itemScore', { n: item.score })}</Badge>
          <Badge>{item.language}</Badge>
        </div>
        <div
          dir={hasArabic(item.text) ? 'rtl' : 'ltr'}
          className="whitespace-pre-wrap text-[13.5px] leading-relaxed"
        >
          {item.text}
        </div>
        <div className="mt-2 font-mono text-[11px] text-muted">
          {new Date(item.scheduled_at).toLocaleString(lang === 'ar' ? 'ar' : 'en-GB', {
            weekday: 'short',
            day: '2-digit',
            month: 'short',
            hour: '2-digit',
            minute: '2-digit',
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function DayCell({
  cell,
  items,
  slots,
  smartChips,
}: {
  cell: Cell;
  items: CalendarItem[];
  slots: string[];
  smartChips: SmartSlotChip[];
}) {
  const { lang } = useApp();
  const { setNodeRef, isOver } = useDroppable({ id: cell.key });
  const wd = cell.date.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', { weekday: 'short' });
  return (
    <div
      ref={setNodeRef}
      className={cn(
        'min-h-[96px] rounded-xl border border-edge bg-panel p-1.5 transition-colors',
        cell.isToday && 'border-accent/80',
        !cell.inPeriod && 'opacity-35',
        isOver && 'border-accent bg-accent/10',
      )}
    >
      <div className="mb-1 font-mono text-[11px] text-muted">
        {wd} {cell.date.getDate()}
      </div>
      {items.map((it) => (
        <DraggableItem key={it.id} item={it} />
      ))}
      {slots.map((s) => (
        <div
          key={s}
          className="mb-1 rounded-md border border-dashed border-edge px-1.5 py-0.5 text-center font-mono text-[10.5px] text-muted/70"
        >
          + {s}
        </div>
      ))}
      {smartChips.map((c) => (
        <div
          key={`smart-${c.time}`}
          title={`${c.reason} · score ${Math.round(c.score * 100)}`}
          className="mb-1 flex items-center justify-center gap-1 rounded-md border border-dashed border-accent/40 bg-accent/5 px-1.5 py-0.5 text-center font-mono text-[10.5px] text-accent2"
        >
          <Sparkles size={9} className="shrink-0" />
          {c.time} · {Math.round(c.score * 100)}
        </div>
      ))}
    </div>
  );
}

export function CalendarPage() {
  const { t, lang } = useApp();
  const [view, setView] = useState<'month' | 'weeks'>('weeks');
  const [offset, setOffset] = useState(0);
  const [cal, setCal] = useState<CalendarResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeItem, setActiveItem] = useState<CalendarItem | null>(null);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));

  const load = (): void => {
    void (async () => {
      try {
        setCal(await api<CalendarResponse>('calendar'));
      } catch {
        setCal(null);
      } finally {
        setLoading(false);
      }
    })();
  };

  useEffect(load, []);

  const cells = useMemo(() => buildCells(view, offset), [view, offset]);

  const onDragStart = (e: DragStartEvent): void => {
    const id = e.active.id as number;
    for (const list of Object.values(cal?.days ?? {})) {
      const found = list.find((x) => x.id === id);
      if (found) {
        setActiveItem(found);
        return;
      }
    }
  };

  const onDragEnd = (e: DragEndEvent): void => {
    lastDragEnd.at = Date.now();
    setActiveItem(null);
    const { active, over } = e;
    if (!over) return;
    const id = active.id as number;
    const targetKey = String(over.id);
    let origin: CalendarItem | undefined;
    let originKey = '';
    for (const [k, list] of Object.entries(cal?.days ?? {})) {
      const found = list.find((x) => x.id === id);
      if (found) {
        origin = found;
        originKey = k;
        break;
      }
    }
    if (!origin || originKey === targetKey) return;
    const time = origin.time || '09:00';
    void (async () => {
      try {
        await apiPost(`drafts/${id}/reschedule`, { scheduled_at: `${targetKey}T${time}:00` });
        toast.success(
          t('calendar.rescheduled', {
            when: new Date(`${targetKey}T${time}:00`).toLocaleString(
              lang === 'ar' ? 'ar' : 'en-GB',
              { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' },
            ),
          }),
        );
        load();
      } catch (err) {
        toast.error(t('calendar.rescheduleFailed', { msg: err instanceof Error ? err.message : String(err) }));
      }
    })();
  };

  const periodLabel = useMemo(() => {
    if (view === 'month') {
      const d = new Date();
      d.setDate(1);
      d.setMonth(d.getMonth() + offset);
      return d.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', {
        month: 'long',
        year: 'numeric',
      });
    }
    const a = cells[0]?.date ?? new Date();
    const b = cells[cells.length - 1]?.date ?? a;
    const sameMonth = a.getMonth() === b.getMonth();
    const fmt = (d: Date): string =>
      d.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', { day: 'numeric', month: 'short' });
    return sameMonth
      ? `${a.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', { month: 'long', year: 'numeric' })}`
      : `${fmt(a)} – ${fmt(b)}`;
  }, [view, offset, cells, lang]);

  const totalItems = useMemo(
    () => Object.values(cal?.days ?? {}).reduce((n, l) => n + l.length, 0),
    [cal],
  );

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1060px] px-6 py-6">
        <PageHeader
          title={t('calendar.title')}
          subtitle={t('calendar.subtitle')}
          actions={
            <div className="flex items-center gap-2">
              {cal?.smart?.enabled ? (
                <Badge
                  variant={cal.smart.source === 'real' ? 'green' : 'default'}
                  title={t('calendar.smartHint')}
                  className="cursor-default"
                >
                  <Sparkles size={10} className="me-1" />
                  {t('calendar.smart')}
                </Badge>
              ) : null}
              <span className="text-[14px] font-semibold">{periodLabel}</span>
              <Button size="icon-sm" variant="ghost" aria-label={t('calendar.prev')} onClick={() => setOffset(offset - 1)}>
                <ChevronLeft size={16} className="rtl:rotate-180" />
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setOffset(0)}>
                {t('common.today')}
              </Button>
              <Button size="icon-sm" variant="ghost" aria-label={t('calendar.next')} onClick={() => setOffset(offset + 1)}>
                <ChevronRight size={16} className="rtl:rotate-180" />
              </Button>
              <Tabs value={view} onValueChange={(v) => setView(v as 'month' | 'weeks')}>
                <TabsList>
                  <TabsTrigger value="month">{t('calendar.month')}</TabsTrigger>
                  <TabsTrigger value="weeks">{t('calendar.twoWeek')}</TabsTrigger>
                </TabsList>
              </Tabs>
            </div>
          }
        />

        {loading ? (
          <div className="grid grid-cols-7 gap-2">
            {Array.from({ length: 14 }, (_, i) => (
              <Skeleton key={i} className="h-28" />
            ))}
          </div>
        ) : (
          <DndContext
            sensors={sensors}
            onDragStart={onDragStart}
            onDragEnd={onDragEnd}
            onDragCancel={() => setActiveItem(null)}
          >
            <div className="grid grid-cols-7 gap-2">
              {cells.map((cell) => (
                <DayCell
                  key={cell.key}
                  cell={cell}
                  items={cal?.days?.[cell.key] ?? []}
                  slots={cal?.empty_slots?.[cell.key] ?? []}
                  smartChips={cal?.smart?.enabled ? cal.smart.slots?.[cell.key] ?? [] : []}
                />
              ))}
            </div>
            <DragOverlay dropAnimation={null}>
              {activeItem ? (
                <div className="w-40">
                  <CalItemChip item={activeItem} solid />
                </div>
              ) : null}
            </DragOverlay>
          </DndContext>
        )}

        {!loading && totalItems === 0 ? (
          <div className="mt-6 flex items-center justify-center gap-2 py-10 text-[13px] text-muted">
            <CalendarDays size={15} />
            {t('calendar.empty')}
          </div>
        ) : null}
      </div>
    </div>
  );
}
