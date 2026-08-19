import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Archive, KeyRound, Users } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useApp } from '@/lib/app-context';
import {
  activateAccount,
  deleteAccount,
  getAccounts,
  setAccountCookies,
  type Account,
} from '@/lib/api';
import { errMsg } from '@/lib/utils';

/*
 * AccountsCard (v0.5.0) — Settings' account management: the registry with
 * per-account cookies (write-only) and archive/disconnect. Cookies are
 * never displayed — only the masked hint the API returns.
 */

export function AccountsCard() {
  const { t } = useApp();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [cookiesDraft, setCookiesDraft] = useState('');
  const [cookiesFor, setCookiesFor] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback((): void => {
    void (async () => {
      try {
        const r = await getAccounts();
        setAccounts(r.accounts);
        setActiveId(r.active_account_id);
      } catch {
        /* keep the last known list */
      }
    })();
  }, []);

  useEffect(load, [load]);

  const activate = (id: number): void => {
    void (async () => {
      try {
        await activateAccount(id);
        window.location.reload(); // every page renders active-account data
      } catch (e) {
        toast.error(errMsg(e));
        load();
      }
    })();
  };

  const saveCookies = (): void => {
    if (cookiesFor == null || !cookiesDraft.trim()) return;
    setSaving(true);
    void (async () => {
      try {
        const r = await setAccountCookies(cookiesFor, cookiesDraft.trim());
        const handle = accounts.find((a) => a.id === cookiesFor)?.handle ?? '';
        toast.success(
          t('account.cookiesSaved', { handle, masked: r.cookies_masked ?? '••••' }),
        );
        setCookiesDraft('');
        setCookiesFor(null);
        load();
      } catch (e) {
        toast.error(errMsg(e));
      } finally {
        setSaving(false);
      }
    })();
  };

  const archive = (a: Account): void => {
    if (!window.confirm(t('account.disconnectHint'))) return;
    void (async () => {
      try {
        const r = await deleteAccount(a.id);
        toast.success(t('account.archived', { path: r.archived_to }));
        load();
      } catch (e) {
        toast.error(
          String(e).includes('409')
            ? t('account.lastAccount')
            : errMsg(e),
        );
      }
    })();
  };

  return (
    <div className="mb-4 rounded-xl border border-edge bg-panel p-5">
      <div className="mb-1.5 flex items-center gap-2">
        <Users size={14} className="text-accent2" />
        <span className="font-semibold">{t('account.manage')}</span>
      </div>
      <p className="mb-3 text-[12.5px] text-muted">{t('account.manageHint')}</p>

      <div className="divide-y divide-edge/60">
        {accounts.map((a) => (
          <div key={a.id} className="flex flex-wrap items-center gap-2 py-2 text-[13px]">
            <Badge variant={a.active ? 'accent' : 'default'}>
              #{a.id}
            </Badge>
            <span className="min-w-0 truncate font-medium">
              @{a.handle || t('account.noHandle')}
            </span>
            {a.followers != null ? (
              <span className="text-muted">{t('account.followers', { n: a.followers })}</span>
            ) : null}
            <span className="text-muted">{t('account.posts', { n: a.own_posts })}</span>
            <Badge variant={a.cookies_set ? 'green' : 'default'}>
              {a.cookies_masked ?? (a.cookies_set ? 'set' : '—')}
            </Badge>
            <span className="ms-auto flex items-center gap-2">
              {a.active ? (
                <Badge variant="accent">{t('account.current')}</Badge>
              ) : (
                <Button size="sm" onClick={() => activate(a.id)}>
                  {t('account.activate')}
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setCookiesFor(a.id);
                  setCookiesDraft('');
                }}
                title={t('account.cookiesField', { handle: a.handle || t('account.noHandle') })}
              >
                <KeyRound size={12} />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => archive(a)}
                disabled={accounts.length <= 1}
                title={t('account.disconnectHint')}
              >
                <Archive size={12} />
              </Button>
            </span>
            {cookiesFor === a.id ? (
              <div className="w-full">
                <Textarea
                  value={cookiesDraft}
                  onChange={(e) => setCookiesDraft(e.target.value)}
                  placeholder={t('account.cookiesPlaceholder')}
                  className="mb-1 font-mono text-[12px]"
                  rows={3}
                  dir="ltr"
                />
                <div className="flex gap-2">
                  <Button size="sm" variant="primary" onClick={saveCookies} disabled={saving}>
                    {t('account.cookiesSave')}
                  </Button>
                  <Button size="sm" onClick={() => setCookiesFor(null)}>
                    {t('common.cancel')}
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
        ))}
      </div>
      {activeId == null ? (
        <p className="py-3 text-center text-[13px] text-muted">{t('common.loading')}</p>
      ) : null}
    </div>
  );
}
