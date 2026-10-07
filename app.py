import os, re, json, base64, hashlib, urllib.request, urllib.error, urllib.parse
from io import BytesIO
from datetime import datetime, date, time as dt_time
import pandas as pd
import streamlit as st
from PIL import Image
import fitz

st.set_page_config(page_title='10 Sul • Conferência de Laudos', page_icon='📄', layout='wide')

def secret(name, default=''):
    try: return str(st.secrets.get(name, default)).strip()
    except Exception: return os.getenv(name, default).strip()

GEMINI_API_KEY = secret('GEMINI_API_KEY') or secret('GOOGLE_API_KEY')
GEMINI_MODEL = secret('GEMINI_VISION_MODEL', 'gemini-3.5-flash-lite')
SUPABASE_URL = secret('SUPABASE_URL').rstrip('/')
SUPABASE_SERVICE_KEY = secret('SUPABASE_SERVICE_KEY')

st.markdown('''<style>.block-container{max-width:1500px;padding-top:1.2rem}.stButton>button{border-radius:10px;font-weight:700}[data-testid="stMetric"]{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:10px 14px}</style>''', unsafe_allow_html=True)

def buscar_ultima_itr_frota(frota):
    """Busca na base publicada pelo sistema principal somente a ITR mais recente da frota."""
    fr=re.sub(r'\D','',str(frota or ''))
    if not fr:return None,'Informe a frota.'
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:return None,'Supabase não configurado.'
    params=urllib.parse.urlencode({
        'select':'os_id,frota,evento,inicio,fim',
        'frota':f'eq.{fr}',
        'evento':'ilike.*ITR*',
        'order':'fim.desc.nullslast,inicio.desc',
        'limit':'1'
    })
    url=f'{SUPABASE_URL}/rest/v1/monitor_atendimentos?{params}'
    req=urllib.request.Request(url,headers={'apikey':SUPABASE_SERVICE_KEY,'Authorization':f'Bearer {SUPABASE_SERVICE_KEY}'})
    try:
        with urllib.request.urlopen(req,timeout=30) as resp:
            rows=json.loads(resp.read().decode() or '[]')
        if not rows:return None,f'Nenhuma ITR encontrada para a frota {fr}.'
        return rows[0],None
    except urllib.error.HTTPError as e:
        return None,f'Supabase HTTP {e.code}: '+e.read().decode(errors='ignore')[:500]
    except Exception as e:return None,str(e)

def parse_json(text):
    text=re.sub(r'^```(?:json)?\s*','',str(text or '').strip(),flags=re.I); text=re.sub(r'\s*```$','',text); a,b=text.find('{'),text.rfind('}')
    if a>=0 and b>a:text=text[a:b+1]
    return json.loads(text)

def preparar_imagem(png_bytes):
    img=Image.open(BytesIO(png_bytes)).convert('RGB')
    if max(img.size)>1800:
        k=1800/max(img.size); img=img.resize((int(img.width*k),int(img.height*k)))
    return img

def jpg_b64(img):
    bio=BytesIO(); img.save(bio,format='JPEG',quality=90,optimize=True)
    return base64.b64encode(bio.getvalue()).decode('ascii')

def detectar_orientacao(png_bytes):
    """Decide 0/180 antes da extração. O laudo é paisagem; frente/verso pode vir invertido."""
    if not GEMINI_API_KEY:return 0
    img=preparar_imagem(png_bytes)
    a=jpg_b64(img); b=jpg_b64(img.rotate(180,expand=True))
    prompt='''Estas duas imagens são a MESMA página de um formulário "LAUDO DE INSPEÇÃO DE CARRETA", uma normal e outra girada 180 graus.
Escolha qual está em pé para leitura humana: título no topo, FROTA/SR no cabeçalho, tabela abaixo e OBSERVAÇÕES no rodapé.
Retorne SOMENTE JSON {"rotacao":0} se a PRIMEIRA estiver correta, ou {"rotacao":180} se a SEGUNDA estiver correta.'''
    payload={'contents':[{'role':'user','parts':[{'text':prompt},{'inline_data':{'mime_type':'image/jpeg','data':a}},{'inline_data':{'mime_type':'image/jpeg','data':b}}]}],
             'generationConfig':{'responseMimeType':'application/json','temperature':0.0,'maxOutputTokens':100}}
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'x-goog-api-key':GEMINI_API_KEY,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=90) as resp: raw=json.loads(resp.read().decode())
        txt='\n'.join(p.get('text','') for p in raw.get('candidates',[{}])[0].get('content',{}).get('parts',[]) if p.get('text'))
        return 180 if int(parse_json(txt).get('rotacao',0))==180 else 0
    except Exception:
        return 0

