import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { isValidElement, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Components } from 'react-markdown';
import { toast } from 'sonner';
import { FilePlus2 } from 'lucide-react';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  streamChat,
  getLoopsStatus,
  uploadMedia,
  type ChatAction,
  type ChatCandidate,
  type ChatFallbackResponse,
  type ChatHistoryEntry,
  type ContextChunk,
  type Idea,
  type InsightsResponse,
  type LoopStatusEntry,
  type Settings,
  type ThinkingStep,
} from '@/lib/api';
import { triggerLoop, type LoopName } from '@/lib/loops';
import { cn, errMsg, fmtDateTime, hasArabic } from '@/lib/utils';
import { ScoreBadge } from '@/components/ScoreBadge';
import { VoiceChip } from '@/components/VoiceChip';

// beautifului primitives (beautifului.dev, MIT © 2026 Shane Levine)
import LoadingState from '@/components/bui/components/LoadingState';
import ThinkingState from '@/components/bui/components/Thinking';
import ToolChips from '@/components/bui/components/ToolChips';
import type { ToolCall } from '@/components/bui/components/ToolChips';
import ApprovalCard from '@/components/bui/components/ApprovalCard';
import RecommendationCard from '@/components/bui/components/RecommendationCard';
import type { RecOption } from '@/components/bui/components/RecommendationCard';
import ContextCards from '@/components/bui/components/ContextCards';
import SelectionActions from '@/components/bui/components/SelectionActions';
import type { SelectionActionDef } from '@/components/bui/components/SelectionActions';
import TaskRows from '@/components/bui/components/TaskRows';
import type { TaskRowItem } from '@/components/bui/components/TaskRows';
import FineTuneCard from '@/components/bui/components/FineTuneCard';
import type { VoiceTune } from '@/components/bui/components/FineTuneCard';
import CodeBlock from '@/components/bui/components/CodeBlock';
import PromptBar from '@/components/bui/components/PromptBar';
import type {
  PromptCommand,
  PromptLanguage,
  PromptSource,
  PromptTemperature,
} from '@/components/bui/components/PromptBar';
import { ChatSection, ChatTabs, UserBubble } from '@/components/bui/components/Chat';

interface ChatMsg {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  /** tokens still arriving */
  streaming?: boolean;
  /** waiting for the first token / thinking trace */
  waiting?: boolean;
  steps?: ThinkingStep[];
  chunks?: ContextChunk[];
  tools?: ToolCall[];
  candidates?: ChatCandidate[];
  actions?: ChatAction[];
}

/** /command → full OpenStanley prompt */
function expandCommand(raw: string): string {
  const m = /^\/(\w+)\s*(.*)$/s.exec(raw.trim());
  if (!m) return raw;
  const [, cmd, rest] = m;
  switch (cmd) {
    case 'draft':
      return `Write a post${rest ? ` about ${rest}` : ''}. Put the post in a quote block so I can approve it.`;
    case 'schedule':
      return `Schedule my next post${rest ? ` (${rest})` : ' at your recommended best hour'}.`;
    case 'quote':
      return `Create a quote post${rest ? ` about: ${rest}` : ''}. Put it in a quote block.`;
    case 'scan':
      return 'Scan my account and summarize my style profile.';
    case 'strategy':
      return 'Show me my current strategy and what to change next.';
    case 'best-post':
      return "What's my best post recently and why did it work?";
    default:
      return raw;
  }
}

/** reply-language picker → language marker the backend's detector reads */
const LANG_PREFIX: Record<PromptLanguage, string> = {
  auto: '',
  ar: 'رُدّ بالعربية فقط.\n',
  en: 'Reply in English only.\n',
  mixed: 'ردّ بخليط العربية والإنجليزية معاً.\n',
};

/** Recursively flatten react children to plain text (for saving blockquotes). */
function nodeToText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(nodeToText).join('');
  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode };
    return nodeToText(props.children);
  }
  return '';
}

