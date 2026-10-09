/* Nishpaksh reader features (owner, Oct 9 2026). Working, deliberately plain: the owner designs them later.
   Accounts (a name gives a login id; skip = a guest on this device), follows for notifications (sections at any
   level, states, people, single stories, the daily recap), Web Push, the inbox, audio on request with each sentence
   lit in its own colour while it is read, videos, sorting, filters, search, read later, a share card, the recap.
   Follows only notify: they never reorder or filter the front page (owner). The page (index.html) gives window.NP. */
(() => {
"use strict";
const NP = window.NP;
if (!NP) return;
const SB = "https://mmjojuccdgmmiwyzypvh.supabase.co";
const KEY = "sb_publishable_0Gex7dG16My6MPvW2M1Jsg_raI1lqfS";
const FN = `${SB}/functions/v1/account`;
const VAPID = "BFEdVPEJCsATLjoBAx5aVqtwreIZuNadvNy6-5MdcM9blblHfBVHOd0aegFs5NVywQIJlzU4R4oI9fmUVi0PY5U";
const esc = NP.esc, $ = id => document.getElementById(id);

const STATES = {"andhra-pradesh": ["Andhra Pradesh", "आंध्र प्रदेश"], "arunachal-pradesh": ["Arunachal Pradesh", "अरुणाचल प्रदेश"], "assam": ["Assam", "असम"], "bihar": ["Bihar", "बिहार"], "chhattisgarh": ["Chhattisgarh", "छत्तीसगढ़"], "goa": ["Goa", "गोवा"], "gujarat": ["Gujarat", "गुजरात"], "haryana": ["Haryana", "हरियाणा"], "himachal-pradesh": ["Himachal Pradesh", "हिमाचल प्रदेश"], "jharkhand": ["Jharkhand", "झारखंड"], "karnataka": ["Karnataka", "कर्नाटक"], "kerala": ["Kerala", "केरल"], "madhya-pradesh": ["Madhya Pradesh", "मध्य प्रदेश"], "maharashtra": ["Maharashtra", "महाराष्ट्र"], "manipur": ["Manipur", "मणिपुर"], "meghalaya": ["Meghalaya", "मेघालय"], "mizoram": ["Mizoram", "मिज़ोरम"], "nagaland": ["Nagaland", "नगालैंड"], "odisha": ["Odisha", "ओडिशा"], "punjab": ["Punjab", "पंजाब"], "rajasthan": ["Rajasthan", "राजस्थान"], "sikkim": ["Sikkim", "सिक्किम"], "tamil-nadu": ["Tamil Nadu", "तमिलनाडु"], "telangana": ["Telangana", "तेलंगाना"], "tripura": ["Tripura", "त्रिपुरा"], "uttar-pradesh": ["Uttar Pradesh", "उत्तर प्रदेश"], "uttarakhand": ["Uttarakhand", "उत्तराखंड"], "west-bengal": ["West Bengal", "पश्चिम बंगाल"], "andaman-nicobar": ["Andaman and Nicobar Islands", "अंडमान और निकोबार"], "chandigarh": ["Chandigarh", "चंडीगढ़"], "dadra-daman-diu": ["Dadra and Nagar Haveli and Daman and Diu", "दादरा और नगर हवेली और दमन और दीव"], "delhi": ["Delhi", "दिल्ली"], "jammu-kashmir": ["Jammu and Kashmir", "जम्मू-कश्मीर"], "ladakh": ["Ladakh", "लद्दाख"], "lakshadweep": ["Lakshadweep", "लक्षद्वीप"], "puducherry": ["Puducherry", "पुडुचेरी"]};

const L = {
  en: {
    me: "You", close: "Close", account: "Account", name: "Your name", password: "Password (6+ characters)",
    create: "Create my login", login: "Log in", loginId: "Login id", logout: "Log out", skip: "Continue without an account",
    guestNote: "You are a guest on this device. Give your name and a password to get a login id you can use anywhere.",
    keep: "Keep my follows: get a login id", yourId: id => `Your login id: ${id}`, hello: n => `Logged in as ${n}`,
    email: "Email, only for a forgotten password (optional)", saveEmail: "Save email", newPass: "New password",
    changePass: "Change password", del: "Delete my account and everything in it", delSure: "Delete your account, follows and saved articles?",
    forgot: "Forgot password", forgotSent: "If that login has an email, a reset link is on its way.",
    setPass: "Set a new password", inbox: "Notifications", noInbox: "Nothing yet.", following: "You follow",
    noFollows: "Nothing yet. Follow a section, a state, a person or a story to be told when there is news.",
    sections: "Sections", states: "States", people: "People and organisations", personPh: "A name, e.g. Narendra Modi",
    add: "Follow", recapFollow: "The day's recap, every night", sortTitle: "Order and filter", sort: "Order",
    sorts: {newest: "Newest", verified: "Most established", contested: "Most disputed", developing: "Still developing",
            coverage: "Most outlets", shortest: "Shortest read", audio: "Audio first"},
    place: "State", anyPlace: "Any state", person: "Person", mainOnly: "Only where it is the main section",
    search: "Search the live stories", searchPh: "Words, a name, a place", noHits: "No live story matches.",
    saved: "Read later", noSaved: "Nothing saved.", recap: "The day's recap", openRecap: "Open the latest recap",
    noRecap: "No recap yet: it is made every night.", followStory: "Follow this story", unfollowStory: "Following this story",
    save: "Read later", unsave: "Saved", listen: "Listen", videos: "Videos", shareCard: "Share card", follow: "Follow",
    pickLang: "Listen in", en: "English", hi: "Hindi", requested: "Asked for. You will be notified when it is ready (usually within an hour).",
    queued: "Already asked for. You will be notified.", limit: "You can ask for one new audio a day. Audio already made is free.",
    gone: "This article is no longer live, so it cannot be read aloud.", loginFirst: "Something went wrong with your login. Log in again.",
    noVideos: "No videos found for this story.", videoNote: "Videos are from YouTube, in YouTube's order, at most two per channel. Nishpaksh has not checked them.",
    primary: "Primary footage", openYT: "Open on YouTube", pushOff: "Notifications are blocked for this site in your browser settings. You will still see them here.",
    pushIOS: "On iPhone, add Nishpaksh to your Home Screen (Share → Add to Home Screen) to get notifications. They also appear here.",
    pushOn: "Notifications on.", error: "Could not do that. Try again.", playRecap: "Listen to the recap",
    recapWait: "The recap's audio is being made.", followed: "Following", unfollowed: "Not following",
    est: "established", outlets: "outlets", read: "Read",
  },
  hi: {
    me: "आप", close: "बंद करें", account: "खाता", name: "आपका नाम", password: "पासवर्ड (6+ अक्षर)",
    create: "मेरी लॉगिन आईडी बनाएं", login: "लॉग इन", loginId: "लॉगिन आईडी", logout: "लॉग आउट", skip: "बिना खाते के जारी रखें",
    guestNote: "आप इस डिवाइस पर अतिथि हैं। नाम और पासवर्ड दें, तो कहीं भी चलने वाली लॉगिन आईडी मिलेगी।",
    keep: "फ़ॉलो सहेजें: लॉगिन आईडी लें", yourId: id => `आपकी लॉगिन आईडी: ${id}`, hello: n => `${n} के रूप में लॉग इन`,
    email: "ईमेल, केवल पासवर्ड भूलने पर (वैकल्पिक)", saveEmail: "ईमेल सहेजें", newPass: "नया पासवर्ड",
    changePass: "पासवर्ड बदलें", del: "मेरा खाता और उसका सब कुछ मिटाएं", delSure: "आपका खाता, फ़ॉलो और सहेजे लेख मिटा दें?",
    forgot: "पासवर्ड भूल गए", forgotSent: "अगर उस लॉगिन के साथ ईमेल है, तो रीसेट लिंक भेजा जा रहा है।",
    setPass: "नया पासवर्ड रखें", inbox: "सूचनाएं", noInbox: "अभी कुछ नहीं।", following: "आप फ़ॉलो करते हैं",
    noFollows: "अभी कुछ नहीं। खंड, राज्य, व्यक्ति या ख़बर फ़ॉलो करें, नई ख़बर पर सूचना मिलेगी।",
    sections: "खंड", states: "राज्य", people: "लोग और संस्थाएं", personPh: "नाम, जैसे Narendra Modi",
    add: "फ़ॉलो", recapFollow: "दिन का सार, हर रात", sortTitle: "क्रम और छंटाई", sort: "क्रम",
    sorts: {newest: "सबसे नई", verified: "सबसे अधिक स्थापित", contested: "सबसे अधिक मतभेद", developing: "अभी विकसित हो रही",
            coverage: "सबसे अधिक स्रोत", shortest: "सबसे छोटी", audio: "ऑडियो पहले"},
    place: "राज्य", anyPlace: "कोई भी राज्य", person: "व्यक्ति", mainOnly: "केवल जहां यह मुख्य खंड है",
    search: "चल रही ख़बरों में खोजें", searchPh: "शब्द, नाम, जगह", noHits: "कोई ख़बर नहीं मिली।",
    saved: "बाद में पढ़ें", noSaved: "कुछ सहेजा नहीं।", recap: "दिन का सार", openRecap: "आज का सार खोलें",
    noRecap: "अभी कोई सार नहीं: यह हर रात बनता है।", followStory: "यह ख़बर फ़ॉलो करें", unfollowStory: "यह ख़बर फ़ॉलो हो रही है",
    save: "बाद में पढ़ें", unsave: "सहेजा गया", listen: "सुनें", videos: "वीडियो", shareCard: "शेयर कार्ड", follow: "फ़ॉलो",
    pickLang: "किस भाषा में सुनें", en: "अंग्रेज़ी", hi: "हिंदी", requested: "अनुरोध हो गया। तैयार होने पर सूचना मिलेगी (आमतौर पर एक घंटे में)।",
    queued: "अनुरोध पहले से है। सूचना मिलेगी।", limit: "आप दिन में एक नया ऑडियो मांग सकते हैं। बने हुए ऑडियो मुफ़्त हैं।",
    gone: "यह लेख अब लाइव नहीं है, इसलिए इसे पढ़कर नहीं सुनाया जा सकता।", loginFirst: "लॉगिन में दिक्कत हुई। फिर से लॉग इन करें।",
    noVideos: "इस ख़बर के लिए कोई वीडियो नहीं मिला।", videoNote: "वीडियो YouTube से हैं, उसी के क्रम में, एक चैनल से अधिकतम दो। निष्पक्ष ने इन्हें जांचा नहीं है।",
    primary: "मूल फ़ुटेज", openYT: "YouTube पर खोलें", pushOff: "आपके ब्राउज़र में इस साइट की सूचनाएं बंद हैं। वे यहां दिखती रहेंगी।",
    pushIOS: "iPhone पर सूचनाओं के लिए निष्पक्ष को होम स्क्रीन पर जोड़ें (Share → Add to Home Screen)। सूचनाएं यहां भी दिखेंगी।",
    pushOn: "सूचनाएं चालू।", error: "यह नहीं हो सका। फिर कोशिश करें।", playRecap: "सार सुनें",
    recapWait: "सार का ऑडियो बन रहा है।", followed: "फ़ॉलो हो रहा है", unfollowed: "फ़ॉलो नहीं",
    est: "स्थापित", outlets: "स्रोत", read: "पढ़ें",
  },
};
const t = () => L[NP.lang] || L.en;

/* ---------- small storage ---------- */
function load(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } }
function save(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }

/* ---------- styles (plain, for the owner's design pass later) ---------- */
const css = document.createElement("style");
css.textContent = `
  .npx-me { position: relative; border: 0; background: rgba(255,255,255,.16); color: #fff; border-radius: 999px; height: 30px;
            min-width: 30px; padding: 0 10px; font: 600 13px/30px var(--body, system-ui); cursor: pointer; margin-right: 8px; }
  .npx-badge { position: absolute; top: -4px; right: -4px; background: #b2251c; color: #fff; border-radius: 999px; font-size: 10px;
               min-width: 16px; height: 16px; line-height: 16px; padding: 0 4px; }
  #npx-panel { position: fixed; inset: 0; z-index: 90; background: var(--ground, #ECECE9); overflow-y: auto; display: none;
               color: var(--ink, #16181A); font-family: var(--body, system-ui); }
  #npx-panel.on { display: block; }
  .npx-wrap { max-width: 640px; margin: 0 auto; padding: 14px 16px 120px; }
  .npx-top { display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; background: inherit; padding: 8px 0; z-index: 1; }
  .npx-top h2 { margin: 0; font-size: 20px; }
  .npx-box { background: #fff; border-radius: 14px; padding: 14px; margin: 12px 0; }
  .npx-box h3 { margin: 0 0 8px; font-size: 16px; }
  .npx-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 6px 0; }
  .npx-in { flex: 1 1 160px; min-width: 0; padding: 9px 10px; border: 1px solid #cfd0d6; border-radius: 9px; font: inherit; background: #fff; }
  .npx-b { border: 1px solid #c9cbe0; background: #f4f5fb; color: #1f2550; border-radius: 999px; padding: 7px 12px; font: 600 13px/1.2 inherit; cursor: pointer; }
  .npx-b.on { background: #1f2550; color: #fff; border-color: #1f2550; }
  .npx-b.warn { color: #b2251c; border-color: #e3b8b4; background: #fff6f5; }
  .npx-note { font-size: 13px; color: #5d6070; margin: 6px 0; }
  .npx-list { list-style: none; margin: 0; padding: 0; }
  .npx-list li { padding: 9px 0; border-top: 1px solid #eee; }
  .npx-list li:first-child { border-top: 0; }
  .npx-list a { color: inherit; text-decoration: none; }
  .npx-list .unread b { color: #1f2550; }
  .npx-list small { color: #6b6e7b; display: block; margin-top: 2px; }
  .npx-tools { margin: 4px 0 14px; }
  .npx-tools .npx-row { margin: 4px 0; }
  .npx-chips { font-size: 12px; color: #5d6070; }
  .npx-panelin { background: #fff; border-radius: 14px; padding: 12px; margin: 8px 0; }
  .npx-vid { display: grid; grid-template-columns: 120px 1fr; gap: 10px; padding: 8px 0; border-top: 1px solid #eee; }
  .npx-vid:first-of-type { border-top: 0; }
  .npx-vid img { width: 120px; aspect-ratio: 16/9; object-fit: cover; border-radius: 8px; cursor: pointer; }
  .npx-vid iframe { grid-column: 1 / -1; width: 100%; aspect-ratio: 16/9; border: 0; border-radius: 10px; }
  .npx-tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 6px; background: #e8f3ee; color: #1d6a45; margin-left: 4px; }
  #npx-player { position: fixed; left: 0; right: 0; bottom: 0; z-index: 95; background: #1f2550; color: #fff; display: none;
                padding: 10px 16px calc(10px + env(safe-area-inset-bottom)); font-family: var(--body, system-ui); }
  #npx-player.on { display: block; }
  #npx-player .npx-row { margin: 0; flex-wrap: nowrap; }
  #npx-player button { background: rgba(255,255,255,.15); color: #fff; border: 0; border-radius: 999px; padding: 7px 12px; font: 600 13px inherit; cursor: pointer; }
  #npx-player input[type=range] { flex: 1; min-width: 60px; }
  #npx-player .npx-tl { font-size: 12px; min-width: 76px; text-align: right; font-variant-numeric: tabular-nums; }
  .sent.npx-on .s, .npx-on.s { background: color-mix(in srgb, currentColor 16%, transparent); border-radius: 3px;
                                text-decoration: underline 2px; text-underline-offset: 3px; }
  h1.npx-on { text-decoration: underline 2px; text-underline-offset: 4px; }
  .npx-rc { padding: 12px 0; border-top: 1px solid #eee; }
  .npx-rc h4 { margin: 0 0 4px; font-size: 17px; }
  .npx-rc h4.npx-on { text-decoration: underline 2px; }
`;
document.head.appendChild(css);

/* ---------- account ---------- */
let A = load("npx-auth", null);       // {login, name, guest, access_token, refresh_token, expires_at, gp}
function setAuth(d) {
  A = {login: d.login, name: d.name, guest: !!d.guest, ...(d.session || {}), gp: d.guest_password || (A && A.gp) || null};
  if (!A.guest) A.gp = null;
  save("npx-auth", A);
}
function uid() {
  try { return JSON.parse(atob(A.access_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))).sub; } catch (e) { return null; }
}
async function fn(action, body = {}, auth = false) {
  const h = {"Content-Type": "application/json", apikey: KEY};
  if (auth) h.Authorization = `Bearer ${await token()}`;
  const r = await fetch(FN, {method: "POST", headers: h, body: JSON.stringify({action, ...body})});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
async function token() {
  if (!A) throw new Error(t().loginFirst);
  if ((A.expires_at || 0) * 1000 - Date.now() > 60000) return A.access_token;
  const r = await fetch(`${SB}/auth/v1/token?grant_type=refresh_token`, {method: "POST",
    headers: {apikey: KEY, "Content-Type": "application/json"}, body: JSON.stringify({refresh_token: A.refresh_token})});
  if (r.ok) {
    const d = await r.json();
    Object.assign(A, {access_token: d.access_token, refresh_token: d.refresh_token, expires_at: d.expires_at});
    save("npx-auth", A);
    return A.access_token;
  }
  if (A.guest && A.gp) { setAuth(await fn("login", {login: A.login, password: A.gp})); return A.access_token; }
  A = null; save("npx-auth", null);
  throw new Error(t().loginFirst);
}
async function ensure() {              // a guest on this device the first time a reader does something personal
  if (A) return;
  setAuth(await fn("signup", {name: "", lang: NP.lang}));
}
async function rest(path, opts = {}) {
  const h = {apikey: KEY, Authorization: `Bearer ${A ? await token() : KEY}`, "Content-Type": "application/json", ...(opts.headers || {})};
  const r = await fetch(`${SB}/rest/v1/${path}`, {...opts, headers: h});
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const txt = await r.text();
  return txt ? JSON.parse(txt) : null;
}
const pub = path => fetch(`${SB}/rest/v1/${path}`, {headers: {apikey: KEY, Authorization: `Bearer ${KEY}`}}).then(r => r.ok ? r.json() : []);

/* ---------- follows ---------- */
let F = new Map();                     // "kind|key" -> label
async function loadFollows() {
  if (!A) { F = new Map(); return; }
  try {
    const rows = await rest("follows?select=kind,key,label");
    F = new Map(rows.map(r => [`${r.kind}|${r.key}`, r.label || r.key]));
  } catch (e) { /* offline */ }
}
const isF = (kind, key) => F.has(`${kind}|${key}`);
async function toggleFollow(kind, key, label) {
  const adding = !isF(kind, key);
  const perm = adding ? askPermission() : null;     // asked inside the tap (Safari needs that)
  try {
    await ensure();
    key = String(key);
    if (adding) {
      await rest("follows", {method: "POST", headers: {Prefer: "resolution=ignore-duplicates"},
                             body: JSON.stringify({reader: uid(), kind, key, label: label || key})});
      F.set(`${kind}|${key}`, label || key);
      enablePush(perm);                  // never waits on the permission prompt
    } else {
      await rest(`follows?kind=eq.${encodeURIComponent(kind)}&key=eq.${encodeURIComponent(key)}`, {method: "DELETE"});
      F.delete(`${kind}|${key}`);
    }
    NP.toast(adding ? `${t().followed}: ${label || key}` : `${t().unfollowed}: ${label || key}`);
  } catch (e) { NP.toast(e.message || t().error); }
  return isF(kind, key);
}

/* ---------- Web Push ---------- */
function askPermission() {
  if (!("Notification" in window) || Notification.permission !== "default") return Promise.resolve(window.Notification ? Notification.permission : "unsupported");
  try { return Notification.requestPermission(); } catch (e) { return Promise.resolve("default"); }
}
function b64u(s) {
  const p = "=".repeat((4 - s.length % 4) % 4), raw = atob((s + p).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}
async function enablePush(permP) {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) { NP.toast(t().pushIOS); return; }
  const perm = await (permP || askPermission());
  if (perm !== "granted") { if (perm === "denied") NP.toast(t().pushOff); return; }
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = (await reg.pushManager.getSubscription()) ||
      await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64u(VAPID)});
    const j = sub.toJSON();
    await rest("push_subscriptions?on_conflict=endpoint", {method: "POST", headers: {Prefer: "resolution=merge-duplicates"},
      body: JSON.stringify({endpoint: j.endpoint, reader: uid(), p256dh: j.keys.p256dh, auth: j.keys.auth, lang: NP.lang})});
    save("npx-endpoint", j.endpoint);
  } catch (e) { /* the inbox still has them */ }
}
async function syncLang(l) {
  if (!A) return;
  const ep = load("npx-endpoint", null);
  try {
    await rest(`profiles?id=eq.${uid()}`, {method: "PATCH", body: JSON.stringify({lang: l})});
    if (ep) await rest(`push_subscriptions?endpoint=eq.${encodeURIComponent(ep)}`, {method: "PATCH", body: JSON.stringify({lang: l})});
  } catch (e) { /* next time */ }
}

