-- Reports from nishpaksh/tools/probe.py (measurements on real data). The app role cannot create tables.
create table if not exists public.diagnostics (id serial primary key, created_at timestamp default now(), kind varchar(40), report json);
alter table public.diagnostics enable row level security;
grant select, insert, delete on public.diagnostics to nishpaksh_app;
grant usage, select on sequence public.diagnostics_id_seq to nishpaksh_app;
create policy app_full_access on public.diagnostics for all to nishpaksh_app using (true) with check (true);
