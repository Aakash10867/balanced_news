-- pg_cron runs in UTC: the pipeline starts at :05 UTC (:35 IST). The desk moves from :35 to :45 UTC
-- (:15 IST), 40 minutes after the pipeline starts, so it overlaps only an unusually long pipeline run
-- (Oct 6 2026: runs took 15-50 minutes).
select cron.unschedule(jobid) from cron.job where jobname = 'nishpaksh-desk';
select cron.schedule('nishpaksh-desk', '45 * * * *', 'select public.dispatch_desk()');
