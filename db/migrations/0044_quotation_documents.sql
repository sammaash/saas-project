-- ============================================================================
-- 0044 -- stored, immutable quotation PDF documents (portal redesign Stage 3)
-- ============================================================================
-- One PDF per issued quotation, rendered once from the quotation's immutable snapshot
-- (quotations + quotation_lines) and kept so that what staff download is byte-for-byte
-- what the customer was sent, regardless of later catalogue or profile edits.
--
-- Security model (same as 0037, tightened):
--   * tenant-scoped rows; RLS enabled AND forced; policies for app_backend only, keyed
--     on app.current_tenant_id();
--   * app_backend may SELECT and INSERT only -- no UPDATE, no DELETE grant;
--   * a trigger rejects UPDATE for every role (including the table owner), so a stored
--     document can never be altered once written;
--   * DELETE happens only through the ON DELETE CASCADE of the owning quotation/tenant
--     (referential actions run as the table owner, not as an application role);
--   * no grant to anon/authenticated/service_role: the Data API cannot reach the bytes.
--     The tenant portal serves documents through the backend after its own membership
--     check.
--
-- Additive only: no existing table, column, policy or grant is changed.
-- ============================================================================

create table public.quotation_documents (
  quotation_id  uuid primary key,
  tenant_id     uuid not null references public.tenants (id) on delete cascade,
  content_type  text not null default 'application/pdf',
  content       bytea not null,
  size_bytes    integer not null,
  sha256        text not null,
  created_at    timestamptz not null default now(),
  constraint quotation_documents_quotation_fk
    foreign key (quotation_id, tenant_id)
    references public.quotations (id, tenant_id) on delete cascade,
  constraint quotation_documents_content_type_check
    check (content_type = 'application/pdf'),
  constraint quotation_documents_size_check
    check (size_bytes between 1 and 5242880 and size_bytes = octet_length(content)),
  constraint quotation_documents_pdf_signature_check
    check (substring(content from 1 for 5) = '\x255044462d'::bytea),
  constraint quotation_documents_sha256_check
    check (sha256 ~ '^[0-9a-f]{64}$')
);

create index quotation_documents_tenant_idx
  on public.quotation_documents (tenant_id, created_at desc);

comment on table public.quotation_documents is
  'Immutable PDF rendering of an issued quotation snapshot (one per quotation).';

-- ---------------------------------------------------------------------------
-- Immutability: no role may rewrite a stored document.
-- ---------------------------------------------------------------------------
create or replace function app.quotation_documents_immutable()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  raise exception 'quotation documents are immutable'
    using errcode = '42501';
end;
$$;

revoke all on function app.quotation_documents_immutable() from public;

create trigger quotation_documents_no_update
  before update on public.quotation_documents
  for each row execute function app.quotation_documents_immutable();

-- ---------------------------------------------------------------------------
-- Row Level Security: tenant-scoped, backend only.
-- ---------------------------------------------------------------------------
alter table public.quotation_documents enable row level security;
alter table public.quotation_documents force row level security;

create policy quotation_documents_backend_select on public.quotation_documents
  for select to app_backend
  using (tenant_id = app.current_tenant_id());

create policy quotation_documents_backend_insert on public.quotation_documents
  for insert to app_backend
  with check (tenant_id = app.current_tenant_id());

-- ---------------------------------------------------------------------------
-- Grants: app_backend SELECT + INSERT only. Revoke Supabase default grants.
-- ---------------------------------------------------------------------------
revoke all on public.quotation_documents from public, anon, authenticated;
grant select, insert on public.quotation_documents to app_backend;

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'service_role') then
    execute 'revoke all on public.quotation_documents from service_role';
  end if;
end
$$;
