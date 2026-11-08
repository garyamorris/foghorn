// App bootstrap: shell (top bar + nav), hash router with lazy views, shared services.
import { h, mount, clear } from './core/dom.js';
import { icon, logo } from './core/icons.js';
import { parseHash, matchRoute, link } from './core/router.js';
import { createState, lsGet } from './core/store.js';
import { applyTheme, toggleTheme, effectiveTheme } from './core/theme.js';
import { toast, initToasts } from './core/toast.js';
import { errorBox, selectEl, button, iconButton, badge, confirmDialog, inputEl, closeAllOverlays } from './core/ui.js';
import { formatClock, formatDate } from './core/format.js';
import { api, studioBase, studioThreadUrl, studioGraphUrl, setToken, API_BASE } from './api.js';
import { createLive } from './live.js';

const NAV = [
  { key: '', label: 'Mission Control', icon: 'activity', href: '#/' },
  { key: 'incidents', label: 'Incidents', icon: 'alert', href: '#/incidents' },
  { key: 'approvals', label: 'Approvals', icon: 'inbox', href: '#/approvals', badge: 'approvals' },
  { key: 'scenarios', label: 'Scenarios', icon: 'target', href: '#/scenarios' },
  { key: 'faults', label: 'Fault injector', icon: 'bolt', href: '#/faults' },
  { key: 'graphs', label: 'Graphs', icon: 'graph', href: '#/graphs' },
  { key: 'threads', label: 'Threads', icon: 'history', href: '#/threads' },
  { key: 'datasets', label: 'Datasets', icon: 'database', href: '#/datasets' },
  { key: 'evals', label: 'Evaluations', icon: 'flask', href: '#/evals' },
  { key: 'memory', label: 'Memory', icon: 'chip', href: '#/memory' },
  { key: 'guide', label: 'Studio guide', icon: 'book', href: '#/guide' },
  { key: 'system', label: 'System', icon: 'sliders', href: '#/system' },
];

const ROUTES = [
  { pattern: '/', view: 'mission', nav: '' },
  { pattern: '/events/:id', view: 'mission', nav: '' },
  { pattern: '/incidents', view: 'incidents', nav: 'incidents' },
  { pattern: '/incidents/:id', view: 'incident', nav: 'incidents' },
  { pattern: '/runs/:threadId', view: 'incident', nav: 'incidents' },
  { pattern: '/approvals', view: 'approvals', nav: 'approvals' },
  { pattern: '/approvals/:id', view: 'approvals', nav: 'approvals' },
  { pattern: '/scenarios', view: 'scenarios', nav: 'scenarios' },
  { pattern: '/scenarios/:tab', view: 'scenarios', nav: 'scenarios' },
  { pattern: '/scenarios/:tab/:id', view: 'scenarios', nav: 'scenarios' },
  { pattern: '/faults', view: 'faults', nav: 'faults' },
  { pattern: '/graphs', view: 'graphs', nav: 'graphs' },
  { pattern: '/graphs/:id', view: 'graphs', nav: 'graphs' },
  { pattern: '/threads', view: 'threads', nav: 'threads' },
  { pattern: '/threads/:threadId', view: 'threads', nav: 'threads' },
  { pattern: '/datasets', view: 'datasets', nav: 'datasets' },
  { pattern: '/datasets/:name', view: 'datasets', nav: 'datasets' },
  { pattern: '/datasets/:name/:rec', view: 'datasets', nav: 'datasets' },
  { pattern: '/evals', view: 'evals', nav: 'evals' },
  { pattern: '/evals/:id', view: 'evals', nav: 'evals' },
  { pattern: '/memory/*rest', view: 'memory', nav: 'memory' },
  { pattern: '/guide', view: 'guide', nav: 'guide' },
  { pattern: '/guide/:graph', view: 'guide', nav: 'guide' },
  { pattern: '/system', view: 'system', nav: 'system' },
];

const PERSONAS = ['on-call', 'incident-commander', 'dba', 'payments-lead'];

// -------------------------------------------------------------------------------------------------------------------
const params = new URLSearchParams(location.search);
const MOCK = params.get('mock'); // '1' => fall back to mock data when the real endpoint is missing; 'force' => always mock

