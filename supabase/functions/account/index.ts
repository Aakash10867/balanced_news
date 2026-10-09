// Reader accounts (owner, Oct 9 2026). A reader gives a name and gets a login id ("aakash-4821") and a password;
// "skip" makes a guest on that device (a random password the browser keeps), which can later take a name.
// Logging in with the login id works on any device. An email is optional, only to reset a forgotten password.
//
// Users are created here with the admin API (email confirmed), so the project needs neither anonymous sign-ins
// nor "Confirm email" turned off. Auth emails are internal (<uuid>@readers.nishpaksh.invalid) until a reader
// gives a real one. Deployed with verify_jwt off: the site calls it with the publishable key; actions on an
// account check the reader's own access token here.
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const URL_ = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANON = Deno.env.get("SUPABASE_ANON_KEY") || SERVICE;
const SITE = "https://nishpakshnews.github.io/";
const admin = createClient(URL_, SERVICE, { auth: { persistSession: false, autoRefreshToken: false } });
const NEW_PER_HOUR = 12;          // new accounts from one address in an hour

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const reply = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

function slug(name: string): string {
  const s = name.normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 16);
  return s.length >= 2 ? s : "reader";
}
function randomPassword(): string {
  const b = new Uint8Array(18);
  crypto.getRandomValues(b);
  return btoa(String.fromCharCode(...b)).replace(/[+/=]/g, "x");
}
async function freeLogin(name: string): Promise<string> {
  const base = slug(name);
  for (let i = 0; i < 20; i++) {
    const n = 1000 + Math.floor(Math.random() * 9000);
    const login = `${base}-${n}`;
    const { data } = await admin.from("profiles").select("id").eq("login", login).maybeSingle();
    if (!data) return login;
  }
  throw new Error("no free login");
}
async function session(email: string, password: string) {
  const c = createClient(URL_, ANON, { auth: { persistSession: false, autoRefreshToken: false } });
  const { data, error } = await c.auth.signInWithPassword({ email, password });
  if (error || !data.session) throw new Error(error?.message || "sign-in failed");
  const s = data.session;
  return { access_token: s.access_token, refresh_token: s.refresh_token, expires_at: s.expires_at };
}
async function me(req: Request) {
  const tok = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!tok) return null;
  const { data } = await admin.auth.getUser(tok);
  return data?.user || null;
}
const cleanName = (x: unknown) => String(x || "").replace(/\s+/g, " ").trim().slice(0, 40);
const okPassword = (p: unknown) => typeof p === "string" && p.length >= 6 && p.length <= 72;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return reply({ error: "POST only" }, 405);
  let b: Record<string, unknown> = {};
  try { b = await req.json(); } catch { return reply({ error: "bad request" }, 400); }
  const action = String(b.action || "");
  try {
    if (action === "signup") {                       // a name (and a password), or a guest
      const ip = (req.headers.get("x-forwarded-for") || "").split(",")[0].trim() || "?";
      const since = new Date(Date.now() - 3600_000).toISOString();
      const { count } = await admin.from("account_events").select("id", { count: "exact", head: true })
        .eq("ip", ip).gte("at", since);
      if ((count || 0) >= NEW_PER_HOUR) return reply({ error: "too many new accounts from here; try later" }, 429);
      const guest = !b.password;
      const name = cleanName(b.name) || (guest ? "Reader" : "");
      if (!name) return reply({ error: "a name is needed" }, 400);
      if (!guest && !okPassword(b.password)) return reply({ error: "the password needs 6 or more characters" }, 400);
      const password = guest ? randomPassword() : String(b.password);
      const login = await freeLogin(guest ? "guest" : name);
      const email = `${crypto.randomUUID()}@readers.nishpaksh.invalid`;
      const { data: u, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
      if (error || !u.user) return reply({ error: error?.message || "could not create" }, 500);
      const lang = b.lang === "hi" ? "hi" : "en";
      const ins = await admin.from("profiles").insert({ id: u.user.id, login, name, guest, lang });
      if (ins.error) { await admin.auth.admin.deleteUser(u.user.id); return reply({ error: ins.error.message }, 500); }
      await admin.from("account_events").insert({ ip });
      return reply({ login, name, guest, session: await session(email, password), ...(guest ? { guest_password: password } : {}) });
    }
    if (action === "login") {
      const login = String(b.login || "").trim().toLowerCase();
      const { data: p } = await admin.from("profiles").select("id, login, name, guest").eq("login", login).maybeSingle();
      if (!p || !okPassword(b.password)) return reply({ error: "wrong login id or password" }, 401);
      const { data: u } = await admin.auth.admin.getUserById(p.id);
      if (!u?.user?.email) return reply({ error: "wrong login id or password" }, 401);
      try {
        return reply({ login: p.login, name: p.name, guest: p.guest, session: await session(u.user.email, String(b.password)) });
      } catch { return reply({ error: "wrong login id or password" }, 401); }
    }
    if (action === "forgot") {                       // a reset link to the reader's own email, if they gave one
      const login = String(b.login || "").trim().toLowerCase();
      const { data: p } = await admin.from("profiles").select("email").eq("login", login).maybeSingle();
      if (p?.email) {
        const c = createClient(URL_, ANON, { auth: { persistSession: false } });
        await c.auth.resetPasswordForEmail(p.email, { redirectTo: SITE });
      }
      return reply({ ok: true });                    // the same answer either way: no one learns who has an email
    }
    const user = await me(req);
    if (!user) return reply({ error: "log in first" }, 401);
    if (action === "claim") {                        // a guest takes a name and a password: a login id is made
      const name = cleanName(b.name);
      if (!name) return reply({ error: "a name is needed" }, 400);
      if (!okPassword(b.password)) return reply({ error: "the password needs 6 or more characters" }, 400);
      const { data: p } = await admin.from("profiles").select("guest, login").eq("id", user.id).single();
      const login = p?.guest ? await freeLogin(name) : p?.login;
      const up = await admin.auth.admin.updateUserById(user.id, { password: String(b.password) });
      if (up.error) return reply({ error: up.error.message }, 500);
      await admin.from("profiles").update({ name, login, guest: false }).eq("id", user.id);
      return reply({ login, name, guest: false, session: await session(user.email!, String(b.password)) });
    }
    if (action === "password") {
      if (!okPassword(b.password)) return reply({ error: "the password needs 6 or more characters" }, 400);
      const up = await admin.auth.admin.updateUserById(user.id, { password: String(b.password) });
      return up.error ? reply({ error: up.error.message }, 500) : reply({ ok: true });
    }
    if (action === "email") {                        // optional, for a forgotten password; "" removes it
      const email = String(b.email || "").trim().toLowerCase();
      if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return reply({ error: "that email does not look right" }, 400);
      const authEmail = email || `${crypto.randomUUID()}@readers.nishpaksh.invalid`;
      const up = await admin.auth.admin.updateUserById(user.id, { email: authEmail, email_confirm: true });
      if (up.error) return reply({ error: up.error.message.includes("already") ? "that email is used by another account" : up.error.message }, 400);
      await admin.from("profiles").update({ email: email || null }).eq("id", user.id);
      return reply({ ok: true });
    }
    if (action === "rename") {
      const name = cleanName(b.name);
      if (!name) return reply({ error: "a name is needed" }, 400);
      await admin.from("profiles").update({ name }).eq("id", user.id);
      return reply({ ok: true, name });
    }
    if (action === "delete") {                       // everything of the reader goes with the account (cascade)
      await admin.auth.admin.deleteUser(user.id);
      return reply({ ok: true });
    }
    return reply({ error: "unknown action" }, 400);
  } catch (e) {
    return reply({ error: String((e as Error).message || e) }, 500);
  }
});
