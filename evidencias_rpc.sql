-- Apenas o servidor da Conferência pode chamar estas funções.
create or replace function public.anexar_evidencia_itr(p_id bigint, p_atividade_id text, p_os_id text, p_frota text, p_path text)
returns jsonb language plpgsql security invoker set search_path = public, pg_temp as $$
declare r public.laudos_monitor; paths jsonb;
begin
  select * into strict r from public.laudos_monitor where id=p_id;
  perform pg_advisory_xact_lock(hashtextextended(r.os_id||'/'||r.frota||'/'||r.compartimento,0));
  select * into strict r from public.laudos_monitor where id=p_id for update;
  if r.atividade_id is distinct from p_atividade_id or r.os_id is distinct from p_os_id or r.frota is distinct from p_frota then
    raise exception 'A atividade foi alterada. Atualize a consulta antes de vincular.';
  end if;
  if p_path !~ '^avarias-itr/arquivos/[a-f0-9]{64}/[A-Za-z0-9._-]+$' or not exists(select 1 from storage.objects where bucket_id='evidencias-desvios' and name=p_path) then
    raise exception 'Arquivo de evidência não encontrado ou caminho inválido.';
  end if;
  paths=coalesce(nullif(r.evidencias,''),'[]')::jsonb;
  if jsonb_typeof(paths)<>'array' then raise exception 'Lista de evidências inválida.'; end if;
  if not paths @> jsonb_build_array(p_path) then paths=paths||jsonb_build_array(p_path); end if;
  update public.laudos_monitor set evidencias=paths::text, atualizado_em=now() where id=r.id;
  return jsonb_build_object('id',r.id,'evidencias',paths);
end $$;
revoke all on function public.anexar_evidencia_itr(bigint,text,text,text,text) from public,anon,authenticated;
grant execute on function public.anexar_evidencia_itr(bigint,text,text,text,text) to service_role;

create or replace function public.salvar_laudo_preservando_evidencias(p_rows jsonb, p_os_id text, p_frota text, p_compartimento text)
returns jsonb language plpgsql security invoker set search_path = public, pg_temp as $$
declare item jsonb; incoming public.laudos_monitor; old public.laudos_monitor;
        matching bigint; matches integer; kept bigint[]='{}'; new_id bigint; total integer=0;
begin
  if jsonb_typeof(p_rows)<>'array' or jsonb_array_length(p_rows)=0 then raise exception 'Nenhuma atividade para salvar.'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_os_id||'/'||p_frota||'/'||p_compartimento,0));
  if exists(select 1 from jsonb_array_elements(p_rows) a group by a->>'atividade_id' having count(*)>1) then
    raise exception 'Identificador de atividade duplicado no lote.';
  end if;
  for item in select value from jsonb_array_elements(p_rows) loop
    incoming=jsonb_populate_record(null::public.laudos_monitor,item);
    if incoming.os_id is distinct from p_os_id or incoming.frota is distinct from p_frota or incoming.compartimento is distinct from p_compartimento or coalesce(incoming.atividade_id,'')='' or coalesce(incoming.atividade,'')='' then
      raise exception 'Atividade com vínculo incompleto ou incompatível.';
    end if;
    select count(*), min(id) into matches, matching from public.laudos_monitor l
      where l.os_id=p_os_id and l.frota=p_frota and l.compartimento=p_compartimento and not (l.id=any(kept))
      and lower(trim(l.atividade))=lower(trim(incoming.atividade))
      and (l.atividade_id=incoming.atividade_id or
        (coalesce(l.executante,'')=coalesce(incoming.executante,'') and l.inicio_atividade is not distinct from incoming.inicio_atividade and l.fim_atividade is not distinct from incoming.fim_atividade));
    if matches=1 then
      select * into old from public.laudos_monitor where id=matching for update;
      update public.laudos_monitor set registro=incoming.registro,status=incoming.status,
        inicio_manutencao=incoming.inicio_manutencao,fim_manutencao=incoming.fim_manutencao,
        atividade=incoming.atividade,executante=incoming.executante,classificacao=incoming.classificacao,
        inicio_atividade=incoming.inicio_atividade,fim_atividade=incoming.fim_atividade,horas=incoming.horas,
        atualizado_em=now() where id=matching;
      new_id=matching;
    else
      if exists(select 1 from public.laudos_monitor where atividade_id=incoming.atividade_id) then
        incoming.atividade_id=incoming.atividade_id||'_'||substr(md5(item::text||clock_timestamp()::text),1,12);
      end if;
      insert into public.laudos_monitor(registro,os_id,frota,compartimento,status,inicio_manutencao,fim_manutencao,
        atividade_id,atividade,executante,classificacao,inicio_atividade,fim_atividade,horas,evidencias)
      values(incoming.registro,p_os_id,p_frota,p_compartimento,incoming.status,incoming.inicio_manutencao,incoming.fim_manutencao,
        incoming.atividade_id,incoming.atividade,incoming.executante,incoming.classificacao,incoming.inicio_atividade,incoming.fim_atividade,incoming.horas,'[]') returning id into new_id;
    end if;
    kept=array_append(kept,new_id); total=total+1;
  end loop;
  if exists(select 1 from public.laudos_monitor where os_id=p_os_id and frota=p_frota and compartimento=p_compartimento and not(id=any(kept)) and coalesce(nullif(evidencias,''),'[]')::jsonb<>'[]'::jsonb) then
    raise exception 'Há atividade com evidência que não corresponde ao novo laudo. A gravação foi cancelada para preservar os anexos. Confira essa atividade antes de regravar.';
  end if;
  delete from public.laudos_monitor where os_id=p_os_id and frota=p_frota and compartimento=p_compartimento and not(id=any(kept));
  return jsonb_build_object('total',total,'evidencias_preservadas',true);
end $$;
revoke all on function public.salvar_laudo_preservando_evidencias(jsonb,text,text,text) from public,anon,authenticated;
grant execute on function public.salvar_laudo_preservando_evidencias(jsonb,text,text,text) to service_role;
