"""Importação de evidências; correspondências conservadoras e arquivos deduplicados."""
import hashlib
import json
import mimetypes
import re
import unicodedata
import urllib.request
import urllib.error
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from io import BytesIO

BUCKET = 'evidencias-desvios'

def normalizar(value):
    text = unicodedata.normalize('NFKD', str(value or '').lower())
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).strip()

def componentes(text):
    t = normalizar(text)
    rules = {
        'protetor': r'protet|protecao\s+lat|motqueiro|motoqueiro',
        'paralama': r'paralama|paralamo|para lama',
        'chassi': r'chassi|chasis',
        'mesa': r'\bmesa\b',
        'assoalho': r'assoalho',
        'malhal': r'malhal|vergalhao',
        'catraca': r'catraca|catrica|cathala',
        'lanterna': r'lanterna',
        'barrote': r'barrote|balrote',
        'mao_francesa': r'mao francesa',
        'lona': r'\blona\b',
        'tambor': r'tambor',
        'flange': r'flange|cara de cavalo',
        'para_choque': r'para choque',
        'grade': r'\bgrade\b',
    }
    return {k for k, pattern in rules.items() if re.search(pattern, t)}

def restricoes(text):
    t = normalizar(text)
    side = 'LE' if re.search(r'\bl\s*e\b|\ble\b|esquerd', t) else 'LD' if re.search(r'\bl\s*d\b|\bld\b|direit', t) else ''
    comp = re.search(r'([123])\s*(?:o\s*)?compartimento', t)
    return side, comp.group(1) if comp else ''

