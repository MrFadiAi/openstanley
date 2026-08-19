import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Check, ChevronsUpDown, Plus, UserRound } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { useApp } from '@/lib/app-context';
import { activateAccount, createAccount, getAccounts, type Account } from '@/lib/api';
import { cn } from '@/lib/utils';

/*
 * AccountSwitcher (v0.5.0) — the workspace row's account dropdown: every
 * X account in this install (handle + follower snapshot), "Add account…"
 * and per-account Disconnect. Switching reloads the app so every page
 * refetches active-account data.
 */

export function AccountSwitcher() {
  const { t } = useApp();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [newHandle, setNewHandle] = useState('');
  const [creating, setCreating] = useState(false);

  const load = useCallback((): void => {
    void (async () => {
      try {
        const r = await getAccounts();
        setAccounts(r.accounts);
        setActiveId(r.active_account_id);
      } catch {
        /* server briefly unreachable — keep the last known list */
      }
    })();
  }, []);

  useEffect(load, [load]);

  const active = accounts.find((a) => a.id === activeId) ?? null;

  const switchTo = (id: number): void => {
    if (id === activeId) return;
    void (async () => {
      try {
        await activateAccount(id);
        toast.success(t('account.switched', {
          handle: accounts.find((a) => a.id === id)?.handle || `#${id}`,
        }));
        // every page renders active-account data — a full refetch is the
        // honest way to guarantee none of the old account stays on screen
        window.location.reload();
      } catch {
        load();
      }
    })();
  };

  const create = (): void => {
    setCreating(true);
    void (async () => {
      try {
        const r = await createAccount(newHandle.trim() || 'new-account');
        toast.success(t('account.created', { id: r.account_id }));
        setAddOpen(false);
        setNewHandle('');
        load();
      } catch (e) {
        toast.error(String(e));
      } finally {
        setCreating(false);
      }
    })();
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="flex w-full items-center gap-2 rounded-control px-1.5 py-1 text-start
              transition-colors duration-100 hover:bg-hover active:scale-[0.98]"
            title={t('account.switcher')}
          >
            <span className="flex size-6 shrink-0 items-center justify-center rounded-[7px] bg-surface text-ink-2 shadow-hairline">
              <UserRound size={12} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12px] font-semibold leading-tight text-ink">
                @{active?.handle || t('account.noHandle')}
              </span>
              {active?.followers != null ? (
                <span className="block truncate font-mono text-[10px] leading-tight text-ink-3">
                  {t('account.followers', { n: active.followers })}
                </span>
              ) : null}
            </span>
            <ChevronsUpDown size={11} className="shrink-0 text-ink-3" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-60">
          {accounts.map((a) => (
            <DropdownMenuItem key={a.id} onClick={() => switchTo(a.id)}>
              <Check
                size={12}
                className={cn('shrink-0', a.active ? 'opacity-100' : 'opacity-0')}
              />
              <span className="min-w-0 flex-1 truncate">
                @{a.handle || t('account.noHandle')}
              </span>
              {a.followers != null ? (
                <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
                  {a.followers}
                </span>
              ) : null}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={() => {
              setAddOpen(true);
            }}
          >
            <Plus size={12} className="shrink-0" />
            <span className="flex-1">{t('account.add')}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogTitle>{t('account.addTitle')}</DialogTitle>
          <DialogDescription>
            <p className="mb-3 text-[12.5px] text-muted">{t('account.addHint')}</p>
            <label className="mb-1 block text-[12px] text-muted">
              {t('account.addHandle')}
            </label>
            <Input
              value={newHandle}
              onChange={(e) => setNewHandle(e.target.value)}
              placeholder="@handle"
              dir="ltr"
              className="mb-3"
            />
            <div className="flex items-center gap-2">
              <Button variant="primary" size="sm" onClick={create} disabled={creating}>
                {creating ? t('common.loading') : t('account.addBtn')}
              </Button>
              <DialogClose asChild>
                <Button size="sm">{t('common.cancel')}</Button>
              </DialogClose>
            </div>
          </DialogDescription>
        </DialogContent>
      </Dialog>
    </>
  );
}
