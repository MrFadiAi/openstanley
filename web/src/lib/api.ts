/**
 * Typed fetch wrapper against the OpenStanley HTTP contract (docs/API_CONTRACT.md).
 * All calls go to same-origin `/api` (Vite dev proxy -> 127.0.0.1:7878).
 */

// ---------- core shapes ----------

export type DraftKind = 'post' | 'reply' | 'quote';
export type DraftStatus = 'draft' | 'approved' | 'rejected' | 'published' | 'failed';
export type DraftLanguage = 'ar' | 'en' | 'mixed';

export interface AlgFactor {
  name: string;
  impact: number;
  note?: string;
}

export interface AlgScore {
  score: number; // 0-100
  grade: 'excellent' | 'good' | 'fair' | 'weak';
  factors: AlgFactor[];
}

export interface QuoteOf {
  x_id: string;
  url: string;
  text?: string;
  author?: string;
}

/** v0.3.8 engage quality gate — how good the reply TARGET was (draft.meta.target_score) */
export interface TargetScore {
  score: number; // 0-100 composite
  verdict?: 'fresh' | 'rising' | 'warm' | 'stale';
  age_h?: number | null;
  components?: {
    recency?: number;
    traction?: number;
    author?: number;
    crowding?: number;
    fit?: number;
  };
  reasons?: string[];
}

export interface DraftMeta {
  alg?: AlgScore;
  voice_match?: number | null;
  /** v0.4.0 voice-lock verdict attached when the draft passed the lock */
  voice?: VoiceCheckMeta;
  reply_to_x_id?: string;
  target_author?: string;
  /** mention-reply drafts carry the engagement author here */
  author?: string;
  target_score?: TargetScore;
  engagement_id?: number;
  idea_title?: string;
  idea_angle?: string;
  format?: string;
  source?: string;
  language?: DraftLanguage;
  /** v0.4.1 smart slots — why the scheduler put this draft where it is */
  scheduled_reason?: string;
}

/** voice lock verdict (meta.voice on drafts, `voice` on chat candidates) */
export interface VoiceCheckMeta {
  score: number;
  checked?: boolean;
  fixed?: boolean;
  violations?: string[];
}

export interface Draft {
  id: number;
  idea_id: number | null;
  kind: DraftKind;
  text: string;
  thread: string[] | null;
  status: DraftStatus;
  temperature: string;
  scheduled_at: string | null;
  x_id: string | null;
  published_at: string | null;
  created_at: string;
  image: string | null;
  quote_of: QuoteOf | null;
  /** top-level in v0.3; v0.2 servers carry it inside meta */
  language?: DraftLanguage;
  meta: DraftMeta;
}

// ---------- mention inbox (v0.3.9) ----------

export interface MentionDraftInfo {
  id: number;
  status: string;
}

export interface MentionRow {
  x_id: string;
  author: string;
  text: string;
  created_at: string | null;
  first_seen: string;
  handled: number; // 1 = a reply draft exists
  tweet_link: string | null;
  conversation_id: string | null;
  reply_to_me: number;
  draft: MentionDraftInfo | null;
}

// ---------- calendar ----------

export interface CalendarItem {
  id: number;
  kind: DraftKind;
  state: string; // "scheduled" | "published" | ...
  text: string;
  time: string; // "09:00"
  scheduled_at: string;
  image: string | null;
  score: number;
  language?: DraftLanguage;
  /** v0.4.1 smart slots — scheduler reason, when the slot was auto-picked */
  scheduled_reason?: string | null;
  reply_to?: { x_id?: string | null; author?: string } | null;
}

/** v0.4.1 smart slots — one scored candidate slot on the Calendar */
export interface SmartSlotChip {
  time: string; // "13:00"
  hour: number;
  score: number; // 0..1, metrics(0.6)+spread(0.25)+freshness(0.15)
  reason: string; // "metrics peak 13:00 · 5h after last post"
  at: string; // ISO occurrence
}

export interface SmartSlotsInfo {
  enabled: boolean;
  source: 'real' | 'heuristic';
  slots: Record<string, SmartSlotChip[]>;
}

export interface CalendarResponse {
  days: Record<string, CalendarItem[]>;
  empty_slots?: Record<string, string[]>;
  smart?: SmartSlotsInfo;
}

