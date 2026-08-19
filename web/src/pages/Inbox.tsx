import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { toast } from 'sonner';
import { ExternalLink, ImagePlus, Link2, MessageSquareQuote, Plus, Sparkles, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import { DraftCard } from '@/components/DraftCard';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  uploadMedia,
  type CalendarResponse,
  type Draft,
  type MentionRow,
  type Settings,
  type Stats,
  type TweetPreview,
} from '@/lib/api';
import { errMsg, hasArabic } from '@/lib/utils';
import type { TFn } from '@/lib/i18n';

function StatTile({ value, label }: { value: string | number; label: string }) {
  return (
    <div className="rounded-xl border border-edge bg-panel px-4 py-3">
      <b className="block text-[22px] font-bold leading-tight">{value}</b>
      <small className="text-[12px] text-muted">{label}</small>
    </div>
  );
}

/** Next upcoming slot: first future empty_slot, else next post_time occurrence. */
function nextSlot(cal: CalendarResponse | null, settings: Settings | null): string {
  const now = new Date();
  if (cal?.empty_slots) {
    const keys = Object.keys(cal.empty_slots).sort();
    for (const k of keys) {
      for (const time of cal.empty_slots[k]) {
        const dt = new Date(`${k}T${time}:00`);
        if (!Number.isNaN(dt.getTime()) && dt.getTime() > Date.now()) {
          return `${k}T${time}`;
        }
      }
    }
  }
  const times = settings?.post_times?.length ? settings.post_times : ['09:00'];
  for (let add = 0; add < 8; add++) {
    const d = new Date(now);
    d.setDate(now.getDate() + add);
    for (const time of times) {
      const dt = new Date(
        `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T${time}:00`,
      );
      if (dt.getTime() > Date.now()) {
        return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}T${time.slice(0, 5)}`;
      }
    }
  }
  const d = new Date(now.getTime() + 86400000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T09:00`;
}

const TWEET_URL = /https?:\/\/(www\.)?(x|twitter)\.com\/\w+\/status\/(\d+)/i;

/** Age chip: "now" / "12m" / "3h" / "2d" — falls back to "?" for unknown stamps. */
function fmtAge(iso: string | null, t: TFn): string {
  if (!iso) return '?';
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return '?';
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return t('inbox.ageNow');
  if (mins < 60) return t('inbox.ageM', { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t('inbox.ageH', { n: hours });
  return t('inbox.ageD', { n: Math.floor(hours / 24) });
}

interface MentionRowCardProps {
  mention: MentionRow;
  onDraft: (xId: string) => void;
  drafting: boolean;
}

/** Avatar-less mention row: author, text, age chip, draft-reply button, tweet link. */
function MentionRowCard({ mention, onDraft, drafting }: MentionRowCardProps) {
  const { t } = useApp();
  const age = useMemo(
    () => fmtAge(mention.created_at ?? mention.first_seen, t),
    [mention.created_at, mention.first_seen, t],
  );
  return (
    <div className="rounded-xl border border-edge bg-panel px-4 py-3">
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <span className="inline-flex items-center gap-1 text-[13px] font-semibold">
          <MessageSquareQuote size={13} className="text-accent2" />
          @{mention.author}
        </span>
        <Badge>{age}</Badge>
        {mention.reply_to_me ? <Badge variant="cyan">{t('inbox.mentionReplyToMe')}</Badge> : null}
        {mention.handled ? (
          <Badge variant="green">{t('inbox.mentionHandled')}</Badge>
        ) : null}
        {mention.tweet_link ? (
          <a
            href={mention.tweet_link}
            target="_blank"
            rel="noreferrer"
            className="ms-auto inline-flex items-center gap-1 text-[11.5px] text-muted transition-colors hover:text-cyan"
          >
            <ExternalLink size={11} /> x.com
          </a>
        ) : null}
      </div>
      <p
        dir={hasArabic(mention.text) ? 'rtl' : 'ltr'}
        className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-base/90"
      >
        {mention.text}
      </p>
      {!mention.handled ? (
        <div className="mt-2">
          <Button size="sm" variant="primary" onClick={() => onDraft(mention.x_id)} disabled={drafting}>
            <Sparkles size={13} /> {drafting ? t('inbox.mentionDrafting') : t('inbox.mentionDraft')}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function InboxPage() {
  const { t } = useApp();
  const [stats, setStats] = useState<Stats | null>(null);
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [mentions, setMentions] = useState<MentionRow[] | null>(null);
  const [draftingMention, setDraftingMention] = useState<string | null>(null);
  const [slot, setSlot] = useState<string>('');
  const [composeText, setComposeText] = useState('');
  const [composeImage, setComposeImage] = useState<string | null>(null);
  const [tweetUrl, setTweetUrl] = useState('');
  const [tweetPreview, setTweetPreview] = useState<TweetPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    void (async () => {
      try {
        const [s, d, m] = await Promise.all([
          api<Stats>('stats'),
          api<Draft[]>('drafts?status=draft&limit=100'),
          api<MentionRow[]>('mentions?pending=1').catch(() => [] as MentionRow[]),
        ]);
        setStats(s);
        setDrafts(d);
        setMentions(m);
      } catch {
        setDrafts([]);
      }
    })();
  }, []);

  const draftMentionReply = (xId: string): void => {
    setDraftingMention(xId);
    void (async () => {
      try {
        await apiPost<{ ok: boolean; draft_id: number }>(`mentions/${encodeURIComponent(xId)}/draft`, {});
        toast.success(t('inbox.mentionDrafted'));
        load();
      } catch (e) {
        toast.error(t('inbox.mentionDraftFailed', { msg: errMsg(e) }));
      } finally {
        setDraftingMention(null);
      }
    })();
  };

  useEffect(() => {
    load();
    void (async () => {
      try {
        const [cal, s] = await Promise.all([
          api<CalendarResponse>('calendar').catch(() => null),
          api<Settings>('settings').catch(() => null),
        ]);
        setSlot(nextSlot(cal, s));
      } catch {
        setSlot('');
      }
    })();
  }, [load]);

  // fetch quote preview when a tweet URL is entered
  useEffect(() => {
    if (!tweetUrl.trim() || !TWEET_URL.test(tweetUrl.trim())) {
      setTweetPreview(null);
      return;
    }
    let alive = true;
    const id = setTimeout(() => {
      void api<TweetPreview>(`tweet?url=${encodeURIComponent(tweetUrl.trim())}`)
        .then((p) => {
          if (alive) setTweetPreview(p);
        })
        .catch(() => undefined);
    }, 400);
    return () => {
      alive = false;
      clearTimeout(id);
    };
  }, [tweetUrl]);

  const posts = useMemo(
    () => (drafts ?? []).filter((d) => d.kind === 'post' || d.kind === 'quote'),
    [drafts],
  );
  const replies = useMemo(() => (drafts ?? []).filter((d) => d.kind === 'reply'), [drafts]);

  const onFile = (f: File | undefined): void => {
    if (!f) return;
    const tid = toast.loading(t('inbox.uploading'));
    void uploadMedia(f)
      .then((r) => {
        toast.success(t('inbox.uploaded', { name: r.name }), { id: tid });
        setComposeImage(r.name);
      })
      .catch((e: unknown) => toast.error(t('inbox.uploadFailed', { msg: errMsg(e) }), { id: tid }));
  };

  const compose = (): void => {
    const text = composeText.trim();
    if (!text) {
      toast.error(t('inbox.composeNeedText'));
      return;
    }
    setBusy(true);
    void (async () => {
      try {
        const url = tweetUrl.trim();
        const isQuote = TWEET_URL.test(url);
        const body: Record<string, unknown> = { text, kind: isQuote ? 'quote' : undefined };
        if (composeImage) body.image = composeImage;
        if (isQuote) {
          body.quote_of = {
            url,
            ...(tweetPreview ? { text: tweetPreview.text, author: tweetPreview.author } : {}),
          };
        }
        const r = await apiPost<{ ok: boolean; draft_id: number }>('drafts', body);
        toast.success(t('inbox.composed', { id: r.draft_id }));
        setComposeText('');
        setComposeImage(null);
        setTweetUrl('');
        load();
      } catch (e) {
        toast.error(t('inbox.composeFailed', { msg: errMsg(e) }));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[880px] px-6 py-6">
        <PageHeader title={t('inbox.title')} subtitle={t('inbox.subtitle')} />

        {/* stat tiles */}
        <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats ? (
            <>
              <StatTile value={stats.drafts?.draft ?? 0} label={t('inbox.pending')} />
              <StatTile value={stats.new_engagements ?? 0} label={t('inbox.mentions')} />
              <StatTile value={stats.ideas_bank ?? 0} label={t('inbox.ideaBank')} />
              <StatTile value={stats.drafts?.published ?? 0} label={t('inbox.publishedCount')} />
            </>
          ) : (
            Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[62px]" />)
          )}
        </div>

        {/* compose */}
        <div className="mb-5 rounded-xl border border-edge bg-panel p-4">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
            {t('inbox.compose')}
          </div>
          <Textarea
            value={composeText}
            onChange={(e) => setComposeText(e.target.value)}
            placeholder={t('inbox.composePlaceholder')}
            rows={3}
          />
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              className="hidden"
              onChange={(e) => {
                onFile(e.target.files?.[0]);
                e.target.value = '';
              }}
            />
            <Button size="sm" onClick={() => fileRef.current?.click()} disabled={busy}>
              <ImagePlus size={13} /> {t('inbox.composeImage')}
            </Button>
            {composeImage ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-panel2 px-2.5 py-1 text-[12px]">
                <img src={`/api/media/${composeImage}`} alt="" className="h-5 w-5 rounded object-cover" />
                <span className="max-w-[140px] truncate font-mono text-[11px]">{composeImage}</span>
                <button
                  onClick={() => setComposeImage(null)}
                  className="cursor-pointer text-muted hover:text-bad"
                  aria-label="remove image"
                >
                  <X size={11} />
                </button>
              </span>
            ) : null}
            <div className="flex min-w-[220px] flex-1 items-center gap-1.5">
              <Link2 size={13} className="shrink-0 text-muted" />
              <Input
                value={tweetUrl}
                onChange={(e) => setTweetUrl(e.target.value)}
                placeholder={t('inbox.composeTweetUrl')}
                className="h-7 text-[12.5px]"
              />
            </div>
            <Button size="sm" variant="primary" onClick={compose} disabled={busy}>
              <Plus size={13} /> {t('inbox.composeSubmit')}
            </Button>
          </div>
          {tweetPreview ? (
            <div className="mt-2.5 rounded-lg border border-edge border-s-2 border-s-cyan bg-panel2/60 px-3 py-2 text-[12.5px] text-muted">
              <span className="font-medium text-cyan">@{tweetPreview.author}</span> —{' '}
              <span className="line-clamp-2">{tweetPreview.text}</span>
            </div>
          ) : null}
        </div>

        {/* sections */}
        {drafts === null ? (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-36" />
            ))}
          </div>
        ) : (
          <>
            {/* mention inbox — people talking to us directly */}
            <h3 className="mb-2.5 mt-1 text-[11px] font-semibold uppercase tracking-[0.9px] text-muted">
              {t('inbox.mentionsSection')}
            </h3>
            {mentions === null ? (
              <Skeleton className="mb-4 h-20" />
            ) : mentions.length > 0 ? (
              <div className="mb-4 flex flex-col gap-3">
                {mentions.map((m) => (
                  <MentionRowCard
                    key={m.x_id}
                    mention={m}
                    onDraft={draftMentionReply}
                    drafting={draftingMention === m.x_id}
                  />
                ))}
              </div>
            ) : (
              <div className="mb-4 py-6 text-center text-[13px] text-muted">
                {t('inbox.mentionsEmpty')}
              </div>
            )}

            {replies.length > 0 ? (
              <>
                <h3 className="mb-2.5 mt-1 text-[11px] font-semibold uppercase tracking-[0.9px] text-muted">
                  {t('inbox.replies')}
                </h3>
                <div className="mb-4 flex flex-col gap-3">
                  {replies.map((d) => (
                    <DraftCard key={d.id} draft={d} defaultSlot={slot} onChanged={load} />
                  ))}
                </div>
              </>
            ) : null}

            <h3 className="mb-2.5 mt-1 text-[11px] font-semibold uppercase tracking-[0.9px] text-muted">
              {t('inbox.postsAndQuotes')}
            </h3>
            {posts.length > 0 ? (
              <motion.div layout className="flex flex-col gap-3">
                {posts.map((d) => (
                  <DraftCard key={d.id} draft={d} defaultSlot={slot} onChanged={load} />
                ))}
              </motion.div>
            ) : (
              <div className="py-10 text-center text-[13px] text-muted">{t('inbox.noDrafts')}</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
