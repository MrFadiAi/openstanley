import { useEffect, useMemo, useState, type ReactNode } from 'react';
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
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Clock,
  Pencil,
  Plus,
  Send,
  Settings2,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { Dialog, DialogClose, DialogContent, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  type CalendarItem,
  type CalendarResponse,
  type Draft,
  type Settings,
  type SmartSlotChip,
} from '@/lib/api';
import { cn, dateKey, hasArabic } from '@/lib/utils';

/** timestamp of the last completed drag — suppresses the trailing click */
const lastDragEnd = { at: 0 };

type View = 'days' | 'week' | 'month';

interface DayCell {
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

/** the day columns (days view = 3, week view = 7), page-stepped by offset */
function buildDays(n: number, offset: number): DayCell[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const todayKey = dateKey(today);
  const start = new Date(today);
  start.setDate(start.getDate() + offset * n);
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return { key: dateKey(d), date: d, inPeriod: true, isToday: dateKey(d) === todayKey };
  });
}

function buildMonthCells(offset: number): DayCell[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const todayKey = dateKey(today);
  const first = new Date(today.getFullYear(), today.getMonth() + offset, 1);
  const start = mondayOnOrBefore(first);
  return Array.from({ length: 42 }, (_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return {
      key: dateKey(d),
      date: d,
      inPeriod: d.getMonth() === first.getMonth(),
      isToday: dateKey(d) === todayKey,
    };
  });
}

/** "09:00" → "9:00 AM" (en) / "٩:٠٠ ص" (ar) — one source for slots + cards */
function fmtTime(hhmm: string, lang: string): string {
  const d = new Date(`2000-01-01T${hhmm}:00`);
  return Number.isNaN(d.getTime())
    ? hhmm
    : d.toLocaleTimeString(lang === 'ar' ? 'ar' : 'en-US', { hour: 'numeric', minute: '2-digit' });
}

/** the user's wall-clock offset, e.g. "GMT+3" — honest about which clock we schedule by */
function tzLabel(): string {
  try {
    const part = new Intl.DateTimeFormat(undefined, { timeZoneName: 'shortOffset' })
      .formatToParts()
      .find((p) => p.type === 'timeZoneName');
    return part?.value?.replace('GMT', 'UTC') ?? '';
  } catch {
    return '';
  }
}

interface DragPayload {
  id: number;
  kind: 'post' | 'queue';
  text: string;
  time?: string;
}

// ---------------- shared detail popover (calendar slot + queue draft) ----------------

interface DetailItem {
  id: number;
  kind?: string;
  state?: string;
  text: string;
  scheduled_at?: string | null;
  scheduled_reason?: string | null;
  language?: string | null;
  score?: number | null;
  image?: string | null;
  time?: string;
  reply_to?: { x_id?: string | null; author?: string } | null;
}

