import { toast } from 'sonner';
import { apiPost, type LoopResult } from './api';
import type { TFn } from './i18n';
import { errMsg } from './utils';

export type LoopName = 'import' | 'study' | 'create' | 'engage' | 'mentions' | 'publish' | 'learn' | 'scan';

export const LOOP_NAMES: LoopName[] = [
  'import',
  'study',
  'create',
  'engage',
  'mentions',
  'publish',
  'learn',
  'scan',
];

/** Fire an agent loop with loading toast; resolves true on success. */
export async function triggerLoop(name: LoopName, t: TFn): Promise<boolean> {
  const id = toast.loading(t('loops.running', { name: t(`loops.${name}`) }));
  try {
    await apiPost<LoopResult>(`loops/${name}`, {});
    toast.success(t('loops.done', { name: t(`loops.${name}`) }), { id });
    return true;
  } catch (e) {
    toast.error(t('loops.failed', { name: t(`loops.${name}`), msg: errMsg(e) }), { id });
    return false;
  }
}