def ler_zip(data):
    """Não extrai arquivos. Número de frota é explícito; contexto próximo fica sinalizado."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(e.file_size for e in entries) > 500_000_000:
            raise ValueError('ZIP acima do limite de 5.000 arquivos ou 500 MB descompactados.')
        texts = [e for e in entries if e.filename.lower().endswith('.txt')]
        if len(texts) != 1:
            raise ValueError('O ZIP precisa conter um único TXT da conversa.')
        chat = archive.read(texts[0]).decode('utf-8-sig')
        start = re.compile(r'(?m)^(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}) - ([^\n]+)')
        matches = list(start.finditer(chat))
        messages = []
        for i, match in enumerate(matches):
            body = match.group(3) + chat[match.end():matches[i+1].start() if i+1 < len(matches) else len(chat)]
            if ': ' not in body:
                continue
            sender, body = body.split(': ', 1)
            media = re.findall(r'((?:IMG|VID)-[^\s]+\.(?:jpg|jpeg|png|webp|mp4))\s*\(arquivo anexado\)', body, re.I)
            caption = re.sub(r'\u200e?(?:IMG|VID)-[^\s]+\s*\(arquivo anexado\)', '', body).strip()
            fleets = sorted(set(re.findall(r'(?<!\d)((?:13|23|12)\d{3})(?!\d)', caption)))
            messages.append({'data':datetime.strptime(match.group(1)+' '+match.group(2), '%d/%m/%Y %H:%M'), 'autor':sender, 'legenda':caption, 'arquivos':media, 'frotas':fleets})
        names = {e.filename.rsplit('/', 1)[-1]:e for e in entries}
        records = {}
        for i, message in enumerate(messages):
            for name in message['arquivos']:
                if name not in names:
                    continue
                raw = archive.read(names[name])
                digest = hashlib.sha256(raw).hexdigest()
                fleets = message['frotas']
                origin = 'legenda' if len(fleets) == 1 else ''
                if not fleets:
                    # Referência explícita na mensagem imediatamente após a foto.
                    for following in messages[i+1:i+5]:
                        if not 0 <= (following['data']-message['data']).total_seconds() <= 60:
                            break
                        if following['arquivos'] and following['legenda']:
                            break
                        if not following['arquivos'] and len(following['frotas']) == 1:
                            fleets = following['frotas']; origin = 'mensagem seguinte'; break
                    # Contexto herdado é apenas uma sugestão, nunca autorização automática.
                    if not fleets:
                        for previous in reversed(messages[:i]):
                            if previous['autor'] != message['autor'] or (message['data']-previous['data']).total_seconds() > 300:
                                break
                            if previous['frotas']:
                                fleets = previous['frotas']; origin = 'contexto — conferir'; break
                item = {'arquivo':name, 'sha256':digest, 'data':message['data'].isoformat(), 'autor':message['autor'], 'legenda':message['legenda'], 'frota':fleets[0] if len(fleets)==1 else '', 'origem_frota':origin, 'tipo':mimetypes.guess_type(name)[0] or 'application/octet-stream', 'path':f'avarias-itr/arquivos/{digest}/{name}', 'ocorrencias':[]}
                if digest in records:
                    records[digest]['ocorrencias'].append({'data':item['data'], 'legenda':item['legenda'], 'frota':item['frota']})
                else:
                    records[digest] = item
        return list(records.values())

def propor_vinculo(item, rows):
    """Só propõe automaticamente quando data, frota e componente são inequívocos."""
    fleet = item.get('frota', '')
    if not fleet:
        return [], 'Frota não identificada'
    stamp = datetime.fromisoformat(item['data'])
    explicit = re.search(r'(?:ITR\s*(?:do|para)?\s*(?:dia)?\s*)(\d{2})/(\d{2})', item.get('legenda',''), re.I)
    ref = stamp.date()
    if explicit:
        ref = ref.replace(month=int(explicit.group(2)), day=int(explicit.group(1)))
    eligible = []
    for row in rows:
        if str(row.get('frota')) != fleet:
            continue
        try:
            ini = datetime.fromisoformat(row['inicio_manutencao'])
            fim = datetime.fromisoformat(row['fim_manutencao']) if row.get('fim_manutencao') else None
        except (ValueError, TypeError, KeyError):
            continue
        if explicit:
            relevant = ini.date() == ref
        else:
            relevant = ini.date() == ref or (fim is not None and ini <= stamp <= fim)
        if relevant:
            eligible.append(row)
    if not eligible:
        return [], 'Sem laudo correspondente à data'
    wanted = componentes(item.get('legenda',''))
    if len(wanted) != 1:
        return eligible, 'Descrição sem componente único — conferir imagem'
    side, comp = restricoes(item.get('legenda',''))
    caption = normalizar(item.get('legenda',''))
    if 'estrutural' in caption or 'travessa' in caption:
        return [], 'Descrição estrutural — conferir reparo específico'
    candidates = []
    for row in eligible:
        found = componentes(row.get('atividade',''))
        activity = normalizar(row.get('atividade',''))
        row_side, row_comp = restricoes(row.get('atividade',''))
        actual_comp = str(row.get('compartimento',''))[:1]
        if 'suporte' in caption and 'suporte' not in activity:
            continue
        if wanted.issubset(found) and not (side and row_side and side != row_side) and not (comp and actual_comp != comp):
            candidates.append(row)
    if len({str(r['os_id']) for r in eligible}) != 1:
        return candidates, 'Mais de uma OS possível'
    if len(candidates) != 1:
        return candidates, 'Atividade não identificada' if not candidates else 'Mais de uma atividade possível'
    if item.get('origem_frota') not in ('legenda', 'mensagem seguinte'):
        return candidates, 'Frota por contexto — conferir'
    return candidates, 'Correspondência única'

def request(url, key, path, method='GET', data=None, content_type='application/json'):
    headers = {'apikey':key, 'Authorization':f'Bearer {key}', 'Content-Type':content_type}
    req = urllib.request.Request(url.rstrip('/')+path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()

def carregar_laudos(url, key):
    rows = []; offset = 0
    while True:
        query = urllib.parse.urlencode({'select':'*', 'order':'id.asc', 'limit':1000, 'offset':offset})
        page = json.loads(request(url, key, '/rest/v1/laudos_monitor?'+query))
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)

def enviar_arquivo(url, key, item, raw):
    encoded = urllib.parse.quote(item['path'], safe='/')
    try:
        request(url, key, '/storage/v1/object/'+BUCKET+'/'+encoded, 'POST', raw, item['tipo'])
    except urllib.error.HTTPError as exc:
        # Conflito só é duplicidade depois de comparar os bytes já armazenados.
        existing = request(url, key, '/storage/v1/object/authenticated/'+BUCKET+'/'+encoded)
        if hashlib.sha256(existing).hexdigest() != item['sha256']:
            raise ValueError('Arquivo existente difere da evidência enviada.') from exc
    stored = request(url, key, '/storage/v1/object/authenticated/'+BUCKET+'/'+encoded)
    if hashlib.sha256(stored).hexdigest() != item['sha256']:
        raise ValueError('Falha na verificação da evidência enviada.')

def anexar(url, key, row, path):
    payload = {'p_id':int(row['id']), 'p_atividade_id':row['atividade_id'], 'p_os_id':row['os_id'], 'p_frota':row['frota'], 'p_path':path}
    return json.loads(request(url, key, '/rest/v1/rpc/anexar_evidencia_itr', 'POST', json.dumps(payload).encode()))

def salvar_manifesto(url, key, archive_hash, records):
    payload = {'versao':1, 'arquivo_hash':archive_hash, 'evidencias':records}
    path = '/storage/v1/object/'+BUCKET+'/avarias-itr/importacoes/'+archive_hash+'.json'
    # Manifestos são revisáveis; confirmação explícita pode atualizar seus vínculos.
    headers = {'apikey':key, 'Authorization':f'Bearer {key}', 'Content-Type':'application/json', 'x-upsert':'true'}
    req = urllib.request.Request(url.rstrip('/')+path, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=90) as resp:
        resp.read()

def render_importador(st, url, key):
    st.subheader('📷 Evidências de avarias — por atividade do laudo')
    st.caption('Importe o ZIP do grupo. Cada evidência é vinculada à atividade escolhida; casos duvidosos ficam pendentes. Reenvios são reconhecidos pelo conteúdo do arquivo.')
    up = st.file_uploader('ZIP do REGISTRO DE AVARIAS ITR', type=['zip'], key='zip_avarias_itr')
    if not up:
        render_importacoes_salvas(st, url, key)
        return
    archive_hash = hashlib.sha256(up.getvalue()).hexdigest()
    state_key = 'avarias_'+archive_hash
    if st.button('Ler conversa e consultar laudos', key='ler_avarias'):
        with st.spinner('Cruzando mensagens e atividades...'):
            records = ler_zip(up.getvalue()); rows = carregar_laudos(url, key)
            for item in records:
                candidates, reason = propor_vinculo(item, rows)
                item['candidatos'] = [r['id'] for r in candidates]
                item['situacao'] = reason
                item['selecionado'] = candidates[0]['id'] if reason == 'Correspondência única' else None
            st.session_state[state_key] = {'records':records, 'rows':rows}
    state = st.session_state.get(state_key)
    if not state:
        return
    records = state['records']; rows = state['rows']; lookup = {r['id']:r for r in rows}
    st.write(f'{len(records)} arquivos únicos • {sum(bool(i.get("selecionado")) for i in records)} correspondências propostas')
    fleet = st.text_input('Filtrar evidências por frota', key='frota_evidencias')
    visible = [i for i in records if not fleet or fleet in i.get('frota','')]
    labels = [f'{i["data"][:10]} | {i.get("frota") or "sem frota"} | {i["arquivo"]} | {i["situacao"]}' for i in visible]
    if visible:
        index = st.selectbox('Evidência para conferir', range(len(visible)), format_func=lambda n:labels[n])
        item = visible[index]
        with zipfile.ZipFile(BytesIO(up.getvalue())) as archive:
            entry = next(n for n in archive.namelist() if n.rsplit('/',1)[-1]==item['arquivo'])
            raw = archive.read(entry)
        if item['tipo'].startswith('image/'):
            st.image(raw, width=600)
        elif item['tipo'].startswith('video/'):
            st.video(raw)
        st.write(item.get('legenda') or 'Sem legenda na conversa.')
        fleet_edit = st.text_input('Frota conferida da evidência', item.get('frota',''), key='fr_'+item['sha256'])
        options = [r for r in rows if str(r['frota']) == fleet_edit]
        ids = [None]+[r['id'] for r in options]
        current = item.get('selecionado')
        chosen = st.selectbox('OS / compartimento / atividade', ids, index=ids.index(current) if current in ids else 0, format_func=lambda n:'Deixar pendente' if n is None else f'{lookup[n]["os_id"]} | {lookup[n]["inicio_manutencao"]} | {lookup[n]["compartimento"]} | {lookup[n]["atividade"]}', key='atividade_'+item['sha256'])
        item['selecionado'] = chosen
        item['frota'] = fleet_edit
    if st.button('Salvar arquivos e vincular atividades selecionadas', type='primary', key='salvar_avarias'):
        errors = []; linked = 0
        with zipfile.ZipFile(BytesIO(up.getvalue())) as archive:
            names = {n.rsplit('/',1)[-1]:n for n in archive.namelist()}
            progress = st.progress(0)
            for n, item in enumerate(records):
                try:
                    enviar_arquivo(url, key, item, archive.read(names[item['arquivo']]))
                    if item.get('selecionado'):
                        anexar(url, key, lookup[item['selecionado']], item['path']); linked += 1
                        item['situacao'] = 'Vinculada'
                    item['armazenado'] = True
                except Exception as exc:
                    errors.append(item['arquivo']+': '+str(exc))
                progress.progress((n+1)/len(records))
        try:
            salvar_manifesto(url, key, archive_hash, records)
        except Exception as exc:
            errors.append('Registro da importação: '+str(exc))
        if errors:
            st.error('Algumas evidências não foram salvas: '+' | '.join(errors[:5]))
        else:
            st.success(f'{linked} evidências vinculadas; {len(records)-linked} pendentes de conferência. Arquivos preservados.')

def render_importacoes_salvas(st, url, key):
    if not url or not key:
        st.info('Configure o acesso à base para consultar importações.'); return
    pages = []; offset = 0
    while True:
        payload = {'prefix':'avarias-itr/importacoes','limit':100,'offset':offset,'sortBy':{'column':'created_at','order':'desc'}}
        page = json.loads(request(url, key, '/storage/v1/object/list/'+BUCKET, 'POST', json.dumps(payload).encode()))
        pages.extend(x for x in page if x.get('name','').endswith('.json'))
        if len(page)<100: break
        offset += len(page)
    if not pages:
        st.info('Nenhuma importação de avarias salva.'); return
    name = st.selectbox('Retomar importação salva', [p['name'] for p in pages], format_func=lambda n:'Importação '+n[:12])
    encoded = urllib.parse.quote('avarias-itr/importacoes/'+name,safe='/')
    manifest = json.loads(request(url,key,'/storage/v1/object/authenticated/'+BUCKET+'/'+encoded))
    records = manifest['evidencias']
    rows = carregar_laudos(url,key); lookup = {r['id']:r for r in rows}
    linked_paths = {path for r in rows for path in json.loads(r.get('evidencias') or '[]')}
    pending = [i for i in records if i['path'] not in linked_paths]
    st.write(f'{len(records)-len(pending)} evidências vinculadas • {len(pending)} pendentes')
    fleet = st.text_input('Pesquisar frota ou arquivo',key='pendentes_busca')
    visible = [i for i in pending if not fleet or fleet in i.get('frota','') or fleet in i['arquivo']]
    if not visible:
        st.info('Nenhuma evidência pendente para esta busca.'); return
    item = st.selectbox('Evidência pendente',visible,format_func=lambda i:f'{i["data"][:10]} | {i.get("frota") or "sem frota"} | {i["arquivo"]}',key='pendente_item')
    data = request(url,key,'/storage/v1/object/authenticated/'+BUCKET+'/'+urllib.parse.quote(item['path'],safe='/'))
    if item['tipo'].startswith('image/'): st.image(data,width=600)
    elif item['tipo'].startswith('video/'): st.video(data)
    st.write(item.get('legenda') or 'Sem legenda na conversa.')
    st.caption(item.get('situacao','Pendente de conferência'))
    fr = st.text_input('Frota conferida',item.get('frota',''),key='salva_frota_'+item['sha256'])
    options = [r for r in rows if str(r['frota'])==fr]
    if not options:
        st.warning('Ainda não há atividades de laudo para esta frota.'); return
    ids = [None]+[r['id'] for r in options]
    chosen = st.selectbox('Escolher a atividade correlata',ids,format_func=lambda n:'Selecione a atividade' if n is None else f'{lookup[n]["os_id"]} | {lookup[n]["inicio_manutencao"]} | {lookup[n]["compartimento"]} | {lookup[n]["atividade"]}',key='salva_atividade_'+item['sha256'])
    if chosen and st.button('Confirmar vínculo nesta atividade',key='confirmar_'+item['sha256']):
        anexar(url,key,lookup[chosen],item['path'])
        st.success('Evidência vinculada à atividade.'); st.rerun()