// ---------- insights ----------

export interface EngagementPoint {
  date: string;
  impressions: number;
  engagement: number;
  posts: number;
}

export interface BestHour {
  hour: number;
  avg_engagement: number;
}

export interface HeatCell {
  day: number; // 0=Mon..6=Sun
  hour: number;
  value: number;
}

export interface FormatPerf {
  format: string;
  count: number;
  avg_engagement: number;
}

export interface LangMixEntry {
  language: string;
  count: number;
}

export interface InsightsSummary {
  total_impressions: number;
  total_engagement: number;
  avg_engagement_rate: number;
  best_post: { text: string; likes: number; replies: number } | null;
}

export interface InsightsResponse {
  engagement_over_time: EngagementPoint[];
  best_hours: BestHour[];
  hours_heatmap: HeatCell[];
  format_performance: FormatPerf[];
  language_mix: LangMixEntry[];
  summary: InsightsSummary;
}

// ---------- growth analytics (v0.3.6 — real metrics ground truth) ----------

export interface TopPost {
  rank: number | null;
  x_id: string | null;
  text: string;
  created_at: string | null;
  likes: number;
  reposts: number;
  replies: number;
  impressions: number;
  /** follower-normalized: (likes+reposts+replies) / max(followers,1) */
  rate: number;
  url: string | null;
}

export interface GrowthPoint {
  date: string;
  followers: number | null;
  posts: number;
  avg_engagement_rate: number | null;
  best_post: TopPost | null;
}

export interface GrowthResponse {
  days: number;
  series: GrowthPoint[];
  followers_start: number | null;
  followers_end: number | null;
  followers_delta: number | null;
  total_posts: number;
}

export interface TopPostsResponse {
  posts: TopPost[];
}

export interface TimesHour {
  hour: number;
  posts: number;
  engagement: number;
  avg_engagement: number;
}

export interface TimesResponse {
  source: 'real' | 'heuristic';
  total_posts: number;
  min_posts_for_real: number;
  best_hours: number[];
  hours: TimesHour[];
  /** v0.4.1 — the scheduler's own reason per candidate hour (single source) */
  reasons?: Record<string, string>;
}

// ---------- style profile ----------

export interface StyleProfileStats {
  posts_scanned: number;
  avg_length_chars: number;
  sentence: { avg: number; p50: number; p90: number };
  punctuation: Record<string, number>;
  emoji: { per_post: number; top: string[] };
  hashtags: { per_post: number; pct_with: number };
  casing: { pct_lowercase_start: number; pct_allcaps_word: number };
  formatting: { pct_multiline: number; avg_line_breaks: number; thread_pct: number };
  vocabulary: { top_terms: string[]; uniqueness: number };
  topics: string[];
  posting_times: { histogram: number[]; best_hours: number[] };
  language_mix: { ar: number; en: number; mixed: number };
  humor_markers_per_post: number;
}

export interface StyleProfile {
  exists: boolean;
  stats: StyleProfileStats | null;
  human_summary: string | null;
  updated_at: string | null;
}

// ---------- settings / status ----------

export type Lang = 'en' | 'ar';

export interface Settings {
  daily_draft_target: number;
  post_times: string[];
  niche_accounts: string[];
  evergreen_themes: string[];
  auto_approve_replies?: boolean;
  /** v0.4.1 — metrics-aware slot picking in the approve path */
  smart_slots?: boolean;
  language?: Lang;
  voice_temperature?: 'safe' | 'bold' | 'experimental';
  voice_formality?: number;
  voice_lang_mix?: number;
  voice_emoji_density?: number;
  voice_lock_enabled?: boolean;
  voice_lock_threshold?: number;
  /** v0.4.2 daily digest — webhook (masked in GET) + delivery hour */
  digest_webhook_url?: string;
  digest_webhook_set?: boolean;
  digest_hour?: number;
  digest_last_sent?: string | null;
  /** v0.4.4 telegram — token masked in GET, status = poller state */
  tg_bot_token?: string;
  tg_bot_set?: boolean;
  tg_allowed_chats?: number[];
  tg_enabled?: boolean;
  tg_status?: 'disabled' | 'polling' | 'bad_token';
  x_mode?: string;
  llm_model?: string;
  llm_base_url?: string;
}

