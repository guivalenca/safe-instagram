import { html, render, useState, useEffect, useLayoutEffect, useRef, useCallback } from "./vendor/preact-htm.js";

/* ==========================================================================
   Data layer: stale-while-revalidate cache, persisted across launches.
   Screens render instantly from cache and quietly refresh in the background.
   ========================================================================== */

const CACHE_KEY = "sg-cache-v1";
const store = new Map();
try {
  for (const [k, v] of Object.entries(JSON.parse(localStorage.getItem(CACHE_KEY) || "{}"))) store.set(k, v);
} catch {}
const subs = new Map();
const inflight = new Map();
let persistTimer;

function persist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    const entries = [...store.entries()].sort((a, b) => b[1].at - a[1].at).slice(0, 150);
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(Object.fromEntries(entries)));
    } catch {
      localStorage.removeItem(CACHE_KEY);
    }
  }, 400);
}

function setData(key, data) {
  store.set(key, { data, at: Date.now() });
  subs.get(key)?.forEach((f) => f());
  persist();
}

function dropData(prefix) {
  for (const k of [...store.keys()]) if (k.startsWith(prefix)) store.delete(k);
  persist();
}

class ApiError extends Error {
  constructor(status, body) {
    super(body?.message || `Something went wrong (${status})`);
    this.status = status;
    this.code = body?.error;
  }
}

let onAuthProblem = () => {};

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (form) opts.body = form;
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(0, { error: "offline", message: "No connection." });
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new ApiError(res.status, data);
    if (err.code === "locked" || err.code === "not_connected") onAuthProblem(err.code);
    throw err;
  }
  return data;
}

function load(key) {
  if (inflight.has(key)) return inflight.get(key);
  const p = api(key)
    .then((d) => (setData(key, d), d))
    .finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

function prefetch(key, maxAge = 30000) {
  const e = store.get(key);
  if (!e || Date.now() - e.at > maxAge) load(key).catch(() => {});
}

function useQuery(key, maxAge = 30000) {
  const [, force] = useState(0);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!key) return;
    const fn = () => force((n) => n + 1);
    if (!subs.has(key)) subs.set(key, new Set());
    subs.get(key).add(fn);
    setError(null);
    const e = store.get(key);
    if (!e || Date.now() - e.at > maxAge) {
      setLoading(true);
      load(key)
        .catch(setError)
        .finally(() => setLoading(false));
    }
    return () => subs.get(key).delete(fn);
  }, [key]);
  const refresh = useCallback(() => {
    if (!key) return Promise.resolve();
    setLoading(true);
    return load(key)
      .then(() => setError(null))
      .catch(setError)
      .finally(() => setLoading(false));
  }, [key]);
  return { data: key ? store.get(key)?.data : undefined, error, loading, refresh };
}

/* Things we already know about, so the next screen can draw before its data lands. */
const peeks = { users: new Map(), media: new Map(), threads: new Map() };

const RECENT_KEY = "sg-recent";
const recents = () => JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
function addRecent(u) {
  const short = { id: u.id, username: u.username, name: u.name, pic: u.pic, verified: u.verified, private: u.private };
  const list = [short, ...recents().filter((r) => r.id !== u.id)].slice(0, 20);
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
}
function removeRecent(id) {
  localStorage.setItem(RECENT_KEY, JSON.stringify(recents().filter((r) => r.id !== id)));
}

function warmUser(u) {
  peeks.users.set(u.username.toLowerCase(), u);
  prefetch(`/api/u/${encodeURIComponent(u.username)}`, 60000);
}

/* ==========================================================================
   Router
   ========================================================================== */

const routeSubs = new Set();
const here = () => location.pathname + location.search;

function navigate(to, { replace = false } = {}) {
  if (to === here() && !replace) return;
  history.replaceState({ ...history.state, scroll: window.scrollY }, "");
  const idx = (history.state?.idx || 0) + (replace ? 0 : 1);
  if (replace) history.replaceState({ idx }, "", to);
  else history.pushState({ idx }, "", to);
  routeSubs.forEach((f) => f(replace ? "replace" : "push"));
}

window.addEventListener("popstate", () => routeSubs.forEach((f) => f("pop")));

function back(fallback = "/") {
  if ((history.state?.idx || 0) > 0) history.back();
  else navigate(fallback, { replace: true });
}

function useRoute() {
  const [r, setR] = useState({ path: location.pathname, search: location.search, dir: "none", n: 0 });
  useEffect(() => {
    const f = (dir) => setR((p) => ({ path: location.pathname, search: location.search, dir, n: p.n + 1 }));
    routeSubs.add(f);
    return () => routeSubs.delete(f);
  }, []);
  return r;
}

function Link({ href, warm, children, ...rest }) {
  return html`<a
    href=${href}
    ...${rest}
    onClick=${(e) => {
      if (e.metaKey || e.ctrlKey) return;
      e.preventDefault();
      warm?.();
      navigate(href);
    }}
    >${children}</a
  >`;
}

/* ==========================================================================
   Little UI pieces
   ========================================================================== */

const toastSubs = new Set();
function toast(msg) {
  toastSubs.forEach((f) => f(msg));
}

function Toasts() {
  const [msg, setMsg] = useState(null);
  useEffect(() => {
    let t;
    const f = (m) => {
      setMsg(m);
      clearTimeout(t);
      t = setTimeout(() => setMsg(null), 3200);
    };
    toastSubs.add(f);
    return () => toastSubs.delete(f);
  }, []);
  return msg ? html`<div class="toast" onClick=${() => setMsg(null)}>${msg}</div>` : null;
}

const I = {
  search: html`<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>`,
  send: html`<svg viewBox="0 0 24 24"><path d="M21 3 10 14"/><path d="m21 3-7 18-4-7-7-4z"/></svg>`,
  plus: html`<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5"/><path d="M12 8v8M8 12h8"/></svg>`,
  back: html`<svg viewBox="0 0 24 24"><path d="m15 5-7 7 7 7"/></svg>`,
  heart: html`<svg viewBox="0 0 24 24"><path d="M12 20s-7.5-4.6-9.3-9.2C1.4 7.4 3.6 4 7.1 4c2 0 3.4 1.1 4.9 3 1.5-1.9 2.9-3 4.9-3 3.5 0 5.7 3.4 4.4 6.8C19.5 15.4 12 20 12 20z"/></svg>`,
  gear: html`<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>`,
  x: html`<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>`,
  comment: html`<svg viewBox="0 0 24 24"><path d="M20.7 16.4A9 9 0 1 0 17 20l4 1z"/></svg>`,
  bookmark: html`<svg viewBox="0 0 24 24"><path d="M19 21 12 16 5 21V4a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1z"/></svg>`,
  requests: html`<svg viewBox="0 0 24 24"><circle cx="10" cy="8" r="4"/><path d="M2.5 20a7.5 7.5 0 0 1 15 0"/><path d="M19.5 8v6M16.5 11h6"/></svg>`,
  grid: html`<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/></svg>`,
  plusPlain: html`<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>`,
  sun: html`<svg viewBox="0 0 24 24"><path d="M3 18h18M6.5 18a5.5 5.5 0 0 1 11 0"/><path d="M12 7V4M5.2 10.2 3.6 8.6M18.8 10.2l1.6-1.6"/></svg>`,
  lock: html`<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>`,
  camera: html`<svg viewBox="0 0 24 24"><rect x="3.96" y="3.96" width="16.08" height="16.08" rx="4.32"/><circle cx="12" cy="12" r="3.7"/><circle cx="16.51" cy="7.44" r="1.03" class="dot"/></svg>`,
  album: html`<svg viewBox="0 0 24 24" class="fill"><path d="M7 3h12a2 2 0 0 1 2 2v12h-2V5H7z"/><rect x="3" y="7" width="14" height="14" rx="2"/></svg>`,
  play: html`<svg viewBox="0 0 24 24" class="fill"><path d="M7 4v16l13-8z"/></svg>`,
  check: html`<svg viewBox="0 0 24 24" class="verified"><circle cx="12" cy="12" r="10"/><path d="m8 12.5 2.6 2.5L16 9.5"/></svg>`,
};