function PostDetail({ it, onChanged, onClose }: { it: DetailItem; onChanged?: () => void; onClose?: () => void }) {
  const { t, lang } = useApp();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(it.text);
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const remove = (): void => {
    void (async () => {
      setBusy(true);
      try {
        await apiPost(`drafts/${it.id}/reject`, {});
        toast.success(t('calendar.removedToast'));
        onClose?.();
        onChanged?.();
      } catch (err) {
        toast.error(t('calendar.removeFailed', { msg: err instanceof Error ? err.message : String(err) }));
      } finally {
        setBusy(false);
        setConfirmRemove(false);
      }
    })();
  };

  const saveEdit = (): void => {
    void (async () => {
      setBusy(true);
      try {
        await apiPost(`drafts/${it.id}/edit`, { text });
        toast.success(t('calendar.editedToast'));
        setEditing(false);
        onChanged?.();
      } catch (err) {
        toast.error(t('calendar.editFailed', { msg: err instanceof Error ? err.message : String(err) }));
      } finally {
        setBusy(false);
      }
    })();
  };

  const publishNow = (): void => {
    void (async () => {
      setBusy(true);
      try {
        const r = await apiPost<{ ok: boolean; url?: string }>(`drafts/${it.id}/publish`, {});
        toast.success(t('calendar.publishedToast'));
        onClose?.();
        onChanged?.();
        if (r.url) window.open(r.url, '_blank', 'noopener');
      } catch (err) {
        toast.error(t('calendar.publishFailed', { msg: err instanceof Error ? err.message : String(err) }));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <PopoverContent className="w-96 max-h-[70vh] overflow-y-auto">
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {it.state ? <Badge variant={it.state === 'published' ? 'green' : 'default'}>{it.state}</Badge> : null}
        {it.kind ? <Badge>{it.kind}</Badge> : null}
        {typeof it.score === 'number' ? <Badge>{t('calendar.itemScore', { n: it.score })}</Badge> : null}
        {it.language ? <Badge>{it.language}</Badge> : null}
        <span className="ms-auto font-mono text-[10.5px] text-ink-3">#{it.id}</span>
      </div>

      {it.reply_to?.author ? (
        <p className="mb-2 rounded-lg border border-line bg-inset px-2 py-1.5 text-[11.5px] text-ink-2">
          <span className="font-medium">{t('calendar.replyTo')}:</span> @{it.reply_to.author}
        </p>
      ) : null}

      {editing ? (
        <div className="flex flex-col gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            dir={hasArabic(text) ? 'rtl' : 'ltr'}
            rows={Math.min(14, Math.max(4, Math.ceil(text.length / 48)))}
            className="w-full resize-y rounded-lg border border-line bg-field px-2.5 py-2 text-[13.5px] leading-relaxed text-ink"
          />
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10.5px] text-ink-3">{text.length}</span>
            <Button size="sm" disabled={busy || !text.trim()} onClick={saveEdit}>
              {t('common.save')}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => { setEditing(false); setText(it.text); }}>
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      ) : (
        <div dir={hasArabic(it.text) ? 'rtl' : 'ltr'} className="whitespace-pre-wrap text-[13.5px] leading-relaxed">
          {it.text}
        </div>
      )}

      {it.scheduled_reason && !editing ? (
        <p className="mt-2.5 flex items-start gap-1.5 rounded-lg border border-line bg-inset px-2 py-1.5 text-[11.5px] leading-relaxed text-ink-2">
          <Sparkles size={11} className="mt-0.5 shrink-0" />
          <span>
            <span className="font-medium">{t('calendar.whySlot')}:</span> {it.scheduled_reason}
          </span>
        </p>
      ) : null}

      {it.scheduled_at ? (
        <div className="mt-2 font-mono text-[11px] text-muted">
          {new Date(it.scheduled_at).toLocaleString(lang === 'ar' ? 'ar' : 'en-GB', {
            weekday: 'short',
            day: '2-digit',
            month: 'short',
            hour: '2-digit',
            minute: '2-digit',
          })}
        </div>
      ) : null}

      <div className="mt-3 flex items-center gap-2">
        {it.state !== 'published' ? (
          <Button size="sm" disabled={busy} onClick={publishNow}>
            <Send size={12} className="me-1" />
            {t('calendar.publishNow')}
          </Button>
        ) : null}
        {!editing ? (
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
            <Pencil size={12} className="me-1" />
            {t('common.edit')}
          </Button>
        ) : null}
        {it.state !== 'published' ? (
          confirmRemove ? (
            <Button size="sm" variant="danger" disabled={busy} onClick={remove} className="ms-auto">
              <Trash2 size={12} className="me-1" />
              {t('calendar.removeConfirm')}
            </Button>
          ) : (
            <Button size="sm" variant="ghost" className="ms-auto text-danger" title={t('calendar.remove')}
                    onClick={() => {
                      setConfirmRemove(true);
                      window.setTimeout(() => setConfirmRemove(false), 4000);
                    }}>
              <Trash2 size={12} />
            </Button>
          )
        ) : null}
      </div>
    </PopoverContent>
  );
}

// ---------------- post card inside a slot ----------------

function SlotPost({ item, onChanged }: { item: CalendarItem; onChanged?: () => void }) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: item.id });

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
          className={cn(
            'cursor-grab select-none rounded-xl border border-line bg-surface px-3 py-2.5 shadow-sm transition-shadow hover:shadow-md active:cursor-grabbing',
            isDragging && 'opacity-30',
          )}
        >
          <div className="mb-1 flex items-center gap-1.5">
            <span
              className={cn(
                'size-1.5 rounded-full',
                item.state === 'published' ? 'bg-green' : item.state === 'pending' ? 'bg-orange' : 'bg-ink',
              )}
            />
            <span className="font-mono text-[10px] uppercase tracking-wide text-ink-3">{item.kind}</span>
            {item.reply_to?.author ? (
              <span className="truncate font-mono text-[10px] text-ink-3">↩ @{item.reply_to.author}</span>
            ) : null}
            {item.image ? <span className="font-mono text-[10px] text-ink-3">· 🖼</span> : null}
            <span className="ms-auto flex items-center gap-1.5 font-mono text-[10px] text-ink-3">
              {item.time}
              {typeof item.score === 'number' ? <span>{item.score}</span> : null}
            </span>
          </div>
          <p
            dir={hasArabic(item.text) ? 'rtl' : 'ltr'}
            className="line-clamp-3 text-[13px] leading-snug text-base/90"
          >
            {item.text}
          </p>
        </div>
      </PopoverAnchor>
      {open ? <PostDetail it={item} onChanged={onChanged} onClose={() => setOpen(false)} /> : null}
    </Popover>
  );
}