/* ---------- order, filters, search (owner: verified-first and other orders; live stories only) ---------- */
let P = load("npx-view", {sort: "newest", place: "", person: "", mainOnly: false, q: ""});
const share = (c, k) => { const b = c.bar || {}, tot = Object.values(b).reduce((a, v) => a + v, 0) || 1; return (b[k] || 0) / tot; };
const SORTS = {
  newest: null,
  verified: (a, b) => share(b, "e") - share(a, "e"),
  contested: (a, b) => share(b, "d") - share(a, "d"),
  developing: (a, b) => share(b, "v") - share(a, "v"),
  coverage: (a, b) => (b.n || 0) - (a.n || 0),
  shortest: (a, b) => (a.w ?? 1e9) - (b.w ?? 1e9),
  audio: (a, b) => ((b.au || []).length ? 1 : 0) - ((a.au || []).length ? 1 : 0),
};
function textOf(c) {
  return [c.h, ...(c.paras || []).flat().map(x => x.t), ...(c.pp || [])].join(" ").toLowerCase();
}
const NPX = window.NPX = {
  keep(c, fKey) {
    if (P.place && !(c.pl || []).includes(P.place)) return false;
    if (P.person && !(c.pp || []).some(n => n.toLowerCase().includes(P.person.toLowerCase()))) return false;
    if (P.mainOnly && fKey && fKey !== "all") {
      const cat = c.cat || {};
      if (![(cat.primary || [])[0], (cat.secondary || [])[0], (cat.tertiary || [])[0]].includes(fKey)) return false;
    }
    if (P.q) { const words = P.q.toLowerCase().split(/\s+/).filter(Boolean), txt = textOf(c); if (!words.every(w => txt.includes(w))) return false; }
    return true;
  },
  order(list) {
    const f = SORTS[P.sort];
    if (!f) return list;
    return list.map((c, i) => [c, i]).sort((x, y) => f(x[0], y[0]) || x[1] - y[1]).map(x => x[0]);   // ties: newest first
  },
  onArticle, route, onLang(l) { syncLang(l); updateMe(); if (panel.classList.contains("on")) render(view); },
};
function setView(k, v) { P[k] = v; save("npx-view", P); NP.refresh(); }