def normalizar_orientacao(png_bytes):
    img=preparar_imagem(png_bytes)
    rot=detectar_orientacao(png_bytes)
    if rot==180: img=img.rotate(180,expand=True)
    bio=BytesIO(); img.save(bio,format='PNG')
    return bio.getvalue(),rot

def image_versions(png_bytes):
    # Mantido para a prévia manual: agora retorna somente a imagem já preparada.
    img=preparar_imagem(png_bytes)
    bio=BytesIO(); img.save(bio,format='PNG')
    return bio.getvalue(),[jpg_b64(img)]

def ler_gemini(png_bytes, ja_orientada=False):
    if not GEMINI_API_KEY:return {'_erro':'GEMINI_API_KEY não configurada nos Secrets.'}
    if ja_orientada:
        img=preparar_imagem(png_bytes); rot=0
        bio=BytesIO(); img.save(bio,format='PNG'); preview=bio.getvalue()
    else:
        preview,rot=normalizar_orientacao(png_bytes)
        img=preparar_imagem(preview)
    prompt='''Leia este formulário 10 Sul "LAUDO DE INSPEÇÃO DE CARRETA". A página JÁ FOI COLOCADA NA ORIENTAÇÃO CORRETA. Não gire mentalmente e não misture campos.

REGRAS FIXAS DO FORMULÁRIO:
1. FROTA: leia SOMENTE o campo "FROTA / SR:" no canto superior esquerdo. Deve ser o número da frota. Nunca coloque nome de pessoa em FROTA.
2. ID/OS: somente preencha se existir um número explicitamente identificado como ID, Nº OS, N° OS ou ORDEM DE SERVIÇO. O campo "OS / EVENTO" pode conter ITR e NÃO é o ID. "Nº LAUDO" também NÃO é ID. Se não houver ID/OS explícito, retorne vazio. NUNCA copie a FROTA para ID.
3. DATA DA INSPEÇÃO: leia somente o campo com esse rótulo.
4. INÍCIO MANUTENÇÃO e FIM MANUTENÇÃO: leia SOMENTE esses dois campos do cabeçalho. NÃO use o quadro "TEMPO DA ITR".
5. Cada linha da tabela possui ATIVIDADE/SERVIÇO, EXECUTANTE, marcação ITR/CNP/GM/OUTROS e TEMPO REAL INÍCIO/FIM. Preserve a associação horizontal da MESMA LINHA.
6. EXECUTANTE vem exclusivamente da coluna EXECUTANTE. Se vazio/ilegível, retorne vazio.
7. INÍCIO e FIM de atividade vêm exclusivamente das colunas "TEMPO REAL > INÍCIO" e "TEMPO REAL > FIM". Não use TEMPO ESTIMADO nem TEMPO DA ITR.
8. Classificação: marque somente a coluna que tiver X na mesma linha. Se não for possível identificar, use OUTROS.
9. Compartimento: identifique 1º, 2º ou 3º pelo título da tabela.
10. Não invente. Campo duvidoso deve ficar vazio.

Retorne SOMENTE JSON:
{"id_os":"","frota":"","data_inspecao":"","compartimento":"1º","inicio_manutencao":"","fim_manutencao":"","atividades":[{"servico":"","executante":"","inicio":"","fim":"","classificacao":"ITR"}],"confianca":0.0}'''
    payload={'contents':[{'role':'user','parts':[{'text':prompt},{'inline_data':{'mime_type':'image/jpeg','data':jpg_b64(img)}}]}],
             'generationConfig':{'responseMimeType':'application/json','temperature':0.0,'maxOutputTokens':8000}}
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'x-goog-api-key':GEMINI_API_KEY,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=150) as resp:raw=json.loads(resp.read().decode())
        txt='\n'.join(p.get('text','') for p in raw.get('candidates',[{}])[0].get('content',{}).get('parts',[]) if p.get('text'))
        data=parse_json(txt); data['_rotacao_aplicada']=rot
        frota=re.sub(r'\D','',str(data.get('frota','') or ''))
        osid=re.sub(r'\D','',str(data.get('id_os','') or ''))
        data['frota']=frota
        data['id_os']='' if (osid and frota and osid==frota) else osid
        return data
    except urllib.error.HTTPError as e:return {'_erro':f'Gemini HTTP {e.code}: '+e.read().decode(errors='ignore')[:800]}
    except Exception as e:return {'_erro':f'{type(e).__name__}: {e}'}