const app = {
  api,
  live: createLive(),
  config: null,
  health: null,
  persona: createState(lsGet('persona', 'on-call'), 'persona'),
  pendingApprovals: createState(0),
  route: null,
  mock: MOCK,
  toast,
  link,
  navigate(hash) {
    location.hash = hash;
  },
  studioUrl: () => studioBase(app.config),
  studioThread: (tid, graph) => studioThreadUrl(app.config, tid, graph),
  studioGraph: (graph) => studioGraphUrl(app.config, graph),
  refreshApprovals,
};

window.foghorn = app; // debugging handle

let currentView = null;
let routeSeq = 0;
const viewCache = new Map();

async function loadView(name) {
  if (!viewCache.has(name)) viewCache.set(name, import(`./views/${name}.js`));
  return viewCache.get(name);
}

async function renderRoute() {
  const parsed = parseHash(location.hash);
  const m = matchRoute(ROUTES, parsed);
  const seq = ++routeSeq;
  const main = document.getElementById('main');
  if (currentView && currentView.cleanup) {
    try {
      currentView.cleanup();
    } catch (e) {
      console.error('view cleanup', e);
    }
  }
  currentView = null;
  closeAllOverlays();
  setActiveNav(m ? m.route.nav : null);
  closeNavOnMobile();
  if (!m) {
    mount(main, h('div', { class: 'view' }, h('div', { class: 'empty' }, icon('help', 'xl'), h('h4', null, 'Page not found'), h('div', { class: 'small' }, `No view for ${parsed.path}`), button('Go to Mission Control', { href: '#/', variant: 'primary' }))));
    return;
  }
  app.route = { ...parsed, params: m.params, name: m.route.view };
  document.title = `${titleFor(m.route)} - Foghorn`;
  const root = h('div', { class: 'view', id: 'view-root' });
  mount(main, root);
  main.scrollTop = 0;
  try {
    const mod = await loadView(m.route.view);
    if (seq !== routeSeq) return;
    const result = await mod.default(root, app.route, app);
    if (seq !== routeSeq) {
      if (typeof result === 'function') result();
      else if (result && result.cleanup) result.cleanup();
      return;
    }
    currentView = typeof result === 'function' ? { cleanup: result } : result || null;
  } catch (err) {
    console.error(err);
    mount(root, errorBox(err, () => renderRoute(), 'This view failed to load'));
  }
  main.focus({ preventScroll: true });
}

function titleFor(route) {
  const n = NAV.find((x) => x.key === route.nav);
  return n ? n.label : 'Foghorn';
}

