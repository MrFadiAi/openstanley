import { useState } from 'react';
import { motion } from 'framer-motion';
import {
  CalendarClock,
  Check,
  ExternalLink,
  MoreHorizontal,
  Music2,
  Quote as QuoteIcon,
  RefreshCw,
  Send,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { ScoreBadge } from '@/components/ScoreBadge';
import { TargetScoreBadge } from '@/components/TargetScoreBadge';
import { VoiceChip } from '@/components/VoiceChip';
import { useApp } from '@/lib/app-context';
import { apiPost, type Draft, type DraftKind } from '@/lib/api';
import { cn, errMsg, fmtDateTime, hasArabic } from '@/lib/utils';
import { toast } from 'sonner';

const KIND_LABEL: Record<DraftKind, string> = { post: 'post', reply: 'reply', quote: 'quote' };

function kindVariant(kind: DraftKind): 'accent' | 'amber' | 'cyan' {
  if (kind === 'reply') return 'amber';
  if (kind === 'quote') return 'cyan';
  return 'accent';
}

interface DraftCardProps {
  draft: Draft;
  /** default approve slot in datetime-local format (YYYY-MM-DDTHH:MM) */
  defaultSlot: string;
  onChanged: () => void;
}

export function DraftCard({ draft, defaultSlot, onChanged }: DraftCardProps) {
  const { t, lang } = useApp();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(draft.text);
  const [slot, setSlot] = useState(defaultSlot);
  const [busy, setBusy] = useState(false);

  const act = async (fn: () => Promise<void>): Promise<void> => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = (): void => {
    void act(async () => {
      try {
        await apiPost(`drafts/${draft.id}/edit`, { text });
        toast.success(t('inbox.edited'));
        setEditing(false);
        onChanged();
      } catch (e) {
        toast.error(t('inbox.editFailed', { msg: errMsg(e) }));
      }
    });
  };

  const approve = (): void => {
    void act(async () => {
      try {
        const r = await apiPost<{ ok: boolean; scheduled_at: string }>(
          `drafts/${draft.id}/approve`,
          { scheduled_at: `${slot}:00` },
        );
        toast.success(t('inbox.approvedAt', { when: fmtDateTime(r.scheduled_at, lang) }));
        onChanged();
      } catch (e) {
        toast.error(t('inbox.approveFailed', { msg: errMsg(e) }));
      }
    });
  };

  const discard = (): void => {
    void act(async () => {
      try {
        await apiPost(`drafts/${draft.id}/reject`, {});
        toast.success(t('inbox.discarded'));
        onChanged();
      } catch (e) {
        toast.error(t('inbox.discardFailed', { msg: errMsg(e) }));
      }
    });
  };

  const regenerate = (): void => {
    void act(async () => {
      try {
        const r = await apiPost<{ ok: boolean; new_draft_id: number }>(
          `drafts/${draft.id}/regenerate`,
          {},
        );
        toast.success(t('inbox.regenerated', { id: r.new_draft_id }));
        onChanged();
      } catch (e) {
        toast.error(t('inbox.regenFailed', { msg: errMsg(e) }));
      }
    });
  };

  const sendReply = (): void => {
    void act(async () => {
      try {
        const r = await apiPost<{ ok: boolean; x_id: string | null }>(
          `replies/${draft.id}/send`,
          {},
        );
        toast.success(t('inbox.sent', { xid: r.x_id ? ` (x ${r.x_id})` : '' }));
        onChanged();
      } catch (e) {
        toast.error(t('inbox.sendFailed', { msg: errMsg(e) }));
      }
    });
  };

  const draftLang = draft.language ?? draft.meta?.language;
  const rtl = hasArabic(draft.text);

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className={cn(
        'rounded-xl border border-edge bg-panel p-4',
        draft.kind === 'reply' && 'border-s-[3px] border-s-warn',
      )}
    >
      {/* meta row */}
      <div className="mb-2.5 flex flex-wrap items-center gap-1.5">
        <Badge variant={kindVariant(draft.kind)}>{KIND_LABEL[draft.kind]}</Badge>
        <ScoreBadge alg={draft.meta?.alg} />
        <VoiceChip voice={draft.meta?.voice} />
        {draft.meta?.voice_match != null ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex cursor-help items-center gap-1 rounded-full border border-edge bg-panel2 px-2 py-0.5 text-[11px] text-accent2">
                <Music2 size={10} />
                {t('inbox.voiceMatch', { n: draft.meta.voice_match })}
              </span>
            </TooltipTrigger>
            <TooltipContent>{t('inbox.voiceMatchHint')}</TooltipContent>
          </Tooltip>
        ) : null}
        {draftLang ? <Badge>{draftLang}</Badge> : null}
        {draft.meta?.target_author ? (
          <Badge variant="amber">
            {t('inbox.replyTo', { author: draft.meta.target_author })}
          </Badge>
        ) : null}
        {draft.meta?.source === 'mention' ? (
          <Badge variant="cyan">{t('inbox.mentionChip')}</Badge>
        ) : null}
        {draft.meta?.target_score ? (
          <TargetScoreBadge ts={draft.meta.target_score} />
        ) : null}
        {draft.meta?.idea_title ? (
          <Badge>
            <Sparkles size={10} className="text-accent2" /> {draft.meta.idea_title}
          </Badge>
        ) : null}
        {draft.scheduled_at ? (
          <Badge variant="green">
            <CalendarClock size={10} />
            {fmtDateTime(draft.scheduled_at, lang)}
          </Badge>
        ) : null}
      </div>

      {/* smart-slot reason — why the scheduler put this draft where it is */}
      {draft.meta?.scheduled_reason ? (
        <div
          className="mb-2.5 flex items-center gap-1.5 rounded-lg border border-edge/60 bg-accent/5 px-2.5 py-1.5 text-[11.5px] text-accent2"
          title={t('inbox.slotReason')}
        >
          <Sparkles size={11} className="shrink-0" />
          <span className="truncate">{draft.meta.scheduled_reason}</span>
        </div>
      ) : null}

      {/* quoted tweet */}
      {draft.quote_of ? (
        <a
          href={draft.quote_of.url}
          target="_blank"
          rel="noreferrer"
          dir={hasArabic(draft.quote_of.text ?? '') ? 'rtl' : 'ltr'}
          className="mb-2.5 block rounded-lg border border-edge border-s-2 border-s-cyan bg-panel2/60 px-3 py-2 text-[12.5px] text-muted transition-colors hover:border-cyan/50"
        >
          <span className="mb-0.5 flex items-center gap-1.5 font-medium text-cyan">
            <QuoteIcon size={11} />
            {t('inbox.quotedTweet', { author: draft.quote_of.author ?? 'x' })}
            <ExternalLink size={10} />
          </span>
          {draft.quote_of.text}
        </a>
      ) : null}

      {/* body */}
      {editing ? (
        <div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            dir={rtl ? 'rtl' : 'ltr'}
            rows={Math.min(10, Math.max(3, text.split('\n').length + 1))}
            className="w-full rounded-lg border border-edge bg-panel2 px-3 py-2 text-[14px] leading-relaxed text-base focus:border-accent/70 focus:outline-none"
          />
          <div className="mt-2 flex gap-2">
            <Button variant="primary" size="sm" onClick={saveEdit} disabled={busy}>
              <Check size={13} /> {t('inbox.saveEdit')}
            </Button>
            <Button size="sm" onClick={() => setEditing(false)} disabled={busy}>
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div dir={rtl ? 'rtl' : 'ltr'} className="whitespace-pre-wrap text-[14.5px] leading-relaxed">
            {draft.text}
          </div>
          {(draft.thread ?? []).slice(1).map((part, i) => (
            <div
              key={i}
              dir={hasArabic(part) ? 'rtl' : 'ltr'}
              className="mt-2 whitespace-pre-wrap border-s-2 border-edge ps-3 text-[13.5px] text-base/90"
            >
              {part}
            </div>
          ))}
          {draft.image ? (
            <img
              src={`/api/media/${draft.image}`}
              alt=""
              loading="lazy"
              className="mt-3 max-h-44 rounded-lg border border-edge"
            />
          ) : null}
        </>
      )}

      {/* actions */}
      {!editing ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {draft.kind === 'reply' ? (
            <Button variant="primary" size="sm" onClick={sendReply} disabled={busy}>
              <Send size={13} /> {t('inbox.sendNow')}
            </Button>
          ) : (
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="green" size="sm" disabled={busy}>
                  <Check size={13} /> {t('common.approve')}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-72">
                <div className="mb-2 text-[12px] font-semibold uppercase tracking-wider text-muted">
                  {t('inbox.approveAt')}
                </div>
                <input
                  type="datetime-local"
                  value={slot}
                  onChange={(e) => setSlot(e.target.value)}
                  className="mb-3 h-8 w-full rounded-lg border border-edge bg-panel2 px-2.5 text-[13px] text-base focus:border-accent/70 focus:outline-none"
                />
                <Button variant="green" size="sm" className="w-full" onClick={approve} disabled={busy}>
                  <CalendarClock size={13} /> {t('inbox.approveAt')}
                </Button>
              </PopoverContent>
            </Popover>
          )}
          <Button size="sm" onClick={() => setEditing(true)} disabled={busy}>
            {t('common.edit')}
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon-sm" disabled={busy} aria-label="more">
                <MoreHorizontal size={15} />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuItem onSelect={regenerate}>
                <RefreshCw size={13} className="text-muted" /> {t('inbox.regenerate')}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={discard} className="text-bad focus:bg-bad/10">
                <Trash2 size={13} /> {t('common.discard')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ) : null}
    </motion.div>
  );
}