def hora(v):
    t=str(v or '').strip()
    for f in ('%H:%M','%H:%M:%S'):
        try:return datetime.strptime(t,f).time()
        except:pass
    return None

def supabase_upsert(rows,os_id,frota,compartimento):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:return False,'Supabase ainda não configurado nos Secrets.'
    headers={'apikey':SUPABASE_SERVICE_KEY,'Authorization':f'Bearer {SUPABASE_SERVICE_KEY}','Content-Type':'application/json','Prefer':'return=minimal'}
    qs=urllib.parse.urlencode({'os_id':f'eq.{os_id}','frota':f'eq.{frota}','compartimento':f'eq.{compartimento}'})
    try:
        req=urllib.request.Request(f'{SUPABASE_URL}/rest/v1/laudos_monitor?{qs}',method='DELETE',headers=headers)
        with urllib.request.urlopen(req,timeout=45) as r:r.read()
        req=urllib.request.Request(f'{SUPABASE_URL}/rest/v1/laudos_monitor',data=json.dumps(rows,ensure_ascii=False).encode(),method='POST',headers=headers)
        with urllib.request.urlopen(req,timeout=60) as r:r.read()
        return True,f'{len(rows)} atividade(s) sincronizada(s).'
    except urllib.error.HTTPError as e:return False,f'Supabase HTTP {e.code}: '+e.read().decode(errors='ignore')[:900]
    except Exception as e:return False,str(e)

def iso(dt):return pd.Timestamp(dt).strftime('%Y-%m-%dT%H:%M:%S')


def pagina_para_linhas(data, pagina):
    if not data or data.get('_erro'):
        return []
    osid=str(data.get('id_os','') or '').strip()
    frota=str(data.get('frota','') or '').strip()
    d0=pd.to_datetime(data.get('data_inspecao'),dayfirst=True,errors='coerce')
    if pd.isna(d0): d0=pd.Timestamp.today()
    dia=d0.date()
    def osdt(v):
        s=str(v or '').strip()
        z=pd.to_datetime(s,dayfirst=True,errors='coerce')
        if not pd.isna(z) and (':' in s or 'T' in s): return z.strftime('%d/%m/%Y %H:%M')
        h=hora(s)
        return datetime.combine(dia,h).strftime('%d/%m/%Y %H:%M') if h else ''
    ini_os=osdt(data.get('inicio_manutencao'))
    fim_os=osdt(data.get('fim_manutencao'))
    rows=[]
    for a in data.get('atividades',[]) or []:
        serv=str(a.get('servico','') or '').strip()
        if not serv: continue
        cl=str(a.get('classificacao','') or '').upper().strip()
        rows.append({
            'SELECIONAR':True,'ID':osid,'FROTA':frota,'INICIO OS':ini_os,'FIM OS':fim_os,
            'ATIVIDADE/DESCRIÇÃO':serv,'EXECUTANTE':str(a.get('executante','') or '').strip(),
            'ITR':cl=='ITR','CNP':cl=='CNP','GM':cl=='GM','OUTROS':cl=='OUTROS',
            'INICIO':str(a.get('inicio','') or '').strip(),'FIM':str(a.get('fim','') or '').strip(),
            '_PAGINA':int(pagina),'_COMPARTIMENTO':str(data.get('compartimento','1º') or '1º')
        })
    return rows

