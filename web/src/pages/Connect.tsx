import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Download,
  HeartPulse,
  KeyRound,
  Loader2,
  ScanSearch,
  ShieldCheck,
  UserRound,
  XCircle,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/PageHeader';
import { useApp } from '@/lib/app-context';
import {
  api,
  apiPost,
  bootstrapAccount,
  getSmoke,
  runSmoke,
  type SmokeProbe,
  type SmokeReport,
  type StyleProfile,
  type XStatus,
} from '@/lib/api';
import { cn, errMsg, fmtDateTime } from '@/lib/utils';
import { triggerLoop } from '@/lib/loops';

const SMOKE_COOLDOWN_S = 300; // server rate-limits a fresh run to 1 / 5 min

const PROBE_LABEL_KEYS = [
  'identity',
  'timeline_read',
  'search_read',
  'notifications_read',
  'llm',
  'brain',
  'db',
] as const;

function probeState(p: SmokeProbe): 'pass' | 'warn' | 'fail' {
  if (p.ok) return 'pass';
  return p.warn ? 'warn' : 'fail';
}

function HealthCard({
  report,
  running,
  cooldownS,
  onRun,
}: {
  report: SmokeReport | null;
  running: boolean;
  cooldownS: number;
  onRun: () => void;
}) {
  const { t, lang } = useApp();
  const status = report?.status ?? 'never';
  const lights: { key: 'green' | 'amber' | 'red'; on: string }[] = [
    { key: 'green', on: 'border-good bg-good' },
    { key: 'amber', on: 'border-warn bg-warn' },
    { key: 'red', on: 'border-bad bg-bad' },
  ];
  const stateIcon = {
    pass: <CheckCircle2 size={13} className="shrink-0 text-good" />,
    warn: <AlertTriangle size={13} className="shrink-0 text-warn" />,
    fail: <XCircle size={13} className="shrink-0 text-bad" />,
  } as const;

  return (
    <div className="mb-4 rounded-xl border border-edge bg-panel p-4">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <Activity size={14} className="text-accent2" />
        <span className="font-semibold">{t('connect.health')}</span>
        <div className="flex items-center gap-1.5" aria-label={status}>
          {lights.map((l) => (
            <span
              key={l.key}
              className={cn(
                'h-2.5 w-2.5 rounded-full border',
                status === l.key ? l.on : 'border-edge bg-panel2',
              )}
            />
          ))}
        </div>
        <Badge
          variant={
            status === 'green' ? 'green' : status === 'amber' ? 'amber' : status === 'red' ? 'red' : 'default'
          }
        >
          {t(`smoke.status.${status}`)}
        </Badge>
        <Button
          variant="primary"
          size="sm"
          className="ms-auto"
          onClick={onRun}
          disabled={running || cooldownS > 0}
        >
          {running ? (
            <>
              <Loader2 size={13} className="animate-spin" /> {t('connect.healthRunning')}
            </>
          ) : cooldownS > 0 ? (
            t('connect.healthCooldown', { s: cooldownS })
          ) : (
            t('connect.healthRun')
          )}
        </Button>
      </div>
      <p className="mb-3 text-[12.5px] text-muted">{t('connect.healthHint')}</p>

      {report && report.ran_at ? (
        <div className="mb-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-muted">
          <span>{t('connect.healthRan', { time: fmtDateTime(report.ran_at, lang) })}</span>
          {report.ms != null ? <span className="font-mono">{report.ms}ms</span> : null}
          {report.x_reads != null ? (
            <span>{t('connect.healthReads', { n: report.x_reads })}</span>
          ) : null}
        </div>
      ) : (
        <p className="mb-2.5 text-[12px] text-muted">{t('connect.healthNever')}</p>
      )}

      <div className="divide-y divide-edge/60">
        {(report?.probes ?? []).map((p) => {
          const st = probeState(p);
          const known = (PROBE_LABEL_KEYS as readonly string[]).includes(p.name);
          const label = known
            ? t(`smoke.${p.name as (typeof PROBE_LABEL_KEYS)[number]}`)
            : p.name;
          return (
            <div key={p.name} className="flex items-center gap-2 py-1.5 text-[12.5px]">
              {stateIcon[st]}
              <span className="w-40 shrink-0 truncate">{label}</span>
              <span
                className={cn(
                  'w-9 shrink-0 text-center text-[10.5px] font-semibold uppercase',
                  st === 'pass' && 'text-good',
                  st === 'warn' && 'text-warn',
                  st === 'fail' && 'text-bad',
                )}
              >
                {t(`smoke.${st}`)}
              </span>
              <span className="w-14 shrink-0 text-right font-mono text-[11px] text-muted">
                {p.ms}ms
              </span>
              <span className="min-w-0 flex-1 truncate text-muted" title={p.detail} dir="auto">
                {p.detail}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function UsageBar({ used, cap, label }: { used: number; cap: number; label: string }) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  return (
    <div className="flex-1">
      <div className="mb-1 flex justify-between text-[12px] text-muted">
        <span>{label}</span>
        <span className="font-mono">
          {used}/{cap || '?'}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-panel2">
        <div
          className="h-full rounded-full bg-accent transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function ConnectPage() {
  const { t, lang } = useApp();
  const [status, setStatus] = useState<XStatus | null>(null);
  const [profile, setProfile] = useState<StyleProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [authToken, setAuthToken] = useState('');
  const [ct0, setCt0] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [caps, setCaps] = useState({
    max_posts_per_day: 4,
    max_replies_per_day: 10,
    min_delay_s: 5,
    max_delay_s: 20,
  });
  const [savingSafety, setSavingSafety] = useState(false);
  const [smoke, setSmoke] = useState<SmokeReport | null>(null);
  const [smokeRunning, setSmokeRunning] = useState(false);
  const [nowTick, setNowTick] = useState(0);

  const smokeCooldownS = smokeRunning
    ? 0
    : Math.max(
        0,
        Math.ceil(
          SMOKE_COOLDOWN_S -
            (smoke?.ran_at ? (Date.now() - new Date(smoke.ran_at).getTime()) / 1000 : Infinity),
        ),
      );
  const cooldownActive = smokeCooldownS > 0;

  useEffect(() => {
    if (!cooldownActive) return;
    const id = window.setInterval(() => setNowTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [cooldownActive]);
  void nowTick; // recomputes smokeCooldownS every second while cooling down

  const load = (): void => {
    void (async () => {
      try {
        const [st, sp, sm] = await Promise.all([
          api<XStatus>('x/status'),
          api<StyleProfile>('style-profile').catch(() => null),
          getSmoke().catch(() => null),
        ]);
        setStatus(st);
        setProfile(sp);
        setSmoke(sm);
        const c = st.safety?.caps ?? {};
        setCaps({
          max_posts_per_day: c.max_posts_per_day ?? 4,
          max_replies_per_day: c.max_replies_per_day ?? 10,
          min_delay_s: c.min_delay_s ?? 5,
          max_delay_s: c.max_delay_s ?? 20,
        });
      } catch {
        setStatus(null);
      } finally {
        setLoading(false);
      }
    })();
  };

  useEffect(load, []);

  const runSelfCheck = (): void => {
    setSmokeRunning(true);
    void (async () => {
      try {
        const rep = await runSmoke();
        setSmoke(rep);
        toast.success(t('connect.healthDone', { status: t(`smoke.status.${rep.status}`) }));
      } catch (e) {
        toast.error(t('connect.healthFailed', { msg: errMsg(e) }));
      } finally {
        setSmokeRunning(false);
      }
    })();
  };

  const connect = (): void => {
    setConnecting(true);
    void (async () => {
      try {
        // v0.5.0 bootstrap: validate the cookies via me(), then create or
        // re-select THAT account — each X account gets its own world.
        // Two labeled fields → one payload: a bare auth_token alone hits
        // X's code-353 csrf wall, so ct0 has its own field. A full cookie
        // string / JSON pasted into the auth_token box still goes through
        // as-is (the backend normalizes whatever shape it gets).
        const a = authToken.trim();
        const c = ct0.trim();
        const payload = c && !a.includes('{') && !a.includes('=')
          ? JSON.stringify({ auth_token: a, ct0: c })
          : a;
        const r = await bootstrapAccount(payload);
        toast.success(
          r.action === 'created'
            ? t('account.created', { id: r.account_id })
            : t('account.switched', { handle: r.handle }),
        );
        setDialogOpen(false);
        setAuthToken('');
        setCt0('');
        setTimeout(load, 600);
      } catch (e) {
        toast.error(t('connect.connectFailed', { msg: errMsg(e) }));
      } finally {
        setConnecting(false);
      }
    })();
  };

  const saveSafety = (): void => {
    setSavingSafety(true);
    void (async () => {
      try {
        await apiPost('x/safety', caps);
        toast.success(t('connect.safetySaved'));
        load();
      } catch (e) {
        toast.error(t('connect.safetyFailed', { msg: errMsg(e) }));
      } finally {
        setSavingSafety(false);
      }
    })();
  };

  const deepScan = (): void => {
    void triggerLoop('scan', t).then((ok) => {
      if (ok) setTimeout(load, 3000);
    });
  };

  const capsInputs: { key: keyof typeof caps; label: string }[] = [
    { key: 'max_posts_per_day', label: t('connect.maxPosts') },
    { key: 'max_replies_per_day', label: t('connect.maxReplies') },
    { key: 'min_delay_s', label: t('connect.minDelay') },
    { key: 'max_delay_s', label: t('connect.maxDelay') },
  ];

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[820px] px-6 py-6">
        <PageHeader title={t('connect.title')} subtitle={t('connect.subtitle')} />

        {loading ? (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-44" />
            <Skeleton className="h-52" />
            <Skeleton className="h-40" />
          </div>
        ) : (
          <>
            {/* status card */}
            <div className="mb-4 rounded-xl border border-edge bg-panel p-4">
              <div className="mb-3 flex flex-wrap items-center gap-2 text-[13px]">
                <ShieldCheck size={14} className="text-good" />
                <span className="text-muted">{t('connect.status')}:</span>
                <Badge variant={status?.mode === 'dryrun' ? 'amber' : 'green'}>
                  {status?.mode ?? '?'}
                </Badge>
                {status?.account_id != null ? (
                  <Badge variant="accent">
                    #{status.account_id}
                  </Badge>
                ) : null}
                {status?.username ? (
                  <Badge variant="accent">
                    <UserRound size={10} /> @{status.username}
                  </Badge>
                ) : null}
                {status?.followers != null ? (
                  <Badge>{t('connect.followers', { n: status.followers })}</Badge>
                ) : null}
                <Badge variant={status?.cookies_set ? 'green' : 'default'}>
                  {status?.cookies_set ? t('connect.cookiesSet') : t('connect.cookiesUnset')}
                </Badge>
              </div>

              {status?.mode === 'cookie' && (status.cookies_stale || status.heal_ok === true) ? (
                status.heal_ok === true ? (
                  <div className="mb-3 flex items-center gap-2 rounded-lg border border-good/30 bg-good/10 px-3 py-2 text-[12px] text-good">
                    <HeartPulse size={13} className="shrink-0" />
                    <span>
                      {t('connect.healed')}
                      {status.last_heal ? ` · ${fmtDateTime(status.last_heal, lang)}` : ''}
                    </span>
                  </div>
                ) : (
                  <div className="mb-3 flex items-center gap-2 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[12px] text-warn">
                    <AlertTriangle size={13} className="shrink-0" />
                    {t('connect.cookiesExpired')}
                  </div>
                )
              ) : null}
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                {t('connect.usage')}
              </div>
              <div className="flex flex-wrap gap-6">
                <UsageBar
                  used={status?.safety?.usage?.posts ?? 0}
                  cap={status?.safety?.caps?.max_posts_per_day ?? 0}
                  label={t('connect.postsUsed')}
                />
                <UsageBar
                  used={status?.safety?.usage?.replies ?? 0}
                  cap={status?.safety?.caps?.max_replies_per_day ?? 0}
                  label={t('connect.repliesUsed')}
                />
              </div>

              <div className="mt-4 border-t border-edge pt-3">
                <div className="mb-2.5 text-[11px] font-semibold uppercase tracking-wider text-muted">
                  {t('connect.safety')}
                </div>
                <div className="grid gap-2.5 sm:grid-cols-2">
                  {capsInputs.map(({ key, label }) => (
                    <label key={key} className="flex items-center justify-between gap-3">
                      <span className="text-[13px] text-muted">{label}</span>
                      <Input
                        type="number"
                        value={caps[key]}
                        onChange={(e) =>
                          setCaps({ ...caps, [key]: parseInt(e.target.value, 10) || 0 })
                        }
                        className="h-7 w-20 text-center"
                      />
                    </label>
                  ))}
                </div>
                <Button
                  variant="primary"
                  size="sm"
                  className="mt-3"
                  onClick={saveSafety}
                  disabled={savingSafety}
                >
                  {t('connect.saveSafety')}
                </Button>
              </div>
            </div>

            {/* system health (live self-check) */}
            <HealthCard
              report={smoke}
              running={smokeRunning}
              cooldownS={smokeCooldownS}
              onRun={runSelfCheck}
            />

            {/* cookie wizard dialog */}
            <div className="mb-4 rounded-xl border border-edge bg-panel p-4">
              <div className="mb-1.5 flex items-center gap-2 font-semibold">
                <KeyRound size={14} className="text-accent2" />
                {t('connect.cookieWizard')}
              </div>
              <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
                <DialogTrigger asChild>
                  <Button variant="green" size="sm">
                    {t('connect.connectBtn')}
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogTitle>{t('connect.cookieWizard')}</DialogTitle>
                  <DialogDescription>
                    <ol className="mb-3 list-decimal space-y-1 ps-4 text-[13px] text-muted">
                      <li>{t('connect.step1')}</li>
                      <li>{t('connect.step2')}</li>
                      <li>{t('connect.step3')}</li>
                      <li>{t('connect.step4')}</li>
                      <li>{t('connect.step5')}</li>
                    </ol>
                    <div className="mb-2">
                      <label
                        htmlFor="connect-auth-token"
                        className="mb-1 block font-mono text-[12px] font-semibold text-muted"
                      >
                        {t('connect.authTokenLabel')}
                      </label>
                      <Input
                        id="connect-auth-token"
                        value={authToken}
                        onChange={(e) => setAuthToken(e.target.value)}
                        placeholder={t('connect.authTokenPlaceholder')}
                        className="font-mono text-[12.5px]"
                        dir="ltr"
                        autoComplete="off"
                        spellCheck={false}
                      />
                    </div>
                    <div className="mb-1">
                      <label
                        htmlFor="connect-ct0"
                        className="mb-1 block font-mono text-[12px] font-semibold text-muted"
                      >
                        {t('connect.ct0Label')}
                      </label>
                      <Input
                        id="connect-ct0"
                        value={ct0}
                        onChange={(e) => setCt0(e.target.value)}
                        placeholder={t('connect.ct0Placeholder')}
                        className="font-mono text-[12.5px]"
                        dir="ltr"
                        autoComplete="off"
                        spellCheck={false}
                      />
                    </div>
                  </DialogDescription>
                  <div className="mt-3 flex items-center gap-2">
                    <Button
                      variant="green"
                      size="sm"
                      onClick={connect}
                      disabled={connecting || !authToken.trim()}
                    >
                      {connecting ? t('connect.connecting') : t('connect.connectBtn')}
                    </Button>
                    <DialogClose asChild>
                      <Button size="sm">{t('common.cancel')}</Button>
                    </DialogClose>
                  </div>
                  <p className="mt-3 flex items-start gap-2 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[12px] text-warn">
                    <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                    {t('connect.tosWarn')}
                  </p>
                </DialogContent>
              </Dialog>
            </div>

            {/* deep scan + style profile */}
            <div className="mb-4 grid gap-4 md:grid-cols-[1fr_1.4fr]">
              <div className="rounded-xl border border-edge bg-panel p-4">
                <div className="mb-1.5 flex items-center gap-2 font-semibold">
                  <ScanSearch size={14} className="text-accent2" />
                  {t('connect.scan')}
                </div>
                <p className="mb-3 text-[12.5px] text-muted">{t('connect.scanHint')}</p>
                <Button variant="primary" size="sm" onClick={deepScan}>
                  <ScanSearch size={13} /> {t('connect.scan')}
                </Button>
              </div>

              <div className="rounded-xl border border-edge bg-panel p-4">
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                  {t('connect.styleProfile')}
                </div>
                {profile?.exists && profile.stats ? (
                  <div>
                    <p className="mb-2.5 text-[13px] leading-relaxed">{profile.human_summary}</p>
                    <div className="flex flex-wrap gap-1.5">
                      <Badge variant="accent">
                        {t('connect.styleScanned', { n: profile.stats.posts_scanned })}
                      </Badge>
                      {typeof profile.stats.avg_length_chars === 'number' ? (
                        <Badge>
                          {t('connect.avgLength')}:{' '}
                          {t('connect.chars', { n: profile.stats.avg_length_chars })}
                        </Badge>
                      ) : null}
                      {profile.stats.posting_times?.best_hours?.length ? (
                        <Badge variant="green">
                          {t('connect.bestHours')}: {profile.stats.posting_times.best_hours.join(', ')}
                        </Badge>
                      ) : null}
                      {(profile.stats.vocabulary?.top_terms ?? []).slice(0, 5).map((term) => (
                        <Badge key={term} variant="cyan">
                          {term}
                        </Badge>
                      ))}
                      {(profile.stats.topics ?? []).slice(0, 4).map((topic) => (
                        <Badge key={topic}>{topic}</Badge>
                      ))}
                    </div>
                    {profile.updated_at ? (
                      <div className="mt-2 text-[11px] text-muted">
                        {fmtDateTime(profile.updated_at, lang)}
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <p className="py-4 text-center text-[13px] text-muted">
                    {t('connect.styleEmpty')}
                  </p>
                )}
              </div>
            </div>

            {/* import */}
            <div className="rounded-xl border border-edge bg-panel p-4">
              <div className="mb-1.5 flex items-center gap-2 font-semibold">
                <Download size={14} className="text-accent2" />
                {t('connect.importBtn')}
              </div>
              <p className="mb-3 text-[12.5px] text-muted">{t('connect.importHint')}</p>
              <Button
                variant="primary"
                size="sm"
                onClick={() => void triggerLoop('import', t)}
              >
                <Download size={13} /> {t('connect.importBtn')}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