// ---------------- a posting-time slot (open or occupied) ----------------

function TimeSlot({
  dateKeyStr,
  time,
  items,
  chip,
  compact,
  onChanged,
}: {
  dateKeyStr: string;
  time: string;
  items: CalendarItem[];
  chip?: SmartSlotChip;
  compact?: boolean;
  onChanged?: () => void;
}) {
  const { t, lang } = useApp();
  const { setNodeRef, isOver } = useDroppable({ id: `slot-${dateKeyStr}-${time}` });
  const occupied = items.length > 0;

  return (
    <div
      ref={setNodeRef}
      className={cn(
        'rounded-2xl border transition-colors',
        compact ? 'p-1.5' : 'p-2.5',
        occupied
          ? 'border-line bg-surface'
          : cn('border-dashed border-line-strong/70 bg-transparent', isOver && 'border-ink bg-inset'),
      )}
    >
      <div className="mb-1.5 flex items-baseline gap-1.5 px-0.5">
        <span className={cn('font-serif', compact ? 'text-[13px]' : 'text-[15px]')}>{fmtTime(time, lang)}</span>
        {!occupied ? (
          <span className="text-[11.5px] italic text-ink-3 font-serif">— {t('calendar.openSlot')}</span>
        ) : null}
        {chip && !occupied ? (
          <span
            title={chip.reason}
            className="ms-auto inline-flex items-center gap-0.5 rounded-full border border-line px-1.5 py-px font-mono text-[10px] text-ink-2"
          >
            <Sparkles size={9} className="shrink-0" />
            {Math.round(chip.score * 100)}
          </span>
        ) : null}
      </div>
      {items.map((it) => (
        <SlotPost key={it.id} item={it} onChanged={onChanged} />
      ))}
    </div>
  );
}

// ---------------- dashed custom-time slot + picker ----------------