// Some Instagram images are marked "show only on instagram.com", which Safari
// enforces; when one fails, fetch it through our server instead.
const viaServer = (u) => (u && /^https:\/\/[^/]+\.(cdninstagram\.com|fbcdn\.net)\//.test(u) ? `/api/img?u=${encodeURIComponent(u)}` : null);

function Img({ src, fallback, class: cls = "", alt = "", ...rest }) {
  const [ok, setOk] = useState(false);
  const [fail, setFail] = useState({ src, n: 0 });
  const n = fail.src === src ? fail.n : 0;
  const tries = [...new Set([src, viaServer(src), fallback, viaServer(fallback)].filter(Boolean))];
  src = tries[Math.min(n, tries.length - 1)];
  return src
    ? html`<img
        src=${src}
        onError=${() => n < tries.length - 1 && setFail({ src: tries[0], n: n + 1 })}
        alt=${alt}
        class=${`${cls} ${ok ? "loaded" : ""}`}
        decoding="async"
        loading="lazy"
        referrerpolicy="no-referrer"
        onLoad=${() => setOk(true)}
        ...${rest}
      />`
    : html`<span class=${`${cls} img-empty`}></span>`;
}

function Avatar({ src, fallback, size = 44, ring = false, onClick }) {
  return html`<span class=${`avatar ${ring ? "ring" : ""}`} style=${`--s:${size}px`} onClick=${onClick}>
    <${Img} src=${src} fallback=${fallback} />
  </span>`;
}

function Name({ u }) {
  return html`<span class="name">${u.username}${u.verified && I.check}</span>`;
}

function TopBar({ title, left, right, sub }) {
  return html`<header class="topbar">
    <div class="tb-left">${left === undefined ? html`<button class="icon" onClick=${() => back()}>${I.back}</button>` : left}</div>
    <div class="tb-title">${title}${sub && html`<small>${sub}</small>`}</div>
    <div class="tb-right">${right}</div>
  </header>`;
}

function Spinner() {
  return html`<div class="spinner"></div>`;
}

function Empty({ children }) {
  return html`<div class="empty">${children}</div>`;
}

function ErrorView({ error, retry }) {
  return html`<div class="empty">
    <p>${error.message}</p>
    ${retry && html`<button class="btn secondary" onClick=${retry}>Try again</button>`}
  </div>`;
}

function Sheet({ children, onClose, tall = false }) {
  return html`<div class="sheet-backdrop" onClick=${onClose}>
    <div class=${`sheet ${tall ? "tall" : ""}`} onClick=${(e) => e.stopPropagation()}>${children}</div>
  </div>`;
}

function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function count(n) {
  if (n == null) return "–";
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, "")}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, "")}K`;
  return n.toLocaleString();
}

/* Pull down at the top of a screen to refresh. */
function usePullToRefresh(refresh) {
  const [pull, setPull] = useState(0);
  const [busy, setBusy] = useState(false);
  const running = useRef(false);
  useEffect(() => {
    let startY = null;
    let dist = 0;
    const down = (e) => {
      // Overlays (stories, sheets) have their own swipe-down; and one refresh at a time.
      const overlay = e.target.closest?.(".stories, .sheet-backdrop");
      startY = window.scrollY <= 0 && !overlay && !running.current ? e.touches[0].clientY : null;
      dist = 0;
    };
    const move = (e) => {
      if (startY == null) return;
      dist = Math.max(0, e.touches[0].clientY - startY);
      setPull(Math.min(dist * 0.5, 70));
    };
    const up = async () => {
      if (startY == null) return;
      startY = null;
      setPull(0);
      if (dist * 0.5 > 55 && !running.current) {
        running.current = true;
        setBusy(true);
        try {
          await refresh();
        } finally {
          running.current = false;
          setBusy(false);
        }
      }
    };
    window.addEventListener("touchstart", down, { passive: true });
    window.addEventListener("touchmove", move, { passive: true });
    window.addEventListener("touchend", up);
    return () => {
      window.removeEventListener("touchstart", down);
      window.removeEventListener("touchmove", move);
      window.removeEventListener("touchend", up);
    };
  }, [refresh]);
  return pull || busy
    ? html`<div class="ptr" style=${`height:${busy ? 44 : pull}px`}>
        <div class=${`spinner ${busy ? "" : "still"}`} style=${`transform:rotate(${pull * 5}deg)`}></div>
      </div>`
    : null;
}

/* ==========================================================================
   Unlock & connect
   ========================================================================== */

function Unlock({ done }) {
  const [pass, setPass] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/unlock", { method: "POST", body: { passcode: pass } });
      await done();
    } catch (err) {
      setError(err.message);
      setPass("");
    }
    setBusy(false);
  }
  return html`<div class="gate">
    <div class="logo">${I.camera}</div>
    <h1>Quiet Instagram</h1>
    <p class="muted">Search, follow, message. Nothing to scroll.</p>
    <form onSubmit=${submit}>
      <input type="password" placeholder="Passcode" value=${pass} onInput=${(e) => setPass(e.target.value)} autocomplete="current-password" />
      ${error && html`<p class="error">${error}</p>`}
      <button class="btn" disabled=${busy || !pass}>${busy ? "Unlocking…" : "Unlock"}</button>
    </form>
  </div>`;
}

function Connect({ done }) {
  const [step, setStep] = useState("login");
  const [kind, setKind] = useState("");
  const [f, setF] = useState({ username: "", password: "", sessionid: "", code: "", totp: "" });
  const [useCookie, setUseCookie] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const device = { timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", locale: navigator.language };

  async function handle(promise) {
    setBusy(true);
    setError(null);
    try {
      const r = await promise;
      if (r.status === "ok") return await done();
      if (r.status === "need_code") {
        setKind(r.kind);
        setStep("code");
        setF((x) => ({ ...x, code: "" }));
      } else setError(r.message);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const login = (e) => {
    e.preventDefault();
    const body = useCookie ? { sessionid: f.sessionid, ...device } : { username: f.username, password: f.password, code: f.totp, ...device };
    handle(api("/api/connect", { method: "POST", body }));
  };
  const code = (e) => {
    e.preventDefault();
    handle(api("/api/connect/code", { method: "POST", body: { code: f.code } }));
  };
  const where =
    kind === "2fa" ? "your authenticator app or texts" : kind.includes("mail") ? "your email" : kind.includes("sms") || kind.includes("phone") ? "your phone" : "your email or phone";

  return html`<div class="gate">
    <div class="logo">${I.camera}</div>
    <h1>Connect Instagram</h1>
    ${step === "login"
      ? html`<form onSubmit=${login}>
          ${useCookie
            ? html`<textarea rows="3" placeholder="sessionid cookie value" value=${f.sessionid} onInput=${set("sessionid")}></textarea>`
            : html`<input placeholder="Username" autocapitalize="off" autocorrect="off" autocomplete="username" value=${f.username} onInput=${set("username")} />
                <input type="password" placeholder="Password" autocomplete="current-password" value=${f.password} onInput=${set("password")} />
                <input inputmode="numeric" autocomplete="one-time-code" placeholder="Authenticator code (only if you use one)" value=${f.totp} onInput=${set("totp")} />`}
          ${error && html`<p class="error">${error}</p>`}
          <button class="btn" disabled=${busy}>${busy ? "Logging in… this can take a moment" : "Log in"}</button>
          <button type="button" class="link small" onClick=${() => setUseCookie(!useCookie)}>
            ${useCookie ? "Use username and password" : "Use a session cookie instead"}
          </button>
          <p class="muted small">Your password goes straight to Instagram and is never stored.</p>
        </form>`
      : html`<form onSubmit=${code}>
          <p>Instagram sent a code to ${where}.</p>
          <input inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code" value=${f.code} onInput=${set("code")} />
          ${error && html`<p class="error">${error}</p>`}
          <button class="btn" disabled=${busy || f.code.length < 6}>${busy ? "Checking…" : "Continue"}</button>
          <button type="button" class="link small" onClick=${() => setStep("login")}>Start over</button>
        </form>`}
  </div>`;
}

/* ==========================================================================
   Search
   ========================================================================== */

function UserRow({ u, onRemove }) {
  return html`<li class="row">
    <${Link} href=${`/u/${encodeURIComponent(u.username)}`} warm=${() => warmUser(u)} class="row-main">
      <${Avatar} src=${u.pic} />
      <span class="row-text">
        <${Name} u=${u} />
        <small class="muted">${u.name}${u.private && u.name ? " · " : ""}${u.private ? "Private" : ""}</small>
        ${u.social && html`<small class="muted">${u.social}</small>`}
      </span>
    <//>
    ${onRemove && html`<button class="icon small muted" onClick=${onRemove}>${I.x}</button>`}
  </li>`;
}

function Search({ initial }) {
  const [q, setQ] = useState(initial);
  const [term, setTerm] = useState(initial.trim());
  const [recent, setRecent] = useState(recents);
  const input = useRef();
  useEffect(() => {
    const t = setTimeout(() => {
      const v = q.trim();
      setTerm(v.length >= 2 ? v : "");
      history.replaceState(history.state, "", v ? `/?q=${encodeURIComponent(v)}` : "/");
    }, 450);
    return () => clearTimeout(t);
  }, [q]);
  const key = term ? `/api/search?q=${encodeURIComponent(term.toLowerCase())}` : null;
  const { data, error, loading } = useQuery(key, 5 * 60000);
  const last = useRef(null);
  if (data) last.current = data;
  const results = key ? data || last.current : null;
  results?.forEach((u) => peeks.users.set(u.username.toLowerCase(), u));

  return html`<div class="page">
    <div class="searchbar">
      <div class="field">
        ${I.search}
        <input
          ref=${input}
          type="search"
          placeholder="Search"
          value=${q}
          autocapitalize="off"
          autocorrect="off"
          spellcheck="false"
          enterkeyhint="search"
          onInput=${(e) => setQ(e.target.value)}
          onKeyDown=${(e) => e.key === "Enter" && input.current.blur()}
        />
        ${loading && html`<div class="spinner tiny"></div>`}
        ${q && !loading && html`<button class="icon small" onClick=${() => (setQ(""), input.current.focus())}>${I.x}</button>`}
      </div>
    </div>
    ${!key
      ? recent.length
        ? html`<div class="section-head"><span>Recent</span><button class="link" onClick=${() => (localStorage.removeItem(RECENT_KEY), setRecent([]))}>Clear</button></div>
            <ul class="list">${recent.map((u) => html`<${UserRow} key=${u.id} u=${u} onRemove=${() => (removeRecent(u.id), setRecent(recents()))} />`)}</ul>`
        : html`<${Empty}><p class="muted">Find someone.</p><//>`
      : error && !results
      ? html`<${ErrorView} error=${error} />`
      : results && !results.length && !loading
      ? html`<${Empty}><p class="muted">No one called “${term}”.</p><//>`
      : html`<ul class="list">${(results || []).map((u) => html`<${UserRow} key=${u.id} u=${u} />`)}</ul>`}
  </div>`;
}

/* ==========================================================================
   Profile
   ========================================================================== */

let me = { username: "", id: "" };
const gridPages = new Map(); // userId -> cursors loaded, so "back" keeps your place

function Profile({ username }) {
  const key = `/api/u/${encodeURIComponent(username)}`;
  const { data: p, error, refresh } = useQuery(key, 60000);
  const peek = peeks.users.get(username.toLowerCase()) || recents().find((r) => r.username === username);
  const u = p || peek;
  const id = u?.id;
  const stories = useQuery(id ? `/api/u/${id}/stories` : null, 60000).data;
  const hasStories = stories?.length > 0;
  useEffect(() => {
    if (hasStories) new Image().src = stories[0].img; // so tapping the ring opens instantly
  }, [hasStories && stories[0].id]);
  const requests = useQuery(p?.me ? "/api/requests" : null, 60000).data;
  const newRequests = requests?.some((r) => !seenRequests().has(r.id));
  const [sheet, setSheet] = useState(false);
  const [myTab, setMyTab] = useState(profileTab.value);
  const ptr = usePullToRefresh(
    useCallback(async () => {
      dropData(`/api/u/${id}/posts`);
      gridPages.delete(id);
      await Promise.all([refresh(), id && load(`/api/u/${id}/stories`).catch(() => {})]);
    }, [id, refresh])
  );

  useEffect(() => {
    if (p && !p.me) addRecent(p);
  }, [p?.id]);

  if (!u) return error ? html`<${TopBar} title=${username} /><${ErrorView} error=${error} retry=${refresh} />` : html`<${TopBar} title=${username} /><${Spinner} />`;

  const openStories = () => hasStories && navigate(`/u/${encodeURIComponent(u.username)}/stories`);

  return html`<div class="page">
    <${TopBar}
      title=${html`<${Name} u=${u} />`}
      left=${p?.me ? html`<${Link} href="/requests" class="icon" aria-label="Follow requests">${I.requests}${newRequests && html`<i class="icon-dot"></i>`}<//>` : undefined}
      right=${p?.me && html`<${Link} href="/settings" class="icon">${I.gear}<//>`}
    />
    ${ptr}
    <section class="profile-head">
      <${Avatar} src=${p?.pic_hd || u.pic} fallback=${u.pic} size=${86} ring=${hasStories} onClick=${openStories} />
      <div class="stats">
        <div><b>${count(p?.posts)}</b><span>posts</span></div>
        <div><b>${count(p?.followers)}</b><span>followers</span></div>
        <div><b>${count(p?.following)}</b><span>following</span></div>
      </div>
    </section>
    <section class="profile-bio">
      ${u.name && html`<b>${u.name}</b>`}
      ${p?.bio && html`<p>${p.bio}</p>`}
      ${p?.url && html`<a href=${p.url} target="_blank" rel="noopener" class="url">${p.url.replace(/^https?:\/\/(www\.)?/, "")}</a>`}
      ${p?.social && html`<small class="muted">${p.social}</small>`}
      ${p?.rel?.followed_by && html`<small class="muted">Follows you</small>`}
    </section>
    ${p && !p.me && html`<${ProfileActions} p=${p} k=${key} onUnfollow=${() => setSheet(true)} />`}
    ${!p && html`<div class="actions"><button class="btn ghost" disabled>…</button></div>`}
    ${id && (p?.can_see ?? !u.private) && html`<${Highlights} userId=${id} username=${u.username} />`}
    ${p?.me
      ? html`<div class="grid-tabs">
          ${[
            ["posts", I.grid, "Posts"],
            ["saved", I.bookmark, "Saved"],
            ["liked", I.heart, "Liked"],
          ].map(
            ([k, icon, label]) => html`<button class=${myTab === k ? "on" : ""} aria-label=${label} onClick=${() => ((profileTab.value = k), setMyTab(k))}>${icon}</button>`
          )}
        </div>`
      : html`<div class="grid-tabs"><span class="on">${I.grid}</span></div>`}
    ${p?.me && myTab !== "posts"
      ? html`<${Collection} key=${myTab} which=${myTab} />`
      : p && !p.can_see
      ? html`<${Empty}><div class="big-icon">${I.lock}</div><b>This account is private</b><p class="muted">Follow to see their photos.</p><//>`
      : p && p.posts === 0 && !p.me
      ? html`<${Empty}><p class="muted">No posts yet.</p><//>`
      : id && (p?.can_see ?? !u.private)
      ? html`<${Grid} userId=${id} mine=${!!p?.me} empty=${p?.posts === 0} />`
      : null}
    ${sheet &&
    html`<${Sheet} onClose=${() => setSheet(false)}>
      <${Avatar} src=${u.pic} size=${64} />
      <p><b>@${u.username}</b></p>
      ${p.rel.following &&
      html`<div class="toggles">
          <label class="toggle-row"><span>Mute posts</span><input type="checkbox" class="switch" checked=${p.rel.muted_posts} onChange=${(e) => mute(p, key, { posts: e.target.checked })} /></label>
          <label class="toggle-row"><span>Mute stories</span><input type="checkbox" class="switch" checked=${p.rel.muted_stories} onChange=${(e) => mute(p, key, { stories: e.target.checked })} /></label>
        </div>
        <p class="muted small">They won't know. Muted posts and stories stay out of Today.</p>`}
      ${p.private && !p.rel.requested && html`<p class="muted small">If you unfollow, you'll need to request again to see their posts.</p>`}
      <button class="btn danger" onClick=${() => (setSheet(false), follow(p, key, false))}>${p.rel.requested ? "Cancel request" : "Unfollow"}</button>
      <button class="btn secondary" onClick=${() => setSheet(false)}>Cancel</button>
    <//>`}
  </div>`;
}

const SEEN_REQUESTS_KEY = "seen-requests";
const seenRequests = () => new Set(JSON.parse(localStorage.getItem(SEEN_REQUESTS_KEY) || "[]"));

// Who you both know. The requests list doesn't always say, so ask their profile
// (the same call as opening it) for the first 3.
function RequestSocial({ u, look }) {
  const p = useQuery(look && !u.social ? `/api/u/${encodeURIComponent(u.username)}` : null, 10 * 60000).data;
  const text = u.social || p?.social;
  return text ? html`<small class="muted">${text}</small>` : null;
}

function Requests() {
  const key = "/api/requests";
  const { data, error, refresh } = useQuery(key, 30000);
  const ptr = usePullToRefresh(refresh);
  useEffect(() => {
    if (data) localStorage.setItem(SEEN_REQUESTS_KEY, JSON.stringify(data.map((u) => u.id)));
  }, [data]);
  const answer = async (u, approve) => {
    setData(key, store.get(key).data.filter((x) => x.id !== u.id));
    try {
      await api(`/api/requests/${u.id}`, { method: "POST", body: { approve } });
      const pk = `/api/u/${encodeURIComponent(u.username)}`;
      const prof = store.get(pk)?.data;
      if (prof?.rel) setData(pk, { ...prof, rel: { ...prof.rel, incoming: false, followed_by: approve } });
      if (approve) bumpMine("followers", 1);
      if (approve) toast(`@${u.username} can now see your posts`);
    } catch (e) {
      const now = store.get(key)?.data || [];
      if (!now.some((x) => x.id === u.id)) setData(key, [u, ...now]); // only this one comes back
      toast(e.message);
    }
  };
  return html`<div class="page">
    <${TopBar} title="Follow requests" />
    ${ptr}
    ${!data
      ? error
        ? html`<${ErrorView} error=${error} retry=${refresh} />`
        : html`<${Spinner} />`
      : !data.length
      ? html`<${Empty}><p class="muted">No one's waiting.</p><//>`
      : html`<ul class="list">
          ${data.map(
            (u, i) => html`<li class="row" key=${u.id}>
              <${Link} href=${`/u/${encodeURIComponent(u.username)}`} warm=${() => warmUser(u)} class="row-main">
                <${Avatar} src=${u.pic} />
                <span class="row-text">
                  <${Name} u=${u} />
                  <small class="muted">${u.name}</small>
                  <${RequestSocial} u=${u} look=${i < 3} />
                </span>
              <//>
              <div class="row-actions">
                <button class="btn small" onClick=${() => answer(u, true)}>Confirm</button>
                <button class="btn small secondary" onClick=${() => answer(u, false)}>Delete</button>
              </div>
            </li>`
          )}
        </ul>`}
  </div>`;
}

function Highlights({ userId, username }) {
  const { data } = useQuery(`/api/u/${userId}/highlights`, 10 * 60000);
  if (!data?.length) return null;
  return html`<div class="highlights">
    ${data.map(
      (h) => html`<${Link}
        key=${h.id}
        href=${`/u/${encodeURIComponent(username)}/highlights/${h.id}`}
        class="highlight"
        warm=${() => prefetch(`/api/highlight/${h.id}`, 10 * 60000)}
      >
        <${Avatar} src=${h.cover} size=${64} />
        <span>${h.title}</span>
      <//>`
    )}
  </div>`;
}

/* A post changed (like/save): update every copy the app keeps, so the post page,
   Today and your Saved/Liked tabs agree. */
function patchPost(nm) {
  const page = store.get(`/api/p/${nm.id}`)?.data;
  if (page) setData(`/api/p/${nm.id}`, { ...page, liked: nm.liked, likes: nm.likes, saved: nm.saved });
  for (const [which, on] of [["saved", nm.saved], ["liked", nm.liked]]) {
    for (const k of [...store.keys()].filter((k) => k.startsWith(`/api/me/${which}`))) {
      const d = store.get(k).data;
      if (!d?.items) continue;
      const has = d.items.some((t) => t.id === nm.id);
      if (!on && has) setData(k, { ...d, items: d.items.filter((t) => t.id !== nm.id) });
      if (on && !has && k === `/api/me/${which}`) setData(k, { ...d, items: [{ id: nm.id, thumb: nm.items[0]?.img, type: nm.type }, ...d.items] });
    }
  }
}

// Your own profile's counts after you follow, unfollow or approve someone.
function bumpMine(field, by) {
  const k = `/api/u/${encodeURIComponent(me.username)}`;
  const mine = store.get(k)?.data;
  if (mine && mine[field] != null) setData(k, { ...mine, [field]: Math.max(0, mine[field] + by) });
}

async function mute(p, key, what) {
  const before = store.get(key)?.data;
  const rel = { ...p.rel };
  if ("posts" in what) rel.muted_posts = what.posts;
  if ("stories" in what) rel.muted_stories = what.stories;
  setData(key, { ...before, rel });
  try {
    await api(`/api/u/${p.id}/mute`, { method: "POST", body: what });
  } catch (e) {
    setData(key, before);
    toast(e.message);
  }
}

async function follow(p, key, on) {
  const before = store.get(key)?.data;
  const followingNow = on && !p.private;
  const wasFollowing = p.rel.following;
  setData(key, {
    ...p,
    rel: { ...p.rel, following: followingNow, requested: on && p.private },
    followers: p.followers + (followingNow ? 1 : 0) - (!on && p.rel.following ? 1 : 0),
  });
  try {
    const rel = await api(`/api/u/${p.id}/follow`, { method: "POST", body: { follow: on } });
    const cur = store.get(key).data;
    setData(key, { ...cur, rel: { ...cur.rel, ...rel }, can_see: cur.me || !cur.private || rel.following });
    if (rel.following !== wasFollowing) bumpMine("following", rel.following ? 1 : -1);
  } catch (e) {
    setData(key, before);
    toast(e.message);
  }
}

function ProfileActions({ p, k, onUnfollow }) {
  const r = p.rel;
  const openChat = () => {
    const threads = store.get("/api/dm")?.data || [];
    const t = threads.find((t) => !t.group && t.users.length === 1 && t.users[0].id === p.id);
    navigate(t ? `/dm/${t.id}` : `/dm/new/${encodeURIComponent(p.username)}`);
  };
  const answer = async (approve) => {
    const before = store.get(k).data;
    setData(k, { ...before, rel: { ...r, incoming: false, followed_by: approve } });
    const reqs = store.get("/api/requests")?.data;
    const row = reqs?.find((u) => u.id === p.id);
    if (reqs) setData("/api/requests", reqs.filter((u) => u.id !== p.id));
    try {
      await api(`/api/requests/${p.id}`, { method: "POST", body: { approve } });
      if (approve) bumpMine("followers", 1);
    } catch (e) {
      setData(k, before);
      const now = store.get("/api/requests")?.data;
      if (row && now && !now.some((u) => u.id === row.id)) setData("/api/requests", [row, ...now]);
      toast(e.message);
    }
  };
  return html`${r.incoming &&
    html`<div class="incoming">
      <span><b>${p.username}</b> wants to follow you</span>
      <button class="btn small" onClick=${() => answer(true)}>Confirm</button>
      <button class="btn small secondary" onClick=${() => answer(false)}>Delete</button>
    </div>`}
  <div class="actions">
    ${r.following || r.requested
      ? html`<button class="btn secondary" onClick=${onUnfollow}>${r.requested ? "Requested" : "Following"}</button>`
      : html`<button class="btn" onClick=${() => follow(p, k, true)}>${r.followed_by ? "Follow back" : "Follow"}</button>`}
    <button class="btn secondary" onClick=${openChat}>Message</button>
  </div>`;
}

const profileTab = { value: "posts" }; // remembered while the app is open

function Collection({ which }) {
  const [cursors, setCursors] = useState([""]);
  return html`<div class="grid">
    ${cursors.map(
      (c, i) => html`<${CollectionPage} key=${c} which=${which} cursor=${c} last=${i === cursors.length - 1} more=${(n) => setCursors([...cursors, n])} />`
    )}
  </div>`;
}

function CollectionPage({ which, cursor, last, more }) {
  const { data, error, refresh } = useQuery(`/api/me/${which}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`, 60000);
  if (!data) {
    if (error) return html`<div class="grid-wide"><${ErrorView} error=${error} retry=${refresh} /></div>`;
    return Array.from({ length: cursor ? 3 : 9 }, () => html`<span class="tile skeleton"></span>`);
  }
  if (!cursor && !data.items.length)
    return html`<div class="grid-wide"><${Empty}><p class="muted">${which === "saved" ? "Nothing saved yet." : "No liked posts yet."}</p><//></div>`;
  return html`${data.items.map((m) => html`<${Tile} key=${m.id} m=${m} />`)}
  ${last && data.next && html`<button class="grid-wide btn ghost more" onClick=${() => more(data.next)}>Show more</button>`}`;
}

