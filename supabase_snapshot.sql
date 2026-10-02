-- Authoritative FatSecret snapshots; backfill's legacy append/upsert stays unchanged.
create or replace function public.pos_apply_fatsecret_snapshot(p_user_id uuid, p_days jsonb, p_entries jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n_days integer; n_entries integer; n_deleted integer; stamp timestamptz := now();
begin
  if p_user_id is null or jsonb_typeof(p_days) <> 'array' or jsonb_typeof(p_entries) <> 'array'
     or jsonb_array_length(p_days) < 1 or jsonb_array_length(p_days) > 14 or jsonb_array_length(p_entries) > 200 then
    raise exception 'invalid_snapshot';
  end if;
  if exists(select 1 from jsonb_to_recordset(p_entries) e(date date)
    where not exists(select 1 from jsonb_to_recordset(p_days) d(date date) where d.date=e.date)) then
    raise exception 'entry_outside_snapshot';
  end if;
  -- Serialize snapshots for this account, including overlapping Actions runs.
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  insert into public.pos_nutrition_daily(user_id,entry_date,calories,carbs_g,protein_g,fat_g,source,source_updated_at,synced_at)
  select p_user_id,d.date,d.calories,d.carbs,d.protein,d.fat,'fatsecret',stamp,stamp
  from jsonb_to_recordset(p_days) d(date date,calories numeric,carbs numeric,protein numeric,fat numeric)
  on conflict(user_id,entry_date,source) do update set calories=excluded.calories,carbs_g=excluded.carbs_g,protein_g=excluded.protein_g,fat_g=excluded.fat_g,source_updated_at=stamp,synced_at=stamp;
  get diagnostics n_days = row_count;
  insert into public.pos_nutrition_food_entries(user_id,fatsecret_id,entry_date,food,meal,calories,carbs_g,protein_g,fat_g,source,synced_at)
  select p_user_id,e.fatsecret_id,e.date,e.food,e.meal,e.calories,e.carbs,e.protein,e.fat,'fatsecret',stamp
  from jsonb_to_recordset(p_entries) e(fatsecret_id text,date date,food text,meal text,calories numeric,carbs numeric,protein numeric,fat numeric)
  on conflict(user_id,fatsecret_id) do update set entry_date=excluded.entry_date,food=excluded.food,meal=excluded.meal,calories=excluded.calories,carbs_g=excluded.carbs_g,protein_g=excluded.protein_g,fat_g=excluded.fat_g,source=excluded.source,synced_at=stamp;
  get diagnostics n_entries = row_count;
  delete from public.pos_nutrition_food_entries f
  where f.user_id=p_user_id and f.source='fatsecret'
    and f.entry_date in (select d.date from jsonb_to_recordset(p_days) d(date date))
    and not exists (select 1 from jsonb_to_recordset(p_entries) e(fatsecret_id text) where e.fatsecret_id=f.fatsecret_id);
  get diagnostics n_deleted = row_count;
  return jsonb_build_object('ok',true,'upserted',n_days,'upserted_entries',n_entries,'deleted_entries',n_deleted);
end $$;
revoke all on function public.pos_apply_fatsecret_snapshot(uuid,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.pos_apply_fatsecret_snapshot(uuid,jsonb,jsonb) to service_role;