function CustomTimeSlot({
  dateKeyStr,
  pending,
  onPick,
  onDismiss,
  compact,
}: {
  dateKeyStr: string;
  pending: { id: number } | null;
  onPick: (id: number, time: string) => void;
  onDismiss: () => void;
  compact?: boolean;
}) {
  const { t } = useApp();
  const [time, setTime] = useState('18:00');
  const { setNodeRef, isOver } = useDroppable({ id: `custom-${dateKeyStr}` });

  return (
    <Popover open={!!pending} onOpenChange={(o) => { if (!o) onDismiss(); }}>
      <PopoverAnchor asChild>
        <div
          ref={setNodeRef}
          className={cn(
            'flex items-center justify-center gap-1.5 rounded-2xl border border-dashed border-line-strong/70 text-ink-3 transition-colors',
            compact ? 'px-1.5 py-1.5' : 'px-2.5 py-3.5',
            isOver && 'border-ink bg-inset text-ink',
          )}
        >
          <Plus size={12} />
          <span className="text-[12.5px] italic font-serif">{t('calendar.customTime')}</span>
        </div>
      </PopoverAnchor>
      <PopoverContent className="w-44">
        <p className="mb-2 text-[12px] text-muted">{t('calendar.pickTime')}</p>
        <input
          type="time"
          value={time}
          onChange={(e) => setTime(e.target.value)}
          className="mb-2 w-full rounded-lg border border-line bg-field px-2 py-1.5 font-mono text-[13px] text-ink"
          dir="ltr"
        />
        <Button size="sm" className="w-full" onClick={() => onPick(pending!.id, time)}>
          {t('calendar.customTime')}
        </Button>
      </PopoverContent>
    </Popover>
  );
}

// ---------------- one day column ----------------

function DayColumn({
  cell,
  postTimes,
  items,
  smartChips,
  compact,
  pendingCustom,
  onPickCustom,
  onDismissCustom,
  onChanged,
}: {
  cell: DayCell;
  postTimes: string[];
  items: CalendarItem[];
  smartChips: SmartSlotChip[];
  compact?: boolean;
  pendingCustom: { id: number } | null;
  onPickCustom: (id: number, time: string) => void;
  onDismissCustom: () => void;
  onChanged?: () => void;
}) {
  const { t, lang } = useApp();
  const { setNodeRef, isOver } = useDroppable({ id: `day-${cell.key}` });
  const chipByTime = useMemo(
    () => new Map(smartChips.map((c) => [c.time, c])),
    [smartChips],
  );
  const wd = cell.date.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', { weekday: 'long' });
  const mo = cell.date.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', { month: 'short', day: 'numeric' });

  return (
    <div
      ref={setNodeRef}
      className={cn(
        'flex min-w-0 flex-col gap-2 rounded-2xl border p-2.5 transition-colors',
        cell.isToday ? 'border-ink/25 bg-surface/70' : 'border-line/70',
        isOver && 'border-ink/40 bg-inset',
      )}
    >
      <div className="px-0.5">
        <div className="font-serif text-[17px] leading-tight">
          {cell.isToday ? t('common.today') : wd}
        </div>
        <div className="text-[12px] italic font-serif text-ink-3">{mo}</div>
      </div>
      {postTimes.map((tm) => (
        <TimeSlot
          key={tm}
          dateKeyStr={cell.key}
          time={tm}
          compact={compact}
          items={items.filter((it) => it.time === tm)}
          chip={chipByTime.get(tm)}
          onChanged={onChanged}
        />
      ))}
      {items
        .filter((it) => !postTimes.includes(it.time))
        .map((it) => (
          <SlotPost key={it.id} item={it} onChanged={onChanged} />
        ))}
      <CustomTimeSlot
        dateKeyStr={cell.key}
        compact={compact}
        pending={pendingCustom}
        onPick={onPickCustom}
        onDismiss={onDismissCustom}
      />
      <p className="sr-only">{t('calendar.queueDrag')}</p>
    </div>
  );
}

// ---------------- queue rail ----------------

