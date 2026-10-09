-- Readers (owner, Oct 9 2026): accounts, follows, push notifications, audio on request, the daily recap,
-- videos and read later. Non-destructive: new tables only.
--
-- Accounts are made by the edge function `account` (supabase/functions/account): a reader gives a name and gets a
-- login id ("aakash-4821") and a password; "skip" makes a guest on that device. Readers see and change only their
-- own rows (RLS on auth.uid()). The pipeline (nishpaksh_app) reads follows and subscriptions and writes
-- notifications, audio and recaps. The site reads audio_files, recaps and videos with the public key.

create table if not exists public.profiles (
  id uuid primary key references auth.users on delete cascade,
  login text unique not null,               -- what the reader logs in with: "aakash-4821"
  name text not null,
  guest boolean not null default false,
  email text,                               -- optional, only for a forgotten password
  lang varchar(4) not null default 'en',
  created_at timestamptz not null default now()
);

create table if not exists public.follows (
  reader uuid not null references auth.users on delete cascade,
  kind varchar(10) not null check (kind in ('section', 'place', 'entity', 'story', 'recap')),
  key text not null,                         -- "justice", "justice/courts", "business/domains/hr", "HR", "narendra modi", "16197", "daily"
  label text,
  created_at timestamptz not null default now(),
  primary key (reader, kind, key)
);

create table if not exists public.push_subscriptions (
  endpoint text primary key,
  reader uuid not null references auth.users on delete cascade,
  p256dh text not null,
  auth text not null,
  lang varchar(4) not null default 'en',
  created_at timestamptz not null default now(),
  last_ok_at timestamptz,
  failures integer not null default 0
);

create table if not exists public.notifications (
  id bigserial primary key,
  reader uuid not null references auth.users on delete cascade,
  kind varchar(12) not null,                 -- article | followup | audio | recap
  ref text not null,                         -- what it is about (story id, "123-hi", recap day): one per reader
  story_id integer,
  title text,
  body text,
  url text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  read_at timestamptz,
  unique (reader, kind, ref)
);
create index if not exists notifications_unsent on public.notifications (created_at) where sent_at is null;

create table if not exists public.audio_requests (
  id bigserial primary key,
  reader uuid not null references auth.users on delete cascade,
  story_id integer not null,
  lang varchar(4) not null,
  requested_at timestamptz not null default now(),
  status varchar(10) not null default 'queued',   -- queued | done | failed
  done_at timestamptz,
  unique (reader, story_id, lang)
);

create table if not exists public.audio_files (
  key text primary key,                      -- "16197-hi", "recap-2026-10-09-en"
  story_id integer,
  lang varchar(4) not null,
  url text not null,                         -- on the site: /audio/<key>.m4a
  seconds real,
  model text,
  made_at timestamptz not null default now()
);

create table if not exists public.recaps (
  day date primary key,                      -- the IST day
  cutoff timestamptz not null,               -- articles published up to here
  made_at timestamptz not null default now(),
  story_ids integer[] not null default '{}',
  payload_en jsonb,
  payload_hi jsonb
);

create table if not exists public.videos (
  story_id integer primary key,
  fetched_at timestamptz not null default now(),
  items jsonb not null default '[]'
);

create table if not exists public.saved (
  reader uuid not null references auth.users on delete cascade,
  story_id integer not null,
  created_at timestamptz not null default now(),
  primary key (reader, story_id)
);

create table if not exists public.notify_state (
  key text primary key,
  value jsonb
);

create table if not exists public.account_events (   -- new accounts per address, so nobody makes thousands
  id bigserial primary key,
  ip text,
  at timestamptz not null default now()
);

-- row level security ---------------------------------------------------------------------------------
alter table public.profiles enable row level security;
alter table public.follows enable row level security;
alter table public.push_subscriptions enable row level security;
alter table public.notifications enable row level security;
alter table public.audio_requests enable row level security;
alter table public.audio_files enable row level security;
alter table public.recaps enable row level security;
alter table public.videos enable row level security;
alter table public.saved enable row level security;
alter table public.notify_state enable row level security;
alter table public.account_events enable row level security;