def salvar_lote(df):
    erros=[]; total=0
    if df is None or df.empty: return False,'Nenhuma atividade para salvar.'
    work=df.copy()
    if 'SELECIONAR' in work.columns:
        work=work[work['SELECIONAR'].fillna(False).astype(bool)].copy()
    if work.empty:return False,'Nenhum laudo marcado para gravar.'
    for (osid,frota,comp),g in work.groupby(['ID','FROTA','_COMPARTIMENTO'],dropna=False):
        if not str(osid).strip() or not str(frota).strip():
            erros.append('Há linha sem ID ou FROTA.'); continue
        rows=[]
        for i,r in g.iterrows():
            ativ=str(r.get('ATIVIDADE/DESCRIÇÃO','') or '').strip()
            if not ativ: continue
            marc=[x for x in ['ITR','CNP','GM','OUTROS'] if bool(r.get(x,False))]
            if len(marc)!=1:
                erros.append(f'{osid} / {frota}: marque exatamente um tipo em {ativ}.'); continue
            try:
                ini_os=pd.to_datetime(r.get('INICIO OS'),dayfirst=True)
                fim_os=pd.to_datetime(r.get('FIM OS'),dayfirst=True)
            except:
                erros.append(f'{osid} / {frota}: confira INICIO OS e FIM OS.'); continue
            h1,h2=hora(r.get('INICIO')),hora(r.get('FIM'))
            if not h1 or not h2:
                erros.append(f'{osid} / {frota}: confira INICIO/FIM da atividade {ativ}.'); continue
            d1=pd.Timestamp(datetime.combine(ini_os.date(),h1)); d2=pd.Timestamp(datetime.combine(ini_os.date(),h2))
            if d2<d1:d2+=pd.Timedelta(days=1)
            reg=f'WEB_LOTE_{st.session_state.get("pdf_hash","")}_{int(r.get("_PAGINA",0))}'
            rows.append({'registro':reg,'os_id':str(osid).strip(),'frota':str(frota).strip(),'compartimento':str(comp),'status':'CONFERIDO',
                'inicio_manutencao':iso(ini_os),'fim_manutencao':iso(fim_os),'atividade_id':f'{reg}_{i}',
                'atividade':ativ,'executante':str(r.get('EXECUTANTE','') or '').strip(),'classificacao':marc[0],
                'inicio_atividade':iso(d1),'fim_atividade':iso(d2),'horas':max(0,(d2-d1).total_seconds()/3600),'evidencias':'[]'})
        if rows:
            ok,msg=supabase_upsert(rows,str(osid).strip(),str(frota).strip(),str(comp))
            if ok: total+=len(rows)
            else: erros.append(f'{osid}/{frota}: {msg}')
    return (len(erros)==0, f'{total} atividade(s) sincronizada(s).' if not erros else ' | '.join(erros[:5]))

st.title('📄 10 Sul • Central de Conferência de Laudos')
st.caption('Portal independente para leitura e conferência de laudos escaneados. Somente dados confirmados são enviados ao monitor.')
pdf=st.file_uploader('📤 Selecione o PDF com os laudos escaneados',type=['pdf'])
if not pdf:st.info('Envie um PDF para iniciar a conferência página por página.');st.stop()
pdf_bytes=pdf.getvalue();pdf_hash=hashlib.sha256(pdf_bytes).hexdigest()[:16]
try:doc=fitz.open(stream=pdf_bytes,filetype='pdf')
except Exception as e:st.error(f'Não foi possível abrir o PDF: {e}');st.stop()
n=len(doc)
if st.session_state.get('pdf_hash')!=pdf_hash:
    st.session_state['pdf_hash']=pdf_hash;st.session_state['pag']=1;st.session_state['ok_pages']=set();st.session_state.pop('leitura',None)
ok_pages=st.session_state.setdefault('ok_pages',set())
m1,m2,m3=st.columns(3);m1.metric('PÁGINAS',n);m2.metric('CONFERIDAS',len(ok_pages));m3.metric('PENDENTES',n-len(ok_pages));st.progress(len(ok_pages)/max(n,1),text=f'{len(ok_pages)}/{n} páginas conferidas nesta sessão')


st.divider()
st.subheader('🚀 Lançamento em lote')
st.caption('Lê todas as páginas com Gemini, monta uma linha por atividade e só grava depois da sua conferência.')
bulk_key=f'bulk_{pdf_hash}'
if st.button(f'🤖 LER TODAS AS {n} PÁGINAS COM GEMINI',type='primary',use_container_width=True,key='ler_todas'):
    linhas=[]; falhas=[]; barra=st.progress(0,text='Iniciando leitura em lote...')
    for p in range(1,n+1):
        pg=doc.load_page(p-1); px=pg.get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False); img=px.tobytes('png')
        img_corrigida,rot=normalizar_orientacao(img)
        dados=ler_gemini(img_corrigida,ja_orientada=True)
        dados['_rotacao_aplicada']=rot
        if dados.get('_erro'): falhas.append(f'Página {p}: {dados["_erro"]}')
        else: linhas.extend(pagina_para_linhas(dados,p))
        barra.progress(p/max(n,1),text=f'Lendo página {p} de {n}...')
    st.session_state[bulk_key]=pd.DataFrame(linhas)
    st.session_state[f'{bulk_key}_falhas']=falhas
    barra.empty()
    st.rerun()