/* ---------- the header button and the panel ---------- */
const me = document.createElement("button");
me.type = "button"; me.className = "npx-me"; me.id = "npx-me";
const row = document.querySelector(".greetrow .pill");
if (row) row.parentNode.insertBefore(me, row);
const panel = document.createElement("div");
panel.id = "npx-panel"; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
document.body.appendChild(panel);
let view = "home", unread = 0;
function updateMe() {
  me.innerHTML = `${esc(A && !A.guest ? A.name.split(" ")[0] : t().me)}${unread ? `<span class="npx-badge">${unread}</span>` : ""}`;
  me.setAttribute("aria-label", t().me);
}
me.addEventListener("click", () => open("home"));
function open(v) { view = v; panel.classList.add("on"); render(v); }
function closePanel() {
  panel.classList.remove("on");
  if (/^#\/recap\//.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
}
document.addEventListener("keydown", e => { if (e.key === "Escape" && panel.classList.contains("on")) closePanel(); });

async function render(v) {
  const T = t();
  const top = title => `<div class="npx-top"><h2>${esc(title)}</h2><button type="button" class="npx-b" data-a="close">${esc(T.close)}</button></div>`;
  if (v === "recap") return renderRecap(top);
  panel.innerHTML = `<div class="npx-wrap">${top(A && !A.guest ? T.hello(A.name) : T.me)}
    <div class="npx-box" id="npx-acc"></div>
    <div class="npx-box"><h3>${esc(T.inbox)}</h3><ul class="npx-list" id="npx-inbox"><li>…</li></ul></div>
    <div class="npx-box"><h3>${esc(T.sortTitle)}</h3>
      <div class="npx-row"><label>${esc(T.sort)} <select class="npx-in" id="npx-sort">${Object.entries(T.sorts).map(([k, l]) => `<option value="${k}"${P.sort === k ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label></div>
      <div class="npx-row"><select class="npx-in" id="npx-place"><option value="">${esc(T.anyPlace)}</option>${Object.entries(STATES).map(([k, n]) => `<option value="${k}"${P.place === k ? " selected" : ""}>${esc(n[NP.lang === "hi" ? 1 : 0])}</option>`).join("")}</select>
        <input class="npx-in" id="npx-person" placeholder="${esc(T.person)}" value="${esc(P.person)}"></div>
      <label class="npx-row"><input type="checkbox" id="npx-main"${P.mainOnly ? " checked" : ""}> ${esc(T.mainOnly)}</label>
      <div class="npx-row"><input class="npx-in" id="npx-q" type="search" placeholder="${esc(T.searchPh)}" value="${esc(P.q)}" aria-label="${esc(T.search)}"></div>
      <ul class="npx-list" id="npx-hits"></ul></div>
    <div class="npx-box"><h3>${esc(T.recap)}</h3><div class="npx-row"><button type="button" class="npx-b" data-a="recap">${esc(T.openRecap)}</button>
      <button type="button" class="npx-b${isF("recap", "daily") ? " on" : ""}" data-f="recap|daily|recap">${esc(T.recapFollow)}</button></div></div>
    <div class="npx-box"><h3>${esc(T.following)}</h3><div id="npx-follows"></div></div>
    <div class="npx-box"><h3>${esc(T.saved)}</h3><ul class="npx-list" id="npx-saved"></ul></div>
  </div>`;
  renderAccount(); renderFollows(); renderInbox(); renderSaved(); renderHits();
}
function renderAccount() {
  const T = t(), box = $("npx-acc");
  if (!box) return;
  if (!A) {
    box.innerHTML = `<h3>${esc(T.account)}</h3>
      <div class="npx-row"><input class="npx-in" id="npx-name" placeholder="${esc(T.name)}" autocomplete="name"><input class="npx-in" id="npx-pw" type="password" placeholder="${esc(T.password)}" autocomplete="new-password"></div>
      <div class="npx-row"><button type="button" class="npx-b on" data-a="signup">${esc(T.create)}</button></div>
      <div class="npx-row"><input class="npx-in" id="npx-lid" placeholder="${esc(T.loginId)}" autocomplete="username"><input class="npx-in" id="npx-lpw" type="password" placeholder="${esc(T.password)}" autocomplete="current-password"></div>
      <div class="npx-row"><button type="button" class="npx-b" data-a="login">${esc(T.login)}</button><button type="button" class="npx-b" data-a="forgot">${esc(T.forgot)}</button><button type="button" class="npx-b" data-a="close">${esc(T.skip)}</button></div>`;
  } else if (A.guest) {
    box.innerHTML = `<h3>${esc(T.account)}</h3><p class="npx-note">${esc(T.guestNote)}</p>
      <div class="npx-row"><input class="npx-in" id="npx-name" placeholder="${esc(T.name)}"><input class="npx-in" id="npx-pw" type="password" placeholder="${esc(T.password)}" autocomplete="new-password"></div>
      <div class="npx-row"><button type="button" class="npx-b on" data-a="claim">${esc(T.keep)}</button></div>
      <div class="npx-row"><input class="npx-in" id="npx-lid" placeholder="${esc(T.loginId)}"><input class="npx-in" id="npx-lpw" type="password" placeholder="${esc(T.password)}"><button type="button" class="npx-b" data-a="login">${esc(T.login)}</button></div>`;
  } else {
    box.innerHTML = `<h3>${esc(T.account)}</h3><p><b>${esc(T.yourId(A.login))}</b></p>
      <div class="npx-row"><input class="npx-in" id="npx-email" type="email" placeholder="${esc(T.email)}"><button type="button" class="npx-b" data-a="email">${esc(T.saveEmail)}</button></div>
      <div class="npx-row"><input class="npx-in" id="npx-np" type="password" placeholder="${esc(T.newPass)}" autocomplete="new-password"><button type="button" class="npx-b" data-a="password">${esc(T.changePass)}</button></div>
      <div class="npx-row"><button type="button" class="npx-b" data-a="logout">${esc(T.logout)}</button><button type="button" class="npx-b warn" data-a="delete">${esc(T.del)}</button></div>`;
  }
}
function sectionTree() {
  const S = NP.SECTIONS[NP.lang] || NP.SECTIONS.en, out = [];
  for (const [p, subs] of Object.entries(S.tree || {})) {
    out.push([p, S.primary[p], 0]);
    for (const s of subs) {
      out.push([`${p}/${s}`, S.secondary[s], 1]);
      for (const x of (S.tree3 || {})[s] || []) out.push([`${p}/${s}/${x}`, S.tertiary[x], 2]);
    }
  }
  return out;
}
function renderFollows() {
  const T = t(), box = $("npx-follows");
  if (!box) return;
  const mine = [...F.entries()].filter(([k]) => !k.startsWith("recap|"));
  const chip = (kind, key, label) => `<button type="button" class="npx-b${isF(kind, key) ? " on" : ""}" data-f="${esc(kind)}|${esc(key)}|${esc(label)}">${esc(label)}</button>`;
  box.innerHTML = `${mine.length ? `<div class="npx-row">${mine.map(([k, label]) => { const [kind, ...rest] = k.split("|"); return chip(kind, rest.join("|"), label); }).join("")}</div>` : `<p class="npx-note">${esc(T.noFollows)}</p>`}
    <h3 style="margin-top:14px">${esc(T.sections)}</h3>
    ${sectionTree().map(([k, l, d]) => d === 0 ? `<div class="npx-row" style="margin-top:10px">${chip("section", k, l)}</div>` : chip("section", k, l)).join(" ")}
    <h3 style="margin-top:14px">${esc(T.states)}</h3><div class="npx-row">${Object.entries(STATES).map(([k, n]) => chip("place", k, n[NP.lang === "hi" ? 1 : 0])).join("")}</div>
    <h3 style="margin-top:14px">${esc(T.people)}</h3>
    <div class="npx-row"><input class="npx-in" id="npx-pname" placeholder="${esc(T.personPh)}"><button type="button" class="npx-b" data-a="followname">${esc(T.add)}</button></div>`;
}
async function renderInbox() {
  const T = t(), box = $("npx-inbox");
  if (!box) return;
  if (!A) { box.innerHTML = `<li class="npx-note">${esc(T.noInbox)}</li>`; return; }
  try {
    const rows = await rest("notifications?select=id,kind,title,body,url,created_at,read_at&order=created_at.desc&limit=40");
    box.innerHTML = rows.length ? rows.map(r => `<li class="${r.read_at ? "" : "unread"}"><a href="${esc(r.url || "#")}" data-n="${r.id}"><b>${esc(r.title)}</b><small>${esc(r.body || "")} · ${esc(new Date(r.created_at).toLocaleString())}</small></a></li>`).join("")
                                : `<li class="npx-note">${esc(T.noInbox)}</li>`;
    const ids = rows.filter(r => !r.read_at).map(r => r.id);
    if (ids.length) rest(`notifications?id=in.(${ids.join(",")})`, {method: "PATCH", body: JSON.stringify({read_at: new Date().toISOString()})}).catch(() => {});
    unread = 0; updateMe();
  } catch (e) { box.innerHTML = `<li class="npx-note">${esc(T.error)}</li>`; }
}
let savedIds = new Set();
async function loadSaved() {
  if (!A) { savedIds = new Set(); return; }
  try { savedIds = new Set((await rest("saved?select=story_id")).map(r => String(r.story_id))); } catch (e) { /* offline */ }
}
async function renderSaved() {
  const T = t(), box = $("npx-saved");
  if (!box) return;
  if (!savedIds.size) { box.innerHTML = `<li class="npx-note">${esc(T.noSaved)}</li>`; return; }
  const pool = NP.pool || [];
  const items = await Promise.all([...savedIds].map(async id => {
    const c = pool.find(x => String(x.id) === id);
    if (c) return [id, c.h];
    const r = await NP.loadStory(id).catch(() => null);
    return [id, r ? ((NP.lang === "hi" && r.headline_hi) || r.headline_en) : null];
  }));
  box.innerHTML = items.filter(x => x[1]).map(([id, h]) => `<li><a href="#/story/${esc(id)}" data-story="${esc(id)}">${esc(h)}</a></li>`).join("") || `<li class="npx-note">${esc(T.noSaved)}</li>`;
}
function renderHits() {
  const box = $("npx-hits");
  if (!box) return;
  if (!P.q) { box.innerHTML = ""; return; }
  const hits = (NP.pool || []).filter(c => NPX.keep(c, "all")).slice(0, 15);
  box.innerHTML = hits.length ? hits.map(c => `<li><a href="#/story/${esc(c.id)}" data-story="${esc(c.id)}">${esc(c.h)}</a></li>`).join("") : `<li class="npx-note">${esc(t().noHits)}</li>`;
}

panel.addEventListener("change", e => {
  const id = e.target.id;
  if (id === "npx-sort") setView("sort", e.target.value);
  else if (id === "npx-place") setView("place", e.target.value);
  else if (id === "npx-main") setView("mainOnly", e.target.checked);
  else if (id === "npx-person") setView("person", e.target.value.trim());
});
let qTimer = null;
panel.addEventListener("input", e => {
  if (e.target.id !== "npx-q") return;
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { setView("q", e.target.value.trim()); renderHits(); }, 250);
});
panel.addEventListener("click", async e => {
  const T = t();
  const st = e.target.closest("a[data-story]");
  if (st) { e.preventDefault(); closePanel(); NP.openStory(st.dataset.story); return; }
  const note = e.target.closest("a[data-n]");
  if (note) { closePanel(); return; }          // the link itself opens the story
  const f = e.target.closest("[data-f]");
  if (f) {
    const [kind, key, ...rest] = f.dataset.f.split("|");
    const on = await toggleFollow(kind, key, rest.join("|"));
    f.classList.toggle("on", on);
    if (view === "home") { renderFollows(); renderAccount(); }
    return;
  }
  const b = e.target.closest("[data-a]");
  if (!b) return;
  const a = b.dataset.a, val = id => (($(id) || {}).value || "").trim();
  try {
    if (a === "close") closePanel();
    else if (a === "recap") { const d = await pub("recaps?select=day&order=day.desc&limit=1"); if (d.length) { history.pushState(null, "", `#/recap/${d[0].day}`); open("recap"); } else NP.toast(T.noRecap); }
    else if (a === "signup") { setAuth(await fn("signup", {name: val("npx-name"), password: $("npx-pw").value, lang: NP.lang})); await afterLogin(); NP.toast(T.yourId(A.login)); }
    else if (a === "claim") { setAuth(await fn("claim", {name: val("npx-name"), password: $("npx-pw").value}, true)); await afterLogin(); NP.toast(T.yourId(A.login)); }
    else if (a === "login") { setAuth(await fn("login", {login: val("npx-lid"), password: $("npx-lpw").value})); await afterLogin(); }
    else if (a === "forgot") { await fn("forgot", {login: val("npx-lid")}); NP.toast(T.forgotSent); }
    else if (a === "email") { await fn("email", {email: val("npx-email")}, true); NP.toast("✓"); }
    else if (a === "password") { await fn("password", {password: $("npx-np").value}, true); NP.toast("✓"); }
    else if (a === "logout") { A = null; save("npx-auth", null); F = new Map(); savedIds = new Set(); unread = 0; render("home"); updateMe(); }
    else if (a === "delete") { if (confirm(T.delSure)) { await fn("delete", {}, true); A = null; save("npx-auth", null); F = new Map(); savedIds = new Set(); render("home"); updateMe(); } }
    else if (a === "followname") { const n = val("npx-pname"); if (n) { await toggleFollow("entity", n.toLowerCase(), n); renderFollows(); } }
  } catch (err) { NP.toast(err.message || T.error); }
});
async function afterLogin() {
  await Promise.all([loadFollows(), loadSaved()]);
  syncLang(NP.lang);
  if (load("npx-endpoint", null) && window.Notification && Notification.permission === "granted") enablePush(Promise.resolve("granted"));
  render(view); updateMe(); countUnread();
}
async function countUnread() {
  if (!A) return;
  try {
    const r = await rest("notifications?select=id&read_at=is.null&limit=99");
    unread = r.length; updateMe();
  } catch (e) { /* offline */ }
}

