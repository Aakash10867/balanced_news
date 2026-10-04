-- Threads: a later story that is a development of an earlier one (parent -> daughter).
create table if not exists public.story_links (
  parent_id integer not null, child_id integer not null, created_at timestamp default now(), reason text,
  primary key (parent_id, child_id));
alter table public.story_links enable row level security;
grant select, insert, update, delete on public.story_links to nishpaksh_app;
create policy app_full_access on public.story_links for all to nishpaksh_app using (true) with check (true);
