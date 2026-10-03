-- Columns for proactive search, independent origins, the fact/characterisation flag and the
-- model that made each embedding.
set lock_timeout = '5s';
alter table public.articles  add column if not exists found_by varchar(10);
alter table public.stories   add column if not exists last_searched_at timestamp;
alter table public.canonical add column if not exists origins json;
alter table public.canonical add column if not exists checkable boolean;
alter table public.articles  add column if not exists embed_model varchar(60);