/* ---------- the article's tools ---------- */
function catPaths(cat) {
  const S = NP.SECTIONS[NP.lang] || NP.SECTIONS.en, c = NP.normCat(cat), out = [];
  const parentOf = s => Object.keys(S.tree || {}).find(p => (S.tree[p] || []).includes(s));
  const secOf = x => Object.keys(S.tree3 || {}).find(s => (S.tree3[s] || []).includes(x));
  for (const p of c.primary || []) out.push([p, S.primary[p]]);
  for (const s of c.secondary || []) { const p = parentOf(s); if (p && !(S.tree3 || {})[s]) out.push([`${p}/${s}`, S.secondary[s]]); }
  for (const x of c.tertiary || []) { const s = secOf(x), p = s && parentOf(s); if (p) out.push([`${p}/${s}/${x}`, S.tertiary[x]]); }
  return out;
}
let current = null;                    // {row, payload, id}
function onArticle(row) {
  const T = t(), id = String(row.story_id), p = (NP.lang === "hi" && row.payload_hi) || row.payload_en || {};
  const pe = row.payload_en || {};
  current = {row, id, payload: p};
  const rise = $("rise");
  if (!rise) return;
  // every sentence gets its place (paragraph.sentence) for the audio's highlighting
  const flat = (p.narrative && p.narrative.paragraphs || []).flatMap((para, k) => (para || []).map((_, i) => `${k}.${i}`));
  rise.querySelectorAll(".sent").forEach((el, i) => { if (flat[i]) el.dataset.k = flat[i]; });
  const card = (NP.pool || []).find(c => String(c.id) === id) || {};
  const chip = (kind, key, label) => `<button type="button" class="npx-b${isF(kind, key) ? " on" : ""}" data-f="${esc(kind)}|${esc(key)}|${esc(label)}">${esc(label)}</button>`;
  const secs = catPaths(pe.category).map(([k, l]) => chip("section", k, l));
  const pls = (pe.places || []).filter(k => STATES[k]).map(k => chip("place", k, STATES[k][NP.lang === "hi" ? 1 : 0]));
  const ppl = (pe.people || []).slice(0, 6).map(n => chip("entity", n.toLowerCase(), n));
  const box = document.createElement("div");
  box.className = "npx-tools";
  box.innerHTML = `<div class="npx-row">
      <button type="button" class="npx-b${isF("story", id) ? " on" : ""}" data-f="story|${esc(id)}|${esc(row.headline_en || "")}">${esc(T.followStory)}</button>
      <button type="button" class="npx-b${savedIds.has(id) ? " on" : ""}" data-t="save">${esc(savedIds.has(id) ? T.unsave : T.save)}</button>
      <button type="button" class="npx-b" data-t="listen">${esc(T.listen)}${(card.au || []).length ? " ✓" : ""}</button>
      <button type="button" class="npx-b" data-t="videos">${esc(T.videos)}${card.vd ? ` (${card.vd})` : ""}</button>
      <button type="button" class="npx-b" data-t="card">${esc(T.shareCard)}</button></div>
    ${secs.length + pls.length + ppl.length ? `<div class="npx-row npx-chips">${esc(T.follow)}: ${[...secs, ...pls, ...ppl].join("")}</div>` : ""}
    <div id="npx-more"></div>`;
  rise.insertBefore(box, rise.firstChild);
  if (/[?&]listen=1/.test(location.hash)) listenChoice();
  if (player.key && player.key.startsWith(`${id}-`)) light(true);
}
document.addEventListener("click", async e => {
  const tools = e.target.closest(".npx-tools");
  if (!tools || !current) return;
  const f = e.target.closest("[data-f]");
  if (f) { const [kind, key, ...rest] = f.dataset.f.split("|"); f.classList.toggle("on", await toggleFollow(kind, key, rest.join("|"))); return; }
  const b = e.target.closest("[data-t]");
  if (!b) return;
  const T = t(), id = current.id;
  try {
    if (b.dataset.t === "save") {
      await ensure();
      if (savedIds.has(id)) { await rest(`saved?story_id=eq.${id}`, {method: "DELETE"}); savedIds.delete(id); }
      else { await rest("saved", {method: "POST", headers: {Prefer: "resolution=ignore-duplicates"}, body: JSON.stringify({reader: uid(), story_id: Number(id)})}); savedIds.add(id); }
      b.classList.toggle("on", savedIds.has(id)); b.textContent = savedIds.has(id) ? T.unsave : T.save;
    } else if (b.dataset.t === "listen") listenChoice();
    else if (b.dataset.t === "lang") listen(b.dataset.l);
    else if (b.dataset.t === "videos") showVideos();
    else if (b.dataset.t === "card") shareCard();
    else if (b.dataset.t === "play") playKey(b.dataset.k);
  } catch (err) { NP.toast(err.message || T.error); }
});
function listenChoice() {
  const T = t(), more = $("npx-more");
  if (!more) return;
  more.innerHTML = `<div class="npx-panelin"><div class="npx-row">${esc(T.pickLang)}:
    <button type="button" class="npx-b${NP.lang === "en" ? " on" : ""}" data-t="lang" data-l="en">${esc(T.en)}</button>
    <button type="button" class="npx-b${NP.lang === "hi" ? " on" : ""}" data-t="lang" data-l="hi">${esc(T.hi)}</button></div></div>`;
}
async function listen(l) {
  const T = t(), id = current.id, key = `${id}-${l}`;
  const have = await pub(`audio_files?key=eq.${encodeURIComponent(key)}&select=url`);
  if (have.length) { playKey(key); return; }
  const perm = askPermission();
  await ensure();
  const r = await rest("rpc/request_audio", {method: "POST", body: JSON.stringify({p_story: Number(id), p_lang: l})});
  const s = r && r.status;
  if (s === "ready") { playKey(key); return; }
  NP.toast(s === "queued" ? T.requested : s === "limit" ? T.limit : s === "gone" ? T.gone : T.error);
  if (s === "queued") enablePush(perm);
}