/** POST /api/voice-lock/check — Settings "test a line" verdict */
export interface VoiceCheckResponse {
  score: number;
  violations: string[];
  passed: boolean;
  threshold: number;
  rules_source: 'brain' | 'neutral';
}

export async function checkVoiceLine(
  text: string,
  kind: DraftKind = 'post',
): Promise<VoiceCheckResponse> {
  return apiPost<VoiceCheckResponse>('voice-lock/check', { text, kind });
}

export interface SafetyCaps {
  max_posts_per_day?: number;
  max_replies_per_day?: number;
  min_delay_s?: number;
  max_delay_s?: number;
}

export interface XStatus {
  mode: string;
  username: string | null;
  followers: number | null;
  account_id?: number;
  cookies_set?: boolean;
  cookies_masked?: string | null;
  cookies_stale?: boolean;
  last_heal?: string | null;
  heal_ok?: boolean | null;
  safety: { caps: SafetyCaps; usage: { posts?: number; replies?: number } } | null;
}

// ---------- accounts (v0.5.0 multi-account) ----------

export interface Account {
  id: number;
  handle: string;
  created_at: string | null;
  status: string;
  cookies_set: boolean;
  cookies_masked: string | null;
  followers: number | null;
  own_posts: number;
  active: boolean;
}

export interface AccountsResponse {
  active_account_id: number;
  accounts: Account[];
}

export interface AccountBootstrapResponse {
  ok: boolean;
  account_id: number;
  handle: string;
  action: 'created' | 'reconnected';
  followers: number | null;
  mode: string;
  active_account_id: number;
}

export async function getAccounts(): Promise<AccountsResponse> {
  return apiGet<AccountsResponse>('accounts');
}

export async function createAccount(
  handle: string,
  cookiesJson?: string,
): Promise<{ ok: boolean; account_id: number; handle: string }> {
  return apiPost('accounts', { handle, cookies_json: cookiesJson ?? null });
}

export async function activateAccount(id: number): Promise<{ ok: boolean }> {
  return apiPost(`accounts/${id}/activate`, {});
}

export async function deleteAccount(
  id: number,
): Promise<{ ok: boolean; archived_to: string }> {
  const r = await fetch(`/api/accounts/${id}`, { method: 'DELETE' });
  if (!r.ok) {
    throw new Error(`delete account failed (${r.status})`);
  }
  return r.json();
}

export async function setAccountCookies(
  id: number,
  cookiesJson: string,
): Promise<{ ok: boolean; cookies_masked: string | null }> {
  return apiPost(`accounts/${id}/cookies`, { cookies_json: cookiesJson });
}

export async function bootstrapAccount(
  cookiesJson: string,
): Promise<AccountBootstrapResponse> {
  return apiPost('accounts/bootstrap', { cookies_json: cookiesJson });
}

// ---------- system smoke (v0.3.7 live self-check) ----------

export interface SmokeProbe {
  name: string;
  ok: boolean;
  ms: number;
  detail: string;
  warn: boolean;
}

export type SmokeStatus = 'green' | 'amber' | 'red' | 'never';

export interface SmokeReport {
  ok: boolean | null; // null = never ran
  status: SmokeStatus;
  ms: number | null;
  x_reads: number | null;
  ran_at: string | null;
  probes: SmokeProbe[];
}

export async function getSmoke(): Promise<SmokeReport> {
  return api<SmokeReport>('system/smoke');
}

export async function runSmoke(): Promise<SmokeReport> {
  return apiPost<SmokeReport>('system/smoke');
}

export interface Stats {
  drafts?: Partial<Record<DraftStatus, number>>;
  new_engagements?: number;
  ideas_bank?: number;
}

export interface Health {
  ok: boolean;
  mode: string;
  time: string;
}

export interface LogEntry {
  ts: string;
  level: string; // info|ok|warn|error
  loop: string;
  message: string;
}

/** v0.4.3 replenish sources — the deterministic mining chain's badges */
export type IdeaSource = 'scan' | 'brain' | 'study' | 'evergreen';

export interface Idea {
  id: number;
  title: string;
  angle: string;
  format: string;
  score: number;
  source?: string;
  created_at?: string;
}

