-- A shadow copy of the pipeline's data in schema "shadow", for a full pipeline run on real data
-- that cannot change what the site shows (NISHPAKSH_SCHEMA=shadow). Model and Tavily usage is
-- NOT copied: shadow.quota_usage is a view of the real table, so a shadow run spends from, and
-- counts against, the same real quotas. Drop with: drop schema shadow cascade;
drop schema if exists shadow cascade;
create schema shadow;
grant usage on schema shadow to nishpaksh_app;
do $$
declare t text;
begin
  foreach t in array array['feeds','articles','stories','claims','canonical','story_pairs','source_clusters',
                           'published','translations','runs'] loop
    execute format('create table shadow.%I (like public.%I including defaults including constraints including indexes)', t, t);
    execute format('insert into shadow.%I select * from public.%I', t, t);
    execute format('grant select, insert, update, delete on shadow.%I to nishpaksh_app', t);
  end loop;
end $$;
create view shadow.quota_usage as select * from public.quota_usage;
grant select, insert, update, delete on shadow.quota_usage to nishpaksh_app;
