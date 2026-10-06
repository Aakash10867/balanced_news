-- The writing desk (nishpaksh/desk.py, workflow writer.yml) runs on its own at :35 every hour, after
-- the pipeline started at :05 has prepared stories (owner, Oct 6 2026). Same token as the pipeline
-- ('github_dispatch_token' in Vault: this repo, Actions read and write).
create or replace function public.dispatch_desk() returns bigint
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
    url := 'https://api.github.com/repos/Aakash10867/balanced_news/actions/workflows/writer.yml/dispatches',
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

revoke all on function public.dispatch_desk() from public, anon, authenticated;

select cron.unschedule(jobid) from cron.job where jobname = 'nishpaksh-desk';
select cron.schedule('nishpaksh-desk', '35 * * * *', 'select public.dispatch_desk()');