function QueueCard({ draft, onChanged }: { draft: Draft; onChanged?: () => void }) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: `q-${draft.id}` });
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
          title={t('calendar.queueDrag')}
          className={cn(
            'cursor-grab select-none rounded-xl border border-line bg-surface px-3 py-2 shadow-sm transition-shadow hover:shadow-md active:cursor-grabbing',
            isDragging && 'opacity-30',
          )}
        >
          <div className="mb-1 flex items-center gap-1.5">
            <span className="font-mono text-[10px] uppercase tracking-wide text-ink-3">{draft.kind}</span>
            {draft.image ? <span className="font-mono text-[10px] text-ink-3">· 🖼</span> : null}
            <span className="ms-auto font-mono text-[10px] text-ink-3">#{draft.id}</span>
          </div>
          <p dir={hasArabic(draft.text) ? 'rtl' : 'ltr'} className="line-clamp-3 text-[13px] leading-snug text-base/90">
            {draft.text}
          </p>
        </div>
      </PopoverAnchor>
      {open ? (
        <PostDetail
          it={{ id: draft.id, kind: draft.kind, state: draft.status, text: draft.text,
                scheduled_at: draft.scheduled_at, language: draft.language, image: draft.image }}
          onChanged={onChanged}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </Popover>
  );
}


// ---------------- confirm-gated clear-all ----------------

function ClearAllButton({
  icon, label, count, confirmTitle, onConfirm,
}: {
  icon: ReactNode;
  label: string;
  count: number;
  confirmTitle: string;
  onConfirm: () => Promise<number>;
}) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="icon-sm" variant="ghost" title={label} aria-label={label} disabled={count <= 0}>
          {icon}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>{label}</DialogTitle>
        <p className="text-[13px] leading-relaxed text-muted">{confirmTitle}</p>
        <div className="mt-3 flex items-center gap-2">
          <Button size="sm" variant="danger" onClick={() => { void onConfirm().then(() => setOpen(false)); }}>
            {label}
          </Button>
          <DialogClose asChild>
            <Button size="sm" variant="ghost">{t('common.cancel')}</Button>
          </DialogClose>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------- the page ----------------

