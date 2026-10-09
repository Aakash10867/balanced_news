// Reader accounts (owner, Oct 9 2026, trial_version_2.1): sign in with a name, an email and a password; log in again
// on any device with the email and password. The site keeps the session until the reader logs out.
//
// Users are created here with the admin API (email confirmed), so the project's Auth settings need no change. Deployed with verify_jwt off: the site calls it with the publishable key; actions on an
// account check the reader's own access token here.
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const URL_ = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const ANON = Deno.env.get("SUPABASE_ANON_KEY") || SERVICE;
const admin = createClient(URL_, SERVICE, { auth: { persistSession: false, autoRefreshToken: false } });
const NEW_PER_HOUR = 12;          // new accounts from one address in an hour

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const reply = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

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
    if (action === "signup") {                       // name, email and password (owner, Oct 9 2026: trial_version_2.1)
      const ip = (req.headers.get("x-forwarded-for") || "").split(",")[0].trim() || "?";
      const since = new Date(Date.now() - 3600_000).toISOString();
      const { count } = await admin.from("account_events").select("id", { count: "exact", head: true })
        .eq("ip", ip).gte("at", since);
      if ((count || 0) >= NEW_PER_HOUR) return reply({ error: "too_many" }, 429);
      const name = cleanName(b.name);
      const email = String(b.email || "").trim().toLowerCase();
      if (!name) return reply({ error: "name" }, 400);
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return reply({ error: "email" }, 400);
      if (!okPassword(b.password)) return reply({ error: "password" }, 400);
      const password = String(b.password);
      const { data: u, error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
      if (error || !u.user) {
        const taken = /already|registered|exists/i.test(error?.message || "");
        return reply({ error: taken ? "taken" : (error?.message || "could not create") }, taken ? 409 : 500);
      }
      const lang = b.lang === "hi" ? "hi" : "en";
      const ins = await admin.from("profiles").insert({ id: u.user.id, login: email, name, guest: false, email, lang });
      if (ins.error) { await admin.auth.admin.deleteUser(u.user.id); return reply({ error: ins.error.message }, 500); }
      await admin.from("account_events").insert({ ip });
      return reply({ name, email, session: await session(email, password) });
    }
    if (action === "login") {                        // email and password; the name comes from the profile
      const email = String(b.email || "").trim().toLowerCase();
      if (!email || !okPassword(b.password)) return reply({ error: "wrong" }, 401);
      try {
        const s = await session(email, String(b.password));
        const { data: p } = await admin.from("profiles").select("name").eq("login", email).maybeSingle();
        return reply({ name: p?.name || email.split("@")[0], email, session: s });
      } catch { return reply({ error: "wrong" }, 401); }
    }
    const user = await me(req);
    if (!user) return reply({ error: "log in first" }, 401);
    if (action === "password") {
      if (!okPassword(b.password)) return reply({ error: "the password needs 6 or more characters" }, 400);
      const up = await admin.auth.admin.updateUserById(user.id, { password: String(b.password) });
      return up.error ? reply({ error: up.error.message }, 500) : reply({ ok: true });
    }
    if (action === "email") {                        // a new email to log in with
      const email = String(b.email || "").trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return reply({ error: "email" }, 400);
      const up = await admin.auth.admin.updateUserById(user.id, { email, email_confirm: true });
      if (up.error) return reply({ error: /already|registered|exists/i.test(up.error.message) ? "taken" : up.error.message }, 400);
      await admin.from("profiles").update({ email, login: email }).eq("id", user.id);
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