/* ---------- the player: each sentence lit in its own colour while it is read ---------- */
const player = {el: null, key: null, units: [], on: null};
const bar = document.createElement("div");
bar.id = "npx-player";
bar.innerHTML = `<div class="npx-row"><button type="button" id="npx-pp">❚❚</button><input type="range" id="npx-seek" min="0" max="1000" value="0" aria-label="Position">
  <span class="npx-tl" id="npx-tl">0:00</span><button type="button" id="npx-x" aria-label="Close">✕</button></div>`;
document.body.appendChild(bar);
const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
async function playKey(key) {
  if (!player.el) {
    player.el = new Audio();
    player.el.preload = "auto";
    player.el.addEventListener("timeupdate", () => light(false));
    player.el.addEventListener("play", () => { $("npx-pp").textContent = "❚❚"; });
    player.el.addEventListener("pause", () => { $("npx-pp").textContent = "▶"; });
    player.el.addEventListener("ended", () => { clearLight(); });
  }
  player.key = key;
  player.units = [];
  try { player.units = (await (await fetch(`audio/${key}.json`)).json()).units || []; } catch (e) { /* plays without lighting */ }
  player.el.src = `audio/${key}.m4a`;
  bar.classList.add("on");
  player.el.play().catch(() => { $("npx-pp").textContent = "▶"; });
}
$("npx-pp").addEventListener("click", () => { if (!player.el) return; if (player.el.paused) player.el.play(); else player.el.pause(); });
$("npx-x").addEventListener("click", () => { if (player.el) player.el.pause(); bar.classList.remove("on"); clearLight(); player.key = null; });
$("npx-seek").addEventListener("input", e => { const a = player.el; if (a && a.duration) a.currentTime = a.duration * e.target.value / 1000; });
function clearLight() { document.querySelectorAll(".npx-on").forEach(x => x.classList.remove("npx-on")); player.on = null; }
function light(force) {
  const a = player.el;
  if (!a) return;
  if (a.duration) { $("npx-seek").value = String(Math.round(1000 * a.currentTime / a.duration)); $("npx-tl").textContent = `${fmt(a.currentTime)} / ${fmt(a.duration)}`; }
  const us = player.units;
  if (!us.length) return;
  let lo = 0, hi = us.length - 1, k = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (us[m].t <= a.currentTime) { k = m; lo = m + 1; } else hi = m - 1; }
  const u = us[k], tag = `${u.p}.${u.s}`;
  if (tag === player.on && !force) return;
  clearLight(); player.on = tag;
  const lang = player.key.split("-").pop();
  let el = null;
  if (player.key.startsWith("recap-")) el = panel.querySelector(`[data-k="${tag}"]`);
  else if (current && player.key === `${current.id}-${lang}` && NP.lang === lang && NP.openId === current.id) {
    el = u.p === -1 ? $("sheet-h") : u.s >= 0 ? $("rise").querySelector(`.sent[data-k="${tag}"]`) : null;
  }
  if (el) { el.classList.add("npx-on"); if (!player.el.paused) el.scrollIntoView({block: "center", behavior: "smooth"}); }
}