function useDraftSaver(): (text: string) => void {
  const { t } = useApp();
  return useCallback(
    (text: string) => {
      void (async () => {
        try {
          const r = await apiPost<{ ok: boolean; draft_id: number }>('chat/draft', { text });
          toast.success(t('write.draftSaved', { id: r.draft_id }));
        } catch (e) {
          toast.error(t('write.draftSaveFailed', { msg: errMsg(e) }));
        }
      })();
    },
    [t],
  );
}

function DraftQuoteBox({ text, children }: { text: string; children: ReactNode }) {
  const { t } = useApp();
  const save = useDraftSaver();
  return (
    <div dir={hasArabic(text) ? 'rtl' : 'ltr'} className="my-2 rounded-lg bg-inset">
      <div className="border-s-[3px] border-accent px-3 py-2 text-[13.5px] text-ink/90">
        {children}
      </div>
      <div className="px-3 pb-2">
        <button
          onClick={() => save(text)}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-line bg-surface px-2.5 py-1 text-[11.5px] text-accent-ink transition-colors hover:border-accent"
        >
          <FilePlus2 size={11} />
          {t('write.saveAsDraft')}
        </button>
      </div>
    </div>
  );
}

const mdComponents: Components = {
  blockquote({ children }) {
    const text = nodeToText(children).trim();
    return <DraftQuoteBox text={text}>{children}</DraftQuoteBox>;
  },
};

// ---------------- assistant turn ----------------

function CandidateApproval({
  candidate,
  busy,
  onRegenerate,
  stagedImage,
  onImageConsumed,
}: {
  candidate: ChatCandidate;
  busy: boolean;
  onRegenerate: () => void;
  stagedImage?: string | null;
  onImageConsumed?: () => void;
}) {
  const { t } = useApp();
  const [savedId, setSavedId] = useState<number | null>(null);
  const rtl = hasArabic(candidate.text);
  return (
    <ApprovalCard
      question={t('write.approvalShip')}
      acceptLabel={t('write.approvalAccept')}
      regenLabel={t('write.approvalRegen')}
      savedLabel={t('write.approvalSaved', { id: savedId ?? '…' })}
      altHint={t('write.approvalAltHint')}
      factorsLabel={t('write.approvalFactors')}
      factors={candidate.alg?.factors}
      disabled={busy}
      scoreChip={
        candidate.alg ? (
          <span className="inline-flex h-5.5 items-center gap-1 rounded-full bg-inset px-2 font-mono text-[11px] font-semibold text-ink-2 shadow-hairline">
            <span className="size-1.5 rounded-full bg-accent" />
            {t('write.approvalScore', { score: candidate.alg.score })}
          </span>
        ) : undefined
      }
      voiceChip={
        candidate.voice ? (
          <VoiceChip voice={candidate.voice} />
        ) : candidate.voice_match !== undefined && candidate.voice_match !== null ? (
          <span className="inline-flex h-5.5 items-center rounded-full bg-green-tint px-2 font-mono text-[11px] font-semibold text-green">
            {t('write.approvalVoice', { n: Math.round(candidate.voice_match) })}
          </span>
        ) : undefined
      }
      onAccept={async () => {
        try {
          const r = await apiPost<{ ok: boolean; draft_id: number }>('chat/draft', {
            text: candidate.text,
            image: stagedImage ?? undefined,
          });
          setSavedId(r.draft_id);
          onImageConsumed?.();
          toast.success(t('write.draftSaved', { id: r.draft_id }));
        } catch (e) {
          toast.error(t('write.draftSaveFailed', { msg: errMsg(e) }));
        }
      }}
      onRegenerate={onRegenerate}
    >
      <div
        dir={rtl ? 'rtl' : 'ltr'}
        className="mt-2 whitespace-pre-wrap rounded-control bg-inset px-3 py-2.5 text-[13.5px] leading-relaxed text-ink"
        style={{ animation: 'fade-in 250ms ease-out both' }}
      >
        {candidate.text}
      </div>
    </ApprovalCard>
  );
}