function Grid({ userId, mine = false, empty = false }) {
  const [cursors, setCursors] = useState(() => gridPages.get(userId) || [""]);
  const more = (next) => {
    const list = [...cursors, next];
    gridPages.set(userId, list);
    setCursors(list);
  };
  return html`<div class="grid">
    ${mine &&
    html`<${Link} href="/new" class="tile new-tile" aria-label="New post">
      <span class="new-plus">${I.plusPlain}</span>
      <small>New post</small>
    <//>`}
    ${!empty && cursors.map((c, i) => html`<${GridPage} key=${c} userId=${userId} cursor=${c} last=${i === cursors.length - 1} more=${more} />`)}
  </div>`;
}

function GridPage({ userId, cursor, last, more }) {
  const { data, error, refresh } = useQuery(`/api/u/${userId}/posts${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`, 5 * 60000);
  if (!data) {
    if (error) return html`<div class="grid-wide"><${ErrorView} error=${error} retry=${refresh} /></div>`;
    return Array.from({ length: cursor ? 3 : 9 }, () => html`<span class="tile skeleton"></span>`);
  }
  return html`${data.items.map((m) => html`<${Tile} key=${m.id} m=${m} />`)}
  ${last && data.next && html`<button class="grid-wide btn ghost more" onClick=${() => more(data.next)}>Show more</button>`}`;
}

function Tile({ m }) {
  return html`<${Link}
    href=${`/p/${m.id}`}
    class="tile"
    warm=${() => (peeks.media.set(m.id, m), prefetch(`/api/p/${m.id}`, 60000))}
  >
    <${Img} src=${m.thumb} />
    ${m.type === 8 ? html`<span class="badge">${I.album}</span>` : m.type === 2 ? html`<span class="badge">${I.play}</span>` : null}
  <//>`;
}

/* ==========================================================================
   A single post
   ========================================================================== */

function Post({ id }) {
  const key = `/api/p/${id}`;
  const { data: m, error, refresh } = useQuery(key, 60000);
  const peek = peeks.media.get(id);
  if (!m) {
    return html`<div class="page">
      <${TopBar} title="Post" />
      ${error ? html`<${ErrorView} error=${error} retry=${refresh} />` : html`<div class="media" style="aspect-ratio:1"><${Img} src=${peek?.thumb} class="media-img" /></div><${Spinner} />`}
    </div>`;
  }
  return html`<div class="page">
    <${TopBar} title="Post" />
    <${PostCard} m=${m} onChange=${(nm) => setData(key, nm)} />
  </div>`;
}

/* One post: header, swipeable media, double-tap to like, caption. */
function PostCard({ m, onChange, showTime = false }) {
  const [idx, setIdx] = useState(0);
  const [burst, setBurst] = useState(0);
  const [panel, setPanel] = useState(null); // "comments" | "share"
  const lastTap = useRef(0);

  const latest = useRef(m);
  latest.current = m;
  const change = (patch) => {
    const nm = { ...latest.current, ...patch };
    latest.current = nm;
    onChange(nm);
    patchPost(nm);
  };

  const save = async (on) => {
    change({ saved: on });
    try {
      await api(`/api/p/${m.id}/save`, { method: "POST", body: { media_id: m.media_id, save: on } });
      toast(on ? "Saved" : "Removed from saved");
    } catch (e) {
      change({ saved: !on }); // undo just this, keeping anything else that changed meanwhile
      toast(e.message);
    }
  };

  const like = async (on) => {
    const step = (yes) => ({ liked: yes, likes: latest.current.likes == null ? null : latest.current.likes + (yes ? 1 : -1) });
    change(step(on));
    try {
      await api(`/api/p/${m.id}/like`, { method: "POST", body: { media_id: m.media_id, like: on } });
    } catch (e) {
      change(step(!on));
      toast(e.message);
    }
  };
  const tap = (e) => {
    if (e.target.tagName === "VIDEO") return;
    const now = Date.now();
    if (now - lastTap.current < 300) {
      setBurst((b) => b + 1);
      if (!m.liked) like(true);
    }
    lastTap.current = now;
  };
  const ratio = Math.min(1.91, Math.max(0.8, m.ratio || 1));

  return html`<article class="post">
    <${Link} href=${`/u/${encodeURIComponent(m.user.username)}`} warm=${() => warmUser(m.user)} class="post-user">
      <${Avatar} src=${m.user.pic} size=${32} />
      <${Name} u=${m.user} />
      ${showTime && html`<small class="muted">· ${ago(m.taken_at)}</small>`}
    <//>
    <div class="media" style=${`aspect-ratio:${ratio}`} onClick=${tap}>
      <div class="carousel" onScroll=${(e) => setIdx(Math.round(e.target.scrollLeft / e.target.clientWidth))}>
        ${m.items.map(
          (it, i) => html`<div class="slide" key=${i}>
            ${it.type === 2 && it.video
              ? html`<video src=${it.video} poster=${it.img} controls playsinline preload="none"></video>`
              : html`<${Img} src=${it.img} class="media-img" />`}
          </div>`
        )}
      </div>
      ${burst > 0 && html`<span class="burst" key=${burst}>${I.heart}</span>`}
    </div>
    ${m.items.length > 1 && html`<div class="dots">${m.items.map((_, i) => html`<i class=${i === idx ? "on" : ""}></i>`)}</div>`}
    <div class="post-actions">
      <button class=${`icon like ${m.liked ? "on" : ""}`} onClick=${() => like(!m.liked)}>${I.heart}</button>
      <button class="icon" onClick=${() => setPanel("comments")}>${I.comment}</button>
      <button class="icon" onClick=${() => setPanel("share")}>${I.send}</button>
      <button class=${`icon save ${m.saved ? "on" : ""}`} onClick=${() => save(!m.saved)}>${I.bookmark}</button>
    </div>
    <div class="post-meta">
      ${m.likes != null && html`<b>${count(m.likes)} ${m.likes === 1 ? "like" : "likes"}</b>`}
      ${m.caption && html`<p class="caption"><b>${m.user.username}</b> ${m.caption}</p>`}
      ${m.comments > 0 && html`<button class="link-muted" onClick=${() => setPanel("comments")}>View all ${count(m.comments)} comments</button>`}
      ${!showTime && html`<small class="muted">${new Date(m.taken_at).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}</small>`}
    </div>
    ${panel === "comments" && html`<${Comments} m=${m} onClose=${() => setPanel(null)} />`}
    ${panel === "share" && html`<${ShareSheet} m=${m} onClose=${() => setPanel(null)} />`}
  </article>`;
}

function Comments({ m, onClose }) {
  const [cursors, setCursors] = useState([""]);
  return html`<${Sheet} onClose=${onClose} tall=${true}>
    <div class="sheet-head"><b>Comments</b></div>
    <div class="sheet-scroll">
      ${cursors.map(
        (c, i) => html`<${CommentPage} key=${c} m=${m} cursor=${c} last=${i === cursors.length - 1} more=${(n) => setCursors([...cursors, n])} />`
      )}
    </div>
  <//>`;
}

function CommentPage({ m, cursor, last, more }) {
  const { data, error } = useQuery(`/api/p/${m.id}/comments?media_id=${encodeURIComponent(m.media_id)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, 120000);
  if (!data) return error ? html`<p class="muted small pad">${error.message}</p>` : cursor ? null : html`<${Spinner} />`;
  if (!cursor && !data.items.length) return html`<p class="muted center-text pad">No comments yet.</p>`;
  return html`${data.items.map(
    (c) => html`<div class=${`comment ${c.reply ? "reply" : ""}`} key=${c.id}>
      <${Link} href=${`/u/${encodeURIComponent(c.user.username)}`} warm=${() => warmUser(c.user)}><${Avatar} src=${c.user.pic} size=${32} /><//>
      <div>
        <p><b>${c.user.username}</b> ${c.text}</p>
        <small class="muted">${ago(c.t)}${c.likes ? ` · ${count(c.likes)} like${c.likes === 1 ? "" : "s"}` : ""}</small>
      </div>
    </div>`
  )}
  ${last && data.next && html`<button class="btn ghost more wide" onClick=${() => more(data.next)}>More comments</button>`}`;
}

function ShareSheet({ m, onClose }) {
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [sent, setSent] = useState({});
  useEffect(() => {
    const t = setTimeout(() => setTerm(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  const threads = useQuery("/api/dm", 60000).data || [];
  const found = useQuery(term ? `/api/search?q=${encodeURIComponent(term.toLowerCase())}` : null, 5 * 60000).data || [];
  const video = m.items.length === 1 && m.items[0].type === 2;
  const send = async (target, body) => {
    setSent((x) => ({ ...x, [target]: "sending" }));
    try {
      await api(`/api/p/${m.id}/share`, { method: "POST", body: { media_id: m.media_id, video, ...body } });
      setSent((x) => ({ ...x, [target]: "sent" }));
    } catch (e) {
      setSent((x) => ({ ...x, [target]: null }));
      toast(e.message);
    }
  };
  const row = (key, pic, title, sub, body) => html`<li class="row" key=${key}>
    <span class="row-main"><${Avatar} src=${pic} /><span class="row-text"><b>${title}</b>${sub && html`<small class="muted">${sub}</small>`}</span></span>
    <button class=${`btn small ${sent[key] ? "secondary" : ""}`} disabled=${!!sent[key]} onClick=${() => send(key, body)}>
      ${sent[key] === "sent" ? "Sent" : sent[key] ? "…" : "Send"}
    </button>
  </li>`;
  return html`<${Sheet} onClose=${onClose} tall=${true}>
    <div class="sheet-head"><b>Send to</b></div>
    <div class="field sheet-search">${I.search}<input type="search" placeholder="Search" value=${q} autocapitalize="off" autocorrect="off" onInput=${(e) => setQ(e.target.value)} /></div>
    <ul class="list sheet-scroll">
      ${term
        ? found.map((u) => row(`u${u.id}`, u.pic, u.username, u.name, { user_id: u.id }))
        : threads.map((t) => row(`t${t.id}`, t.users[0]?.pic, t.title, t.group ? `${t.users.length} people` : null, { thread_id: t.id }))}
    </ul>
  <//>`;
}

function CodeRedirect({ code }) {
  const [error, setError] = useState(null);
  useEffect(() => {
    api(`/api/c/${code}`)
      .then((r) => navigate(`/p/${r.id}`, { replace: true }))
      .catch(setError);
  }, [code]);
  return html`<${TopBar} title="Post" />${error ? html`<${ErrorView} error=${error} />` : html`<${Spinner} />`}`;
}

/* ==========================================================================
   Stories: one person at a time. When theirs end, you're back on their profile.
   ========================================================================== */

function Stories({ username, highlight, user, onClose }) {
  const profQ = useQuery(user ? null : `/api/u/${encodeURIComponent(username)}`, 60000).data;
  const prof = user || profQ || peeks.users.get(username.toLowerCase());
  const id = prof?.id;
  const key = highlight ? `/api/highlight/${highlight}` : id ? `/api/u/${id}/stories` : null;
  const q = useQuery(key, highlight ? 10 * 60000 : 60000);
  const items = highlight ? q.data?.items : q.data;
  const error = q.error;
  const [i, setI] = useState(0);
  const [paused, setPaused] = useState(false);
  const [progress, setProgress] = useState(0);
  const [drag, setDrag] = useState(0);
  const [liked, setLiked] = useState({});
  const isLiked = (st) => liked[st.id] ?? !!st.liked;
  const reachedEnd = useRef(false);
  const [replying, setReplying] = useState(false);
  const [reply, setReply] = useState("");
  const seen = useRef(new Map()); // story id -> what Instagram needs to mark it watched
  const video = useRef();
  const elapsed = useRef(0);
  const gesture = useRef(null);
  const cur = items?.[i];

  const sendSeen = () => {
    if (highlight || !seen.current.size) return;
    api("/api/stories/seen", { method: "POST", body: { items: [...seen.current.values()], done: reachedEnd.current } }).catch(() => {});
    seen.current = new Map();
  };
  useEffect(() => sendSeen, []); // the Today timer ending or leaving also closes it
  const close = () => {
    sendSeen();
    if (onClose) onClose(reachedEnd.current);
    else back(`/u/${encodeURIComponent(username)}`);
  };
  const next = () => (items && i < items.length - 1 ? setI(i + 1) : close());
  const prev = () => (i > 0 ? setI(i - 1) : ((elapsed.current = 0), setProgress(0), video.current && (video.current.currentTime = 0)));

  useEffect(() => {
    elapsed.current = 0;
    setProgress(0);
    if (cur?.media_id) seen.current.set(cur.id, { media_id: cur.media_id, taken_at: cur.taken_at });
    if (items && i === items.length - 1) reachedEnd.current = true;
    const n = items?.[i + 1];
    if (n?.img) new Image().src = n.img;
  }, [i, cur?.id]);

  useEffect(() => {
    if (!cur || cur.video || paused) return;
    let last = performance.now();
    let raf;
    const tick = (now) => {
      elapsed.current += now - last;
      last = now;
      const p = elapsed.current / 5000;
      if (p >= 1) return next();
      setProgress(p);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [i, cur?.id, paused]);

  useEffect(() => {
    const v = video.current;
    if (!v) return;
    if (paused) v.pause();
    else
      v.play().catch(() => {
        v.muted = true;
        v.play().catch(() => {});
      });
  }, [paused, cur?.id]);

  const mineStory = prof?.id && prof.id === me.id;
  const likeStory = async () => {
    const on = !isLiked(cur);
    setLiked((x) => ({ ...x, [cur.id]: on }));
    try {
      await api("/api/stories/like", { method: "POST", body: { media_id: cur.media_id, like: on } });
      const sk = `/api/u/${cur.owner}/stories`;
      const list = store.get(sk)?.data;
      if (list) setData(sk, list.map((x) => (x.id === cur.id ? { ...x, liked: on } : x)));
    } catch (e) {
      setLiked((x) => ({ ...x, [cur.id]: !on }));
      toast(e.message);
    }
  };
  const sendReply = async (e) => {
    e.preventDefault();
    const text = reply.trim();
    if (!text) return;
    const story = cur;
    setReply("");
    setReplying(false);
    setPaused(false);
    try {
      const r = await api("/api/stories/reply", {
        method: "POST",
        body: { media_id: story.media_id, owner: story.owner || prof.id, text, video: !!story.video },
      });
      toast(r.linked ? "Sent" : "Sent as a message");
    } catch (err) {
      toast(err.message);
    }
  };

  const onDown = (e) => {
    gesture.current = { x: e.clientX, y: e.clientY, t: Date.now(), hold: setTimeout(() => setPaused(true), 180) };
  };
  const onMove = (e) => {
    const g = gesture.current;
    if (g) setDrag(Math.max(0, e.clientY - g.y));
  };
  const onUp = (e) => {
    const g = gesture.current;
    gesture.current = null;
    if (!g) return;
    clearTimeout(g.hold);
    const dy = e.clientY - g.y;
    setDrag(0);
    if (dy > 90) return close();
    if (Date.now() - g.t > 180) return setPaused(false);
    if (e.clientX < window.innerWidth / 3) prev();
    else next();
  };

  return html`<div class="stories" style=${`transform:translateY(${drag}px);opacity:${1 - drag / 600}`}>
    ${!items
      ? error
        ? html`<${ErrorView} error=${error} />`
        : html`<${Spinner} />`
      : !items.length
      ? html`<div class="empty light"><p>No stories right now.</p><button class="btn secondary" onClick=${close}>Close</button></div>`
      : html`
          <div class="bars">${items.map((_, j) => html`<i><b style=${`width:${j < i ? 100 : j > i ? 0 : progress * 100}%`}></b></i>`)}</div>
          <div class="story-head">
            <${Avatar} src=${prof?.pic} size=${32} />
            <b>${highlight ? q.data?.title || username : username}</b><span>${highlight ? username : ago(cur.taken_at)}</span>
            <button class="icon" onClick=${close}>${I.x}</button>
          </div>
          <div class="story-media" onPointerDown=${onDown} onPointerMove=${onMove} onPointerUp=${onUp} onPointerCancel=${onUp}>
            ${cur.video
              ? html`<video
                  key=${cur.id}
                  ref=${video}
                  src=${cur.video}
                  poster=${cur.img}
                  playsinline
                  autoplay
                  onTimeUpdate=${(e) => e.target.duration && setProgress(e.target.currentTime / e.target.duration)}
                  onEnded=${next}
                ></video>`
              : html`<img key=${cur.id} src=${cur.img} referrerpolicy="no-referrer" alt="" />`}
          </div>
          ${!mineStory &&
          html`<form class=${`story-foot ${replying ? "typing" : ""}`} onSubmit=${sendReply}>
            <input
              placeholder=${`Reply to ${username}…`}
              value=${reply}
              enterkeyhint="send"
              onFocus=${() => (setReplying(true), setPaused(true))}
              onBlur=${() => !reply.trim() && (setReplying(false), setPaused(false))}
              onInput=${(e) => setReply(e.target.value)}
            />
            ${replying
              ? html`<button class="send-reply" disabled=${!reply.trim()}>Send</button>`
              : html`<button type="button" class=${`icon story-like ${isLiked(cur) ? "on" : ""}`} onClick=${likeStory}>${I.heart}</button>`}
          </form>`}
        `}
  </div>`;
}

/* ==========================================================================
   Today: posts and stories from people you follow. Five minutes, once a day,
   enforced by the server, so closing the app doesn't reset it.
   ========================================================================== */

function clock(ms) {
  const t = Math.ceil(ms / 1000);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

function untilText(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

/* Only this re-renders every half second, not the whole feed. */
function Countdown({ deadline, onEnd }) {
  const [left, setLeft] = useState(Math.max(0, deadline - Date.now()));
  const ended = useRef(onEnd);
  ended.current = onEnd;
  useEffect(() => {
    const tick = () => {
      const l = Math.max(0, deadline - Date.now());
      setLeft(l);
      if (l <= 0) ended.current();
    };
    tick();
    const t = setInterval(tick, 500);
    return () => clearInterval(t);
  }, [deadline]);
  return html`<span class=${`timer ${left < 30000 ? "low" : ""}`}>${clock(left)}</span>`;
}

// What Today showed, so coming back from a profile or a post doesn't start over
// (and doesn't ask the server for the same pages again).
let todayKeep = null;

function Today() {
  // Kept for the same day, and while the timer (when there is one) hasn't run out.
  const kept = todayKeep && todayKeep.day === new Date().toDateString() && (todayKeep.deadline == null || todayKeep.deadline > Date.now()) ? todayKeep : null;
  const [st, setSt] = useState(kept?.st ?? null);
  const [deadline, setDeadline] = useState(kept?.deadline ?? null);
  const [stories, setStories] = useState(kept?.stories ?? null);
  const [posts, setPosts] = useState(kept?.posts ?? null); // Instagram's feed, a page at a time
  const [next, setNext] = useState(kept?.next ?? null);
  const [loading, setLoading] = useState(false);
  const [nearEnd, setNearEnd] = useState(false);
  const [spent, setSpent] = useState(kept?.spent ?? false);
  const [viewer, setViewer] = useState(null);
  const [error, setError] = useState(null);
  const sentinel = useRef();
  const shown = useRef(new Set(kept?.shown ?? [])); // feed posts already on screen
  const lastLoad = useRef(0);
  const mounted = useRef(true);
  useEffect(() => () => (mounted.current = false), []);

  const apply = (x) => {
    setSt(x);
    setDeadline(x.status === "open" && x.remaining != null ? Date.now() + x.remaining * 1000 : null);
  };
  const end = useCallback(() => {
    todayKeep = null;
    setViewer(null);
    setPosts(null);
    setStories(null);
    setDeadline(null);
    setSt((x) => ({ ...x, status: "done" }));
    api("/api/daily").then(apply).catch(() => {});
  }, []);
  const fail = (e) => (e.code === "daily_over" ? end() : setError(e));

  useEffect(() => {
    if (!kept) api("/api/daily").then(apply).catch(setError);
  }, []);

  useEffect(() => {
    if (st?.status === "open") todayKeep = { day: new Date().toDateString(), st, deadline, stories, posts, next, spent, shown: [...shown.current] };
  }, [st, deadline, stories, posts, next, spent]);

  const load = (cursor) => {
    setLoading(true);
    lastLoad.current = Date.now();
    return api(`/api/daily/feed${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`)
      .then((r) => {
        if (!mounted.current) return; // left meanwhile: the page stays cached for when you're back
        const fresh = r.items.filter((m) => !shown.current.has(m.id));
        fresh.forEach((m) => shown.current.add(m.id));
        setPosts((p) => [...(p || []), ...fresh]);
        if (r.spent) setSpent(true);
        // Never ask again on our own after a page that added nothing: the
        // sentinel would still be on screen and we'd loop.
        setNext(fresh.length || !cursor ? r.next : null);
      })
      .catch((e) => (setNext(null), fail(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (st?.status === "open" && posts === null) load(null);
  }, [st?.status]);
  // The tray after the first posts: its slower part shouldn't hold them up.
  useEffect(() => {
    if (st?.status === "open" && stories === null) api("/api/daily/stories").then((x) => mounted.current && setStories(x)).catch(() => mounted.current && setStories([]));
  }, [st?.status]);

  // Load the next page a little before you reach the bottom, at most every 2.5s.
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setNearEnd(e.isIntersecting), { rootMargin: "1200px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [posts !== null]);
  useEffect(() => {
    if (!nearEnd || !next || loading) return;
    const wait = 2500 - (Date.now() - lastLoad.current);
    const t = setTimeout(() => load(next), Math.max(0, wait));
    return () => clearTimeout(t);
  }, [nearEnd, next, loading]);

  const ptr = usePullToRefresh(
    useCallback(async () => {
      api("/api/daily/stories?fresh=1").then(setStories).catch(() => {});
      try {
        const r = await api("/api/daily/feed?fresh=1");
        if (r.spent) {
          setSpent(true);
          toast("That's enough feed for today");
          return;
        }
        shown.current = new Set(r.items.map((m) => m.id));
        lastLoad.current = Date.now();
        setPosts(r.items);
        setNext(r.next);
      } catch (e) {
        fail(e);
      }
    }, [])
  );

  const start = () => api("/api/daily/start", { method: "POST" }).then(apply).catch(setError);
  const update = useCallback((nm) => setPosts((p) => p && p.map((x) => (x.id === nm.id ? nm : x))), []);
  const minutes = st?.minutes || 8;

  if (error) return html`<${TopBar} left=${html`<span></span>`} title="Today" /><${ErrorView} error=${error} />`;
  if (!st) return html`<${TopBar} left=${html`<span></span>`} title="Today" /><${Spinner} />`;

  if (st.status === "ready")
    return html`<div class="page">
      <${TopBar} left=${html`<span></span>`} title="Today" />
      <div class="today-gate">
        <div class="today-icon">${I.sun}</div>
        <h2>Your ${minutes} minutes</h2>
        <p class="muted">Your feed and stories. Once a day, then it closes until tomorrow.</p>
        <button class="btn" onClick=${start}>Start</button>
        <small class="muted">The timer keeps running if you leave.</small>
      </div>
    </div>`;

  if (st.status === "done")
    return html`<div class="page">
      <${TopBar} left=${html`<span></span>`} title="Today" />
      <div class="today-gate">
        <div class="today-icon done">${I.sun}</div>
        <h2>See you tomorrow</h2>
        <p class="muted">Today's ${minutes} minutes are used. Opens again in ${untilText(st.resets_in)}.</p>
      </div>
    </div>`;

  const card = (m) => html`<${PostCard} key=${m.id} m=${m} onChange=${update} showTime=${true} />`;
  return html`<div class="page">
    <${TopBar} left=${html`<span></span>`} title="Today" right=${deadline && html`<${Countdown} deadline=${deadline} onEnd=${end} />`} />
    ${ptr}
    ${stories?.length > 0 &&
    html`<div class="tray">
      ${stories.map(
        (u) => html`<button class="tray-item" key=${u.id} onClick=${() => setViewer(u)}>
          <span class=${`avatar ${u.unseen ? "ring" : "ring seen"}`} style="--s:64px"><${Img} src=${u.pic} /></span>
          <span>${u.username}</span>
        </button>`
      )}
    </div>`}
    ${posts === null
      ? html`<${Spinner} />`
      : !posts.length && !next && !loading
      ? html`<${Empty}><p class="muted">${spent ? "That's enough feed for today." : "Nothing in your feed right now."}</p><//>`
      : posts.map(card)}
    <div ref=${sentinel} class="sentinel"></div>
    ${posts !== null && (next || loading)
      ? html`<${Spinner} />`
      : posts?.length > 0 && html`<p class="caught-up muted">${I.check} ${spent ? "That's enough feed for today" : "That's everything for now. Pull down for new posts."}</p>`}
    ${viewer &&
    html`<${Stories}
      key=${viewer.id}
      username=${viewer.username}
      user=${viewer}
      onClose=${(watchedAll) => {
        setStories((list) => {
          if (!watchedAll) return list; // stopped partway: still has stories you haven't seen
          const marked = list.map((x) => (x.id === viewer.id ? { ...x, unseen: false } : x));
          return [...marked.filter((x) => x.unseen), ...marked.filter((x) => !x.unseen)];
        });
        setViewer(null);
      }}
    />`}
  </div>`;
}

/* ==========================================================================
   Messages
   ========================================================================== */

function preview(m) {
  if (!m) return "";
  const text = m.text || m.note || (m.share ? `Sent a ${m.share.label.toLowerCase().includes("reel") ? "reel" : "post"}` : m.media ? "Sent an attachment" : "");
  return (m.mine ? "You: " : "") + text;
}

function Inbox({ requests }) {
  const key = requests ? "/api/dm?requests=true" : "/api/dm";
  const { data, error, loading, refresh } = useQuery(key, 10000);
  const ptr = usePullToRefresh(refresh);
  useEffect(() => {
    const f = () => document.visibilityState === "visible" && refresh();
    document.addEventListener("visibilitychange", f);
    return () => document.removeEventListener("visibilitychange", f);
  }, [refresh]);

  return html`<div class="page">
    <${TopBar}
      left=${requests ? undefined : html`<span></span>`}
      title=${requests ? "Requests" : "Messages"}
      right=${!requests && html`<${Link} href="/dm/requests" class="link">Requests<//>`}
    />
    ${ptr}
    ${!data
      ? error
        ? html`<${ErrorView} error=${error} retry=${refresh} />`
        : html`<${Spinner} />`
      : !data.length
      ? html`<${Empty}><p class="muted">${requests ? "No message requests." : "No messages yet."}</p><//>`
      : html`<ul class="list">
          ${data.map(
            (t) => html`<li class=${`row ${t.unread ? "unread" : ""}`} key=${t.id}>
              <${Link}
                href=${`/dm/${t.id}`}
                class="row-main"
                warm=${() => (peeks.threads.set(t.id, t), prefetch(`/api/dm/${t.id}?n=20`, 4000))}
              >
                <span class=${`avatars n${Math.min(t.users.length, 2)}`}>${t.users.slice(0, 2).map((u) => html`<${Avatar} src=${u.pic} size=${t.users.length > 1 ? 36 : 52} />`)}</span>
                <span class="row-text">
                  <span class="title">${t.title}</span>
                  <small class="muted">${preview(t.last)} · ${ago(t.at)}</small>
                </span>
                ${t.unread && html`<i class="dot"></i>`}
              <//>
            </li>`
          )}
        </ul>`}
    ${loading && data && html`<div class="spinner tiny floating"></div>`}
  </div>`;
}

function Bubble({ m }) {
  const share = m.share;
  return html`<div class="bubble">
    ${share
      ? html`<div class="share">
          <${Img} src=${share.img} />
          <small>${share.label}${share.user ? ` · @${share.user}` : ""}</small>
        </div>`
      : m.media
      ? m.media.video
        ? html`<video src=${m.media.video} poster=${m.media.img} controls playsinline preload="none"></video>`
        : m.media.audio
        ? html`<audio src=${m.media.audio} controls preload="none"></audio>`
        : html`<${Img} src=${m.media.img} class="media-img" />`
      : null}
    ${m.note && html`<em class="note">${m.note}</em>`}
    ${m.text && html`<span class="text">${m.text}</span>`}
  </div>`;
}

const REACTIONS = ["❤️", "😂", "😮", "😢", "😡", "👍"];

/* Double-tap a message to ❤️ it; press and hold to pick another reaction. */
function Reactable({ m, onReact, children }) {
  const [picker, setPicker] = useState(false);
  const press = useRef(null);
  const lastTap = useRef(0);
  const single = useRef(null);
  const canReact = onReact && !String(m.id).startsWith("local-");
  // A shared post opens on a single tap, but only once we know it isn't a double tap.
  const href = m.share ? (m.share.media ? `/p/${m.share.media}` : m.share.code ? `/c/${m.share.code}` : null) : null;
  const down = () => {
    if (m.share?.media) prefetch(`/api/p/${m.share.media}`, 60000);
    if (!canReact) return;
    press.current = setTimeout(() => ((press.current = null), setPicker(true), navigator.vibrate?.(10)), 450);
  };
  const up = () => {
    if (canReact && !press.current) return; // that was a long press
    clearTimeout(press.current);
    press.current = null;
    const now = Date.now();
    if (canReact && now - lastTap.current < 280) {
      clearTimeout(single.current);
      single.current = null;
      lastTap.current = 0;
      onReact(m, "❤️");
      return;
    }
    lastTap.current = now;
    if (href) single.current = setTimeout(() => ((single.current = null), navigate(href)), canReact ? 280 : 0);
  };
  const cancel = () => press.current && (clearTimeout(press.current), (press.current = null));
  const counts = {};
  (m.reactions || []).forEach((r) => (counts[r.emoji] = (counts[r.emoji] || 0) + 1));
  const mine = (m.reactions || []).find((r) => r.mine)?.emoji;
  return html`<div class="reactable" onPointerDown=${down} onPointerUp=${up} onPointerLeave=${cancel} onPointerCancel=${cancel} onContextMenu=${(e) => e.preventDefault()}>
    ${children}
    ${Object.keys(counts).length > 0 &&
    html`<div class="reactions">${Object.entries(counts).map(([e, n]) => html`<span>${e}${n > 1 ? n : ""}</span>`)}</div>`}
    ${picker &&
    html`<div class="react-cover" onPointerDown=${(e) => (e.stopPropagation(), setPicker(false))}></div>
      <div class="react-bar">
        ${REACTIONS.map(
          (e) => html`<button class=${mine === e ? "on" : ""} onPointerDown=${(ev) => ev.stopPropagation()} onClick=${() => (setPicker(false), onReact(m, e))}>${e}</button>`
        )}
      </div>`}
  </div>`;
}

function Messages({ messages, users, group, onReact }) {
  const names = Object.fromEntries(users.map((u) => [u.id, u.username]));
  let prevT = 0;
  let prevUser = null;
  return messages.map((m) => {
    const t = new Date(m.t).getTime();
    const gap = t - prevT > 45 * 60000;
    const showName = group && !m.mine && (gap || prevUser !== m.user_id);
    prevT = t;
    prevUser = m.user_id;
    return html`
      ${gap && html`<div class="when">${new Date(m.t).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}</div>`}
      <div class=${`msg ${m.mine ? "mine" : ""} ${m.sending ? "sending" : ""} ${m.failed ? "failed" : ""}`} key=${m.id}>
        ${showName && html`<small class="muted">${names[m.user_id] || ""}</small>`}
        <${Reactable} m=${m} onReact=${onReact}><${Bubble} m=${m} /><//>
        ${m.failed && html`<small class="error">Not sent</small>`}
      </div>
    `;
  });
}

function Composer({ onSend, placeholder = "Message…", autofocus = false }) {
  const [text, setText] = useState("");
  const ref = useRef();
  const grow = () => {
    const el = ref.current;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };
  const send = (e) => {
    e.preventDefault();
    if (!text.trim()) return;
    onSend(text.trim());
    setText("");
    requestAnimationFrame(grow);
    ref.current.focus();
  };
  return html`<form class="composer" onSubmit=${send}>
    <textarea
      ref=${ref}
      rows="1"
      placeholder=${placeholder}
      value=${text}
      autofocus=${autofocus}
      onInput=${(e) => (setText(e.target.value), grow())}
    ></textarea>
    <button class="link send" disabled=${!text.trim()}>Send</button>
  </form>`;
}

function Thread({ id }) {
  const [n, setN] = useState(20);
  const key = `/api/dm/${id}?n=${n}`;
  const { data, error, refresh } = useQuery(key, 4000);
  const peek = peeks.threads.get(id);
  const [outbox, setOutbox] = useState([]);
  const active = useRef(Date.now());
  const list = useRef();
  const stick = useRef(true);

  // Light polling while you're in the conversation: every 10s, slowing to once a
  // minute while nothing new arrives, and resting 2 minutes after you last did
  // anything. Not while older messages are loaded: each poll would refetch them all.
  useEffect(() => {
    if (n !== 20) return;
    let gap = 10000;
    let last = store.get(key)?.data?.messages?.at(-1)?.id;
    let t;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      if (document.visibilityState === "visible" && Date.now() - active.current < 2 * 60000) {
        await refresh();
        const now = store.get(key)?.data?.messages?.at(-1)?.id;
        gap = now !== last || Date.now() - active.current < 15000 ? 10000 : Math.min(gap * 2, 60000);
        last = now;
      }
      if (!stopped) t = setTimeout(tick, gap);
    };
    t = setTimeout(tick, gap);
    return () => ((stopped = true), clearTimeout(t));
  }, [refresh, n]);

  const msgs = data ? [...data.messages, ...outbox.filter((o) => !data.messages.some((m) => m.id === o.serverId))] : outbox;
  useEffect(() => {
    const inbox = store.get("/api/dm")?.data;
    if (data && inbox?.some((t) => t.id === id && t.unread)) setData("/api/dm", inbox.map((t) => (t.id === id ? { ...t, unread: false } : t)));
  }, [!!data, id]);
  useLayoutEffect(() => {
    if (stick.current) window.scrollTo(0, document.body.scrollHeight);
  }, [msgs.length, data?.n]);
  useEffect(() => {
    const f = () => (stick.current = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80);
    window.addEventListener("scroll", f, { passive: true });
    return () => window.removeEventListener("scroll", f);
  }, []);

  const send = async (text, retryOf) => {
    active.current = Date.now();
    stick.current = true;
    const local = retryOf || { id: `local-${Date.now()}`, mine: true, text, t: new Date().toISOString(), kind: "text" };
    setOutbox((o) => [...o.filter((x) => x.id !== local.id), { ...local, sending: true, failed: false }]);
    try {
      const sent = await api(`/api/dm/${id}/send`, { method: "POST", body: { text: local.text, accept: !!data?.pending } });
      setOutbox((o) => o.map((x) => (x.id === local.id ? { ...x, sending: false, serverId: sent.id } : x)));
      dropData("/api/dm");
      load(key).catch(() => {});
    } catch (e) {
      setOutbox((o) => o.map((x) => (x.id === local.id ? { ...x, sending: false, failed: true } : x)));
      toast(e.message);
    }
  };

  const react = async (m, emoji) => {
    active.current = Date.now();
    const before = store.get(key)?.data;
    if (!before) return;
    const mine = (m.reactions || []).find((r) => r.mine);
    const remove = mine?.emoji === emoji;
    const reactions = (m.reactions || []).filter((r) => !r.mine).concat(remove ? [] : [{ emoji, mine: true }]);
    setData(key, { ...before, messages: before.messages.map((x) => (x.id === m.id ? { ...x, reactions } : x)) });
    try {
      await api(`/api/dm/${id}/react`, { method: "POST", body: { message_id: m.id, emoji, remove, context: m.context } });
    } catch (e) {
      setData(key, before);
      toast(e.message);
    }
  };

  const t = data || peek;
  const one = t && !t.group && t.users.length === 1 ? t.users[0] : null;
  return html`<div class="page thread" ref=${list}>
    <${TopBar}
      title=${t
        ? one
          ? html`<${Link} href=${`/u/${encodeURIComponent(one.username)}`} warm=${() => warmUser(one)} class="tb-user"><${Avatar} src=${one.pic} size=${28} /><span>${t.title}</span><//>`
          : t.title
        : ""}
    />
    ${data?.pending && html`<div class="banner">Message request. Replying accepts it.</div>`}
    ${!data && error && html`<${ErrorView} error=${error} retry=${refresh} />`}
    ${!data && !error && html`<${Spinner} />`}
    <div class="messages">
      ${data && data.messages.length >= n && n < 200 && html`<button class="link small older" onClick=${() => ((stick.current = false), setN(n + 20))}>Load older</button>`}
      ${data && html`<${Messages} messages=${msgs} users=${data.users} group=${data.group} onReact=${react} />`}
    </div>
    <div class="composer-wrap" onClick=${() => (active.current = Date.now())}>
      <${Composer} onSend=${(text) => send(text)} />
    </div>
    ${outbox.some((o) => o.failed) &&
    html`<button class="retry link small" onClick=${() => outbox.filter((o) => o.failed).forEach((o) => send(o.text, o))}>Retry failed</button>`}
  </div>`;
}

function NewThread({ username }) {
  const { data: p } = useQuery(`/api/u/${encodeURIComponent(username)}`, 60000);
  const u = p || peeks.users.get(username.toLowerCase());
  const [busy, setBusy] = useState(false);
  const send = async (text) => {
    if (!u?.id) return;
    setBusy(true);
    try {
      const r = await api(`/api/dm/to/${u.id}`, { method: "POST", body: { text } });
      dropData("/api/dm");
      navigate(r.thread_id ? `/dm/${r.thread_id}` : "/dm", { replace: true });
    } catch (e) {
      toast(e.message);
      setBusy(false);
    }
  };
  return html`<div class="page thread">
    <${TopBar} title=${u ? html`<span class="tb-user"><${Avatar} src=${u.pic} size=${28} /><span>${u.username}</span></span>` : username} />
    <${Empty}>${u && html`<${Avatar} src=${u.pic} size=${72} /><b>${u.name || u.username}</b>`}<p class="muted">${busy ? "Sending…" : "Say hi."}</p><//>
    <div class="composer-wrap"><${Composer} onSend=${send} autofocus=${true} /></div>
  </div>`;
}

/* ==========================================================================
   Posting
   ========================================================================== */

function NewPost() {
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState(null);
  const [where, setWhere] = useState("feed");
  const [caption, setCaption] = useState("");
  const [progress, setProgress] = useState(null);
  const input = useRef();

  const pick = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (url) URL.revokeObjectURL(url);
    setFile(f);
    setUrl(URL.createObjectURL(f));
  };

  const share = () => {
    const form = new FormData();
    form.append("photo", file);
    form.append("caption", caption);
    form.append("where", where);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/post");
    xhr.upload.onprogress = (e) => e.lengthComputable && setProgress(Math.min(0.9, (e.loaded / e.total) * 0.9));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        toast(where === "story" ? "Added to your story" : "Posted");
        dropData(`/api/u/${me.id}`);
        dropData(`/api/u/${encodeURIComponent(me.username)}`);
        gridPages.delete(me.id);
        navigate("/me", { replace: true });
      } else {
        let msg = "Couldn't post.";
        try {
          msg = JSON.parse(xhr.responseText).message || msg;
        } catch {}
        toast(msg);
        setProgress(null);
      }
    };
    xhr.onerror = () => (toast("No connection."), setProgress(null));
    xhr.send(form);
    setProgress(0.02);
  };

  return html`<div class="page">
    <${TopBar} title="New" right=${file && html`<button class="link" disabled=${progress != null} onClick=${share}>Share</button>`} />
    <div class="segmented">
      <button class=${where === "feed" ? "on" : ""} onClick=${() => setWhere("feed")}>Post</button>
      <button class=${where === "story" ? "on" : ""} onClick=${() => setWhere("story")}>Story</button>
    </div>
    <div class=${`picker ${where}`} onClick=${() => progress == null && input.current.click()}>
      ${url ? html`<img src=${url} alt="" />` : html`<div class="picker-empty">${I.plus}<span>Choose a photo</span></div>`}
      ${progress != null && html`<div class="progress"><b style=${`width:${progress * 100}%`}></b><span>${progress < 0.9 ? "Uploading…" : "Posting…"}</span></div>`}
    </div>
    <input ref=${input} type="file" accept="image/*" hidden onChange=${pick} />
    ${where === "feed" &&
    html`<textarea class="caption-input" rows="3" placeholder="Write a caption…" value=${caption} onInput=${(e) => setCaption(e.target.value)}></textarea>`}
    <p class="muted small pad">${where === "feed" ? "Very tall or wide photos are cropped to fit Instagram's limits." : "Fills the screen, like Instagram's story camera."}</p>
  </div>`;
}

/* ==========================================================================
   Settings
   ========================================================================== */

function Settings({ status, refreshStatus }) {
  const [confirm, setConfirm] = useState(false);
  const lock = async () => {
    await api("/api/lock", { method: "POST" }).catch(() => {});
    localStorage.removeItem(CACHE_KEY);
    store.clear();
    refreshStatus();
  };
  const disconnect = async () => {
    await api("/api/disconnect", { method: "POST" }).catch(() => {});
    localStorage.removeItem(CACHE_KEY);
    localStorage.removeItem(RECENT_KEY);
    store.clear();
    refreshStatus();
  };
  return html`<div class="page">
    <${TopBar} title="Settings" />
    <ul class="list settings">
      <li class="row"><span>Account</span><span class="muted">@${status.username}</span></li>
      ${status.cooldown && html`<li class="row"><span>Paused</span><span class="muted">${status.cooldown.reason}</span></li>`}
      <li class="row"><button class="link" onClick=${lock}>Lock this phone</button></li>
      <li class="row"><button class="link danger" onClick=${() => setConfirm(true)}>Disconnect Instagram</button></li>
    </ul>
    <p class="muted small pad">
      Left out on purpose: the feed, Reels, Explore, the stories tray, suggestions and notifications. There's no switch for
      them.
    </p>
    ${confirm &&
    html`<${Sheet} onClose=${() => setConfirm(false)}>
      <p>Disconnect <b>@${status.username}</b>? You'll need your password to connect again.</p>
      <button class="btn danger" onClick=${disconnect}>Disconnect</button>
      <button class="btn secondary" onClick=${() => setConfirm(false)}>Cancel</button>
    <//>`}
  </div>`;
}

/* ==========================================================================
   Shell
   ========================================================================== */

function route(path, search, status, refreshStatus) {
  let m;
  const d = decodeURIComponent;
  if (path === "/") return { tab: "search", el: html`<${Search} initial=${new URLSearchParams(search).get("q") || ""} />` };
  if ((m = path.match(/^\/u\/([^/]+)\/stories$/))) return { bare: true, el: html`<${Stories} username=${d(m[1])} />` };
  if ((m = path.match(/^\/u\/([^/]+)\/highlights\/(\d+)$/)))
    return { bare: true, el: html`<${Stories} key=${m[2]} username=${d(m[1])} highlight=${m[2]} />` };
  if ((m = path.match(/^\/u\/([^/]+)$/))) return { tab: d(m[1]) === me.username ? "me" : "search", el: html`<${Profile} username=${d(m[1])} />` };
  if ((m = path.match(/^\/p\/(\d+)$/))) return { tab: "search", el: html`<${Post} id=${m[1]} />` };
  if ((m = path.match(/^\/c\/([\w-]+)$/))) return { tab: "dm", el: html`<${CodeRedirect} code=${m[1]} />` };
  if (path === "/dm") return { tab: "dm", el: html`<${Inbox} requests=${false} />` };
  if (path === "/dm/requests") return { tab: "dm", el: html`<${Inbox} requests=${true} />` };
  if ((m = path.match(/^\/dm\/new\/([^/]+)$/))) return { bare: true, el: html`<${NewThread} username=${d(m[1])} />` };
  if ((m = path.match(/^\/dm\/(\d+)$/))) return { bare: true, el: html`<${Thread} id=${m[1]} />` };
  if (path === "/new") return { tab: "me", el: html`<${NewPost} />` };
  if (path === "/today") return { tab: "today", el: html`<${Today} />` };
  if (path === "/me") return { tab: "me", el: html`<${Profile} username=${me.username} />` };
  if (path === "/requests") return { tab: "me", el: html`<${Requests} />` };
  if (path === "/settings") return { tab: "me", el: html`<${Settings} status=${status} refreshStatus=${refreshStatus} />` };
  return { tab: "search", el: html`<${TopBar} title="" /><${Empty}><p class="muted">Nothing here.</p><//>` };
}

function TabBar({ tab }) {
  const go = (href, name) => (e) => {
    e.preventDefault();
    if (tab === name && here() === href) window.scrollTo({ top: 0, behavior: "smooth" });
    else navigate(href);
  };
  const meUser = peeks.users.get(me.username.toLowerCase()) || store.get(`/api/u/${encodeURIComponent(me.username)}`)?.data;
  return html`<nav class="tabbar">
    <a href="/today" class=${tab === "today" ? "on" : ""} onClick=${go("/today", "today")}>${I.sun}</a>
    <a href="/" class=${tab === "search" ? "on" : ""} onClick=${go("/", "search")}>${I.search}</a>
    <a href="/dm" class=${tab === "dm" ? "on" : ""} onClick=${go("/dm", "dm")}>${I.send}</a>
    <a href="/me" class=${tab === "me" ? "on" : ""} onClick=${go("/me", "me")}>
      ${meUser?.pic ? html`<${Avatar} src=${meUser.pic} size=${26} />` : html`<span class="me-dot"></span>`}
    </a>
  </nav>`;
}

/* Swipe from the left edge to go back, since home-screen apps don't get Safari's gesture. */
function useEdgeSwipe(enabled) {
  const [dx, setDx] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let start = null;
    const down = (e) => {
      const t = e.touches[0];
      start = t.clientX < 24 ? { x: t.clientX, y: t.clientY } : null;
    };
    const move = (e) => {
      if (!start) return;
      const t = e.touches[0];
      if (Math.abs(t.clientY - start.y) > Math.abs(t.clientX - start.x) + 10) return (start = null), setDx(0);
      setDx(Math.max(0, t.clientX - start.x));
    };
    const up = (e) => {
      if (!start) return;
      const dist = e.changedTouches[0].clientX - start.x;
      start = null;
      setDx(0);
      if (dist > 90) back();
    };
    window.addEventListener("touchstart", down, { passive: true });
    window.addEventListener("touchmove", move, { passive: true });
    window.addEventListener("touchend", up);
    return () => {
      window.removeEventListener("touchstart", down);
      window.removeEventListener("touchmove", move);
      window.removeEventListener("touchend", up);
    };
  }, [enabled]);
  return dx;
}

function Main({ status, refreshStatus }) {
  me = { username: status.username, id: status.user_id };
  const r = useRoute();
  const { tab, bare, el } = route(r.path, r.search, status, refreshStatus);
  const isRoot = ["/", "/today", "/dm", "/me"].includes(r.path);
  const dx = useEdgeSwipe(!isRoot);
  const [online, setOnline] = useState(navigator.onLine);

  useEffect(() => {
    prefetch(`/api/u/${encodeURIComponent(status.username)}`, 10 * 60000);
    prefetch("/api/dm", 30000);
  }, [status.username]);

  useEffect(() => {
    const f = () => setOnline(navigator.onLine);
    window.addEventListener("online", f);
    window.addEventListener("offline", f);
    return () => (window.removeEventListener("online", f), window.removeEventListener("offline", f));
  }, []);

  useLayoutEffect(() => {
    const y = r.dir === "pop" ? history.state?.scroll || 0 : 0;
    window.scrollTo(0, y);
    requestAnimationFrame(() => window.scrollTo(0, y));
  }, [r.n]);

  return html`
    ${!online && html`<div class="banner offline">Offline. Showing what was saved.</div>`}
    ${status.cooldown && !status.cooldown.writes_only && html`<div class="banner">${status.cooldown.reason}. Pausing for a bit.</div>`}
    <div
      class=${`screen ${r.dir === "push" ? "enter" : r.dir === "pop" ? "return" : ""} ${bare ? "bare" : ""}`}
      key=${r.path}
      style=${dx ? `transform:translateX(${dx}px);transition:none` : ""}
    >
      ${el}
    </div>
    ${!bare && html`<${TabBar} tab=${tab} />`}
    <${Toasts} />
  `;
}

function App() {
  const [status, setStatus] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem("sg-status"));
    } catch {
      return null;
    }
  });
  const refreshStatus = useCallback(async () => {
    const s = await api("/api/status");
    localStorage.setItem("sg-status", JSON.stringify(s));
    setStatus(s);
    return s;
  }, []);
  useEffect(() => {
    onAuthProblem = () => refreshStatus().catch(() => {});
    refreshStatus().catch(() => {});
    const f = () => document.visibilityState === "visible" && refreshStatus().catch(() => {});
    document.addEventListener("visibilitychange", f);
    return () => document.removeEventListener("visibilitychange", f);
  }, []);

  if (!status) return html`<div class="boot"><${Spinner} /></div>`;
  if (!status.unlocked) return html`<${Unlock} done=${refreshStatus} />`;
  if (!status.connected) return html`<${Connect} done=${refreshStatus} />`;
  return html`<${Main} status=${status} refreshStatus=${refreshStatus} />`;
}

render(html`<${App} />`, document.getElementById("app"));

if ("serviceWorker" in navigator && location.protocol === "https:") {
  // When a new version takes over, reload once so you're never a version behind.
  const hadController = !!navigator.serviceWorker.controller;
  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController && !reloaded) (reloaded = true), location.reload();
  });
  navigator.serviceWorker.register("/sw.js");
}