-- a reader: their own rows
create policy own_profile_read on public.profiles for select to authenticated using (id = auth.uid());
create policy own_profile_lang on public.profiles for update to authenticated using (id = auth.uid()) with check (id = auth.uid());
create policy own_follows on public.follows for all to authenticated using (reader = auth.uid()) with check (reader = auth.uid());
create policy own_subs on public.push_subscriptions for all to authenticated using (reader = auth.uid()) with check (reader = auth.uid());
create policy own_notes_read on public.notifications for select to authenticated using (reader = auth.uid());
create policy own_notes_mark on public.notifications for update to authenticated using (reader = auth.uid()) with check (reader = auth.uid());
create policy own_audio_read on public.audio_requests for select to authenticated using (reader = auth.uid());
create policy own_saved on public.saved for all to authenticated using (reader = auth.uid()) with check (reader = auth.uid());
-- everyone: what the site shows
create policy public_audio on public.audio_files for select to anon, authenticated using (true);
create policy public_recaps on public.recaps for select to anon, authenticated using (true);
create policy public_videos on public.videos for select to anon, authenticated using (true);

grant select, update (lang) on public.profiles to authenticated;
grant select, insert, delete on public.follows to authenticated;
grant select, insert, update, delete on public.push_subscriptions to authenticated;
grant select, update (read_at) on public.notifications to authenticated;
grant select on public.audio_requests to authenticated;
grant select, insert, delete on public.saved to authenticated;
grant select on public.audio_files, public.recaps, public.videos to anon, authenticated;

-- the pipeline
grant select on public.profiles to nishpaksh_app;
grant select, delete on public.follows to nishpaksh_app;
grant select, update, delete on public.push_subscriptions to nishpaksh_app;
grant select, insert, update, delete on public.notifications to nishpaksh_app;
grant usage, select on sequence public.notifications_id_seq to nishpaksh_app;
grant select, update on public.audio_requests to nishpaksh_app;
grant select, insert, update, delete on public.audio_files, public.recaps, public.videos, public.notify_state to nishpaksh_app;
grant select, delete on public.saved to nishpaksh_app;
create policy app_full_access on public.profiles for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.follows for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.push_subscriptions for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.notifications for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.audio_requests for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.audio_files for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.recaps for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.videos for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.saved for all to nishpaksh_app using (true) with check (true);
create policy app_full_access on public.notify_state for all to nishpaksh_app using (true) with check (true);

-- audio on request: one new request per reader per IST day; an audio already made costs nothing -------------
create or replace function public.request_audio(p_story integer, p_lang text) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  me uuid := auth.uid();
  f record;
  today_start timestamptz := (date_trunc('day', now() at time zone 'Asia/Kolkata')) at time zone 'Asia/Kolkata';
begin
  if me is null then return jsonb_build_object('status', 'login'); end if;
  if p_lang not in ('en', 'hi') then return jsonb_build_object('status', 'bad'); end if;
  if not exists (select 1 from published where story_id = p_story) then return jsonb_build_object('status', 'gone'); end if;
  select * into f from audio_files where key = p_story || '-' || p_lang;
  if found then return jsonb_build_object('status', 'ready', 'url', f.url); end if;
  if exists (select 1 from audio_requests where reader = me and story_id = p_story and lang = p_lang and status = 'queued') then
    return jsonb_build_object('status', 'queued');
  end if;
  if (select count(*) from audio_requests where reader = me and requested_at >= today_start) >= 1 then
    return jsonb_build_object('status', 'limit');
  end if;
  insert into audio_requests (reader, story_id, lang) values (me, p_story, p_lang)
    on conflict (reader, story_id, lang) do update set status = 'queued', requested_at = now(), done_at = null;
  return jsonb_build_object('status', 'queued');
end $$;
revoke all on function public.request_audio(integer, text) from public, anon;
grant execute on function public.request_audio(integer, text) to authenticated;

-- the Web Push signing key lives in Vault ('vapid_private_key'); only the pipeline can read it
create or replace function public.vapid_private_key() returns text
language sql security definer set search_path = public, vault as $$
  select decrypted_secret from vault.decrypted_secrets where name = 'vapid_private_key' limit 1
$$;
revoke all on function public.vapid_private_key() from public, anon, authenticated;
grant execute on function public.vapid_private_key() to nishpaksh_app;

-- Supabase grants every new public table to anon and authenticated by default: narrowed again (applied as
-- migration readers_narrow_grants), so a reader cannot rename their login or rewrite a notification.
revoke all on public.profiles, public.notifications, public.audio_requests, public.notify_state, public.account_events, public.audio_files, public.recaps, public.videos from anon, authenticated;
revoke all on public.follows, public.push_subscriptions, public.saved from anon;
grant select, update (lang) on public.profiles to authenticated;
grant select, update (read_at) on public.notifications to authenticated;
grant select on public.audio_requests to authenticated;
grant select on public.audio_files, public.recaps, public.videos to anon, authenticated;
