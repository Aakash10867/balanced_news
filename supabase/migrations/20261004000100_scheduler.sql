-- Hourly trigger from inside Supabase. GitHub's own schedule is best-effort and skipped hours at a
-- time; pg_cron fires on time and asks GitHub to start the pipeline workflow (workflow_dispatch).
-- The GitHub token lives in Vault as 'github_dispatch_token' (fine-grained, this repo only,
-- Actions: read and write). Without it the job does nothing.
create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function public.dispatch_pipeline() returns bigint
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
    url := 'https://api.github.com/repos/Aakash10867/balanced_news/actions/workflows/hourly.yml/dispatches',
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

revoke all on function public.dispatch_pipeline() from public, anon, authenticated;

select cron.unschedule(jobid) from cron.job where jobname = 'nishpaksh-hourly';
select cron.schedule('nishpaksh-hourly', '5 * * * *', 'select public.dispatch_pipeline()');