/** GET /api/ideas/bank — Ideas page health chip */
export interface IdeaBank {
  count: number;
  last: {
    at?: string;
    added?: number;
    sources?: IdeaSource[];
  };
}

/** POST /api/ideas/replenish — what the mining chain just added */
export interface ReplenishResult {
  ran: boolean;
  added: number;
  sources: IdeaSource[];
  bank: number;
  bank_before: number;
}

export interface Strategy {
  text?: string | null;
  updated_at?: string | null;
}

// ---------- chat ----------

export interface ChatHistoryEntry {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  ts: string;
  meta?: Record<string, unknown> | null;
}

export interface ChatAction {
  id: string;
  label: string;
}

export interface ChatCandidate {
  text: string;
  alg?: AlgScore;
  voice_match?: number | null;
  voice?: VoiceCheckMeta;
  language?: DraftLanguage;
}

/** one retrieved-context card (voice rubric, idea bank, strategy, …) */
export interface ContextChunk {
  title: string;
  body: string;
  source: string;
  badge: string;
  relevance?: number;
}

/** one context-gathering step ("Checking your voice rubric") */
export interface ThinkingStep {
  id: string;
  primary: string;
  secondary?: string;
}

export interface ChatToolResult {
  name: string;
  args?: Record<string, unknown>;
  ok: boolean;
  result?: unknown;
}

/** POST /api/chat (non-streaming fallback when /api/chat/stream is absent) */
export interface ChatFallbackResponse {
  reply: string;
  actions?: ChatAction[];
  tool_results?: ChatToolResult[];
  candidates?: ChatCandidate[];
  thinking_steps?: ThinkingStep[];
  context_chunks?: ContextChunk[];
}

export interface MediaUpload {
  ok: boolean;
  name: string;
  url: string;
}

export interface TweetPreview {
  x_id: string;
  text: string;
  author: string;
}

export interface LoopResult {
  ok: boolean;
  loop: string;
  result: unknown;
}

// ---------- SSE events ----------

export interface TokenEvent {
  type: 'token';
  text: string;
}

/** first frame: the context-gathering trace + retrieved chunks */
export interface ThinkingStepsEvent {
  type: 'thinking_steps';
  steps: ThinkingStep[];
  chunks: ContextChunk[];
}

/** a chat tool call that actually ran, with its real result */
export interface ToolEvent {
  type: 'tool';
  name: string;
  args?: Record<string, unknown>;
  ok: boolean;
  result?: unknown;
}

/** a post candidate awaiting the user's approval gate */
export interface ApprovalEvent {
  type: 'approval';
  candidate: ChatCandidate;
}

export interface DoneEvent {
  type: 'done';
  reply_id?: number;
  /** final cleaned reply (action fences stripped) — replaces streamed raw */
  reply?: string;
  actions: ChatAction[];
  tool_results?: ChatToolResult[];
  candidates: ChatCandidate[];
}

export interface ErrorEvent {
  type: 'error';
  message: string;
}

export type SSEEvent =
  | TokenEvent
  | ThinkingStepsEvent
  | ToolEvent
  | ApprovalEvent
  | DoneEvent
  | ErrorEvent;

export interface StreamHandlers {
  onToken: (text: string) => void;
  onThinkingSteps?: (ev: ThinkingStepsEvent) => void;
  onTool?: (ev: ToolEvent) => void;
  onApproval?: (ev: ApprovalEvent) => void;
  onDone: (ev: DoneEvent) => void;
  onError?: (message: string) => void;
}

// ---------- loops status ----------

export interface LoopStatusEntry {
  name: string;
  last_run: string | null;
  last_status: 'ok' | 'error' | null;
  last_message: string | null;
  next_run: string | null;
}

export interface LoopsStatusResponse {
  loops: LoopStatusEntry[];
  scheduler_running: boolean;
}

export async function getLoopsStatus(): Promise<LoopsStatusResponse> {
  return api<LoopsStatusResponse>('loops/status');
}

// ---------- autopilot ----------

export interface AutopilotState {
  enabled: boolean;
  last_tick: string | null;
  next_tick: string | null;
  ticks: number;
  errors: string[];
  phase: string | null;
  interval_min: number;
  job_active: boolean;
}