/* ---------- videos ---------- */
async function showVideos() {
  const T = t(), more = $("npx-more");
  if (!more) return;
  more.innerHTML = `<div class="npx-panelin">…</div>`;
  const rows = await pub(`videos?story_id=eq.${current.id}&select=items`);
  const items = (rows[0] && rows[0].items) || [];
  more.innerHTML = `<div class="npx-panelin"><h3 style="margin:0 0 6px">${esc(T.videos)}</h3><p class="npx-note">${esc(T.videoNote)}</p>
    ${items.length ? items.map(v => `<div class="npx-vid" data-v="${esc(v.id)}"><img src="${esc(v.thumb || `https://i.ytimg.com/vi/${v.id}/mqdefault.jpg`)}" alt="" loading="lazy" data-play="${esc(v.id)}">
      <div><b>${esc(v.title)}</b><br><small>${esc(v.channel)}${v.primary ? `<span class="npx-tag">${esc(T.primary)}</span>` : ""} · ${esc((v.at || "").slice(0, 10))}</small><br>
      <a href="https://www.youtube.com/watch?v=${esc(v.id)}" target="_blank" rel="noopener noreferrer">${esc(T.openYT)}</a></div></div>`).join("")
      : `<p>${esc(T.noVideos)}</p>`}</div>`;
}
document.addEventListener("click", e => {
  const img = e.target.closest("img[data-play]");
  if (!img) return;
  const box = img.closest(".npx-vid");
  if (box.querySelector("iframe")) return;
  const f = document.createElement("iframe");
  f.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(img.dataset.play)}?autoplay=1&rel=0`;
  f.allow = "autoplay; encrypted-media; picture-in-picture"; f.allowFullscreen = true;
  box.appendChild(f);
});

/* ---------- share card: headline, the colour bar and its shares, the address ---------- */
async function shareCard() {
  const T = t(), row = current.row, p = current.payload;
  const head = (NP.lang === "hi" && row.headline_hi) || row.headline_en || "";
  const paras = (row.payload_en && row.payload_en.narrative && row.payload_en.narrative.paragraphs) || [];
  const b = {e: 0, v: 0, o: 0, d: 0, r: 0, u: 0}, KEYS = {established: "e", corroborated: "e", confirmed: "e", developing: "v", single: "o", disputed: "d", false: "r"};
  for (const para of paras) for (const s of para || []) for (const c of (s.parts && s.parts.length > 1 ? s.parts.map(x => x.class) : [s.class])) b[KEYS[c] || "u"]++;
  const tot = Object.values(b).reduce((a, v) => a + v, 0) || 1;
  const W = 1080, H = 1350, cv = document.createElement("canvas");
  cv.width = W; cv.height = H;
  const g = cv.getContext("2d");
  const grad = g.createLinearGradient(0, 0, W, H); grad.addColorStop(0, "#46549a"); grad.addColorStop(1, "#161a38");
  g.fillStyle = grad; g.fillRect(0, 0, W, H);
  g.fillStyle = "#fff"; g.font = `64px ${NP.lang === "hi" ? '"Rozha One"' : '"Alfa Slab One"'}, serif`; g.textAlign = "center";
  g.fillText(NP.T[NP.lang].wordmark, W / 2, 150);
  g.textAlign = "left"; g.font = `600 64px ${NP.lang === "hi" ? '"Noto Serif Devanagari"' : '"Newsreader"'}, Georgia, serif`;
  const words = head.split(/\s+/), lines = []; let line = "";
  for (const w of words) { const tryL = line ? `${line} ${w}` : w; if (g.measureText(tryL).width > W - 160 && line) { lines.push(line); line = w; } else line = tryL; }
  if (line) lines.push(line);
  lines.slice(0, 8).forEach((l, i) => g.fillText(l, 80, 330 + i * 82));
  const y = 1040, colors = {e: "#2f9e6a", v: "#2bb3a5", o: "#9a6ad0", d: "#d19a1d", r: "#e0503f", u: "#b98a68"};
  let x = 80;
  for (const k of ["e", "v", "o", "d", "r", "u"]) { const w = (W - 160) * b[k] / tot; if (w) { g.fillStyle = colors[k]; g.fillRect(x, y, w, 26); x += w; } }
  g.fillStyle = "#dfe3ff"; g.font = "36px system-ui, sans-serif";
  g.fillText(`${Math.round(100 * b.e / tot)}% ${T.est} · ${(p.counts || {}).independent_sources || 0} ${T.outlets}`, 80, y + 90);
  g.fillText(`nishpakshnews.github.io/#/story/${current.id}`, 80, H - 80);
  const blob = await new Promise(r => cv.toBlob(r, "image/png"));
  const file = new File([blob], `nishpaksh-${current.id}.png`, {type: "image/png"});
  const url = `https://nishpakshnews.github.io/${NP.lang === "hi" ? "?lang=hi" : ""}#/story/${current.id}`;
  if (navigator.canShare && navigator.canShare({files: [file]})) { try { await navigator.share({files: [file], title: head, text: `${head} ${url}`}); return; } catch (e) { return; } }
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = file.name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/* ---------- the day's recap: #/recap/<day> ---------- */
function route(hash) {
  const m = (hash || "").match(/^#\/recap\/(\d{4}-\d{2}-\d{2})/);
  if (!m) { if (panel.classList.contains("on") && view === "recap") panel.classList.remove("on"); return false; }
  recapDay = m[1]; open("recap");
  return true;
}
let recapDay = null;
async function renderRecap(top) {
  const T = t();
  panel.innerHTML = `<div class="npx-wrap">${top(T.recap)}<div class="npx-box">…</div></div>`;
  const rows = await pub(`recaps?day=eq.${recapDay}&select=day,payload_en,payload_hi`);
  const r = rows[0];
  if (!r) { panel.querySelector(".npx-box").innerHTML = esc(T.noRecap); return; }
  const p = (NP.lang === "hi" && r.payload_hi) || r.payload_en || {};
  const key = `recap-${r.day}-${NP.lang}`;
  const have = (await pub(`audio_files?key=eq.${key}&select=key`)).length;
  panel.querySelector(".npx-box").outerHTML = `<div class="npx-box"><div class="npx-row">
      ${have ? `<button type="button" class="npx-b on" data-t2="play" data-k="${esc(key)}">${esc(T.playRecap)}</button>` : `<span class="npx-note">${esc(T.recapWait)}</span>`}
      <button type="button" class="npx-b${isF("recap", "daily") ? " on" : ""}" data-f="recap|daily|recap">${esc(T.recapFollow)}</button></div>
    <p class="npx-note">${esc(r.day)}</p>
    ${(p.stories || []).map((it, k) => `<div class="npx-rc"><h4 data-k="${k}.-1">${esc(it.h)}</h4>
      <p>${(it.lead || []).map((x, i) => `<span class="sent" data-k="${k}.${i}">${NP.sentenceHtml(x)}</span>`).join("")}</p>
      <a href="#/story/${esc(it.id)}" data-story="${esc(it.id)}">${esc(T.read)} →</a></div>`).join("")}</div>`;
}
panel.addEventListener("click", e => { const b = e.target.closest("[data-t2=play]"); if (b) playKey(b.dataset.k); });

/* ---------- a password reset link lands here ---------- */
async function recovery() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("type") !== "recovery" || !h.get("access_token")) return;
  const pw = prompt(t().setPass);
  if (pw && pw.length >= 6) {
    const r = await fetch(`${SB}/auth/v1/user`, {method: "PUT", headers: {apikey: KEY, Authorization: `Bearer ${h.get("access_token")}`, "Content-Type": "application/json"},
                                                   body: JSON.stringify({password: pw})});
    NP.toast(r.ok ? "✓" : t().error);
  }
  history.replaceState(null, "", location.pathname + location.search);
}

/* ---------- start ---------- */
updateMe();
recovery();
Promise.all([loadFollows(), loadSaved()]).then(() => { countUnread(); });
if (P.sort !== "newest" || P.place || P.person || P.mainOnly || P.q) NP.refresh();
route(location.hash);
if (NP.openId) NP.loadStory(NP.openId).then(r => { if (r && !document.querySelector(".npx-tools")) onArticle(r); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) countUnread(); });
navigator.serviceWorker && navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.np === "push") countUnread(); });
})();
