-- A migration that has to be replayable and lock-safe.
create table analytics.events (
    id bigserial primary key,
    kind text not null,
    payload jsonb,
    created_at timestamptz not null default now()
);

alter table analytics.events alter column kind type varchar(64);

drop table analytics.legacy_events;
