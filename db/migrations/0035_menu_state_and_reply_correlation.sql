-- ============================================================================
-- Phase 5 / 0035 -- interactive menu state + outbound reply correlation
-- ============================================================================
-- Additive only. No backfill and no table rewrite: every new column is either
-- nullable, or NOT NULL with a constant default (a catalog-only change on
-- PostgreSQL 11+). No new table is created, so platform_admin.config.REQUIRED_SCHEMA
-- is deliberately unchanged: menu code must degrade to text-only when these
-- columns are absent rather than turn a missing migration into a boot failure.
--
-- Deploy order: migration first, then code. Old code never selects these columns,
-- so applying them ahead of the deployment is inert.

-- ---------------------------------------------------------------------------
-- Conversations: where an interactive menu currently is.
-- menu_state is a whole document (path/cursor/mode), read and written as one
-- value -- the same JSONB rule as tenant_settings: JSONB only for documents that
-- are never filtered or joined.
-- ---------------------------------------------------------------------------
alter table public.conversations
  add column if not exists menu_state jsonb not null default '{}'::jsonb;

alter table public.conversations
  add column if not exists menu_state_expires_at timestamptz;

alter table public.conversations
  add column if not exists menu_state_version integer not null default 0;

alter table public.conversations
  add constraint conversations_menu_state_object
    check (jsonb_typeof(menu_state) = 'object');

comment on column public.conversations.menu_state is
  'Active interactive-menu position for this conversation; {} when no menu is active.';
comment on column public.conversations.menu_state_expires_at is
  'Absolute expiry of the active menu state; null when no menu is active.';
comment on column public.conversations.menu_state_version is
  'Optimistic-concurrency counter; every menu-state write increments it.';

-- ---------------------------------------------------------------------------
-- Messages: which inbound message an outbound message answers.
--
-- Null for every historical row and for inbound rows. Written only by the
-- webhook path when it records the intent to reply. The value is the inbound
-- provider message id (wamid), never the outbound one.
--
-- IMPORTANT: the unique index below is named messages_outbound_reply_unique and
-- platform_admin.services.customers matches that name to distinguish "this reply
-- was already recorded" from a genuine failure. Renaming it silently removes the
-- idempotency guarantee -- add a new index instead.
-- ---------------------------------------------------------------------------
alter table public.messages
  add column if not exists reply_to_provider_message_id text;

alter table public.messages
  add constraint messages_reply_to_provider_check
    check (reply_to_provider_message_id is null
           or length(btrim(reply_to_provider_message_id)) between 1 and 200);

comment on column public.messages.reply_to_provider_message_id is
  'Inbound provider message id this outbound message answers. With the partial unique '
  'index below it is the at-most-one-reply-per-inbound-message guarantee.';

-- At most one outbound row per inbound provider message, per tenant and channel.
-- Partial, so inbound rows and historical outbound rows (null) are unaffected.
--
-- Scope note: this encodes "one reply per inbound message", which is exactly what
-- the webhook does today. A future multi-message reply (for example a menu followed
-- by a confirmation) would need a different key, not a relaxation of this one.
create unique index if not exists messages_outbound_reply_unique
  on public.messages (tenant_id, channel_id, reply_to_provider_message_id)
  where direction = 'outbound' and reply_to_provider_message_id is not null;

-- Supports the expiry sweep and "which conversations still hold menu state".
create index if not exists conversations_menu_state_expiry_idx
  on public.conversations (menu_state_expires_at)
  where menu_state_expires_at is not null;
