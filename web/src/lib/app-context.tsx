import { createContext, useContext } from 'react';
import type { Lang, TFn } from './i18n';

export type Tab =
  | 'write'
  | 'calendar'
  | 'inbox'
  | 'ideas'
  | 'strategy'
  | 'brain'
  | 'insights'
  | 'harness'
  | 'connect'
  | 'settings'
  | 'log';

export interface AppCtx {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: TFn;
  dense: boolean;
  setDense: (d: boolean) => void;
  tab: Tab;
  navigate: (tab: Tab) => void;
}

export const AppContext = createContext<AppCtx | null>(null);

export function useApp(): AppCtx {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('AppContext provider missing');
  return ctx;
}