bulk=st.session_state.get(bulk_key)
if isinstance(bulk,pd.DataFrame):
    falhas=st.session_state.get(f'{bulk_key}_falhas',[])
    c1,c2,c3=st.columns(3)
    c1.metric('ATIVIDADES LIDAS',len(bulk))
    c2.metric('LAUDOS/OS',bulk['ID'].nunique() if not bulk.empty else 0)
    c3.metric('PÁGINAS COM FALHA',len(falhas))
    if falhas:
        with st.expander('⚠️ Ver páginas que precisam ser relidas'):
            for x in falhas: st.warning(x)
    if bulk.empty:
        st.warning('O Gemini não encontrou atividades nas páginas.')
    else:
        cols=['SELECIONAR','ID','FROTA','INICIO OS','FIM OS','ATIVIDADE/DESCRIÇÃO','EXECUTANTE','ITR','CNP','GM','OUTROS','INICIO','FIM','_PAGINA','_COMPARTIMENTO']
        for c in cols:
            if c not in bulk.columns: bulk[c]=True if c=='SELECIONAR' else ''
        st.markdown('#### 🔎 Buscar última ITR pela frota')
        q1,q2=st.columns([2,1])
        frota_busca=q1.text_input('FROTA',placeholder='Ex.: 13809',key=f'buscar_frota_{bulk_key}')
        if q2.button('🔎 BUSCAR ÚLTIMA ITR',use_container_width=True,key=f'btn_buscar_itr_{bulk_key}'):
            with st.spinner('Buscando na base do sistema principal...'):
                achado,erro=buscar_ultima_itr_frota(frota_busca)
            if erro:
                st.session_state.pop(f'itr_achada_{bulk_key}',None)
                st.error(erro)
            else:
                st.session_state[f'itr_achada_{bulk_key}']=achado
        achado=st.session_state.get(f'itr_achada_{bulk_key}')
        if achado:
            ini=pd.to_datetime(achado.get('inicio'),errors='coerce')
            fim=pd.to_datetime(achado.get('fim'),errors='coerce')
            resumo=pd.DataFrame([{
                'ID':str(achado.get('os_id','')),
                'FROTA':str(achado.get('frota','')),
                'INICIO':'' if pd.isna(ini) else ini.strftime('%d/%m/%Y %H:%M'),
                'FIM':'' if pd.isna(fim) else fim.strftime('%d/%m/%Y %H:%M')
            }])
            st.dataframe(resumo,hide_index=True,use_container_width=True)
            st.caption('Esta é somente a ITR mais recente encontrada para a frota.')
            if st.button('↙️ PREENCHER ESTA ITR NO LAUDO',type='primary',use_container_width=True,key=f'aplicar_itr_{bulk_key}'):
                fr=str(achado.get('frota','')).strip()
                mask=bulk['FROTA'].astype(str).str.replace(r'\\D','',regex=True).eq(re.sub(r'\\D','',fr))
                if not mask.any():
                    st.warning(f'A frota {fr} não está nas linhas lidas deste PDF.')
                else:
                    bulk.loc[mask,'ID']=str(achado.get('os_id',''))
                    bulk.loc[mask,'FROTA']=fr
                    bulk.loc[mask,'INICIO OS']='' if pd.isna(ini) else ini.strftime('%d/%m/%Y %H:%M')
                    bulk.loc[mask,'FIM OS']='' if pd.isna(fim) else fim.strftime('%d/%m/%Y %H:%M')
                    st.session_state[bulk_key]=bulk
                    # troca a chave do editor para reconstruir a grade com os valores preenchidos
                    st.session_state[f'editor_rev_{bulk_key}']=st.session_state.get(f'editor_rev_{bulk_key}',0)+1
                    st.success(f'✅ OS {achado.get("os_id","")} aplicada às atividades da frota {fr}.')
                    st.rerun()

        st.markdown('#### Conferência — tudo abaixo é editável')
        _rev=st.session_state.get(f'editor_rev_{bulk_key}',0)
        edit=st.data_editor(
            bulk[cols],num_rows='dynamic',hide_index=True,use_container_width=True,height=620,key=f'editor_{bulk_key}_{_rev}',
            disabled=['_PAGINA','_COMPARTIMENTO'],
            column_config={
                'SELECIONAR':st.column_config.CheckboxColumn('✓',help='Somente linhas marcadas serão validadas e gravadas.'),
                'ITR':st.column_config.CheckboxColumn('ITR'),
                'CNP':st.column_config.CheckboxColumn('CNP'),
                'GM':st.column_config.CheckboxColumn('GM'),
                'OUTROS':st.column_config.CheckboxColumn('OUTROS'),
                '_PAGINA':st.column_config.NumberColumn('PÁGINA'),
                '_COMPARTIMENTO':st.column_config.TextColumn('COMP.')
            }
        )
        a,b=st.columns([3,1])
        selecionadas=int(edit['SELECIONAR'].fillna(False).astype(bool).sum()) if 'SELECIONAR' in edit.columns else len(edit)
        a.caption(f'{selecionadas} atividade(s) marcada(s) para gravação. Desmarcadas serão ignoradas inclusive nas validações.')
        if a.button('✅ CONFIRMAR E GRAVAR SOMENTE OS MARCADOS',type='primary',use_container_width=True,key='gravar_lote'):
            with st.spinner('Gravando somente os laudos marcados...'):
                ok,msg=salvar_lote(edit)
            if ok:
                st.success('✅ '+msg)
                st.session_state[bulk_key]=edit
            else: st.error(msg)
        if b.button('🗑️ LIMPAR LEITURA',use_container_width=True,key='limpar_lote'):
            st.session_state.pop(bulk_key,None); st.session_state.pop(f'{bulk_key}_falhas',None); st.rerun()

