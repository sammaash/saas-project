-- Dashboard users may view import-batch status, but cannot mutate import batches.
grant select on public.catalogue_import_batches to authenticated;
