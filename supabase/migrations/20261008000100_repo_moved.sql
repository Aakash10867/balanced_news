-- The repository moved to the NishpakshNews organisation as NishpakshNews/NishpakshNews.github.io (owner, Oct 8 2026),
-- so the site is served at https://nishpakshnews.github.io. Both scheduled jobs are started at the new address.
-- 'github_dispatch_token' in Vault must be a token for the new repository (Actions read and write).
create or replace function public.dispatch_workflow(workflow text) returns bigint
language plpgsql security definer set search_path = public, extensions, vault as $$
declare
  tok text;
  req bigint;
begin
  select decrypted_secret into tok from vault.decrypted_secrets where name = 'github_dispatch_token' limit 1;
  if tok is null or tok = '' then
    raise notice 'github_dispatch_token not in vault; nothing dispatched';
    return null;
  end if;
  select net.http_post(
    url := 'https://api.github.com/repos/NishpakshNews/NishpakshNews.github.io/actions/workflows/' || workflow || '/dispatches',
    body := jsonb_build_object('ref', 'main', 'inputs', jsonb_build_object('reason', 'supabase-cron')),
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || tok,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'nishpaksh-scheduler',
      'Content-Type', 'application/json'),
    timeout_milliseconds := 10000) into req;
  return req;
end $$;
revoke all on function public.dispatch_workflow(text) from public, anon, authenticated;

create or replace function public.dispatch_pipeline() returns bigint
language sql security definer set search_path = public as $$ select public.dispatch_workflow('hourly.yml') $$;
create or replace function public.dispatch_desk() returns bigint
language sql security definer set search_path = public as $$ select public.dispatch_workflow('writer.yml') $$;
revoke all on function public.dispatch_pipeline() from public, anon, authenticated;
revoke all on function public.dispatch_desk() from public, anon, authenticated;