export interface AutopilotTickResult {
  ok: boolean;
  phase: string;
  result: unknown;
  error: string | null;
  ticks: number;
  state: AutopilotState;
}

export async function getAutopilot(): Promise<AutopilotState> {
  return api<AutopilotState>('autopilot');
}

export async function setAutopilot(
  enabled: boolean,
  intervalMin?: number,
): Promise<AutopilotState> {
  return apiPost<AutopilotState>('autopilot', {
    enabled,
    ...(intervalMin !== undefined ? { interval_min: intervalMin } : {}),
  });
}

export async function forceAutopilotTick(): Promise<AutopilotTickResult> {
  return apiPost<AutopilotTickResult>('autopilot/tick');
}

// ---------- daily digest (v0.4.2) ----------

export interface DigestResponse {
  day: string;
  markdown: string;
  text: string | null;
  stored: boolean;
}

export interface DigestSendResponse {
  ok: boolean;
  day: string;
  sent: boolean;
  already_sent: boolean;
  status_code: number | null;
  error: string | null;
  file: string;
}

export async function getDigest(day?: string): Promise<DigestResponse> {
  const qs = day ? `?day=${encodeURIComponent(day)}` : '';
  return api<DigestResponse>(`digest${qs}`);
}

export async function getDigestHistory(limit = 7): Promise<{ days: string[] }> {
  return api<{ days: string[] }>(`digest/history?limit=${limit}`);
}

export async function sendDigest(day?: string): Promise<DigestSendResponse> {
  return apiPost<DigestSendResponse>('digest/send', day ? { day } : {});
}

// ---------- telegram (v0.4.4 — second frontend) ----------

export interface TelegramTestResponse {
  ok: boolean;
  chat_id: number;
  status_code: number;
}

export async function testTelegram(): Promise<TelegramTestResponse> {
  return apiPost<TelegramTestResponse>('telegram/test', {});
}

// ---------- fetch core ----------

async function toError(r: Response): Promise<Error> {
  let msg = `HTTP ${r.status}`;
  try {
    const body: unknown = await r.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === 'string') msg = d;
      else if (d !== null && d !== undefined) msg = JSON.stringify(d);
    }
  } catch {
    // non-JSON error body — keep HTTP status message
  }
  return new Error(msg);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api/${path}`, init);
  if (!r.ok) throw await toError(r);
  return (await r.json()) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  return api<T>(path);
}

export async function apiPost<T>(
  path: string,
  body: unknown = {},
  query?: Record<string, string>,
): Promise<T> {
  const qs = query ? `?${new URLSearchParams(query).toString()}` : '';
  return api<T>(`${path}${qs}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function uploadMedia(file: File): Promise<MediaUpload> {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/media', { method: 'POST', body: fd });
  if (!r.ok) throw await toError(r);
  return (await r.json()) as MediaUpload;
}

// ---------- brain (self-improving memory) ----------

export interface BrainPart {
  name: string; // "instructions" | "rules" | "strategies" | "files/x" | "journal" | "photos"
  type: 'md' | 'photos';
  size: number;
  modified: string | null;
  summary: string;
}

export interface BrainRule {
  id: number;
  source: 'chat' | 'learn' | 'scan' | string;
  date: string;
  status: 'active' | 'retired';
  text: string;
}

export interface BrainJournalEntry {
  date: string;
  time: string;
  trigger: string; // "reflect:chat" | "user-edit:rules" | …
  body: string;
  changes: string[];
}

export interface BrainPhoto {
  name: string;
  size: number;
  modified: string | null;
  caption: string;
  url: string;
}

export interface BrainPartData {
  name: string;
  type: 'md' | 'photos';
  content?: string;
  rules?: BrainRule[];
  entries?: BrainJournalEntry[];
  photos?: BrainPhoto[];
}

export interface BrainReflectApplied {
  added_rules: number[];
  retired_rules: number[];
  strategy_updates: string[];
  instructions_updated: boolean;
  dropped_tainted: number;
}

export interface BrainReflectResult {
  ok: boolean;
  trigger: string;
  applied: BrainReflectApplied;
  journal_entry: string;
}

export async function getBrain(): Promise<{ parts: BrainPart[] }> {
  return api<{ parts: BrainPart[] }>('brain');
}