pag=st.number_input('Página',1,n,value=min(int(st.session_state.get('pag',1)),n),step=1);st.session_state['pag']=int(pag)
page=doc.load_page(int(pag)-1);pix=page.get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False);png=pix.tobytes('png');preview,_=image_versions(png)
rotkey=f'rot_{pdf_hash}_{pag}';st.session_state.setdefault(rotkey,False)
if st.session_state[rotkey]:
    im=Image.open(BytesIO(preview)).convert('RGB').rotate(180,expand=True);b=BytesIO();im.save(b,format='PNG');preview=b.getvalue()
left,right=st.columns([1,1.15],gap='large')
with left:
    a,b=st.columns([3,1]);a.markdown(f'#### Laudo original — página {pag}/{n}')
    if b.button('↻ Girar 180°',use_container_width=True):st.session_state[rotkey]=not st.session_state[rotkey];st.rerun()
    st.image(preview,use_container_width=True)
with right:
    st.markdown('#### Leitura para conferência');key=f'{pdf_hash}_{pag}_{int(st.session_state[rotkey])}'
    if st.button('🤖 LER ESTA PÁGINA COM GEMINI',type='primary',use_container_width=True):
        with st.spinner('Lendo o laudo...'):st.session_state['leitura']={'key':key,'data':ler_gemini(preview)}
    cache=st.session_state.get('leitura',{});data=cache.get('data',{}) if cache.get('key')==key else {}
    if int(pag) in ok_pages:st.success('✅ Página conferida nesta sessão.')
    if data.get('_erro'):st.error(data['_erro'])
    elif data:
        c1,c2,c3=st.columns(3);frota=c1.text_input('FROTA',str(data.get('frota','')),key=f'fr_{key}');osid=c2.text_input('OS/ID',str(data.get('id_os','')),key=f'os_{key}');conf=float(data.get('confianca',0) or 0);c3.metric('CONFIANÇA IA',f'{conf*100:.0f}%')
        d0=pd.to_datetime(data.get('data_inspecao'),dayfirst=True,errors='coerce');d0=pd.Timestamp.today() if pd.isna(d0) else d0;dia=st.date_input('Data do laudo',d0.date(),key=f'data_{key}')
        comps=['1º','2º','3º'];cr=str(data.get('compartimento','1º'));comp=next((x for x in comps if x[0] in cr),'1º');comp=st.selectbox('COMPARTIMENTO',comps,index=comps.index(comp),key=f'cp_{key}')
        def dtval(v):
            z=pd.to_datetime(v,dayfirst=True,errors='coerce');return datetime.combine(dia,dt_time(0,0)) if pd.isna(z) else z.to_pydatetime()
        ini=dtval(data.get('inicio_manutencao'));fim=dtval(data.get('fim_manutencao'));x1,x2=st.columns(2);di=x1.date_input('DATA INÍCIO MANUTENÇÃO',ini.date(),key=f'di_{key}');hi=x1.time_input('HORA INÍCIO MANUTENÇÃO',ini.time().replace(second=0,microsecond=0),key=f'hi_{key}');df=x2.date_input('DATA FIM MANUTENÇÃO',fim.date(),key=f'df_{key}');hf=x2.time_input('HORA FIM MANUTENÇÃO',fim.time().replace(second=0,microsecond=0),key=f'hf_{key}');ini_m=datetime.combine(di,hi);fim_m=datetime.combine(df,hf)
        at=pd.DataFrame(data.get('atividades',[]) or [{'servico':'','classificacao':'ITR','inicio':'','fim':''}]).rename(columns={'servico':'ATIVIDADE/DESCRIÇÃO','classificacao':'CLASSIFICAÇÃO','inicio':'INÍCIO','fim':'FIM'})
        for c in ['ATIVIDADE/DESCRIÇÃO','CLASSIFICAÇÃO','INÍCIO','FIM']:
            if c not in at:at[c]=''
        ed=st.data_editor(at[['ATIVIDADE/DESCRIÇÃO','CLASSIFICAÇÃO','INÍCIO','FIM']],num_rows='dynamic',hide_index=True,use_container_width=True,key=f'ed_{key}',column_config={'CLASSIFICAÇÃO':st.column_config.SelectboxColumn('TIPO',options=['ITR','CNP','GM','OUTROS'],required=True)})
        if st.button('✅ SALVAR COMO CONFERIDO',type='primary',use_container_width=True,key=f'save_{key}'):
            if not str(frota).strip() or not str(osid).strip():st.error('Confira FROTA e OS/ID.')
            elif fim_m<=ini_m:st.error('Fim da manutenção precisa ser maior que o início.')
            else:
                rows=[];errs=[];registro=f'WEB_{pdf_hash}_{pag}'
                for _,r in ed.iterrows():
                    ativ=str(r.get('ATIVIDADE/DESCRIÇÃO','') or '').strip()
                    if not ativ:continue
                    h1,h2=hora(r.get('INÍCIO')),hora(r.get('FIM'))
                    if not h1 or not h2:errs.append(ativ);continue
                    d1=pd.Timestamp(datetime.combine(dia,h1));d2=pd.Timestamp(datetime.combine(dia,h2))
                    if d2<d1:d2+=pd.Timedelta(days=1)
                    aid=f'{registro}_{len(rows)+1}';rows.append({'registro':registro,'os_id':str(osid).strip(),'frota':str(frota).strip(),'compartimento':comp,'status':'CONFERIDO','inicio_manutencao':iso(ini_m),'fim_manutencao':iso(fim_m),'atividade_id':aid,'atividade':ativ,'executante':'','classificacao':str(r.get('CLASSIFICAÇÃO','OUTROS')).upper(),'inicio_atividade':iso(d1),'fim_atividade':iso(d2),'horas':max(0,(d2-d1).total_seconds()/3600),'evidencias':'[]'})
                if errs:st.error('Confira INÍCIO/FIM: '+errs[0])
                elif not rows:st.error('Nenhuma atividade válida.')
                else:
                    ok,msg=supabase_upsert(rows,str(osid).strip(),str(frota).strip(),comp)
                    if ok:
                        ok_pages.add(int(pag));st.session_state['ok_pages']=ok_pages;st.success(f'✅ OS {osid} • Frota {frota} • {comp}: {msg}');prox=next((i for i in range(int(pag)+1,n+1) if i not in ok_pages),None)
                        if prox:st.session_state['pag']=prox
                    else:st.error('Não foi possível sincronizar: '+msg)
    else:st.info('Clique em **LER ESTA PÁGINA COM GEMINI** e confira os dados olhando o laudo original.')