export function CalendarPage() {
  const { t, lang, navigate } = useApp();
  const [view, setView] = useState<View>('days');
  const [offset, setOffset] = useState(0);
  const [cal, setCal] = useState<CalendarResponse | null>(null);
  const [queue, setQueue] = useState<Draft[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState<DragPayload | null>(null);
  const [pendingCustom, setPendingCustom] = useState<{ dateKey: string; id: number } | null>(null);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));

  const load = (): void => {
    void (async () => {
      try {
        const [c, d, s] = await Promise.all([
          api<CalendarResponse>('calendar'),
          api<Draft[]>('drafts?limit=200').catch(() => [] as Draft[]),
          api<Settings>('settings').catch(() => null),
        ]);
        setCal(c);
        setQueue(d.filter((x) => !x.scheduled_at && (x.status === 'draft' || x.status === 'approved')));
        setSettings(s);
      } catch {
        setCal(null);
      } finally {
        setLoading(false);
      }
    })();
  };

  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps

  const colCount = view === 'days' ? 3 : 7;
  const cells = useMemo<DayCell[]>(
    () => (view === 'month' ? buildMonthCells(offset) : buildDays(colCount, offset)),
    [view, offset, colCount],
  );
  const postTimes = useMemo(() => {
    const times = settings?.post_times?.length ? settings.post_times : ['09:00', '13:00', '18:00'];
    return [...times].sort();
  }, [settings]);

  const place = (id: number, dayKey: string, time: string): void => {
    void (async () => {
      try {
        await apiPost(`drafts/${id}/reschedule`, { scheduled_at: `${dayKey}T${time}:00` });
        toast.success(
          t('calendar.rescheduled', {
            when: new Date(`${dayKey}T${time}:00`).toLocaleString(lang === 'ar' ? 'ar' : 'en-GB', {
              day: '2-digit',
              month: 'short',
              hour: '2-digit',
              minute: '2-digit',
            }),
          }),
        );
        setPendingCustom(null);
        load();
      } catch (err) {
        toast.error(
          t('calendar.rescheduleFailed', { msg: err instanceof Error ? err.message : String(err) }),
        );
      }
    })();
  };

  const onDragStart = (e: DragStartEvent): void => {
    const sid = String(e.active.id);
    if (sid.startsWith('q-')) {
      const d = queue.find((x) => x.id === Number(sid.slice(2)));
      if (d) setActive({ id: d.id, kind: 'queue', text: d.text });
      return;
    }
    for (const list of Object.values(cal?.days ?? {})) {
      const found = list.find((x) => x.id === Number(sid));
      if (found) {
        setActive({ id: found.id, kind: 'post', text: found.text, time: found.time });
        return;
      }
    }
  };

  const onDragEnd = (e: DragEndEvent): void => {
    lastDragEnd.at = Date.now();
    const payload = active;
    setActive(null);
    const { over } = e;
    if (!over || !payload) return;
    const target = String(over.id);

    if (target.startsWith('slot-')) {
      // slot-2026-08-20-09:00 → day 2026-08-20, time 09:00
      const rest = target.slice(5);
      const dayKey = rest.slice(0, 10);
      const time = rest.slice(11);
      place(payload.id, dayKey, time);
      return;
    }
    if (target.startsWith('custom-')) {
      // hold the drop; the picker popover confirms the time
      setPendingCustom({ dateKey: target.slice(8), id: payload.id });
      return;
    }
    if (target.startsWith('day-')) {
      const dayKey = target.slice(4);
      if (payload.kind === 'post' && payload.time) {
        place(payload.id, dayKey, payload.time);
        return;
      }
      // queue drop on a day → that day's first open posting time
      const taken = new Set((cal?.days?.[dayKey] ?? []).map((x) => x.time));
      const open = postTimes.find((tm) => !taken.has(tm)) ?? postTimes[0] ?? '09:00';
      place(payload.id, dayKey, open);
    }
  };

  const rangeLabel = useMemo(() => {
    const loc = lang === 'ar' ? 'ar' : 'en-GB';
    if (view === 'month') {
      const d = cells[15]?.date ?? new Date();
      return d.toLocaleDateString(loc, { month: 'long', year: 'numeric' });
    }
    const a = cells[0]?.date ?? new Date();
    const b = cells[cells.length - 1]?.date ?? a;
    const fa = a.toLocaleDateString(loc, { month: 'short', day: 'numeric' });
    const fb = b.toLocaleDateString(loc, { month: 'short', day: 'numeric' });
    const y = b.toLocaleDateString(loc, { year: 'numeric' });
    return `${fa} – ${fb}, ${y}`;
  }, [view, cells, lang]);

  const totalItems = useMemo(
    () => Object.values(cal?.days ?? {}).reduce((n, l) => n + l.length, 0),
    [cal],
  );

  const scheduledCount = useMemo(
    () => Object.values(cal?.days ?? {}).flat().filter((i) => i.state !== 'published').length,
    [cal],
  );

  const clearScheduled = async (): Promise<number> => {
    const r = await apiPost<{ ok: boolean; deleted: number }>('drafts/clear-scheduled', {});
    toast.success(t('calendar.cleared', { n: r.deleted }));
    load();
    return r.deleted;
  };

  const clearQueue = async (): Promise<number> => {
    const r = await apiPost<{ ok: boolean; deleted: number }>('drafts/clear-queue', {});
    toast.success(t('calendar.cleared', { n: r.deleted }));
    load();
    return r.deleted;
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="h-full px-6 py-6">
        {/* header */}
        <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="font-serif text-[34px] font-medium leading-none tracking-tight">
              {t('calendar.title')}
            </h1>
            <p className="mt-2 font-serif text-[14px] italic text-ink-3">{t('calendar.subtitle')}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {tzLabel() ? (
              <span className="inline-flex items-center gap-1 rounded-full border border-line px-2.5 py-1 font-mono text-[11px] text-ink-2">
                <Clock size={10} />
                {tzLabel()}
              </span>
            ) : null}
            <span className="text-[13.5px] font-medium">{rangeLabel}</span>
            <Button size="icon-sm" variant="ghost" aria-label={t('calendar.prev')} onClick={() => setOffset(offset - 1)}>
              <ChevronLeft size={16} className="rtl:rotate-180" />
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setOffset(0)}>
              {t('common.today')}
            </Button>
            <Button size="icon-sm" variant="ghost" aria-label={t('calendar.next')} onClick={() => setOffset(offset + 1)}>
              <ChevronRight size={16} className="rtl:rotate-180" />
            </Button>
            <Tabs value={view} onValueChange={(v) => { setView(v as View); setOffset(0); }}>
              <TabsList>
                <TabsTrigger value="days">{t('calendar.days3')}</TabsTrigger>
                <TabsTrigger value="week">{t('calendar.week')}</TabsTrigger>
                <TabsTrigger value="month">{t('calendar.month')}</TabsTrigger>
              </TabsList>
            </Tabs>
            <ClearAllButton
              icon={<Trash2 size={14} />}
              label={t('calendar.clearScheduled')}
              count={scheduledCount}
              confirmTitle={t('calendar.confirmClearScheduled', { n: scheduledCount })}
              onConfirm={clearScheduled}
            />
            <Popover>
              <PopoverAnchor asChild>
                <Button size="icon-sm" variant="ghost" aria-label={t('calendar.postingTimes')}>
                  <Settings2 size={15} />
                </Button>
              </PopoverAnchor>
              <PopoverContent className="w-56">
                <p className="mb-2 text-[12px] text-muted">{t('calendar.postingTimes')}</p>
                <div className="mb-3 flex flex-wrap gap-1">
                  {postTimes.map((tm) => (
                    <Badge key={tm}>{fmtTime(tm, lang)}</Badge>
                  ))}
                </div>
                {cal?.smart?.enabled ? (
                  <p className="mb-3 flex items-center gap-1 text-[11.5px] text-ink-2">
                    <Sparkles size={10} /> {t('calendar.smart')}
                  </p>
                ) : null}
                <Button size="sm" variant="ghost" className="w-full" onClick={() => navigate('settings')}>
                  {t('calendar.changeSettings')}
                </Button>
              </PopoverContent>
            </Popover>
          </div>
        </div>

        {loading ? (
          <div className="grid grid-cols-3 gap-3">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-72 rounded-2xl" />
            ))}
          </div>
        ) : view === 'month' ? (
          <DndContext
            sensors={sensors}
            onDragStart={onDragStart}
            onDragEnd={onDragEnd}
            onDragCancel={() => setActive(null)}
          >
            <div className="grid grid-cols-7 gap-1.5">
              {cells.map((cell) => (
                <MonthCell key={cell.key} cell={cell} items={cal?.days?.[cell.key] ?? []} />
              ))}
            </div>
            <DragOverlay dropAnimation={null}>
              {active ? <DragCard text={active.text} /> : null}
            </DragOverlay>
          </DndContext>
        ) : (
          <DndContext
            sensors={sensors}
            onDragStart={onDragStart}
            onDragEnd={onDragEnd}
            onDragCancel={() => setActive(null)}
          >
            <div className={cn('grid gap-3', view === 'days' ? 'grid-cols-[240px_1fr]' : 'grid-cols-[200px_1fr]')}>
              {/* queue rail */}
              <div className="flex min-w-0 flex-col rounded-2xl border border-line bg-surface/60 p-4">
                <div className="mb-1 flex items-baseline justify-between">
                  <h2 className="font-serif text-[19px]">{t('calendar.queue')}</h2>
                  <span className="flex items-center gap-0.5">
                    <span className="font-serif text-[12.5px] italic text-ink-3">
                      {queue.length === 0 ? t('calendar.queueNone') : t('calendar.queueWaiting', { n: queue.length })}
                    </span>
                    <ClearAllButton
                      icon={<Trash2 size={12} />}
                      label={t('calendar.clearQueue')}
                      count={queue.length}
                      confirmTitle={t('calendar.confirmClearQueue', { n: queue.length })}
                      onConfirm={clearQueue}
                    />
                  </span>
                </div>
                <div className="mb-3 font-serif text-[52px] font-medium leading-none tabular-nums">
                  {queue.length}
                </div>
                {queue.length === 0 ? (
                  <>
                    <p className="mb-4 max-w-[200px] text-[12.5px] leading-relaxed text-ink-2">
                      {t('calendar.queueHint')}
                    </p>
                    <Button size="sm" className="w-full" onClick={() => navigate('write')}>
                      {t('calendar.queueCta')}
                    </Button>
                  </>
                ) : (
                  <div className="flex flex-col gap-2">
                    {queue.map((d) => (
                      <QueueCard key={d.id} draft={d} onChanged={load} />
                    ))}
                  </div>
                )}
              </div>

              {/* day columns */}
              <div className={cn('grid gap-3', view === 'days' ? 'grid-cols-3' : 'grid-cols-7')}>
                {cells.map((cell) => (
                  <DayColumn
                    key={cell.key}
                    cell={cell}
                    postTimes={postTimes}
                    compact={view === 'week'}
                    items={cal?.days?.[cell.key] ?? []}
                    smartChips={cal?.smart?.enabled ? cal.smart.slots?.[cell.key] ?? [] : []}
                    pendingCustom={pendingCustom?.dateKey === cell.key ? { id: pendingCustom.id } : null}
                    onPickCustom={(id, time) => place(id, cell.key, time)}
                    onDismissCustom={() => setPendingCustom(null)}
                    onChanged={load}
                  />
                ))}
              </div>
            </div>

            <DragOverlay dropAnimation={null}>
              {active ? <DragCard text={active.text} /> : null}
            </DragOverlay>
          </DndContext>
        )}

        {!loading && totalItems === 0 && queue.length === 0 ? (
          <div className="mt-8 flex items-center justify-center gap-2 py-10 text-[13px] text-ink-3">
            <CalendarDays size={15} />
            {t('calendar.empty')}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------------- drag ghost ----------------

function DragCard({ text }: { text: string }) {
  return (
    <div className="w-52 rotate-1 rounded-xl border border-line bg-surface px-3 py-2.5 shadow-lg">
      <p dir={hasArabic(text) ? 'rtl' : 'ltr'} className="line-clamp-3 text-[13px] leading-snug text-base/90">
        {text}
      </p>
    </div>
  );
}

// ---------------- month cell (compact) ----------------

function MonthCell({ cell, items }: { cell: DayCell; items: CalendarItem[] }) {
  const { lang } = useApp();
  const { setNodeRef, isOver } = useDroppable({ id: `day-${cell.key}` });
  return (
    <div
      ref={setNodeRef}
      className={cn(
        'min-h-[86px] rounded-xl border border-line/70 p-1.5 transition-colors',
        cell.isToday && 'border-ink/40 bg-surface/70',
        !cell.inPeriod && 'opacity-40',
        isOver && 'border-ink/50 bg-inset',
      )}
    >
      <div className="mb-1 flex items-baseline justify-between px-0.5">
        <span className={cn('font-serif text-[13px]', cell.isToday && 'font-medium')}>{cell.date.getDate()}</span>
        <span className="font-mono text-[9.5px] uppercase text-ink-3">
          {cell.date.toLocaleDateString(lang === 'ar' ? 'ar' : 'en-GB', { weekday: 'narrow' })}
        </span>
      </div>
      {items.slice(0, 2).map((it) => (
        <div
          key={it.id}
          dir={hasArabic(it.text) ? 'rtl' : 'ltr'}
          className={cn(
            'mb-0.5 truncate rounded-md border-s-2 px-1 py-0.5 text-[10.5px] leading-tight',
            it.state === 'published' ? 'border-s-good bg-green-tint' : 'border-s-ink bg-inset',
          )}
          title={it.text}
        >
          <span className="font-mono text-[9px] text-ink-3 me-1">{fmtTime(it.time, lang)}</span>
          {it.text}
        </div>
      ))}
      {items.length > 2 ? (
        <div className="px-1 font-mono text-[9.5px] text-ink-3">+{items.length - 2}</div>
      ) : null}
    </div>
  );
}