function setActiveNav(key) {
  document.querySelectorAll('.nav-link').forEach((a) => {
    if (a.dataset.key === key) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

function closeNavOnMobile() {
  document.querySelector('.nav')?.classList.remove('open');
  const scrim = document.querySelector('.nav-scrim');
  if (scrim) scrim.hidden = true;
}

// -------------------------------------------------------------------------------------------------------------------
// shell

function buildShell() {
  const root = document.getElementById('app');

  const sseDot = h('span', { class: 'dot', id: 'sse-dot' });
  const ssePill = h('span', { class: 'status-pill', id: 'sse-pill', title: 'Live event stream', role: 'status' }, sseDot, h('span', { id: 'sse-text' }, 'connecting'));
  const healthDot = h('span', { class: 'dot', id: 'health-dot' });
  const healthPill = h('span', { class: 'status-pill hide-md', id: 'health-pill', title: 'Server health' }, healthDot, h('span', { id: 'health-text' }, 'server'));
  const providerPill = h('span', { class: 'status-pill hide-md', id: 'provider-pill', title: 'Model provider' }, icon('chip', 'sm'), h('span', { id: 'provider-text' }, '...'));
  const clockText = h('span', { class: 'clock', id: 'clock-text' }, '--:--:--Z');
  const clockMeta = h('span', { class: 'muted tiny hide-md', id: 'clock-meta' }, '');
  const clockPill = h('span', { class: 'status-pill hide-sm', id: 'clock-pill', title: 'Virtual clock (simulated time)' }, icon('clock', 'sm'), clockText, clockMeta);

  const personaSel = selectEl(
    PERSONAS.map((p) => ({ value: p, label: p })),
    app.persona.get(),
    (v) => app.persona.set(v),
    { size: 'sm', label: 'Acting as' },
  );
  personaSel.classList.add('persona');
  personaSel.style.width = '150px';

  const goInput = inputEl({
    size: 'sm',
    placeholder: 'Go to id...',
    label: 'Go to',
    onkeydown: (e) => {
      if (e.key !== 'Enter') return;
      const dest = resolveGoto(e.target.value.trim());
      if (dest) {
        app.navigate(dest);
        e.target.value = '';
        e.target.blur();
      } else toast({ title: 'No match', body: 'Try an incident (INC-...), event (evt_...), approval (apr_...), eval (evr_...), scenario (S01) or thread id.', tone: 'medium' });
    },
  });
  goInput.style.width = '176px';

  const themeBtn = iconButton(effectiveTheme() === 'dark' ? 'sun' : 'moon', 'Toggle light / dark theme', () => {
    const t = toggleTheme();
    themeBtn.replaceChildren(icon(t === 'dark' ? 'sun' : 'moon'));
  }, { variant: 'ghost' });
  const copilotBtn = button('Copilot', { icon: 'chat', title: 'Ask the ops copilot', onclick: () => toggleCopilot(), class: 'hide-md' });
  const copilotIcon = iconButton('chat', 'Ask the ops copilot', () => toggleCopilot(), { variant: 'ghost', class: 'show-md' });
  const studioBtn = button('Open Studio', { icon: 'external', variant: 'primary', title: 'Open LangGraph Studio for this server', onclick: () => window.open(app.studioUrl(), '_blank', 'noopener,noreferrer'), class: 'hide-md' });
  const studioIcon = iconButton('external', 'Open Studio', () => window.open(app.studioUrl(), '_blank', 'noopener,noreferrer'), { variant: 'ghost', class: 'show-md' });
  const menuBtn = iconButton('menu', 'Open navigation', () => {
    const nav = document.querySelector('.nav');
    const open = nav.classList.toggle('open');
    document.querySelector('.nav-scrim').hidden = !open;
    menuBtn.setAttribute('aria-expanded', String(open));
  }, { variant: 'ghost', class: 'nav-toggle' });
  menuBtn.setAttribute('aria-expanded', 'false');
  menuBtn.setAttribute('aria-controls', 'nav');
  const tokenBtn = iconButton('lock', 'API token', () => tokenDialog(), { variant: 'ghost', class: 'hide-md' });

  const top = h(
    'header',
    { class: 'topbar' },
    menuBtn,
    h('a', { class: 'brand', href: '#/', 'aria-label': 'Foghorn, Mission Control' }, logo(), h('span', { class: 'brand-name' }, 'Foghorn')),
    h('div', { class: 'topbar-group' }, ssePill, healthPill, providerPill, clockPill),
    h('div', { class: 'topbar-spacer' }),
    h('div', { class: 'topbar-group hide-md' }, goInput),
    h('div', { class: 'topbar-group hide-md' }, h('span', { class: 'muted tiny' }, 'Acting as'), personaSel),
    h('div', { class: 'topbar-group' }, copilotBtn, copilotIcon, studioBtn, studioIcon, tokenBtn, themeBtn),
  );

  const nav = h(
    'nav',
    { class: 'nav', id: 'nav', 'aria-label': 'Primary' },
    NAV.map((n) =>
      h('a', { class: 'nav-link', href: n.href, dataset: { key: n.key }, title: n.label }, icon(n.icon), h('span', { class: 'label' }, n.label), n.badge && h('span', { class: 'count nav-badge t-high', id: `nav-badge-${n.badge}`, hidden: true, 'aria-label': 'pending' }, '0')),
    ),
    h('div', { class: 'nav-foot' }, h('div', null, 'Foghorn'), h('div', { id: 'nav-foot-sub' }, 'Brightcart on-call')),
  );

  const scrim = h('div', { class: 'nav-scrim', hidden: true, onclick: closeNavOnMobile });
  const main = h('main', { class: 'main', id: 'main', tabindex: '-1', 'aria-label': 'Content' });
  mount(root, top, nav, scrim, main);
  root.removeAttribute('aria-busy');
  if (MOCK) {
    const pill = h('span', { class: 'status-pill t-medium', title: MOCK === 'force' ? 'Mock data: every endpoint is simulated (?mock=force)' : 'Mock fallback: endpoints the server lacks are simulated (?mock=1)', style: 'color:var(--medium);background:var(--medium-soft);font-weight:700' }, MOCK === 'force' ? 'MOCK' : 'MOCK*');
    top.querySelector('.topbar-group').prepend(pill);
  }
}

function resolveGoto(q) {
  if (!q) return null;
  if (/^INC-/i.test(q)) return link('incidents', q.toUpperCase());
  if (/^evt_/i.test(q)) return link('events', q);
  if (/^apr_/i.test(q)) return link('approvals', q);
  if (/^evr_/i.test(q)) return link('evals', q);
  if (/^(S\d{2}|X-\d+)$/i.test(q)) return link('scenarios', 'launcher', q.toUpperCase());
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(q)) return link('threads', q);
  if (/^dlq_/i.test(q)) return link('system');
  return null;
}

function tokenDialog() {
  const input = inputEl({ type: 'password', placeholder: 'Bearer token (FOGHORN_DASHBOARD_API_TOKEN)', label: 'API token', mono: true });
  input.value = lsGet('token', '') || '';
  confirmDialog({ title: 'API token', text: 'Only needed when the server sets FOGHORN_DASHBOARD_API_TOKEN. Stored in this browser only.', confirmLabel: 'Save' }).then((ok) => {
    if (ok) {
      setToken(input.value.trim());
      toast({ title: 'Token saved', tone: 'ok', ttl: 2000 });
      location.reload();
    }
  });
  // confirmDialog has no slot for the input: re-use prompt-free flow by inserting it into the modal body
  const body = document.querySelector('.modal .drawer-body .col');
  if (body) body.insertBefore(input, body.children[1]);
  input.focus();
}

// -------------------------------------------------------------------------------------------------------------------
// live status wiring

function wireTopbar() {
  const live = app.live;
  const setSse = (status, info) => {
    const dot = document.getElementById('sse-dot');
    const txt = document.getElementById('sse-text');
    if (!dot) return;
    const map = { open: ['ok', 'live', true], connecting: ['medium', 'connecting', false], retrying: ['high', 'retrying', false], error: ['critical', 'offline', false], closed: ['info', 'closed', false], idle: ['info', 'idle', false] };
    const [tone, label, pulse] = map[status] || map.idle;
    dot.className = `dot t-${tone}${pulse ? ' live' : ''}`;
    txt.textContent = status === 'retrying' && info?.delay ? `retry in ${(info.delay / 1000).toFixed(1)}s` : label;
    document.getElementById('sse-pill').title = `Event stream: ${status}${info?.error ? ' (' + (info.error.message || info.error) + ')' : ''}`;
  };
  setSse(live.state.status, {});
  live.on('status', ({ status, info }) => setSse(status, info));

  const renderClock = () => {
    const c = live.state.clock;
    const txt = document.getElementById('clock-text');
    const meta = document.getElementById('clock-meta');
    if (!txt) return;
    const now = live.virtualNow();
    if (Number.isNaN(now)) {
      txt.textContent = '--:--:--Z';
      meta.textContent = '';
      return;
    }
    const iso = new Date(now).toISOString();
    txt.textContent = `${formatDate(iso).slice(5)} ${formatClock(iso)}`;
    meta.textContent = c.paused ? 'paused' : `${c.speed}x`;
  };
  live.on('clock', renderClock);
  setInterval(renderClock, 1000);
  renderClock();

  const renderApprovals = (n) => {
    const b = document.getElementById('nav-badge-approvals');
    if (!b) return;
    b.textContent = String(n);
    b.hidden = !n;
    b.setAttribute('aria-label', `${n} pending approvals`);
    document.querySelector('.nav-link[data-key="approvals"]')?.setAttribute('title', n ? `Approvals (${n} pending)` : 'Approvals');
  };
  app.pendingApprovals.subscribe(renderApprovals);

  // approvals: refresh the count on relevant events; toast when a new one is requested
  const seenApprovals = new Set();
  live.on('event', ({ event: e, backfill }) => {
    if (e.type === 'approval.requested' || e.type === 'approval.decided') refreshApprovals();
    if (e.type === 'approval.requested' && !backfill) {
      const id = e.payload?.approval_id || e.id;
      if (seenApprovals.has(id)) return;
      seenApprovals.add(id);
      const recent = Date.now() - Date.parse(e.received_at || '') < 120000;
      if (recent || !e.received_at) {
        toast({
          title: 'Approval requested',
          body: `${e.payload?.title || e.payload?.action || e.payload?.kind || 'Review needed'}${e.payload?.incident_id ? ' · ' + e.payload.incident_id : ''}`,
          tone: 'medium',
          ttl: 12000,
          action: { label: 'Review', href: link('approvals', id) },
        });
      }
    }
  });
  live.on('summary', (s) => {
    if (typeof s.pending_approvals === 'number' && !app._approvalsExact) app.pendingApprovals.set(s.pending_approvals);
  });
}

let approvalsTimer = null;
async function refreshApprovals() {
  clearTimeout(approvalsTimer);
  approvalsTimer = setTimeout(async () => {
    try {
      const res = await api.approvals({ status: 'pending' });
      app._approvalsExact = true;
      app.pendingApprovals.set((res.items || []).length);
      app.live.bus.emit('approvals', res);
    } catch {
      /* the badge keeps the last known value */
    }
  }, 250);
}

async function pollHealth() {
  const set = (tone, text, title) => {
    const dot = document.getElementById('health-dot');
    if (!dot) return;
    dot.className = `dot t-${tone}`;
    document.getElementById('health-text').textContent = text;
    document.getElementById('health-pill').title = title;
  };
  try {
    const hres = await api.health();
    app.health = hres;
    const comps = Object.entries(hres.components || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
    if (hres.status === 'ok' && hres.ready !== false) set('ok', 'healthy', comps || 'ok');
    else set('medium', hres.ready === false ? 'starting' : 'degraded', comps);
  } catch (e) {
    try {
      await api.healthz();
      set('medium', 'api partial', 'GET /api/health failed but /healthz answers');
    } catch {
      set('critical', 'down', 'Server unreachable');
    }
  }
}

async function loadConfig() {
  try {
    app.config = await api.config();
  } catch {
    app.config = { provider: null, public_base_url: location.origin, studio: {} };
    try {
      const hz = await api.healthz();
      app.config.provider = hz.provider;
    } catch {
      /* offline */
    }
  }
  const p = app.config.provider || 'unknown';
  const txt = document.getElementById('provider-text');
  if (txt) {
    txt.textContent = p;
    document.getElementById('provider-pill').className = `status-pill hide-md t-${p === 'offline' ? 'info' : 'accent'}`;
    document.getElementById('provider-pill').title = p === 'offline' ? 'Offline deterministic models (no network, no keys)' : `Live provider: ${p}`;
  }
  app.live.bus.emit('config', app.config);
}

// -------------------------------------------------------------------------------------------------------------------
// copilot panel (lazy)

let copilot = null;
async function toggleCopilot() {
  const mod = await import('./views/copilot.js');
  if (!copilot) copilot = mod.createCopilotPanel(app);
  copilot.toggle();
}
app.toggleCopilot = toggleCopilot;

// -------------------------------------------------------------------------------------------------------------------

async function boot() {
  applyTheme();
  initToasts();
  if (MOCK) {
    const mock = await import('./mock.js');
    mock.installMock({ force: MOCK === 'force' });
  }
  buildShell();
  window.addEventListener('hashchange', renderRoute);
  wireTopbar();
  await loadConfig();
  pollHealth();
  setInterval(pollHealth, 12000);
  app.live.start().catch((e) => console.error('live start', e));
  refreshApprovals();
  setInterval(refreshApprovals, 30000);
  await renderRoute();
  document.addEventListener('keydown', (e) => {
    if (e.target.matches?.('input, textarea, select, [contenteditable]')) return;
    if (e.key === 'c' && e.altKey) toggleCopilot();
  });
}

boot().catch((e) => {
  console.error(e);
  const root = document.getElementById('app');
  clear(root);
  root.appendChild(h('div', { style: 'padding:24px;grid-column:1/-1' }, errorBox(e, () => location.reload(), 'Foghorn failed to start')));
});
