-- 0039: per-conversation AI control and dispatch audit.
--
-- Inbox Phase 2 makes the backend authoritative for whether automation may reply:
-- ``ai_mode`` gates every automated customer-facing send, and every control change
-- (plus system suppressions) is recorded in ``conversation_control_events``.
--
-- Role model follows the post-0030 rule (0033): runtime permissions for
-- ``app_backend`` only. RLS is scoped with app.current_tenant_id() (0001), the same
-- predicate used by every other tenant table. The composite foreign key mirrors the
-- messages -> conversations pattern (0033) and is supported by
-- ``conversations_id_tenant_key unique (id, tenant_id)``.
--
-- Backfill: ``ai_mode = 'ai_active'`` preserves existing behaviour exactly (the bot
-- keeps replying until an operator pauses or takes over) and ``needs_attention`` is
-- false for every existing conversation. No rows are rewritten.

alter table public.conversations
  add column if not exists ai_mode text not null default 'ai_active',
  add column if not exists needs_attention boolean not null default false;

alter table public.conversations
  drop constraint if exists conversations_ai_mode_check;
alter table public.conversations
  add constraint conversations_ai_mode_check
  check (ai_mode in ('ai_active', 'paused', 'human'));

create table if not exists public.conversation_control_events (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references public.tenants (id) on delete cascade,
  conversation_id uuid not null,
  -- Supabase Auth user id of the actor when a human acted; NULL for system work
  -- (0009 audit_log convention). Never a fabricated sentinel UUID.
  actor_user_id   uuid,
  actor_type      text not null default 'system',
  event_type      text not null,
  previous_mode   text,
  new_mode        text,
  metadata        jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  constraint conversation_control_events_actor_type_check
    check (actor_type in ('user', 'system')),
  constraint conversation_control_events_actor_pair_check
    check ((actor_type = 'system') = (actor_user_id is null)),
  constraint conversation_control_events_event_type_check
    check (event_type in (
      'ai_paused', 'ai_resumed', 'human_takeover',
      'needs_attention_set', 'needs_attention_cleared',
      'ai_reply_suppressed'
    )),
  constraint conversation_control_events_system_event_check
    check ((event_type <> 'ai_reply_suppressed') or (actor_type = 'system')),
  constraint conversation_control_events_metadata_check
    check (jsonb_typeof(metadata) = 'object'),
  foreign key (conversation_id, tenant_id)
    references public.conversations (id, tenant_id) on delete cascade
);

create index if not exists conversation_control_events_tenant_time_idx
  on public.conversation_control_events (tenant_id, created_at desc);
create index if not exists conversation_control_events_conversation_idx
  on public.conversation_control_events (tenant_id, conversation_id, created_at desc);

alter table public.conversation_control_events enable row level security;

drop policy if exists conversation_control_events_backend_select
  on public.conversation_control_events;
create policy conversation_control_events_backend_select
  on public.conversation_control_events
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

drop policy if exists conversation_control_events_backend_insert
  on public.conversation_control_events;
create policy conversation_control_events_backend_insert
  on public.conversation_control_events
  for insert to app_backend
  with check (tenant_id = app.current_tenant_id());

revoke all on public.conversation_control_events from public;
grant select, insert on public.conversation_control_events to app_backend;

comment on table public.conversation_control_events is
  'Immutable audit of conversation AI-control changes and system reply suppressions.';
comment on column public.conversation_control_events.actor_user_id is
  'Supabase Auth user id of the actor, when a human acted. NULL for system work.';
comment on column public.conversations.ai_mode is
  'ai_active: automation may reply. paused: automation must not reply. human: a person owns the thread; automation must not reply until an explicit resume.';