-- Protege anexos inclusive contra a republicação integral de versões antigas.
create schema if not exists laudos_internal;
revoke all on schema laudos_internal from public,anon,authenticated;
create or replace function laudos_internal.proteger_evidencias()
returns trigger language plpgsql security invoker set search_path=public,pg_temp as $$
declare previous jsonb; upcoming jsonb;
begin
  previous=coalesce(nullif(old.evidencias,''),'[]')::jsonb;
  if tg_op='DELETE' then
    if previous<>'[]'::jsonb then
      raise exception 'Esta atividade tem evidências anexadas. Use a Conferência de Laudos atualizada para sincronizar preservando os anexos.';
    end if;
    return old;
  end if;
  upcoming=coalesce(nullif(new.evidencias,''),'[]')::jsonb;
  if jsonb_typeof(previous)<>'array' or jsonb_typeof(upcoming)<>'array' then raise exception 'Lista de evidências inválida.'; end if;
  if previous<>'[]'::jsonb then
    new.evidencias=(select coalesce(jsonb_agg(value order by first_order),'[]'::jsonb) from
      (select value,min(ordinality) first_order from jsonb_array_elements(previous||upcoming) with ordinality group by value) entries)::text;
  end if;
  return new;
end $$;
revoke all on function laudos_internal.proteger_evidencias() from public,anon,authenticated;
drop trigger if exists proteger_evidencias_laudos on public.laudos_monitor;
create trigger proteger_evidencias_laudos before update of evidencias or delete on public.laudos_monitor
for each row execute function laudos_internal.proteger_evidencias();
