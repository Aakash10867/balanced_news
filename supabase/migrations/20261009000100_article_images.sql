-- The picture each outlet publishes for its share previews (og:image), Oct 9 2026. Only the link is kept:
-- '' = looked and found none, NULL = not looked yet. Non-destructive: one new nullable column.
alter table public.articles add column if not exists image varchar(1500);