function AssistantTurn({
  msg,
  busy,
  onFollowUp,
  onPatchContent,
  stagedImage,
  onImageConsumed,
}: {
  msg: ChatMsg;
  busy: boolean;
  onFollowUp: (text: string) => void;
  /** selection-rewrite Keep applies the replacement to this turn */
  onPatchContent: (key: string, selected: string, replacement: string) => void;
  stagedImage?: string | null;
  onImageConsumed?: () => void;
}) {
  const { t, lang } = useApp();
  const [tab, setTab] = useState<'reply' | 'reasoning'>('reply');
  const [showRaw, setShowRaw] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const rtl = hasArabic(msg.content);
  const done = !msg.streaming;

  const followUps = useMemo(() => {
    const out: { key: string; label: string }[] = [];
    if (msg.candidates?.length) {
      out.push({ key: 'schedule', label: t('write.followSchedule') });
      out.push({ key: 'raw', label: t('write.followRaw') });
    }
    out.push({ key: 'week', label: t('write.followWeek') });
    out.push({ key: 'best', label: t('write.followAnalyze') });
    return out.slice(0, 3);
  }, [msg.candidates?.length, t]);

  const runFollowUp = (key: string): void => {
    if (key === 'raw') {
      setShowRaw((v) => !v);
      return;
    }
    if (key === 'schedule') onFollowUp(t('write.followSchedule'));
    else if (key === 'week') onFollowUp(t('write.followWeek'));
    else if (key === 'best') onFollowUp(t('write.followAnalyze'));
  };

  const rewriteActions: SelectionActionDef[] = useMemo(
    () => [
      { key: 'shorter', label: t('write.rewriteShorter'), instruction: 'Make it much shorter, same voice' },
      { key: 'arabic', label: t('write.rewriteArabic'), instruction: 'Rewrite it in Arabic, same voice' },
      { key: 'punchier', label: t('write.rewritePunchier'), instruction: 'Make it punchier and bolder, same voice' },
    ],
    [t],
  );

  const onRewrite = useCallback(
    async (instruction: string, selected: string): Promise<string> => {
      const r = await apiPost<ChatFallbackResponse>('chat', {
        message: `${instruction}. Reply with ONLY the rewritten text, no quotes, no commentary:\n\n${selected}`,
      });
      return r.reply;
    },
    [],
  );

  const rawJson = useMemo(() => {
    const c = msg.candidates?.[0];
    return JSON.stringify(c ?? { text: msg.content }, null, 2);
  }, [msg.candidates, msg.content]);

  return (
    <div className="mb-4 flex w-full flex-col items-start">
      <div className="mb-1 ps-1 font-mono text-[11px] text-ink-3">{t('common.openstanley')}</div>
      <div className="relative w-full max-w-[95%] overflow-hidden rounded-[14px] bg-surface shadow-card">
        <ChatTabs
          tabs={[
            { key: 'reply', label: t('write.tabReply') },
            { key: 'reasoning', label: t('write.tabReasoning') },
          ]}
          active={tab}
          onChange={(k) => setTab(k as 'reply' | 'reasoning')}
          trailing={
            msg.candidates?.[0]?.alg ? <ScoreBadge alg={msg.candidates[0].alg} plain /> : undefined
          }
        />

        {tab === 'reply' ? (
          <div className="flex flex-col gap-2.5 p-3">
            {/* thinking trace — expandable above the reply */}
            {msg.steps && msg.steps.length > 0 ? (
              <ThinkingState
                steps={msg.steps}
                running={!!msg.streaming}
                activeLabel={t('write.thinking')}
                doneLabel={t('write.thoughtFor', { n: Math.max(1, msg.steps.length * 2) })}
              />
            ) : null}

            {msg.waiting && !msg.content ? (
              <div className="py-1">
                <LoadingState label={t('write.buildingContext')} variant="Drive" />
              </div>
            ) : null}

            <div ref={hostRef} dir={rtl ? 'rtl' : 'ltr'} className="relative">
              <div className="md text-[14px] leading-relaxed text-ink">
                <Markdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                  {msg.content}
                </Markdown>
              </div>
              {msg.streaming && msg.content ? (
                <span
                  className="ms-0.5 inline-block h-3.5 w-[7px] rounded-[2px] bg-accent align-middle"
                  style={{ animation: 'caret-blink 900ms steps(1) infinite' }}
                />
              ) : null}

              {/* select any reply text → floating rewrite bar */}
              <SelectionActions
                hostRef={hostRef}
                enabled={done && msg.content.length > 0}
                actions={rewriteActions}
                busyLabel={t('write.rewriteBusy')}
                failedLabel={t('write.rewriteFailed')}
                customPlaceholder={t('write.rewriteCustom')}
                keepLabel={t('write.rewriteKeep')}
                discardLabel={t('write.rewriteDiscard')}
                onRewrite={onRewrite}
                onApply={(selected, replacement) =>
                  onPatchContent(msg.key, selected, replacement)
                }
              />
            </div>

            {msg.tools && msg.tools.length > 0 ? <ToolChips tools={msg.tools} /> : null}

            {done && msg.actions && msg.actions.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {msg.actions.map((a) => (
                  <button
                    key={a.id}
                    disabled={busy}
                    onClick={() => void triggerLoop(a.id as LoopName, t)}
                    className={cn(
                      'cursor-pointer rounded-chip bg-hover-2 px-3 py-1 text-[12px] text-accent-ink shadow-hairline transition-colors',
                      'hover:bg-line-strong disabled:opacity-45',
                    )}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            ) : null}

            {msg.candidates && msg.candidates.length > 0 ? (
              <div className="flex flex-col gap-2" style={{ animation: 'fade-up 300ms cubic-bezier(0.23,1,0.32,1) both' }}>
                {msg.candidates.map((c, i) => (
                  <CandidateApproval
                    key={i}
                    candidate={c}
                    busy={busy}
                    onRegenerate={() => onFollowUp('Regenerate that post, hotter.')}
                    stagedImage={stagedImage}
                    onImageConsumed={onImageConsumed}
                  />
                ))}
              </div>
            ) : null}

            {showRaw ? (
              <CodeBlock filename={t('write.rawJson')} language={t('write.rawHint')} code={rawJson} />
            ) : null}

            {/* follow-up suggestion chips */}
            <div
              className="transition-opacity duration-400"
              style={{ opacity: done && msg.content ? 1 : 0, pointerEvents: done ? 'auto' : 'none' }}
            >
              <p className="text-[12px] font-medium text-ink-2">{t('write.followUps')}</p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {followUps.map((f, i) => (
                  <button
                    key={f.key}
                    type="button"
                    onClick={() => runFollowUp(f.key)}
                    className="-mx-0 flex items-center gap-1.5 rounded-full border border-line px-2.5 py-1 text-[12px] text-ink transition-colors duration-100 hover:bg-hover-2"
                    style={{ animation: `fade-up 350ms cubic-bezier(0.23,1,0.32,1) ${i * 90}ms both` }}
                  >
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                      <path d="M9 10l-5 5 5 5" />
                      <path d="M20 4v7a4 4 0 0 1-4 4H4" />
                    </svg>
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          /* reasoning tab — the context-gathering trace + retrieved chunks */
          <div className="flex flex-col gap-3 p-3">
            {(msg.steps ?? []).map((s, i) => (
              <ChatSection
                key={s.id}
                label={s.primary}
                sub={s.secondary}
                body={msg.chunks?.[i]?.body ?? t('write.contextUsed')}
              />
            ))}
            {msg.chunks && msg.chunks.length > 0 ? (
              <ContextCards
                chunks={msg.chunks}
                title={t('write.contextUsed')}
                totalLabel={t('write.chunks', { n: msg.chunks.length })}
              />
            ) : (
              <p className="py-2 text-[12.5px] text-ink-3">{t('write.contextUsed')}</p>
            )}
            {msg.tools && msg.tools.length > 0 ? (
              <ChatSection
                label={t('write.toolOne')}
                sub={`${msg.tools.length}`}
                body={msg.tools.map((tool) => tool.name).join(' · ')}
              />
            ) : null}
            <p className="font-mono text-[10.5px] text-ink-3/70">
              {lang === 'ar' ? 'ستانلي يجمع سياقه من بياناتك الحقيقية فقط' : 'OpenStanley grounds every reply in your real data only'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------- right rail ----------------

const RAIL_LOOPS: LoopName[] = ['study', 'create', 'engage', 'publish'];

function LoopsPanel() {
  const { t, lang } = useApp();
  const [loops, setLoops] = useState<LoopStatusEntry[]>([]);

  useEffect(() => {
    let alive = true;
    const load = (): void => {
      getLoopsStatus()
        .then((r) => {
          if (alive) setLoops(r.loops);
        })
        .catch(() => undefined);
    };
    load();
    const id = setInterval(load, 30000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const tasks = useMemo<TaskRowItem[]>(
    () =>
      RAIL_LOOPS.map((name) => {
        const entry = loops.find((l) => l.name === name);
        const status: TaskRowItem['status'] =
          entry?.last_status === 'ok' ? 'ok' : entry?.last_status === 'error' ? 'error' : 'idle';
        const next = entry?.next_run ? entry.next_run.slice(11, 16) : null;
        return {
          key: name,
          label: t(`loops.${name}`),
          status,
          amount: next ? t('write.loopNext', { when: next }) : t('write.loopIdle'),
          details: [
            {
              label: t('write.loopLast'),
              meta: entry?.last_run ? fmtDateTime(entry.last_run, lang) : t('write.loopNever'),
            },
            {
              label: t('write.loopNext', { when: entry?.next_run?.slice(11, 16) ?? '—' }),
              meta: entry?.last_status === 'error' ? t('write.loopError') : t('write.loopOk'),
            },
            ...(entry?.last_message
              ? [{ label: t('log.message'), meta: entry.last_message.slice(0, 30) }]
              : []),
          ],
        };
      }),
    [loops, t, lang],
  );

  return (
    <section>
      <h3 className="mb-1.5 px-0.5 text-[12px] font-semibold uppercase tracking-[0.08em] text-ink-3">
        {t('write.loopsTitle')}
      </h3>
      <TaskRows tasks={tasks} />
    </section>
  );
}

function BestHourPanel({ onAccept }: { onAccept: (hour: number) => void }) {
  const { t } = useApp();
  const [options, setOptions] = useState<RecOption[]>([]);

  useEffect(() => {
    let alive = true;
    api<InsightsResponse>('insights')
      .then((r) => {
        if (!alive) return;
        const hours = [...r.best_hours].sort((a, b) => b.avg_engagement - a.avg_engagement);
        const top = hours.filter((h) => h.avg_engagement > 0).slice(0, 3);
        const max = top[0]?.avg_engagement ?? 1;
        setOptions(
          top.map((h, i) => {
            const conf = Math.min(95, Math.round(50 + (h.avg_engagement / max) * 45));
            return {
              key: String(h.hour),
              body: (
                <>
                  {t('write.schedTitle', { time: `${String(h.hour).padStart(2, '0')}:00` })} —{' '}
                  <code className="rounded-md bg-accent-tint px-1.5 py-0.5 font-mono text-[12px] text-accent-ink">
                    {h.avg_engagement.toFixed(2)}×
                  </code>{' '}
                  {t('write.schedConfidence', { n: conf })}
                </>
              ),
              short: `${String(h.hour).padStart(2, '0')}:00`,
              signal: i === 0 ? 3 : i === 1 ? 2 : 1,
              tone: i === 0 ? 'var(--green)' : i === 1 ? 'var(--orange)' : 'var(--ink-3)',
              label: i === 0 ? t('write.schedAltHigh') : i === 1 ? t('write.schedAltMid') : t('write.schedAltLow'),
            };
          }),
        );
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [t]);

  return (
    <RecommendationCard
      title={t('write.schedTitle', { time: options[0]?.short ?? '—' })}
      options={options}
      cta={t('write.schedAccept')}
      acceptedLabel={t('write.approvalSaved', { id: '✓' })}
      alternativesLabel={t('write.schedAlt')}
      emptyHint={t('write.schedNoData')}
      onAccept={(key) => onAccept(Number(key))}
    />
  );
}

function VoicePanel() {
  const { t } = useApp();
  const [tune, setTune] = useState<VoiceTune>({
    temperature: 'bold',
    formality: 50,
    langMix: 50,
    emoji: 3,
  });

  useEffect(() => {
    api<Settings>('settings')
      .then((s) => {
        setTune({
          temperature: s.voice_temperature ?? 'bold',
          formality: s.voice_formality ?? 50,
          langMix: s.voice_lang_mix ?? 50,
          emoji: s.voice_emoji_density ?? 3,
        });
      })
      .catch(() => undefined);
  }, []);

  const onChange = (next: VoiceTune): void => {
    setTune(next);
    void apiPost('settings', {
      voice_temperature: next.temperature,
      voice_formality: next.formality,
      voice_lang_mix: next.langMix,
      voice_emoji_density: next.emoji,
    }).catch(() => undefined);
  };

  return (
    <FineTuneCard
      tune={tune}
      onChange={onChange}
      title={t('write.voiceCard')}
      adjustingLabel={t('write.voiceAdjust')}
      editedLabel={t('write.voiceEdited')}
      segLabels={[t('write.tempSafe'), t('write.tempBold'), t('write.tempExperimental')]}
      fieldLabels={{
        formality: t('write.voiceFormality'),
        langMix: t('write.voiceArMix'),
        emoji: t('write.voiceEmoji'),
      }}
      footerHint={t('write.voiceHint')}
    />
  );
}

// ---------------- page ----------------

export function WritePage() {
  const { t } = useApp();
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [sources, setSources] = useState<PromptSource[]>([]);
  const [temperature, setTemperature] = useState<PromptTemperature>('bold');
  const [language, setLanguage] = useState<PromptLanguage>('auto');
  const scrollRef = useRef<HTMLDivElement>(null);
  const counter = useRef(0);
  const [stagedImage, setStagedImage] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const stageFile = async (f: File | undefined) => {
    if (!f) return;
    try {
      setUploading(true);
      const r = await uploadMedia(f);
      setStagedImage(r.name);
    } catch (e) {
      toast.error(errMsg(e));
    } finally {
      setUploading(false);
    }
  };

  const nextKey = (p: string): string => {
    counter.current += 1;
    return `${p}-${counter.current}`;
  };

  // history + composer data (settings, ideas → @sources)
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const hist = await api<ChatHistoryEntry[]>('chat/history');
        if (!alive) return;
        setMessages(hist.map((h, i) => ({ key: `h-${i}`, role: h.role, content: h.content })));
      } catch {
        if (alive) setMessages([]);
      } finally {
        if (alive) setLoaded(true);
      }
    })();
    void (async () => {
      const [settingsR, ideasR] = await Promise.allSettled([
        api<Settings>('settings'),
        api<Idea[]>('ideas?limit=6'),
      ]);
      if (!alive) return;
      const next: PromptSource[] = [];
      if (settingsR.status === 'fulfilled') {
        setTemperature(settingsR.value.voice_temperature ?? 'bold');
        for (const acc of settingsR.value.niche_accounts ?? []) {
          next.push({ key: `acct-${acc}`, name: acc, desc: t('write.sourcesHeader'), glyph: 'at' });
        }
      }
      if (ideasR.status === 'fulfilled') {
        for (const idea of ideasR.value) {
          next.push({
            key: `idea-${idea.id}`,
            name: idea.title.length > 28 ? `${idea.title.slice(0, 28)}…` : idea.title,
            desc: `${t('nav.ideas')} · ${idea.score.toFixed(1)}`,
            glyph: 'idea',
          });
        }
      }
      setSources(next);
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const commands = useMemo<PromptCommand[]>(
    () => [
      { key: 'draft', name: '/draft', desc: t('ideas.writeIt').toLowerCase() + ' — write a post', glyph: 'write' },
      { key: 'schedule', name: '/schedule', desc: t('write.followSchedule'), glyph: 'clock' },
      { key: 'quote', name: '/quote', desc: 'quote-post a tweet', glyph: 'write' },
      { key: 'scan', name: '/scan', desc: t('connect.scan'), glyph: 'target' },
      { key: 'strategy', name: '/strategy', desc: t('strategy.title'), glyph: 'target' },
      { key: 'best-post', name: '/best-post', desc: t('write.followAnalyze'), glyph: 'chart' },
    ],
    [t],
  );

  const send = (raw: string): void => {
    const msg = LANG_PREFIX[language] + expandCommand(raw);
    if (!msg.trim() || busy) return;
    const userKey = nextKey('u');
    const botKey = nextKey('a');
    setMessages((m) => [
      ...m,
      { key: userKey, role: 'user', content: msg },
      { key: botKey, role: 'assistant', content: '', streaming: true, waiting: true },
    ]);
    setBusy(true);

    const patch = (fn: (prev: ChatMsg) => ChatMsg): void => {
      setMessages((m) => m.map((x) => (x.key === botKey ? fn(x) : x)));
    };

    const run = async (): Promise<void> => {
      try {
        await streamChat(msg, {
          onThinkingSteps: (ev) =>
            patch((p) => ({
              ...p,
              steps: ev.steps,
              chunks: ev.chunks,
              waiting: false,
            })),
          onToken: (tok) =>
            patch((p) => ({ ...p, content: p.content + tok, waiting: false })),
          onTool: (ev) =>
            patch((p) => ({
              ...p,
              tools: [
                ...(p.tools ?? []),
                { name: ev.name, args: ev.args, ok: ev.ok, result: ev.result },
              ],
            })),
          onApproval: (ev) =>
            patch((p) => ({
              ...p,
              candidates: [...(p.candidates ?? []), ev.candidate],
            })),
          onDone: (ev) =>
            patch((p) => ({
              ...p,
              streaming: false,
              // server sends the cleaned reply (action fences stripped)
              content: ev.reply ?? p.content,
              actions: ev.actions,
              candidates: ev.candidates.length ? ev.candidates : p.candidates,
            })),
          onError: (message) =>
            patch((p) => ({
              ...p,
              streaming: false,
              content: p.content + (p.content ? '\n\n' : '') + `**${message}**`,
            })),
        });
      } catch (e) {
        // streaming endpoint unavailable (older backend) — non-streaming fallback
        try {
          const r = await apiPost<ChatFallbackResponse>('chat', { message: msg });
          patch((p) => ({
            ...p,
            streaming: false,
            content: r.reply,
            actions: r.actions,
            tools: r.tool_results?.map((tr) => ({
              name: tr.name,
              args: tr.args,
              ok: tr.ok,
              result: tr,
            })),
            candidates: r.candidates,
            steps: r.thinking_steps,
            chunks: r.context_chunks,
          }));
        } catch {
          patch((p) => ({
            ...p,
            streaming: false,
            content:
              p.content + (p.content ? '\n\n' : '') + t('write.sendFailed', { msg: errMsg(e) }),
          }));
        }
      } finally {
        setBusy(false);
      }
    };
    void run();
  };

  const patchContent = (key: string, selected: string, replacement: string): void => {
    setMessages((m) =>
      m.map((x) =>
        x.key === key && x.content.includes(selected)
          ? { ...x, content: x.content.replace(selected, replacement) }
          : x,
      ),
    );
  };

  const welcome = useMemo<ChatMsg>(
    () => ({ key: 'welcome', role: 'assistant', content: t('write.welcome') }),
    [t],
  );

  const list = loaded && messages.length === 0 ? [welcome] : messages;

  return (
    <div className="flex h-full">
      <div className="mx-auto flex h-full w-full min-w-0 max-w-[880px] flex-col px-4 sm:px-6">
        <div ref={scrollRef} className="flex-1 overflow-y-auto py-4">
          {list.map((m) => {
            const rtl = hasArabic(m.content);
            if (m.role === 'user') {
              return (
                <div key={m.key} className="mb-3 flex flex-col items-end gap-1">
                  <div className="pe-1 font-mono text-[11px] text-ink-3">{t('common.you')}</div>
                  <UserBubble>
                    <span dir={rtl ? 'rtl' : 'ltr'}>{m.content}</span>
                  </UserBubble>
                </div>
              );
            }
            return (
              <AssistantTurn
                key={m.key}
                msg={m}
                busy={busy}
                onFollowUp={send}
                onPatchContent={patchContent}
                stagedImage={stagedImage}
                onImageConsumed={() => setStagedImage(null)}
              />
            );
          })}
        </div>

        <div className="pb-4 pt-3">
          {stagedImage ? (
            <div className="mb-2 inline-flex items-center gap-2 rounded-chip bg-inset px-2 py-1">
              <img src={`/api/media/${stagedImage}`} alt="" className="size-6 rounded object-cover" />
              <span className="max-w-40 truncate font-mono text-[11px] text-ink-2">{stagedImage}</span>
              <button
                type="button"
                onClick={() => setStagedImage(null)}
                className="text-ink-3 hover:text-ink"
                aria-label={t('inbox.composeImage')}
              >
                ✕
              </button>
            </div>
          ) : null}
          <PromptBar
            value={input}
            onChange={setInput}
            onSend={send}
            busy={busy}
            sources={sources}
            commands={commands}
            temperature={temperature}
            onTemperature={(temp) => {
              setTemperature(temp);
              void apiPost('settings', { voice_temperature: temp }).catch(() => undefined);
            }}
            language={language}
            onLanguage={setLanguage}
            placeholder={t('write.composerPlaceholder')}
            pickerLabels={{
              temp: t('write.tempLabel'),
              temps: {
                safe: t('write.tempSafe'),
                bold: t('write.tempBold'),
                experimental: t('write.tempExperimental'),
              },
              lang: t('write.langLabel'),
              langs: {
                auto: t('write.langAuto'),
                ar: t('write.langAr'),
                en: t('write.langEn'),
                mixed: t('write.langMixed'),
              },
            }}
            noMatchesLabel={t('write.noMatches')}
            footerHint={t('write.sendHint')}
            onAttach={stagedImage === null && !uploading ? () => fileRef.current?.click() : undefined}
            attachLabel={uploading ? t('inbox.uploading') : t('inbox.composeImage')}
          />
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            className="hidden"
            onChange={(e) => { void stageFile(e.target.files?.[0]); e.target.value = ''; }}
          />
        </div>
      </div>

      {/* right rail — loops, best hour, voice inspector */}
      <aside className="hidden w-[324px] shrink-0 flex-col gap-4 overflow-y-auto border-s border-line p-3 xl:flex">
        <LoopsPanel />
        <BestHourPanel
          onAccept={(hour) => send(`Schedule my next post at ${String(hour).padStart(2, '0')}:00.`)}
        />
        <VoicePanel />
      </aside>
    </div>
  );
}
