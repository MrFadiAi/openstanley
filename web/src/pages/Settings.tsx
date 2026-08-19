import { useEffect, useState } from 'react';
import Markdown from 'react-markdown';
import { toast } from 'sonner';
import { Clock, Globe, Mic, Newspaper, Rows3, Save, Send, Trash2, Zap } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { PageHeader } from '@/components/PageHeader';
import { TagInput } from '@/components/TagInput';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  checkVoiceLine,
  getDigest,
  sendDigest,
  testTelegram,
  type Settings,
  type VoiceCheckResponse,
} from '@/lib/api';
import { errMsg } from '@/lib/utils';
import { LOOP_NAMES, triggerLoop } from '@/lib/loops';
import { cn } from '@/lib/utils';

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <div className="min-w-[190px]">
        <div className="text-[13px] text-muted">{label}</div>
        {hint ? <div className="text-[11.5px] text-muted/70">{hint}</div> : null}
      </div>
      <div className="flex flex-1 flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

export function SettingsPage() {
  const { t, lang, setLang, dense, setDense } = useApp();
  const [s, setS] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [vlText, setVlText] = useState('');
  const [vlBusy, setVlBusy] = useState(false);
  const [vlResult, setVlResult] = useState<VoiceCheckResponse | null>(null);
  const [dgWebhook, setDgWebhook] = useState(''); // typed-over value ('' = keep stored)
  const [dgBusy, setDgBusy] = useState(false);
  const [dgPreview, setDgPreview] = useState<string | null>(null);
  const [dgPreviewOpen, setDgPreviewOpen] = useState(false);
  // telegram (v0.4.4) — token typed-over like the webhook, never echoed back
  const [tgToken, setTgToken] = useState('');
  const [tgChats, setTgChats] = useState('');
  const [tgBusy, setTgBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await api<Settings>('settings');
        if (alive) setS(r);
      } catch {
        if (alive) setS(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const save = (): void => {
    if (!s) return;
    setSaving(true);
    void (async () => {
      try {
        await apiPost('settings', {
          daily_draft_target: s.daily_draft_target,
          post_times: s.post_times,
          niche_accounts: s.niche_accounts,
          evergreen_themes: s.evergreen_themes,
          auto_approve_replies: s.auto_approve_replies ?? false,
          smart_slots: s.smart_slots ?? true,
          voice_lock_enabled: s.voice_lock_enabled ?? true,
          voice_lock_threshold: s.voice_lock_threshold ?? 75,
          digest_hour: s.digest_hour ?? 20,
          ...(dgWebhook.trim() ? { digest_webhook_url: dgWebhook.trim() } : {}),
          tg_enabled: s.tg_enabled ?? false,
          ...(tgToken.trim() ? { tg_bot_token: tgToken.trim() } : {}),
          ...(tgChats.trim()
            ? {
                tg_allowed_chats: tgChats
                  .split(',')
                  .map((c) => c.trim())
                  .filter(Boolean),
              }
            : {}),
          language: lang,
        });
        toast.success(t('settings.saved'));
      } catch (e) {
        toast.error(t('settings.saveFailed', { msg: errMsg(e) }));
      } finally {
        setSaving(false);
      }
    })();
  };

  if (!s) {
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-[760px] px-6 py-6">
          <Skeleton className="mb-5 h-8 w-40" />
          <Skeleton className="mb-4 h-64" />
          <Skeleton className="h-40" />
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[760px] px-6 py-6">
        <PageHeader title={t('settings.title')} />

        <div className="mb-4 rounded-xl border border-edge bg-panel p-5">
          {/* language */}
          <Row label={t('settings.language')} hint={t('settings.languageHint')}>
            <div className="inline-flex overflow-hidden rounded-lg border border-edge">
              {(['en', 'ar'] as const).map((l) => (
                <button
                  key={l}
                  onClick={() => setLang(l)}
                  className={cn(
                    'cursor-pointer px-4 py-1.5 text-[13px] transition-colors',
                    lang === l ? 'bg-accent text-white' : 'bg-panel2 text-muted hover:text-base',
                  )}
                >
                  {l === 'en' ? t('settings.en') : t('settings.ar')}
                </button>
              ))}
            </div>
            <Globe size={14} className="text-muted/60" />
          </Row>

          {/* compact mode */}
          <Row label={t('settings.compact')} hint={t('settings.compactHint')}>
            <Switch checked={dense} onCheckedChange={setDense} aria-label={t('settings.compact')} />
            <Rows3 size={14} className="text-muted/60" />
          </Row>

          {/* daily target */}
          <Row label={t('settings.dailyTarget')}>
            <input
              type="number"
              min={1}
              max={20}
              value={s.daily_draft_target}
              onChange={(e) =>
                setS({ ...s, daily_draft_target: parseInt(e.target.value, 10) || 1 })
              }
              className="h-8 w-20 rounded-lg border border-edge bg-panel2 px-3 text-center text-[13px] text-base focus:border-accent/70 focus:outline-none"
            />
          </Row>

          {/* post times */}
          <Row label={t('settings.postTimes')}>
            {s.post_times.map((time, i) => (
              <span key={i} className="inline-flex items-center gap-1.5">
                <input
                  type="time"
                  value={time}
                  onChange={(e) => {
                    const next = [...s.post_times];
                    next[i] = e.target.value;
                    setS({ ...s, post_times: next });
                  }}
                  className="h-7 rounded-lg border border-edge bg-panel2 px-2 text-[12.5px] text-base focus:border-accent/70 focus:outline-none"
                />
                <button
                  onClick={() =>
                    setS({ ...s, post_times: s.post_times.filter((_, j) => j !== i) })
                  }
                  className="cursor-pointer text-muted hover:text-bad"
                  aria-label={t('common.remove')}
                >
                  <Trash2 size={12} />
                </button>
              </span>
            ))}
            <Button
              size="sm"
              onClick={() => setS({ ...s, post_times: [...s.post_times, '12:00'] })}
            >
              <Clock size={12} /> {t('settings.addTime')}
            </Button>
          </Row>

          {/* niche accounts */}
          <Row label={t('settings.nicheAccounts')}>
            <TagInput
              tags={s.niche_accounts}
              onChange={(v) => setS({ ...s, niche_accounts: v })}
              placeholder={t('settings.nichePlaceholder')}
              prefix="@"
            />
          </Row>

          {/* evergreen themes */}
          <Row label={t('settings.evergreen')}>
            <TagInput
              tags={s.evergreen_themes}
              onChange={(v) => setS({ ...s, evergreen_themes: v })}
              placeholder={t('settings.evergreenPlaceholder')}
            />
          </Row>

          {/* auto approve */}
          <Row label={t('settings.autoApprove')}>
            <Switch
              checked={s.auto_approve_replies ?? false}
              onCheckedChange={(v) => setS({ ...s, auto_approve_replies: v })}
              aria-label={t('settings.autoApprove')}
            />
          </Row>

          {/* smart slots (v0.4.1) */}
          <Row label={t('settings.smartSlots')} hint={t('settings.smartSlotsHint')}>
            <Switch
              checked={s.smart_slots ?? true}
              onCheckedChange={(v) => setS({ ...s, smart_slots: v })}
              aria-label={t('settings.smartSlots')}
            />
          </Row>

          {/* voice lock (v0.4.0) */}
          <div className="mb-4 mt-6 border-t border-edge pt-4">
            <div className="mb-3 flex items-center gap-2">
              <Mic size={14} className="text-accent2" />
              <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                {t('settings.voiceLock')}
              </span>
            </div>
            <Row label={t('settings.voiceLockEnabled')} hint={t('settings.voiceLockHint')}>
              <Switch
                checked={s.voice_lock_enabled ?? true}
                onCheckedChange={(v) => setS({ ...s, voice_lock_enabled: v })}
                aria-label={t('settings.voiceLockEnabled')}
              />
            </Row>
            <Row
              label={t('settings.voiceLockThreshold')}
              hint={t('settings.voiceLockThresholdHint')}
            >
              <input
                type="range"
                min={50}
                max={95}
                step={1}
                value={s.voice_lock_threshold ?? 75}
                onChange={(e) =>
                  setS({ ...s, voice_lock_threshold: parseInt(e.target.value, 10) })
                }
                className="h-1.5 w-44 cursor-pointer accent-accent"
                aria-label={t('settings.voiceLockThreshold')}
              />
              <span className="w-10 text-center font-mono text-[13px] font-semibold">
                {s.voice_lock_threshold ?? 75}
              </span>
            </Row>
            <Row label={t('settings.voiceLockTest')} hint={t('settings.voiceLockTestHint')}>
              <input
                type="text"
                value={vlText}
                onChange={(e) => setVlText(e.target.value)}
                placeholder={t('settings.voiceLockTestPlaceholder')}
                className="h-8 min-w-[220px] flex-1 rounded-lg border border-edge bg-panel2 px-3 text-[13px] text-base focus:border-accent/70 focus:outline-none"
              />
              <Button
                size="sm"
                disabled={vlBusy || !vlText.trim()}
                onClick={() => {
                  setVlBusy(true);
                  void (async () => {
                    try {
                      setVlResult(await checkVoiceLine(vlText));
                    } catch (e) {
                      setVlResult(null);
                      toast.error(t('settings.voiceLockTestFailed', { msg: errMsg(e) }));
                    } finally {
                      setVlBusy(false);
                    }
                  })();
                }}
              >
                <Mic size={12} /> {vlBusy ? t('settings.voiceLockChecking') : t('settings.voiceLockTestRun')}
              </Button>
            </Row>
            {vlResult ? (
              <div className="mt-2 rounded-lg border border-edge bg-panel2/60 px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      'font-mono text-[13px] font-semibold',
                      vlResult.passed ? 'text-good' : 'text-bad',
                    )}
                  >
                    {vlResult.score}/100
                  </span>
                  <span className={cn('text-[12.5px] font-medium', vlResult.passed ? 'text-good' : 'text-bad')}>
                    {vlResult.passed
                      ? t('settings.voiceLockPassed', { t: vlResult.threshold })
                      : t('settings.voiceLockFailed', { t: vlResult.threshold })}
                  </span>
                </div>
                {vlResult.violations.length ? (
                  <div className="mt-1.5 flex flex-col gap-0.5 text-[11.5px] text-muted">
                    {vlResult.violations.slice(0, 5).map((v, i) => (
                      <div key={i}>· {v}</div>
                    ))}
                  </div>
                ) : (
                  <div className="mt-1.5 text-[11.5px] text-good">{t('voice.clean')}</div>
                )}
                {vlResult.rules_source === 'neutral' ? (
                  <div className="mt-1.5 text-[11.5px] text-warn">
                    {t('settings.voiceLockNeutralRules')}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>

          {/* daily digest (v0.4.2) */}
          <div className="mb-4 mt-6 border-t border-edge pt-4">
            <div className="mb-3 flex items-center gap-2">
              <Newspaper size={14} className="text-accent2" />
              <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                {t('settings.digest')}
              </span>
            </div>
            <Row label={t('settings.digestWebhook')} hint={t('settings.digestWebhookHint')}>
              <input
                type="text"
                value={dgWebhook}
                onChange={(e) => setDgWebhook(e.target.value)}
                placeholder={
                  s.digest_webhook_set
                    ? (s.digest_webhook_url ?? 'https://…')
                    : 'https://relay.example/hook'
                }
                dir="ltr"
                className="h-8 min-w-[240px] flex-1 rounded-lg border border-edge bg-panel2 px-3 font-mono text-[12px] text-base focus:border-accent/70 focus:outline-none"
              />
              {s.digest_webhook_set && !dgWebhook ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    void (async () => {
                      try {
                        await apiPost('settings', { digest_webhook_url: '' });
                        setS({ ...(await api<Settings>('settings')) });
                        toast.success(t('settings.saved'));
                      } catch (e) {
                        toast.error(t('settings.saveFailed', { msg: errMsg(e) }));
                      }
                    })();
                  }}
                >
                  <Trash2 size={12} />
                </Button>
              ) : null}
            </Row>
            <Row label={t('settings.digestHour')}>
              <input
                type="number"
                min={0}
                max={23}
                value={s.digest_hour ?? 20}
                onChange={(e) =>
                  setS({ ...s, digest_hour: Math.max(0, Math.min(23, parseInt(e.target.value, 10) || 0)) })
                }
                className="h-8 w-20 rounded-lg border border-edge bg-panel2 px-3 text-center text-[13px] text-base focus:border-accent/70 focus:outline-none"
              />
              <span className="text-[11.5px] text-muted">
                {s.digest_last_sent
                  ? t('settings.digestLastSent', { when: s.digest_last_sent.slice(0, 16).replace('T', ' ') })
                  : t('settings.digestNeverSent')}
              </span>
            </Row>
            <Row label={t('settings.digestTest')}>
              <Button
                size="sm"
                disabled={dgBusy}
                onClick={() => {
                  setDgBusy(true);
                  void (async () => {
                    try {
                      const r = await sendDigest();
                      if (r.sent) toast.success(t('digest.sent'));
                      else if (r.error) toast.error(t('digest.sendFailed', { msg: r.error }));
                      else toast.message(t('digest.notConfigured'));
                      setS(await api<Settings>('settings'));
                    } catch (e) {
                      toast.error(t('digest.sendFailed', { msg: errMsg(e) }));
                    } finally {
                      setDgBusy(false);
                    }
                  })();
                }}
              >
                <Newspaper size={12} /> {dgBusy ? t('digest.sending') : t('settings.digestTest')}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={dgBusy}
                onClick={() => {
                  setDgBusy(true);
                  void (async () => {
                    try {
                      const r = await getDigest();
                      setDgPreview(r.markdown);
                      setDgPreviewOpen(true);
                    } catch (e) {
                      toast.error(t('digest.sendFailed', { msg: errMsg(e) }));
                    } finally {
                      setDgBusy(false);
                    }
                  })();
                }}
              >
                {t('settings.digestPreview')}
              </Button>
            </Row>
          </div>

          {/* telegram (v0.4.4 — second frontend) */}
          <div className="mb-4 mt-6 border-t border-edge pt-4">
            <div className="mb-3 flex items-center gap-2">
              <Send size={14} className="text-accent2" />
              <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                {t('settings.tg')}
              </span>
            </div>
            <Row label={t('settings.tgToken')} hint={t('settings.tgTokenHint')}>
              <input
                type="password"
                autoComplete="off"
                value={tgToken}
                onChange={(e) => setTgToken(e.target.value)}
                placeholder={
                  s.tg_bot_set
                    ? (s.tg_bot_token || '••••')
                    : t('settings.tgTokenPlaceholder')
                }
                dir="ltr"
                className="h-8 min-w-[220px] flex-1 rounded-lg border border-edge bg-panel2 px-3 font-mono text-[12px] text-base focus:border-accent/70 focus:outline-none"
              />
              {s.tg_bot_set && !tgToken ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    void (async () => {
                      try {
                        await apiPost('settings', { tg_bot_token: '' });
                        setS({ ...(await api<Settings>('settings')) });
                        toast.success(t('settings.saved'));
                      } catch (e) {
                        toast.error(t('settings.saveFailed', { msg: errMsg(e) }));
                      }
                    })();
                  }}
                >
                  <Trash2 size={12} />
                </Button>
              ) : null}
            </Row>
            <Row label={t('settings.tgChats')} hint={t('settings.tgChatsHint')}>
              <input
                type="text"
                value={tgChats || (s.tg_allowed_chats ?? []).join(', ')}
                onChange={(e) => setTgChats(e.target.value)}
                placeholder="123456789, 987654321"
                dir="ltr"
                className="h-8 min-w-[220px] flex-1 rounded-lg border border-edge bg-panel2 px-3 font-mono text-[12px] text-base focus:border-accent/70 focus:outline-none"
              />
            </Row>
            <Row label={t('settings.tgEnabled')}>
              <Switch
                checked={s.tg_enabled ?? false}
                onCheckedChange={(v) => setS({ ...s, tg_enabled: v })}
                aria-label={t('settings.tgEnabled')}
              />
              <span
                className={cn(
                  'text-[11.5px]',
                  s.tg_status === 'polling'
                    ? 'text-good'
                    : s.tg_status === 'bad_token'
                      ? 'text-bad'
                      : 'text-muted',
                )}
              >
                {t('settings.tgStatus')}:{' '}
                {s.tg_status === 'polling'
                  ? t('settings.tgStatusPolling')
                  : s.tg_status === 'bad_token'
                    ? t('settings.tgStatusBadToken')
                    : t('settings.tgStatusDisabled')}
              </span>
            </Row>
            <Row label={t('settings.tgTest')}>
              <Button
                size="sm"
                disabled={tgBusy}
                onClick={() => {
                  setTgBusy(true);
                  void (async () => {
                    try {
                      await testTelegram();
                      toast.success(t('settings.tgStatusPolling'));
                    } catch (e) {
                      toast.error(t('digest.sendFailed', { msg: errMsg(e) }));
                    } finally {
                      setTgBusy(false);
                    }
                  })();
                }}
              >
                <Send size={12} /> {tgBusy ? t('settings.tgTesting') : t('settings.tgTest')}
              </Button>
            </Row>
          </div>

          {s.x_mode || s.llm_model ? (
            <div className="mb-3 text-[12px] text-muted">
              X mode: <b>{s.x_mode ?? '?'}</b> · LLM: <b>{s.llm_model ?? '?'}</b>
              {s.llm_base_url ? ` @ ${s.llm_base_url}` : ''}
            </div>
          ) : null}
          <div className="mb-4 text-[12px] text-muted">{t('settings.credentials')}</div>

          <Button variant="primary" onClick={save} disabled={saving}>
            <Save size={13} /> {t('settings.save')}
          </Button>
        </div>

        {/* loops */}
        <div className="rounded-xl border border-edge bg-panel p-5">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">
            {t('settings.loops')}
          </div>
          <div className="mb-3 text-[12.5px] text-muted">{t('settings.loopsHint')}</div>
          <div className="flex flex-wrap gap-2">
            {LOOP_NAMES.map((name) => (
              <Button key={name} size="sm" onClick={() => void triggerLoop(name, t)}>
                <Zap size={12} /> {t(`loops.${name}`)}
              </Button>
            ))}
          </div>
        </div>
      </div>

      {/* digest preview — today's report, rendered */}
      <Dialog open={dgPreviewOpen} onOpenChange={setDgPreviewOpen}>
        <DialogContent className="max-h-[80vh] overflow-y-auto">
          <DialogTitle>{t('settings.digestPreviewTitle')}</DialogTitle>
          <DialogDescription>{t('digest.subtitle')}</DialogDescription>
          {dgPreview ? (
            <div className="mt-2" dir={lang === 'ar' ? 'rtl' : 'ltr'}>
              <Markdown
                components={{
                  h1: (p) => <h1 className="mb-2 text-[15px] font-bold" {...p} />,
                  h2: (p) => (
                    <h2 className="mt-3 mb-1.5 border-t border-edge pt-2.5 text-[12px] font-bold text-muted" {...p} />
                  ),
                  li: (p) => <li className="my-0.5 text-[12.5px] leading-snug text-ink-2" {...p} />,
                }}
              >
                {dgPreview}
              </Markdown>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  );
}
