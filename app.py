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

def parse_json(text):
    text=re.sub(r'^```(?:json)?\s*','',str(text or '').strip(),flags=re.I); text=re.sub(r'\s*```$','',text); a,b=text.find('{'),text.rfind('}')
    if a>=0 and b>a:text=text[a:b+1]
    return json.loads(text)

def image_versions(png_bytes):
    img=Image.open(BytesIO(png_bytes)).convert('RGB')
    if img.height>img.width: img=img.rotate(90,expand=True)
    if max(img.size)>1800:
        k=1800/max(img.size); img=img.resize((int(img.width*k),int(img.height*k)))
    out=[]
    for ang in (0,180):
        im=img.rotate(ang,expand=True) if ang else img.copy(); bio=BytesIO(); im.save(bio,format='JPEG',quality=88,optimize=True); out.append(base64.b64encode(bio.getvalue()).decode('ascii'))
    bio=BytesIO(); img.save(bio,format='PNG'); return bio.getvalue(),out

def ler_gemini(png_bytes):
    if not GEMINI_API_KEY:return {'_erro':'GEMINI_API_KEY não configurada nos Secrets.'}
    preview,imgs=image_versions(png_bytes)
    prompt='''Leia este LAUDO DE INSPEÇÃO DE CARRETA da 10 Sul Service. As imagens são a mesma página em rotações diferentes; use somente a orientação legível e NÃO duplique linhas.
Identifique PRIMEIRO OS/ID (rótulos ID, OS, Nº OS, N° OS, ORDEM DE SERVIÇO), retornando somente dígitos e nunca inventando. Leia FROTA/SR, data do laudo, compartimento 1º/2º/3º, INÍCIO MANUTENÇÃO e FIM MANUTENÇÃO do cabeçalho.
Transcreva TODAS as linhas preenchidas: atividade/serviço, INÍCIO, FIM e classificação ITR/CNP/GM/OUTROS. Horários de atividade devem vir apenas das colunas INÍCIO e FIM da mesma linha. Se ilegível/vazio, retorne string vazia. Horário válido em HH:MM. Não use tempo estimado.
Retorne SOMENTE JSON:
{"id_os":"","frota":"","data_inspecao":"","compartimento":"1º","inicio_manutencao":"","fim_manutencao":"","atividades":[{"servico":"Regular freio","inicio":"07:45","fim":"08:01","classificacao":"ITR"}],"confianca":0.0}'''
    parts=[{'text':prompt}]+[{'inline_data':{'mime_type':'image/jpeg','data':x}} for x in imgs]
    payload={'contents':[{'role':'user','parts':parts}],'generationConfig':{'responseMimeType':'application/json','temperature':0.0,'maxOutputTokens':8000}}
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'; req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'x-goog-api-key':GEMINI_API_KEY,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=150) as resp:raw=json.loads(resp.read().decode())
        txt='\n'.join(p.get('text','') for p in raw.get('candidates',[{}])[0].get('content',{}).get('parts',[]) if p.get('text')); return parse_json(txt)
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
