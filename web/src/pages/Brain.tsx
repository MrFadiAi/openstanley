import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  BookText,
  Compass,
  FileText,
  Image as ImageIcon,
  ListChecks,
  RefreshCw,
  ScrollText,
  Sparkles,
  Upload,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import LoadingState from '@/components/bui/components/LoadingState';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import {
  getBrain,
  getBrainPart,
  putBrainPart,
  reflectBrain,
  uploadBrainPhoto,
  type BrainPart,
  type BrainPartData,
} from '@/lib/api';
import { cn, errMsg } from '@/lib/utils';

/* Brain — OpenStanley's self-maintained memory. Left: file tree over data/brain/;
 * right: viewer/editor, parsed rules with source badges, journal timeline,
 * photo grid. "Reflect now" runs the LLM self-improvement pass and flashes
 * every file it changed. AR/EN + RTL via dir=auto on content blocks. */

type PartIcon = Record<string, LucideIcon>;

const PART_ICONS: PartIcon = {
  instructions: BookText,
  rules: ListChecks,
  strategies: Compass,
  journal: ScrollText,
  photos: ImageIcon,
};

function iconFor(name: string): LucideIcon {
  return PART_ICONS[name] ?? FileText;
}

function shortWhen(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
}

function fileLabel(name: string): string {
  return name.replace(/^files\//, '').replace(/-/g, ' ');
}

/* simple markdown viewer: headings emphasized, everything pre-wrap */
function MarkdownView({ text }: { text: string }) {
  return (
    <div dir="auto" className="text-[13.5px] leading-[1.75]">
      {text.split('\n').map((line, i) => {
        const s = line.trim();
        if (s.startsWith('#')) {
          return (
            <div key={i} className="mb-1.5 mt-4 text-[14px] font-bold text-accent2 first:mt-0">
              {s.replace(/^#+\s*/, '')}
            </div>
          );
        }
        if (s.startsWith('- ') || s.startsWith('* ')) {
          return (
            <div key={i} className="whitespace-pre-wrap ps-3">
              • {s.slice(2)}
            </div>
          );
        }
        return (
          <div key={i} className="whitespace-pre-wrap">
            {line || ' '}
          </div>
        );
      })}
    </div>
  );
}

interface TriggerBadgeProps {
  source: string;
}

function SourceBadge({ source }: TriggerBadgeProps) {
  const { t } = useApp();
  const key = (['chat', 'learn', 'scan'] as const).includes(source as 'chat')
    ? (source as 'chat' | 'learn' | 'scan')
    : null;
  return (
    <span className="rounded-full bg-accent-tint px-1.5 py-px font-mono text-[10px] font-medium text-accent-ink">
      {key ? t(`brain.ruleBadge.${key}`) : source}
    </span>
  );
}

export function BrainPage() {
  const { t } = useApp();
  const [parts, setParts] = useState<BrainPart[]>([]);
  const [selected, setSelected] = useState('instructions');
  const [data, setData] = useState<BrainPartData | null>(null);
  const [loadingPart, setLoadingPart] = useState(true);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [reflecting, setReflecting] = useState<'chat' | 'learn' | 'scan' | null>(null);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [caption, setCaption] = useState('');
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const loadInventory = useCallback(async (): Promise<BrainPart[]> => {
    const inv = await getBrain();
    setParts(inv.parts);
    return inv.parts;
  }, []);

  const loadPart = useCallback(async (part: string) => {
    setLoadingPart(true);
    setEditing(false);
    try {
      setData(await getBrainPart(part));
    } catch (e) {
      toast.error(errMsg(e));
      setData(null);
    } finally {
      setLoadingPart(false);
    }
  }, []);

  useEffect(() => {
    void loadInventory().catch((e) => toast.error(errMsg(e)));
  }, [loadInventory]);

  useEffect(() => {
    void loadPart(selected);
  }, [selected, loadPart]);

  const flashParts = useCallback((names: string[]) => {
    setFlash(new Set(names));
    setTimeout(() => setFlash(new Set()), 2200);
  }, []);

  const runReflect = (trigger: 'chat' | 'learn' | 'scan'): void => {
    setReflecting(trigger);
    void (async () => {
      try {
        const res = await reflectBrain(trigger);
        const a = res.applied;
        const n = a.added_rules.length + a.retired_rules.length +
          a.strategy_updates.length + (a.instructions_updated ? 1 : 0);
        toast.success(t('brain.reflected', { n }));
        await loadInventory();
        flashParts([
          ...(a.instructions_updated ? ['instructions'] : []),
          ...(a.added_rules.length || a.retired_rules.length ? ['rules'] : []),
          ...(a.strategy_updates.length ? ['strategies'] : []),
          'journal',
        ]);
        if (selected === 'journal' || selected === 'rules' ||
            selected === 'strategies' || selected === 'instructions') {
          await loadPart(selected);
        }
      } catch (e) {
        toast.error(t('brain.reflectFailed', { msg: errMsg(e) }));
      } finally {
        setReflecting(null);
      }
    })();
  };

  const saveEdit = (): void => {
    if (!data) return;
    const part = data.name;
    void (async () => {
      try {
        await putBrainPart(part, draft);
        toast.success(t('brain.saved'));
        setEditing(false);
        await loadInventory();
        await loadPart(part);
        flashParts([part]);
      } catch (e) {
        toast.error(t('brain.saveFailed', { msg: errMsg(e) }));
      }
    })();
  };

  const uploadPhoto = (file: File): void => {
    setUploading(true);
    void (async () => {
      try {
        await uploadBrainPhoto(file, caption);
        setCaption('');
        toast.success(t('inbox.uploaded'));
        await loadInventory();
        await loadPart('photos');
        flashParts(['photos']);
      } catch (e) {
        toast.error(t('brain.uploadFailed', { msg: errMsg(e) }));
      } finally {
        setUploading(false);
        if (fileInput.current) fileInput.current.value = '';
      }
    })();
  };

  // tree groups
  const core = parts.filter((p) =>
    ['instructions', 'rules', 'strategies'].includes(p.name));
  const files = parts.filter((p) => p.name.startsWith('files/'));
  const log = parts.filter((p) => ['journal', 'photos'].includes(p.name));
  const rules = data?.rules ?? [];
  const activeRules = rules.filter((r) => r.status === 'active');
  const retiredRules = rules.filter((r) => r.status === 'retired');

  const treeRow = (p: BrainPart) => {
    const Icon = iconFor(p.name);
    const active = selected === p.name;
    return (
      <button
        key={p.name}
        type="button"
        onClick={() => setSelected(p.name)}
        className={cn(
          'flex w-full flex-col gap-0.5 rounded-[8px] px-2 py-1.5 text-start transition-colors duration-100',
          active ? 'bg-hover' : 'hover:bg-hover/60',
          flash.has(p.name) && 'ring-1 ring-accent',
        )}
        style={flash.has(p.name)
          ? { animation: 'pop-in 300ms cubic-bezier(0.23,1,0.32,1) both' }
          : undefined}
      >
        <span className="flex w-full items-center gap-2">
          <Icon size={13} strokeWidth={2} className={cn('shrink-0', active ? 'text-ink' : 'text-ink-3')} />
          <span className={cn(
            'min-w-0 flex-1 truncate text-[12.5px]',
            active ? 'font-medium text-ink' : 'text-ink-2',
          )}>
            {fileLabel(p.name)}
          </span>
        </span>
        <span className="ps-[21px] font-mono text-[10px] text-ink-3">
          {shortWhen(p.modified)} · {p.summary}
        </span>
      </button>
    );
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="mx-auto w-full max-w-[1180px] px-6 pt-6">
        <PageHeader
          title={t('brain.title')}
          subtitle={t('brain.subtitle')}
          actions={
            <div className="flex items-center gap-2">
              {reflecting ? (
                <LoadingState label={t('brain.reflecting', { trigger: reflecting })} />
              ) : (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="primary" size="sm">
                      <Sparkles size={13} /> {t('brain.reflect')}
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => runReflect('chat')}>
                      <RefreshCw size={12} /> {t('brain.reflectChat')}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => runReflect('learn')}>
                      <RefreshCw size={12} /> {t('brain.reflectLearn')}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => runReflect('scan')}>
                      <RefreshCw size={12} /> {t('brain.reflectScan')}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          }
        />
      </div>

      <div className="mx-auto flex min-h-0 w-full max-w-[1180px] flex-1 gap-4 px-6 pb-6">
        {/* file tree */}
        <aside className="flex w-[236px] min-w-[210px] flex-col gap-3 overflow-y-auto rounded-xl border border-edge bg-panel p-2">
          {[
            { label: t('brain.sectionCore'), items: core },
            { label: t('brain.sectionFiles'), items: files },
            { label: t('brain.sectionLog'), items: log },
          ].map((group) =>
            group.items.length ? (
              <div key={group.label}>
                <div className="px-2 pt-1 pb-1 text-[10.5px] font-medium uppercase tracking-[0.08em] text-ink-3">
                  {group.label}
                </div>
                <div className="flex flex-col gap-px">{group.items.map(treeRow)}</div>
              </div>
            ) : null,
          )}
        </aside>

        {/* content pane */}
        <div className="min-w-0 flex-1 overflow-y-auto rounded-xl border border-edge bg-panel p-5">
          {loadingPart ? (
            <div>
              <Skeleton className="mb-4 h-5 w-44" />
              <Skeleton className="mb-2.5 h-4 w-full" />
              <Skeleton className="mb-2.5 h-4 w-[92%]" />
              <Skeleton className="h-4 w-[70%]" />
            </div>
          ) : data ? (
            data.name === 'photos' ? (
              <div>
                <div className="mb-4 flex flex-wrap items-center gap-2">
                  <Input
                    value={caption}
                    onChange={(e) => setCaption(e.target.value)}
                    placeholder={t('brain.photoCaption')}
                    className="h-8 max-w-[320px] text-[13px]"
                  />
                  <input
                    ref={fileInput}
                    type="file"
                    accept="image/png,image/jpeg,image/webp,image/gif"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) uploadPhoto(f);
                    }}
                  />
                  <Button size="sm" disabled={uploading} onClick={() => fileInput.current?.click()}>
                    <Upload size={13} /> {uploading ? t('brain.uploading') : t('brain.addPhoto')}
                  </Button>
                </div>
                <p className="mb-4 text-[11.5px] text-muted">{t('brain.noVision')}</p>
                {(data.photos ?? []).length === 0 ? (
                  <div className="rounded-lg border border-dashed border-edge py-10 text-center text-[13px] text-muted">
                    {t('brain.photosEmpty')}
                  </div>
                ) : (
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                    {(data.photos ?? []).map((ph) => (
                      <figure
                        key={ph.name}
                        className="overflow-hidden rounded-lg border border-edge bg-surface"
                      >
                        <img src={ph.url} alt={ph.caption || ph.name} className="h-32 w-full object-cover" />
                        <figcaption dir="auto" className="p-2 text-[11.5px] leading-snug text-ink-2">
                          {ph.caption || ph.name}
                        </figcaption>
                      </figure>
                    ))}
                  </div>
                )}
              </div>
            ) : data.name === 'journal' ? (
              <div>
                {(data.entries ?? []).length === 0 ? (
                  <div className="rounded-lg border border-dashed border-edge py-10 text-center text-[13px] text-muted">
                    {t('brain.journalEmpty')}
                  </div>
                ) : (
                  <ol className="relative flex flex-col gap-4 ps-5">
                    <span aria-hidden className="absolute inset-y-1 start-[7px] w-px bg-line" />
                    {(data.entries ?? []).map((e, i) => (
                      <li key={`${e.date}-${e.time}-${i}`} className="relative">
                        <span
                          aria-hidden
                          className="absolute -start-5 top-[5px] size-[9px] rounded-full border-2 border-panel bg-accent"
                        />
                        <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-ink-3">
                          <span>{e.date} {e.time}</span>
                          <SourceBadge source={e.trigger.replace(/^reflect:|user-edit:|-edit/, '')} />
                          <span className="rounded-full bg-inset px-1.5 py-px">{e.trigger}</span>
                        </div>
                        {e.body ? (
                          <p dir="auto" className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-ink">
                            {e.body}
                          </p>
                        ) : null}
                        {e.changes.length ? (
                          <ul className="mt-1.5 flex flex-col gap-0.5">
                            {e.changes.map((c, j) => (
                              <li key={j} dir="auto" className="text-[12px] text-ink-2">
                                — {c}
                              </li>
                            ))}
                          </ul>
                        ) : null}
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            ) : data.name === 'rules' && !editing ? (
              <div>
                <div className="mb-4 flex items-center justify-between gap-2">
                  <span className="font-mono text-[11.5px] text-ink-3">
                    {t('brain.rulesActive', { n: activeRules.length, m: retiredRules.length })}
                  </span>
                  <Button size="sm" variant="ghost" onClick={() => { setDraft(data.content ?? ''); setEditing(true); }}>
                    {t('common.edit')}
                  </Button>
                </div>
                {rules.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-edge py-10 text-center text-[13px] text-muted">
                    {t('brain.rulesEmpty')}
                  </div>
                ) : (
                  <ol className="flex flex-col gap-3">
                    {[...activeRules, ...retiredRules].map((r) => (
                      <li
                        key={r.id}
                        className={cn(
                          'rounded-lg border border-edge bg-surface p-3',
                          r.status === 'retired' && 'opacity-55',
                        )}
                      >
                        <div className="mb-1 flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[11px] font-semibold text-accent-ink">
                            R{r.id}
                          </span>
                          <SourceBadge source={r.source} />
                          <span className="font-mono text-[10.5px] text-ink-3">{r.date}</span>
                          {r.status === 'retired' ? (
                            <span className="rounded-full bg-inset px-1.5 py-px font-mono text-[10px] text-ink-3 line-through">
                              {t('brain.retired')}
                            </span>
                          ) : null}
                        </div>
                        <p
                          dir="auto"
                          className={cn('text-[13.5px] leading-relaxed', r.status === 'retired' ? 'line-through text-ink-3' : 'text-ink')}
                        >
                          {r.text}
                        </p>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            ) : editing ? (
              <div className="flex h-full flex-col gap-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-ink-3">
                    {data.name} · {t('brain.rawMarkdown')}
                  </span>
                  <div className="flex gap-2">
                    <Button size="sm" variant="primary" onClick={saveEdit}>
                      {t('common.save')}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                      {t('common.cancel')}
                    </Button>
                  </div>
                </div>
                <Textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  dir="auto"
                  spellCheck={false}
                  className="min-h-[320px] flex-1 resize-none font-mono text-[12.5px] leading-relaxed"
                />
                <p className="text-[11.5px] text-muted">{t('brain.editHint')}</p>
              </div>
            ) : (
              <div>
                <div className="mb-3 flex items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-ink-3">{data.name}</span>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => { setDraft(data.content ?? ''); setEditing(true); }}
                  >
                    {t('common.edit')}
                  </Button>
                </div>
                <MarkdownView text={data.content ?? ''} />
              </div>
            )
          ) : null}
        </div>
      </div>
    </div>
  );
}