export async function getBrainPart(part: string): Promise<BrainPartData> {
  return api<BrainPartData>(`brain/${part}`);
}

export async function putBrainPart(part: string, content: string): Promise<{ ok: boolean }> {
  return api<{ ok: boolean }>(`brain/${part}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
}

export async function reflectBrain(trigger: 'chat' | 'learn' | 'scan'): Promise<BrainReflectResult> {
  return apiPost<BrainReflectResult>('brain/reflect', { trigger });
}

export async function uploadBrainPhoto(file: File, caption: string): Promise<BrainPhoto & { ok: boolean }> {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('caption', caption);
  const r = await fetch('/api/brain/photos', { method: 'POST', body: fd });
  if (!r.ok) throw await toError(r);
  return (await r.json()) as BrainPhoto & { ok: boolean };
}

// ---------- harness (eval + quality measurement) ----------

export type HarnessSuite = 'voice' | 'algorithm' | 'bilingual' | 'tools' | 'safety';

/** per-suite result row inside a run */
export interface HarnessResult {
  suite: HarnessSuite | string;
  score: number;
  details: {
    samples?: { text: string; idea?: string; voice_match?: number; style_distance?: number; score?: number; grade?: string }[];
    mean_voice_match?: number;
    mean_style_distance?: number;
    mean?: number;
    pct_strong?: number;
    pct_weak?: number;
    factor_distribution?: { name: string; avg_impact: number; pct_negative: number }[];
    cases?: { requested: string; detected: string; passed: string; issues: string[] }[];
    scenarios?: { scenario: string; expected: string; got: string | null; passed: boolean; arg_note?: string }[];
    checks?: Record<string, { passed: boolean; note: string }>;
    fail_closed?: boolean;
    note?: string;
    error?: string;
  };
}

export interface HarnessRun {
  id: number;
  ts: string;
  label: string; // manual | ab:pair | ab:no-brain | ab:with-brain
  real_llm: boolean;
  use_brain: boolean;
  status: 'running' | 'done' | 'error';
  total: number | null;
  deltas: Record<string, number> | null;
  report_md?: string | null;
  error?: string | null;
  results: HarnessResult[];
}

/** history list row (GET /api/harness/runs) */
export interface HarnessRunSummary {
  id: number;
  ts: string;
  label: string;
  real_llm: boolean;
  use_brain: boolean;
  status: string;
  total: number | null;
  /** {suite: score} from the deltas column */
  suites: Record<string, number>;
}

export interface HarnessCompareResponse {
  a: { id: number; ts: string; label: string; total: number | null };
  b: { id: number; ts: string; label: string; total: number | null };
  suites: Record<string, { a: number | null; b: number | null; delta: number | null }>;
  total_delta: number | null;
}

export async function startHarnessRun(
  suites: string[],
  ab: boolean,
): Promise<{ ok: boolean; run_id: number; ab: boolean }> {
  return apiPost('harness/run', { suites, ab, real_llm: false });
}

export async function getHarnessRuns(): Promise<{ runs: HarnessRunSummary[] }> {
  return api('harness/runs');
}

export async function getHarnessRun(id: number): Promise<HarnessRun> {
  return api(`harness/runs/${id}`);
}

export async function compareHarnessRuns(a: number, b: number): Promise<HarnessCompareResponse> {
  return apiPost('harness/compare', { a, b });
}

// ---------- harness SSE events ----------

export interface HarnessStartEvent { type: 'start'; run_id: number; suites: string[]; label: string; real_llm: boolean; use_brain: boolean }
export interface HarnessSuiteStartEvent { type: 'suite_start'; run_id: number; suite: string }
export interface HarnessSuiteDoneEvent { type: 'suite_done'; run_id: number; suite: string; score: number; delta: number | null }
export interface HarnessDoneEvent {
  type: 'done';
  run_id: number;
  total: number;
  deltas: Record<string, number>;
  report_path?: string | null;
  regression_notes?: string[];
}
export interface HarnessAbStartEvent { type: 'ab_start'; run_id: number; suites: string[] }
export interface HarnessAbDoneEvent {
  type: 'ab_done';
  run_id: number;
  no_brain_run_id: number;
  with_brain_run_id: number;
  lift: Record<string, number>;
}
export interface HarnessErrorEvent { type: 'error'; run_id: number; message: string }

export type HarnessEvent =
  | HarnessStartEvent
  | HarnessSuiteStartEvent
  | HarnessSuiteDoneEvent
  | HarnessDoneEvent
  | HarnessAbStartEvent
  | HarnessAbDoneEvent
  | HarnessErrorEvent;

export interface HarnessStreamHandlers {
  onSuiteStart?: (ev: HarnessSuiteStartEvent) => void;
  onSuiteDone?: (ev: HarnessSuiteDoneEvent) => void;
  onDone: (ev: HarnessDoneEvent) => void;
  onAbStart?: (ev: HarnessAbStartEvent) => void;
  onAbDone: (ev: HarnessAbDoneEvent) => void;
  onError?: (msg: string) => void;
}

function isHarnessEvent(v: unknown): v is HarnessEvent {
  if (v === null || typeof v !== 'object') return false;
  const t = (v as { type?: unknown }).type;
  return ['start', 'suite_start', 'suite_done', 'done', 'ab_start', 'ab_done', 'error'].includes(t as string);
}

/**
 * GET /api/harness/run/{id}/events as a streaming fetch (same SSE parsing as
 * streamChat). Resolves when the server closes the stream (done/ab_done/error).
 */
export async function streamHarnessRun(runId: number, handlers: HarnessStreamHandlers): Promise<void> {
  const r = await fetch(`/api/harness/run/${runId}/events`);
  if (!r.ok) throw await toError(r);
  if (!r.body) throw new Error('streaming not supported');

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  const handleFrame = (frame: string): void => {
    for (const line of frame.split('\n')) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;
      let ev: unknown;
      try {
        ev = JSON.parse(payload);
      } catch {
        continue;
      }
      if (!isHarnessEvent(ev)) continue;
      switch (ev.type) {
        case 'suite_start': handlers.onSuiteStart?.(ev); break;
        case 'suite_done': handlers.onSuiteDone?.(ev); break;
        case 'done': handlers.onDone(ev); break;
        case 'ab_start': handlers.onAbStart?.(ev); break;
        case 'ab_done': handlers.onAbDone(ev); break;
        case 'error': handlers.onError?.(ev.message); break;
      }
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep = buf.indexOf('\n\n');
    while (sep !== -1) {
      handleFrame(buf.slice(0, sep));
      buf = buf.slice(sep + 2);
      sep = buf.indexOf('\n\n');
    }
  }
  if (buf.trim().length > 0) handleFrame(buf);
}

// ---------- chat streaming ----------

function isSSEEvent(v: unknown): v is SSEEvent {
  if (v === null || typeof v !== 'object') return false;
  const t = (v as { type?: unknown }).type;
  return (
    t === 'token' ||
    t === 'thinking_steps' ||
    t === 'tool' ||
    t === 'approval' ||
    t === 'done' ||
    t === 'error'
  );
}

/**
 * POST /api/chat/stream and dispatch SSE events.
 * Server sends `data: {json}\n\n` frames; parse incrementally from the
 * ReadableStream (fetch keeps the body streaming on POST).
 */
export async function streamChat(message: string, handlers: StreamHandlers): Promise<void> {
  const r = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (!r.ok) throw await toError(r);
  if (!r.body) throw new Error('streaming not supported');

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  const handleFrame = (frame: string): void => {
    const lines = frame.split('\n');
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;
      let ev: unknown;
      try {
        ev = JSON.parse(payload);
      } catch {
        continue;
      }
      if (!isSSEEvent(ev)) continue;
      switch (ev.type) {
        case 'token':
          handlers.onToken(ev.text);
          break;
        case 'thinking_steps':
          handlers.onThinkingSteps?.(ev);
          break;
        case 'tool':
          handlers.onTool?.(ev);
          break;
        case 'approval':
          handlers.onApproval?.(ev);
          break;
        case 'done':
          handlers.onDone(ev);
          break;
        case 'error':
          handlers.onError?.(ev.message);
          break;
      }
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep = buf.indexOf('\n\n');
    while (sep !== -1) {
      handleFrame(buf.slice(0, sep));
      buf = buf.slice(sep + 2);
      sep = buf.indexOf('\n\n');
    }
  }
  if (buf.trim().length > 0) handleFrame(buf);
}
