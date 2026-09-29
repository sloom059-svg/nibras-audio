import os, re, base64, shutil, subprocess, threading, json, queue, uuid, time, html, zipfile, tempfile, hashlib, hmac
from pathlib import Path
from urllib.parse import quote
import requests
import jwt
from jwt import PyJWKClient
from flask import Flask, request, render_template_string, jsonify, send_file, Response

WORK=Path('/tmp/nibras'); D=WORK/'downloads'; S=WORK/'separated'; O=WORK/'output'; QDIR=WORK/'queued'
for p in (D,S,O,QDIR): p.mkdir(parents=True,exist_ok=True)

TOKEN=os.getenv('GITHUB_TOKEN','').strip()
REPO=os.getenv('GITHUB_REPO','').strip()
BRANCH=os.getenv('GITHUB_BRANCH','main').strip()
FOLDER=os.getenv('GITHUB_FOLDER','processed-audio').strip().strip('/')
UPLOAD_KEY=os.getenv('UPLOAD_KEY','').strip()
PUBLISH_KEY=os.getenv('PUBLISH_KEY','').strip()
APK_PUBLISH_KEY=os.getenv('APK_PUBLISH_KEY','').strip()
APK_PUBLIC_REPO=os.getenv('APK_PUBLIC_REPO','sloom059-svg/nibras-catalog').strip()
APK_PUBLIC_BRANCH=os.getenv('APK_PUBLIC_BRANCH','main').strip()
MAP_PATH=os.getenv('AUDIO_MAP_PATH','audio-map.json').strip().strip('/') or 'audio-map.json'
DAILYMOTION_MAP_PATH=os.getenv('DAILYMOTION_AUDIO_MAP_PATH','dailymotion-audio-map.json').strip().strip('/') or 'dailymotion-audio-map.json'
ARCHIVE_MAP_PATH=os.getenv('ARCHIVE_AUDIO_MAP_PATH','archive-audio-map.json').strip().strip('/') or 'archive-audio-map.json'
RUNPOD_API_KEY=os.getenv('RUNPOD_API_KEY','').strip()
RUNPOD_ENDPOINT_ID=os.getenv('RUNPOD_ENDPOINT_ID','').strip()
B2_KEY_ID=os.getenv('B2_KEY_ID','').strip()
B2_APPLICATION_KEY=os.getenv('B2_APPLICATION_KEY','').strip()
B2_BUCKET_NAME=os.getenv('B2_BUCKET_NAME','').strip()
B2_AUTH_CACHE=None
B2_AUTH_LOCK=threading.Lock()
B2_USAGE_CACHE={'at':0,'bytes':0,'files':0}
B2_USAGE_LOCK=threading.Lock()
RUNPOD_BASE='https://api.runpod.ai/v2'

app=Flask(__name__)

JOB_QUEUE=queue.Queue()
JOBS={}
JOBS_LOCK=threading.Lock()
WORKER_STARTED=False
WORKER_START_LOCK=threading.Lock()

PUBLISH_JOBS={}
PUBLISH_JOBS_LOCK=threading.Lock()
RUNPOD_JOBS={}
RUNPOD_JOBS_LOCK=threading.Lock()
RUNPOD_QUEUE=queue.Queue()
RUNPOD_WORKER_STARTED=False
RUNPOD_WORKER_LOCK=threading.Lock()
RUNPOD_AUDIO_EXTS={'.m4a','.aac','.mp3','.wav','.flac','.ogg','.opus','.mp4','.webm'}

HTML='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>نبراس</title>
<style>body{font-family:Arial;background:#15171b;color:white;max-width:800px;margin:40px auto;padding:20px}.c{background:#22262c;padding:25px;border-radius:18px}pre{background:#111;padding:15px;border-radius:10px;white-space:pre-wrap}</style>
<div class=c><h2>نبراس | معالج الصوت</h2><p>Queue مفعّلة: يستقبل الملف فورًا، ثم يعالج الملفات واحدًا واحدًا بالخلفية ويرفع الناتج تلقائيًا.</p><pre>جاهز</pre></div></html>'''

def log(msg):
    print(f'[NIBRAS] {msg}', flush=True)

def run(c, env=None):
    merged=os.environ.copy()
    if env: merged.update(env)
    p=subprocess.run(c,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=merged)
    out=p.stdout or ''
    if p.returncode:
        tail=out[-12000:]
        if p.returncode in (-9, 137):
            raise RuntimeError(f'Process killed (code {p.returncode}) — likely memory pressure.\\n{tail}')
        raise RuntimeError(f'Command failed (code {p.returncode}).\\n{tail}')
    return out

def headers():
    return {'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

def gh_get(path):
    api=f'https://api.github.com/repos/{REPO}/contents/{quote(path, safe="/")}'
    r=requests.get(api,headers=headers(),params={'ref':BRANCH},timeout=30)
    if r.status_code==200:return api,r.json()
    if r.status_code==404:return api,None
    raise RuntimeError(f'GitHub check {r.status_code}: {r.text[:700]}')

def gh_info(vid):
    path=f'{FOLDER}/{vid}.m4a' if FOLDER else f'{vid}.m4a'
    api,old=gh_get(path)
    return path,api,old

def raw_url(path):
    return f'https://raw.githubusercontent.com/{REPO}/{BRANCH}/{quote(path, safe="/")}'

def _github_repo_headers():
    return {'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

def _public_repo_contents(path):
    return f'https://api.github.com/repos/{APK_PUBLIC_REPO}/contents/{quote(path, safe="/")}'

def _public_update_json(metadata):
    path='website/nibras-update.json'
    api=_public_repo_contents(path)
    old=requests.get(api,headers=_github_repo_headers(),params={'ref':APK_PUBLIC_BRANCH},timeout=30)
    sha=None
    if old.status_code==200:
        sha=(old.json() or {}).get('sha')
    elif old.status_code!=404:
        raise RuntimeError(f'Public metadata read {old.status_code}: {old.text[:700]}')
    body={
        'message':f"Publish Nibras {metadata.get('version_name','')} public update",
        'content':base64.b64encode((json.dumps(metadata,ensure_ascii=False,indent=2)+'\n').encode('utf-8')).decode(),
        'branch':APK_PUBLIC_BRANCH,
    }
    if sha: body['sha']=sha
    put=requests.put(api,headers=_github_repo_headers(),json=body,timeout=60)
    if put.status_code not in (200,201):
        raise RuntimeError(f'Public metadata update {put.status_code}: {put.text[:900]}')

def _public_release(code,version_name,apk_path,filename):
    tag=f'nibras-app-{code}'
    base=f'https://api.github.com/repos/{APK_PUBLIC_REPO}'
    check=requests.get(f'{base}/releases/tags/{tag}',headers=_github_repo_headers(),timeout=30)
    if check.status_code==404:
        created=requests.post(f'{base}/releases',headers=_github_repo_headers(),json={
            'tag_name':tag,'target_commitish':APK_PUBLIC_BRANCH,
            'name':f'Nibras {version_name}',
            'body':'Public APK distribution for Nibras. Source code remains private.',
            'draft':False,'prerelease':False
        },timeout=60)
        if created.status_code not in (200,201):
            raise RuntimeError(f'Public release create {created.status_code}: {created.text[:900]}')
        release=created.json()
    elif check.status_code==200:
        release=check.json()
    else:
        raise RuntimeError(f'Public release check {check.status_code}: {check.text[:900]}')

    for asset in release.get('assets') or []:
        if str(asset.get('name') or '')==filename:
            delete=requests.delete(f'{base}/releases/assets/{asset.get("id")}',headers=_github_repo_headers(),timeout=30)
            if delete.status_code not in (204,404):
                raise RuntimeError(f'Public asset delete {delete.status_code}: {delete.text[:700]}')

    upload_url=f'https://uploads.github.com/repos/{APK_PUBLIC_REPO}/releases/{release["id"]}/assets'
    with open(apk_path,'rb') as fh:
        up=requests.post(upload_url,headers={
            'Authorization':f'Bearer {TOKEN}',
            'Accept':'application/vnd.github+json',
            'X-GitHub-Api-Version':'2022-11-28',
            'Content-Type':'application/vnd.android.package-archive'
        },params={'name':filename},data=fh,timeout=600)
    if up.status_code not in (200,201):
        raise RuntimeError(f'Public APK upload {up.status_code}: {up.text[:900]}')
    return f'https://github.com/{APK_PUBLIC_REPO}/releases/download/{tag}/{quote(filename)}'

def update_audio_map_url(vid, url):
    api,old=gh_get(MAP_PATH)
    mapping={}; sha=None
    if old:
        sha=old.get('sha')
        try:
            content=base64.b64decode(old.get('content','')).decode('utf-8')
            obj=json.loads(content)
            if isinstance(obj,dict): mapping=obj
        except Exception:
            mapping={}
    if mapping.get(vid)==url:
        return url
    mapping[vid]=url
    body={
        'message':f'Update audio map {vid}',
        'content':base64.b64encode((json.dumps(mapping,ensure_ascii=False,indent=2)+'\n').encode('utf-8')).decode(),
        'branch':BRANCH,
    }
    if sha: body['sha']=sha
    r=requests.put(api,headers=headers(),json=body,timeout=60)
    if r.status_code not in (200,201):
        raise RuntimeError(f'GitHub map update {r.status_code}: {r.text[:900]}')
    return url

def update_audio_map(vid, path):
    return update_audio_map_url(vid, raw_url(path))

def _map_target(value):
    target=str(value or '').strip().lower()
    if target in ('dailymotion','archive'):
        return target
    return 'youtube'

def update_selected_audio_map_url(vid, url, map_target='youtube'):
    target=_map_target(map_target)
    if target=='youtube':
        return update_audio_map_url(vid,url)

    map_path=DAILYMOTION_MAP_PATH if target=='dailymotion' else ARCHIVE_MAP_PATH
    provider='dailymotion' if target=='dailymotion' else 'archive'
    description=(
        'Independent Dailymotion audio bindings. Existing audio-map.json remains untouched.'
        if target=='dailymotion'
        else 'Independent Archive.org audio bindings. Existing YouTube and Dailymotion maps remain untouched.'
    )
    api,old=gh_get(map_path)
    wrapper={'version':1,'provider':provider,'description':description,'audio':{}}
    sha=None
    if old:
        sha=old.get('sha')
        try:
            content=base64.b64decode(old.get('content','')).decode('utf-8')
            obj=json.loads(content)
            if isinstance(obj,dict):
                wrapper.update({k:v for k,v in obj.items() if k!='audio'})
                if isinstance(obj.get('audio'),dict): wrapper['audio']=dict(obj['audio'])
        except Exception:
            pass
    if wrapper['audio'].get(vid)==url:
        return url
    wrapper['audio'][vid]=url
    body={
        'message':f'Update {provider} audio map {vid}',
        'content':base64.b64encode((json.dumps(wrapper,ensure_ascii=False,indent=2)+'\n').encode('utf-8')).decode(),
        'branch':BRANCH,
    }
    if sha: body['sha']=sha
    r=requests.put(api,headers=headers(),json=body,timeout=60)
    if r.status_code not in (200,201):
        raise RuntimeError(f'GitHub {provider} map update {r.status_code}: {r.text[:900]}')
    return url

def upload(file,vid):
    path,api,old=gh_info(vid)
    if old:
        return 'skipped', update_audio_map(vid,path)
    data=base64.b64encode(Path(file).read_bytes()).decode()
    r=requests.put(api,headers=headers(),json={'message':f'Add cleaned audio {vid}','content':data,'branch':BRANCH},timeout=240)
    if r.status_code not in (200,201):
        raise RuntimeError(f'GitHub upload {r.status_code}: {r.text[:1000]}')
    return 'uploaded', update_audio_map(vid,path)

def cleanup_work(vid):
    for p in (D/f'{vid}.wav', O/f'{vid}.m4a'):
        try:p.unlink()
        except:pass
    for p in S.glob(f'**/{vid}'):
        if p.is_dir(): shutil.rmtree(p,ignore_errors=True)

def one_uploaded(vid, source):
    path,_,old=gh_info(vid)
    if old:
        return 'skipped', update_audio_map(vid,path)
    wav=D/f'{vid}.wav'; final=O/f'{vid}.m4a'
    try:
        log(f'{vid}: ffmpeg -> wav')
        run(['ffmpeg','-y','-i',str(source),'-vn','-ar','44100','-ac','2','-c:a','pcm_s16le',str(wav)])
        log(f'{vid}: demucs start')
        demucs_env={
            'OMP_NUM_THREADS':'1',
            'MKL_NUM_THREADS':'1',
            'OPENBLAS_NUM_THREADS':'1',
            'NUMEXPR_NUM_THREADS':'1',
            'VECLIB_MAXIMUM_THREADS':'1',
        }
        run([
            'python','-m','demucs',
            '--two-stems=vocals',
            '-n','htdemucs',
            '--device','cpu',
            '--segment','2',
            '--overlap','0.05',
            '--shifts','0',
            '-j','1',
            '-o',str(S),
            str(wav)
        ], env=demucs_env)
        cand=list(S.glob(f'**/{vid}/vocals.wav'))
        if not cand: raise RuntimeError('Demucs vocals output missing')
        log(f'{vid}: encode m4a')
        run(['ffmpeg','-y','-i',str(cand[0]),'-vn','-c:a','aac','-b:a','128k',str(final)])
        if not final.exists() or final.stat().st_size < 10000:
            raise RuntimeError('Final clean audio was not created correctly')
        log(f'{vid}: upload github')
        return upload(final,vid)
    finally:
        cleanup_work(vid)

def set_job(job_id, **changes):
    with JOBS_LOCK:
        row=JOBS.get(job_id,{})
        row.update(changes)
        row['updated_at']=time.time()
        JOBS[job_id]=row

def worker_loop():
    log('queue worker started')
    while True:
        job_id=JOB_QUEUE.get()
        try:
            with JOBS_LOCK:
                job=dict(JOBS.get(job_id,{}) or {})
            if not job:
                continue
            vid=job['id']; source=Path(job['source'])
            set_job(job_id,status='processing',stage='demucs',started_at=time.time())
            log(f'{job_id} {vid}: processing')
            try:
                status,url=one_uploaded(vid,source)
                set_job(job_id,status='published',stage='done',result=status,url=url,finished_at=time.time(),error='')
                try: source.unlink()
                except: pass
                log(f'{job_id} {vid}: published {url}')
            except Exception as e:
                set_job(job_id,status='failed',stage='failed',error=str(e)[-2500:],finished_at=time.time())
                log(f'{job_id} {vid}: FAILED {str(e)[-1000:]}')
                # Keep the queued source on Railway after failure for inspection/retry until container restart.
        finally:
            JOB_QUEUE.task_done()

def ensure_worker():
    global WORKER_STARTED
    if WORKER_STARTED:return
    with WORKER_START_LOCK:
        if WORKER_STARTED:return
        threading.Thread(target=worker_loop,daemon=True,name='nibras-audio-worker').start()
        WORKER_STARTED=True

def enqueue_request():
    ensure_worker()
    if UPLOAD_KEY and request.headers.get('X-Upload-Key','') != UPLOAD_KEY:
        return jsonify(ok=False,error='مفتاح الرفع غير صحيح'),401
    if not TOKEN or '/' not in REPO:
        return jsonify(ok=False,error='GitHub settings are missing on Railway'),500
    vid=re.sub(r'[^A-Za-z0-9_-]','',str(request.form.get('id','')).strip())
    title=str(request.form.get('title','')).strip()[:300]
    playlist=str(request.form.get('playlist','')).strip()[:500]
    incoming=request.files.get('file')
    if not vid or incoming is None or not incoming.filename:
        return jsonify(ok=False,error='أرسل id وملفًا باسم file'),400

    # If already published, answer immediately without queueing another Demucs run.
    try:
        path,_,old=gh_info(vid)
        if old:
            url=update_audio_map(vid,path)
            return jsonify(ok=True,status='published',id=vid,url=url,already_exists=True),200
    except Exception as e:
        log(f'{vid}: precheck warning {e}')

    # Reuse an active job for the same video.
    with JOBS_LOCK:
        for jid,row in JOBS.items():
            if row.get('id')==vid and row.get('status') in ('queued','processing'):
                return jsonify(ok=True,status=row.get('status'),job_id=jid,id=vid,queued=True),202

    suffix=Path(incoming.filename).suffix.lower() or '.source'
    job_id=uuid.uuid4().hex
    source=QDIR/f'{job_id}_{vid}{suffix}'
    incoming.save(source)
    if not source.exists() or source.stat().st_size < 1000:
        try: source.unlink()
        except: pass
        return jsonify(ok=False,error='الملف المرفوع فارغ أو غير مكتمل'),400

    row={
        'job_id':job_id,'id':vid,'title':title,'playlist':playlist,'source':str(source),
        'status':'queued','stage':'waiting','created_at':time.time(),'updated_at':time.time(),
        'size':source.stat().st_size,'error':'','url':''
    }
    with JOBS_LOCK:
        JOBS[job_id]=row
    JOB_QUEUE.put(job_id)
    pos=JOB_QUEUE.qsize()
    log(f'{job_id} {vid}: queued size={row["size"]} position~{pos}')
    return jsonify(ok=True,status='queued',job_id=job_id,id=vid,queue_position=pos),202


CLEAN_HTML='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>نبراس | نشر صوت جاهز</title>
<style>
body{font-family:Arial;background:#15171b;color:#fff;max-width:720px;margin:30px auto;padding:18px}
.c{background:#22262c;padding:24px;border-radius:18px}
label{display:block;margin:14px 0 7px;font-weight:700}
input,select,button{width:100%;padding:13px;border-radius:10px;border:1px solid #444;box-sizing:border-box;font-size:16px}
input,select{background:#111;color:#fff}button{margin-top:18px;background:#f6c35f;color:#162436;font-weight:900;cursor:pointer}
button:disabled{opacity:.55;cursor:not-allowed}
small{color:#aaa}.ok{background:#163b2b;padding:12px;border-radius:10px;margin-top:16px}.err{background:#4b1d23;padding:12px;border-radius:10px;margin-top:16px}
.progressWrap{display:none;margin-top:18px;background:#111;border:1px solid #3b4149;border-radius:12px;padding:13px}
.progressTitle{display:flex;justify-content:space-between;gap:10px;margin-bottom:9px;font-weight:800}
.progressTrack{height:14px;background:#2b3037;border-radius:999px;overflow:hidden}
.progressBar{height:100%;width:0%;background:linear-gradient(90deg,#f6c35f,#ffe6a4);transition:width .18s ease}
.progressNote{color:#c7cbd1;font-size:13px;margin-top:8px}
</style>
<div class=c><h2>نبراس | نشر الصوت الجاهز</h2>
<p>ارفع ملفات LALAL الجاهزة وسيتم استخراج Video ID تلقائيًا حتى لو كان الاسم مثل <b>6CBLA5W1N0I (1)_vocals_split_by_lalalai.aac</b>.</p>
<form id=uploadForm method=post enctype=multipart/form-data>
<input type=hidden name=k value="{{key}}">
<label>معرّف الفيديو</label><input name=id value="{{vid}}" placeholder="YouTube: aJ3zGhhMuxE أو Dailymotion: x9itz54 — اتركه فارغًا لـ Archive.org">
<small>اتركه فارغًا عند رفع عدة ملفات؛ سأقرأ الـ Video ID من بداية كل اسم ملف.</small>
<label>مجلد المسلسل</label><input name=series value="{{series}}" placeholder="مثال: barbear" required>
<small>أي ملف موجود مسبقًا داخل نفس مجلد المسلسل سيتم التعرف عليه وتخطيه بدون رفع أو تحويل من جديد.</small>
<label>ملفات الصوت الجاهزة</label><input type=file name=file accept=".zip,.m4a,.aac,.mp3,.wav,.flac,.ogg,.opus,audio/mp4,audio/x-m4a,audio/aac,audio/mpeg,audio/wav,audio/x-wav,audio/flac,audio/ogg,audio/opus,application/zip" multiple required>
<small>يدعم ZIP و M4A و AAC و MP3 و WAV و FLAC و OGG و OPUS، ويدعم لاحقة (1) و(2) وغيرها بعد Video ID.</small>
<label>الماب الذي سيتم الربط فيه بعد الرفع</label>
<select name=map_target required>
  <option value="youtube" selected>الماب العادي (YouTube)</option>
  <option value="dailymotion">ماب Dailymotion</option>
  <option value="archive">ماب Archive.org</option>
</select>
<small>اختيار الماب لا يغيّر التخزين؛ فقط يحدد ملف الربط الذي يضاف إليه الـ ID بعد نجاح الرفع.</small>
<button id=submitBtn type=submit>رفع وربط الآن</button>
</form>
<div id=progressWrap class=progressWrap>
  <div class=progressTitle><span id=progressText>جاري تجهيز الرفع...</span><span id=progressPct>0%</span></div>
  <div class=progressTrack><div id=progressBar class=progressBar></div></div>
  <div class=progressNote id=progressNote>لا تغلق الصفحة حتى يكتمل الرفع والفحص.</div>
</div>
<p style="margin-top:18px"><a style="color:#ffd982" href="/audio-status">عرض حالة المقاطع المربوطة</a></p>
<div id=resultBox>{{message|safe}}</div>
</div>
<script>
(function(){
  const form=document.getElementById('uploadForm'),btn=document.getElementById('submitBtn'),wrap=document.getElementById('progressWrap'),bar=document.getElementById('progressBar'),pct=document.getElementById('progressPct'),txt=document.getElementById('progressText'),note=document.getElementById('progressNote'),result=document.getElementById('resultBox');
  form.addEventListener('submit',function(e){
    e.preventDefault();
    const fd=new FormData(form), files=form.querySelector('input[type=file]').files;
    if(!files||!files.length)return;
    btn.disabled=true;wrap.style.display='block';result.innerHTML='';bar.style.width='2%';pct.textContent='0%';txt.textContent='جاري رفع الملفات...';note.textContent='سيتم تخطي الملفات الموجودة مسبقًا تلقائيًا.';
    const xhr=new XMLHttpRequest(); xhr.open('POST',window.location.href,true);
    xhr.upload.onprogress=function(ev){if(!ev.lengthComputable)return;const p=Math.max(1,Math.min(95,Math.round((ev.loaded/ev.total)*95)));bar.style.width=p+'%';pct.textContent=p+'%';};
    xhr.upload.onload=function(){bar.style.width='96%';pct.textContent='96%';txt.textContent='تم الرفع — جاري فحص التكرار والربط...';};
    function esc(v){const d=document.createElement('div');d.textContent=String(v||'');return d.innerHTML;}
function fmtGB(bytes){return (Number(bytes||0)/1024/1024/1024).toFixed(2)}
function loadStorageUsage(){
 fetch('/b2-usage',{cache:'no-store'}).then(r=>r.json()).then(j=>{
   const text=document.getElementById('storageText'),fill=document.getElementById('storageFill');
   if(!j.ok){text.textContent='تعذر قراءة المساحة الآن';fill.style.width='0%';return}
   text.textContent='المستخدم '+fmtGB(j.bytes)+' GB من 10 GB — المتبقي '+fmtGB(j.remaining_bytes)+' GB — '+j.files+' ملف';
   fill.style.width=Math.max(1,Math.min(100,Number(j.percent||0)))+'%';
 }).catch(()=>{document.getElementById('storageText').textContent='تعذر قراءة المساحة الآن'});
}
loadStorageUsage();
    function renderJob(j){
      const rows=(j.results||[]).map(function(x){const fn=x[0],v=x[1],s=x[2],u=x[3];const label=(s==='exists'||s==='skipped')?'♻️ موجود مسبقًا — تم تخطيه':'✅ تم رفعه وربطه';return '<div style="margin:9px 0;padding:9px 0;border-bottom:1px solid #385044"><b>'+esc(v)+'</b> — '+label+'<br><a style="color:#ffd982" href="'+esc(u)+'">'+esc(fn)+'</a></div>';}).join('');
      const errs=(j.errors||[]).map(function(e){return '❌ '+esc(e);}).join('<br>');
      const uploaded=(j.results||[]).filter(x=>x[2]==='uploaded'||x[2]==='published').length;
      const existing=(j.results||[]).filter(x=>x[2]==='exists'||x[2]==='skipped').length;
      result.innerHTML=(rows?'<div class="ok">تم رفع '+uploaded+' جديد'+(existing?' — وتخطي '+existing+' موجود مسبقًا':'')+' ✅'+rows+'</div>':'')+(errs?'<div class="err">'+errs+'</div>':'');
    }
    function pollJob(jobId){
      fetch('/publish-clean-status/'+encodeURIComponent(jobId),{cache:'no-store'}).then(r=>r.json()).then(function(j){
        if(!j.ok)throw new Error(j.error||'status error');
        const total=Number(j.total||0),done=Number(j.done||0);
        const p=total?Math.min(99,96+Math.round((done/total)*3)):97;
        bar.style.width=p+'%';pct.textContent=p+'%';
        txt.textContent=j.status==='queued'?'تم الاستلام — بانتظار المعالجة...':'جاري معالجة '+done+(total?' من '+total:'')+'...';
        note.textContent=j.current?('الملف الحالي: '+j.current):'يمكنك إبقاء الصفحة مفتوحة لمتابعة النتيجة.';
        renderJob(j);
        if(j.status==='done'){btn.disabled=false;bar.style.width='100%';pct.textContent='100%';txt.textContent='اكتمل ✅';note.textContent='انتهت العملية. راجع نتيجة كل ملف أدناه.';return;}
        if(j.status==='failed'){btn.disabled=false;txt.textContent='فشلت المعالجة';note.textContent=j.error||'حدث خطأ';result.innerHTML='<div class="err">'+esc(j.error||'تعذر إكمال المعالجة')+'</div>';return;}
        setTimeout(()=>pollJob(jobId),2000);
      }).catch(function(){setTimeout(()=>pollJob(jobId),3000);});
    }
    xhr.onload=function(){
      if(xhr.status===202){
        try{const j=JSON.parse(xhr.responseText);if(j.job_id){txt.textContent='تم استلام الملف ✅';note.textContent='بدأت المعالجة بالخلفية؛ لن ينقطع العمل إذا طال الطلب.';pollJob(j.job_id);return;}}catch(e){}
      }
      btn.disabled=false;
      if(xhr.status>=200&&xhr.status<300){bar.style.width='100%';pct.textContent='100%';txt.textContent='اكتمل ✅';const doc=new DOMParser().parseFromString(xhr.responseText,'text/html');const incoming=doc.getElementById('resultBox');result.innerHTML=incoming?incoming.innerHTML:xhr.responseText;note.textContent='انتهت العملية. راجع النتائج أدناه.';}else{txt.textContent='فشل الرفع';note.textContent='HTTP '+xhr.status;result.innerHTML='<div class="err">تعذر إكمال الرفع.</div>';}
    }
    xhr.onerror=function(){btn.disabled=false;txt.textContent='انقطع الاتصال';note.textContent='تحقق من الشبكة ثم حاول مرة أخرى.';result.innerHTML='<div class="err">تعذر الاتصال بالخادم.</div>';};
    xhr.send(fd);
  });
})();
</script>
</html>'''

def _derive_video_id(value, filename):
    raw=re.sub(r'[^A-Za-z0-9_-]','',str(value or '').strip())
    if raw:return raw
    name=Path(filename or '').stem
    # Some downloaders/exporters prefix the filename with '=' (for example
    # =9CA1ByDM8vM.m4a). Ignore that wrapper so the 11-char YouTube ID is
    # still detected, while preserving valid IDs that may begin with '-'/'_'.
    name=re.sub(r'^[=+]+','',name)

    # LALAL may prepend sequence numbers such as:
    # 2-v2j0HEHwWNo_vocals...
    # 10-LIDDzf-WLPQ_vocals...
    # and a YouTube id itself may begin with '-' as in:
    # 3--IhFHYf-9cQ_vocals...
    name=re.sub(r'^\d+-','',name)

    m=re.match(r'^([A-Za-z0-9_-]{11})(?=(?:\s*\(\d+\))?(?:_|$|\s))',name)
    return m.group(1) if m else ''

def _derive_publish_id(value, filename, map_target='youtube'):
    target=_map_target(map_target)
    if target=='youtube':
        return _derive_video_id(value,filename)

    raw=str(value or '').strip()
    if target=='dailymotion':
        raw=re.sub(r'[^A-Za-z0-9_-]','',raw)
        if raw:return raw
        name=Path(filename or '').stem
        name=re.sub(r'^[=+]+','',name)
        name=re.sub(r'^\d+-','',name)
        m=re.match(r'^(x[A-Za-z0-9]{5,})(?=(?:\s*\(\d+\))?(?:_|$|\s|-))',name,re.I)
        return m.group(1) if m else ''

    # Archive.org: derive a stable, filesystem-safe key from the original episode filename.
    # The app uses the exact same SHA-1 rule, so Arabic/spaces are handled without manual IDs.
    if raw.startswith('ia_') and re.fullmatch(r'ia_[a-f0-9]{20}',raw,re.I):
        return raw.lower()
    import unicodedata
    name=Path(filename or '').stem
    name=re.sub(r'\s*\(\d+\)$','',name)
    name=re.sub(r'(?i)_vocals_split_by_lalalai$','',name)
    name=re.sub(r'(?i)_vocals$','',name)
    name=unicodedata.normalize('NFC',name.strip())
    if not name:return ''
    return 'ia_'+hashlib.sha1(name.encode('utf-8')).hexdigest()[:20]

def series_slug(value):
    value=re.sub(r'[^A-Za-z0-9_-]','-',str(value or '').strip()).strip('-_').lower()
    return value or 'general'

def series_existing(vid, series, map_target='youtube'):
    folder=series_slug(series)
    path=f'{FOLDER}/{folder}/{vid}.m4a' if FOLDER else f'{folder}/{vid}.m4a'
    if B2_KEY_ID and B2_APPLICATION_KEY:
        b2url=b2_existing_url(path)
        if b2url:
            return path, update_selected_audio_map_url(vid,b2url,map_target)
    api,old=gh_get(path)
    if old:
        return path, update_selected_audio_map_url(vid,raw_url(path),map_target)
    return path, ''


def _gh_api(path):
    return f'https://api.github.com/repos/{REPO}/{path.lstrip("/")}'

def _git_push_upload(file_path, repo_path, message):
    file_path=Path(file_path)
    if file_path.stat().st_size >= 95*1024*1024:
        raise RuntimeError('الملف يتجاوز حد GitHub العادي 100MB')

    work=Path(tempfile.mkdtemp(prefix='nibras-git-',dir=str(WORK)))
    askpass=work/'askpass.sh'
    repo_dir=work/'repo'
    repo_dir.mkdir(parents=True,exist_ok=True)
    askpass.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  *Username*) echo "x-access-token" ;;\n'
        '  *Password*) echo "$GITHUB_TOKEN" ;;\n'
        'esac\n'
    )
    askpass.chmod(0o700)
    env=os.environ.copy()
    env.update({
        'GIT_ASKPASS':str(askpass),
        'GIT_TERMINAL_PROMPT':'0',
        'GIT_CONFIG_NOSYSTEM':'1'
    })
    remote=f'https://github.com/{REPO}.git'
    try:
        def grun(args, timeout=300):
            p=subprocess.run(args,cwd=repo_dir,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout)
            if p.returncode:
                raise RuntimeError(f'git command failed ({p.returncode}): {(p.stdout or "")[-2000:]}')
            return (p.stdout or '').strip()

        grun(['git','init','-q'])
        grun(['git','config','user.name','Nibras Audio Bot'])
        grun(['git','config','user.email','nibras-bot@users.noreply.github.com'])
        grun(['git','remote','add','origin',remote])

        # Fetch commit/tree metadata only; avoid downloading the ~1GB audio blobs.
        grun(['git','fetch','--depth','1','--filter=blob:none','origin',BRANCH],timeout=600)
        parent=grun(['git','rev-parse','FETCH_HEAD'])
        grun(['git','read-tree',parent])

        blob_sha=grun(['git','hash-object','-w',str(file_path)],timeout=300)
        grun(['git','update-index','--add','--cacheinfo','100644',blob_sha,repo_path])
        tree_sha=grun(['git','write-tree'])
        commit_sha=grun(['git','commit-tree',tree_sha,'-p',parent,'-m',message])

        # Push directly through Git protocol. Retry on non-fast-forward if another
        # audio-map commit landed between fetch and push.
        for attempt in range(3):
            p=subprocess.run(
                ['git','push','origin',f'{commit_sha}:refs/heads/{BRANCH}'],
                cwd=repo_dir,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=600
            )
            if p.returncode==0:
                return commit_sha
            out=(p.stdout or '')
            if 'non-fast-forward' not in out and 'fetch first' not in out and 'rejected' not in out:
                raise RuntimeError(f'git push failed ({p.returncode}): {out[-2000:]}')

            grun(['git','fetch','--depth','1','--filter=blob:none','origin',BRANCH],timeout=600)
            parent=grun(['git','rev-parse','FETCH_HEAD'])
            grun(['git','read-tree',parent])
            grun(['git','update-index','--add','--cacheinfo','100644',blob_sha,repo_path])
            tree_sha=grun(['git','write-tree'])
            commit_sha=grun(['git','commit-tree',tree_sha,'-p',parent,'-m',message])

        raise RuntimeError('git push failed after retries')
    finally:
        shutil.rmtree(work,ignore_errors=True)

def b2_authorize():
    global B2_AUTH_CACHE
    if not B2_KEY_ID or not B2_APPLICATION_KEY:
        return None
    with B2_AUTH_LOCK:
        if B2_AUTH_CACHE and B2_AUTH_CACHE.get('_expires',0)>time.time()+60:
            return B2_AUTH_CACHE
        r=requests.get(
            'https://api.backblazeb2.com/b2api/v2/b2_authorize_account',
            auth=(B2_KEY_ID,B2_APPLICATION_KEY),timeout=30
        )
        if r.status_code!=200:
            raise RuntimeError(f'Backblaze authorization failed HTTP {r.status_code}: {r.text[:400]}')
        data=r.json()
        allowed=data.get('allowed') or {}
        bucket_id=allowed.get('bucketId')
        bucket_name=(B2_BUCKET_NAME or allowed.get('bucketName') or '').strip()
        if not bucket_id or not bucket_name:
            raise RuntimeError('Backblaze key must be restricted to the Nibras audio bucket')
        if allowed.get('bucketName') and B2_BUCKET_NAME and allowed.get('bucketName')!=B2_BUCKET_NAME:
            raise RuntimeError('B2_BUCKET_NAME does not match the bucket allowed by this key')
        capabilities=allowed.get('capabilities') or []
        if 'writeFiles' not in capabilities:
            raise RuntimeError('Backblaze application key needs writeFiles permission')
        data['_bucket_id']=bucket_id
        data['_bucket_name']=bucket_name
        data['_expires']=time.time()+5*60*60
        B2_AUTH_CACHE=data
        return data

def b2_file_url(file_name, auth_data):
    return f"{auth_data['downloadUrl'].rstrip('/')}/file/{quote(auth_data['_bucket_name'],safe='')}/{quote(file_name,safe='/')}"

def b2_storage_usage():
    now=time.time()
    with B2_USAGE_LOCK:
        if B2_USAGE_CACHE.get('at',0)>now-30:
            return dict(B2_USAGE_CACHE)

    auth_data=b2_authorize()
    if not auth_data:
        raise RuntimeError('Backblaze غير مربوط')

    total_bytes=0
    total_files=0
    start_name=None
    start_id=None
    while True:
        payload={'bucketId':auth_data['_bucket_id'],'maxFileCount':1000}
        if start_name:
            payload['startFileName']=start_name
        if start_id:
            payload['startFileId']=start_id
        r=requests.post(
            f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_list_file_versions",
            headers={'Authorization':auth_data['authorizationToken']},
            json=payload,timeout=45
        )
        if r.status_code!=200:
            raise RuntimeError(f'Backblaze usage HTTP {r.status_code}: {r.text[:300]}')
        data=r.json() or {}
        for item in data.get('files') or []:
            if item.get('action') in ('upload','copy'):
                total_bytes += int(item.get('contentLength') or 0)
                total_files += 1
        start_name=data.get('nextFileName')
        start_id=data.get('nextFileId')
        if not start_name:
            break

    row={'at':now,'bytes':total_bytes,'files':total_files}
    with B2_USAGE_LOCK:
        B2_USAGE_CACHE.update(row)
    return row

@app.get('/b2-usage')
def b2_usage():
    try:
        usage=b2_storage_usage()
        free_bytes=10*1024*1024*1024
        used=int(usage.get('bytes') or 0)
        return jsonify(
            ok=True,
            bytes=used,
            files=int(usage.get('files') or 0),
            free_bytes=free_bytes,
            remaining_bytes=max(0,free_bytes-used),
            percent=min(100,round((used/free_bytes)*100,1)) if free_bytes else 0
        )
    except Exception as e:
        return jsonify(ok=False,error=str(e)),200

def b2_existing_url(file_name):
    auth_data=b2_authorize()
    if not auth_data:
        return ''
    url=b2_file_url(file_name,auth_data)
    r=requests.head(url,allow_redirects=True,timeout=30)
    if r.status_code==200:
        return url
    if r.status_code==404:
        return ''
    raise RuntimeError(f'Backblaze public file check failed HTTP {r.status_code}; audio bucket must allow public reads')

def b2_upload(file_path, file_name):
    auth_data=b2_authorize()
    if not auth_data:
        return None
    existing=b2_existing_url(file_name)
    if existing:
        return 'skipped',existing
    r=requests.post(
        f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_get_upload_url",
        headers={'Authorization':auth_data['authorizationToken']},
        json={'bucketId':auth_data['_bucket_id']},timeout=30
    )
    if r.status_code!=200:
        raise RuntimeError(f'Backblaze upload URL request failed HTTP {r.status_code}: {r.text[:400]}')
    upload_info=r.json()
    sha1=hashlib.sha1()
    with Path(file_path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            sha1.update(chunk)
        stream.seek(0)
        response=requests.post(
            upload_info['uploadUrl'],
            headers={
                'Authorization':upload_info['authorizationToken'],
                'X-Bz-File-Name':quote(file_name,safe='/'),
                'Content-Type':'audio/mp4',
                'X-Bz-Content-Sha1':sha1.hexdigest(),
            },
            data=stream,timeout=(30,600)
        )
    if response.status_code not in (200,201):
        raise RuntimeError(f'Backblaze upload failed HTTP {response.status_code}: {response.text[:500]}')
    url=b2_file_url(file_name,auth_data)
    check=requests.head(url,allow_redirects=True,timeout=30)
    if check.status_code!=200:
        raise RuntimeError(f'Backblaze upload finished, but the file is not publicly playable (HTTP {check.status_code}); set the bucket to public')
    return 'uploaded',url

def b2_upload_public_file(file_path, file_name, content_type='application/octet-stream', overwrite=False):
    auth_data=b2_authorize()
    if not auth_data: raise RuntimeError('Backblaze غير مربوط')
    if not overwrite:
        existing=b2_existing_url(file_name)
        if existing:return existing
    r=requests.post(
        f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_get_upload_url",
        headers={'Authorization':auth_data['authorizationToken']},
        json={'bucketId':auth_data['_bucket_id']},timeout=30
    )
    if r.status_code!=200:
        raise RuntimeError(f'Backblaze upload URL request failed HTTP {r.status_code}: {r.text[:400]}')
    upload_info=r.json()
    sha1=hashlib.sha1()
    with Path(file_path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):sha1.update(chunk)
        stream.seek(0)
        response=requests.post(upload_info['uploadUrl'],headers={
            'Authorization':upload_info['authorizationToken'],
            'X-Bz-File-Name':quote(file_name,safe='/'),
            'Content-Type':content_type,
            'X-Bz-Content-Sha1':sha1.hexdigest(),
        },data=stream,timeout=(30,900))
    if response.status_code not in (200,201):
        raise RuntimeError(f'Backblaze upload failed HTTP {response.status_code}: {response.text[:500]}')
    url=b2_file_url(file_name,auth_data)
    check=requests.head(url,allow_redirects=True,timeout=30)
    if check.status_code!=200:
        raise RuntimeError(f'Backblaze public file check failed HTTP {check.status_code}')
    return url

GPU_TEMP_PREFIX='nibras-temp'
GPU_JOB_PREFIX='nibras-job-state'

def b2_upload_bytes(data, file_name, content_type='application/octet-stream'):
    auth_data=b2_authorize()
    if not auth_data: raise RuntimeError('Backblaze غير مربوط')
    r=requests.post(f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_get_upload_url",
        headers={'Authorization':auth_data['authorizationToken']},
        json={'bucketId':auth_data['_bucket_id']},timeout=30)
    if r.status_code!=200:
        raise RuntimeError(f'Backblaze upload URL request failed HTTP {r.status_code}: {r.text[:400]}')
    upload_info=r.json(); raw=bytes(data)
    response=requests.post(upload_info['uploadUrl'],headers={
        'Authorization':upload_info['authorizationToken'],
        'X-Bz-File-Name':quote(file_name,safe='/'),
        'Content-Type':content_type,
        'X-Bz-Content-Sha1':hashlib.sha1(raw).hexdigest(),
    },data=raw,timeout=(30,600))
    if response.status_code not in (200,201):
        raise RuntimeError(f'Backblaze upload failed HTTP {response.status_code}: {response.text[:500]}')
    return b2_file_url(file_name,auth_data)

def b2_delete_file(file_name):
    auth_data=b2_authorize()
    if not auth_data:return False
    caps=(auth_data.get('allowed') or {}).get('capabilities') or []
    if 'deleteFiles' not in caps:
        log(f'Backblaze key cannot delete temporary file: {file_name}')
        return False
    r=requests.post(f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_list_file_versions",
        headers={'Authorization':auth_data['authorizationToken']},
        json={'bucketId':auth_data['_bucket_id'],'startFileName':file_name,'maxFileCount':100},timeout=30)
    if r.status_code!=200:
        raise RuntimeError(f'Backblaze list versions failed HTTP {r.status_code}: {r.text[:300]}')
    removed=False
    for item in (r.json() or {}).get('files') or []:
        if item.get('fileName')!=file_name:break
        fid=item.get('fileId')
        if not fid:continue
        d=requests.post(f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_delete_file_version",
            headers={'Authorization':auth_data['authorizationToken']},
            json={'fileName':file_name,'fileId':fid},timeout=30)
        if d.status_code!=200:
            raise RuntimeError(f'Backblaze delete failed HTTP {d.status_code}: {d.text[:300]}')
        removed=True
    return removed


def _cleanup_legacy_tom_jerry_tests_once():
    marker='nibras-maintenance/delete-nageeb-khalfan-reupload-v1.done'
    try:
        auth_data=b2_authorize()
        if not auth_data:return
        mr=requests.get(b2_file_url(marker,auth_data),timeout=20,headers={'Cache-Control':'no-cache'})
        if mr.status_code==200:
            return
        targets=[
            'processed-audio/nageebkhalfan/ia_a4d4f203c294676f763e.m4a',
        ]
        results={}
        for name in targets:
            try:
                results[name]=bool(b2_delete_file(name))
            except Exception as e:
                results[name]='error:'+repr(e)
        # Only mark complete when both are actually gone. This avoids deleting
        # newly re-uploaded replacements on a later restart.
        all_gone=True
        for name in targets:
            rr=requests.get(b2_file_url(name,auth_data),timeout=20,headers={'Cache-Control':'no-cache'})
            if rr.status_code!=404:
                all_gone=False
        log('NAGEEB_KHALFAN_CLEANUP '+json.dumps(results,ensure_ascii=False))
        if all_gone:
            b2_upload_bytes(b'done',marker,'text/plain')
            log('NAGEEB_KHALFAN_CLEANUP complete')
        else:
            log('NAGEEB_KHALFAN_CLEANUP incomplete')
    except Exception as e:
        log('NAGEEB_KHALFAN_CLEANUP exception '+repr(e))

def gpu_job_token(job_id):
    secret=(UPLOAD_KEY or PUBLISH_KEY or TOKEN or 'nibras-gpu-fallback').encode('utf-8')
    return hmac.new(secret,str(job_id).encode('utf-8'),hashlib.sha256).hexdigest()

def gpu_job_state_name(job_id): return f'{GPU_JOB_PREFIX}/{job_id}.json'

def persist_runpod_job(row):
    if not row or not B2_KEY_ID or not B2_APPLICATION_KEY:return
    safe=dict(row)
    for k in ('token','source','result_path','callback_url'):safe.pop(k,None)
    try:
        b2_upload_bytes(json.dumps(safe,ensure_ascii=False,separators=(',',':')).encode('utf-8'),
            gpu_job_state_name(safe.get('job_id','')),'application/json')
    except Exception as e:log(f'persist gpu job failed {safe.get("job_id","")}: {e}')

def load_persisted_runpod_job(job_id):
    if not B2_KEY_ID or not B2_APPLICATION_KEY:return {}
    try:
        auth_data=b2_authorize()
        r=requests.get(b2_file_url(gpu_job_state_name(job_id),auth_data),timeout=30,headers={'Cache-Control':'no-cache'})
        if r.status_code==404:return {}
        if r.status_code!=200:raise RuntimeError(f'HTTP {r.status_code}')
        row=r.json() or {}
        if row.get('job_id')!=job_id:return {}
        row['token']=gpu_job_token(job_id)
        base=(row.get('base_url') or '').rstrip('/')
        if base:
            row['callback_url']=f"{base}/gpu-result/{job_id}?t={row['token']}"
        return row
    except Exception as e:
        log(f'load gpu job failed {job_id}: {e}'); return {}

def cleanup_gpu_source(row):
    name=(row or {}).get('source_b2_name') or ''
    if not name:return
    try:b2_delete_file(name)
    except Exception as e:log(f'cleanup temporary source failed {name}: {e}')

if B2_KEY_ID and B2_APPLICATION_KEY:
    try:
        auth_data=b2_authorize()
        log(f"Backblaze storage authorized for bucket {auth_data['_bucket_name']}")
    except Exception as e:
        log(f'Backblaze storage authorization failed: {e}')

def upload_series(file,vid,series,map_target='youtube'):
    folder=series_slug(series)
    path=f'{FOLDER}/{folder}/{vid}.m4a' if FOLDER else f'{folder}/{vid}.m4a'
    api,old=gh_get(path)
    if old:
        return 'skipped', update_selected_audio_map_url(vid,raw_url(path),map_target)

    file_path=Path(file)
    if B2_KEY_ID and B2_APPLICATION_KEY:
        result=b2_upload(file_path,path)
        if result:
            status,url=result
            return status, update_selected_audio_map_url(vid,url,map_target)
    message=f'Add cleaned audio {folder}/{vid}'
    size=file_path.stat().st_size

    # Small files: use Contents API. Larger files: bypass REST size limits and
    # push the blob through Git protocol (GitHub accepts regular files <100MB).
    if size < 20*1024*1024:
        data=base64.b64encode(file_path.read_bytes()).decode()
        last=None
        for attempt in range(3):
            try:
                r=requests.put(api,headers=headers(),json={'message':message,'content':data,'branch':BRANCH},timeout=300)
                last=r
                if r.status_code in (200,201):
                    return 'uploaded', update_selected_audio_map_url(vid,raw_url(path),map_target)
                if r.status_code==422 and ('too large' in (r.text or '').lower() or 'input was too large' in (r.text or '').lower()):
                    break
                if r.status_code in (500,502,503,504):
                    time.sleep(2*(attempt+1))
                    continue
                raise RuntimeError(f'GitHub upload {r.status_code}: {r.text[:1000]}')
            except requests.RequestException:
                if attempt<2:
                    time.sleep(2*(attempt+1))
                    continue
                break

    _git_push_upload(file_path,path,message)
    return 'uploaded', update_selected_audio_map_url(vid,raw_url(path),map_target)

AUDIO_EXTS={'.m4a','.aac','.mp3','.wav','.flac','.ogg','.opus'}

def publish_clean_path(source_path, original_name, vid, series, map_target='youtube'):
    suffix=Path(original_name or source_path).suffix.lower() or '.source'
    work=QDIR/f'clean_{uuid.uuid4().hex}_{vid}{suffix}'
    final=O/f'{vid}.m4a'
    if Path(source_path)!=work:
        shutil.copyfile(str(source_path),str(work))
    if not work.exists() or work.stat().st_size<1000:
        raise RuntimeError('الملف فارغ أو غير مكتمل')
    try:
        probe=run(['ffprobe','-v','error','-select_streams','a:0','-show_entries','stream=codec_name','-of','default=noprint_wrappers=1:nokey=1',str(work)]).strip().lower()
        if probe=='aac':
            run(['ffmpeg','-y','-i',str(work),'-vn','-c:a','copy',str(final)])
        else:
            run(['ffmpeg','-y','-i',str(work),'-vn','-c:a','aac','-b:a','192k',str(final)])
        if not final.exists() or final.stat().st_size<10000:
            raise RuntimeError('تعذر تجهيز ملف M4A')
        return upload_series(final,vid,series,map_target)
    finally:
        try:work.unlink()
        except:pass
        try:final.unlink()
        except:pass

def publish_zip_file(incoming, series, map_target='youtube'):
    archive=QDIR/f'zip_{uuid.uuid4().hex}.zip'
    incoming.save(archive)
    results=[]
    errors=[]
    try:
        with zipfile.ZipFile(archive,'r') as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                original=Path(info.filename).name
                if Path(original).suffix.lower() not in AUDIO_EXTS:
                    continue
                if '_no_vocals_' in original.lower():
                    continue
                vid=_derive_publish_id('',original,map_target)
                if not vid:
                    errors.append(f'{info.filename}: تعذر معرفة Video ID من اسم الملف')
                    continue
                try:
                    _,existing_url=series_existing(vid,series,map_target)
                    if existing_url:
                        results.append((original,vid,'exists',existing_url))
                        continue
                except Exception as e:
                    errors.append(f'{info.filename}: تعذر فحص التكرار: {str(e)}')
                    continue
                extracted=QDIR/f'zipitem_{uuid.uuid4().hex}{Path(original).suffix.lower()}'
                try:
                    with z.open(info,'r') as src, open(extracted,'wb') as dst:
                        shutil.copyfileobj(src,dst)
                    status,url=publish_clean_path(extracted,original,vid,series,map_target)
                    results.append((original,vid,status,url))
                except Exception as e:
                    errors.append(f'{info.filename}: {str(e)}')
                finally:
                    try:extracted.unlink()
                    except:pass
        if not results and not errors:
            errors.append('ملف ZIP لا يحتوي ملفات صوت مدعومة')
        return results,errors
    finally:
        try:archive.unlink()
        except:pass

def publish_clean_file(incoming, vid, series, map_target='youtube'):
    suffix=Path(incoming.filename or '').suffix.lower() or '.source'
    source=QDIR/f'incoming_{uuid.uuid4().hex}{suffix}'
    incoming.save(source)
    try:
        return publish_clean_path(source,incoming.filename,vid,series,map_target)
    finally:
        try:source.unlink()
        except:pass

def set_publish_job(job_id, **changes):
    with PUBLISH_JOBS_LOCK:
        row=PUBLISH_JOBS.get(job_id,{})
        row.update(changes)
        row['updated_at']=time.time()
        PUBLISH_JOBS[job_id]=row

def publish_zip_path(archive, series, map_target='youtube', job_id=None):
    results=[]
    errors=[]
    archive=Path(archive)
    try:
        with zipfile.ZipFile(archive,'r') as z:
            items=[x for x in z.infolist() if not x.is_dir() and Path(Path(x.filename).name).suffix.lower() in AUDIO_EXTS and '_no_vocals_' not in Path(x.filename).name.lower()]
            total=len(items)
            if job_id:
                set_publish_job(job_id,status='processing',stage='extracting',total=total,done=0,results=[],errors=[])
            for index,info in enumerate(items,1):
                original=Path(info.filename).name
                vid=_derive_publish_id('',original,map_target)
                if not vid:
                    errors.append(f'{info.filename}: تعذر معرفة Video ID من اسم الملف')
                    if job_id:set_publish_job(job_id,done=index,results=results,errors=errors,current=original)
                    continue
                try:
                    _,existing_url=series_existing(vid,series,map_target)
                    if existing_url:
                        results.append((original,vid,'exists',existing_url))
                        if job_id:set_publish_job(job_id,done=index,results=results,errors=errors,current=original)
                        continue
                except Exception as e:
                    errors.append(f'{info.filename}: تعذر فحص التكرار: {str(e)}')
                    if job_id:set_publish_job(job_id,done=index,results=results,errors=errors,current=original)
                    continue
                extracted=QDIR/f'zipitem_{uuid.uuid4().hex}{Path(original).suffix.lower()}'
                try:
                    if job_id:set_publish_job(job_id,stage='publishing',current=original,done=index-1)
                    with z.open(info,'r') as src, open(extracted,'wb') as dst:
                        shutil.copyfileobj(src,dst)
                    status,url=publish_clean_path(extracted,original,vid,series,map_target)
                    results.append((original,vid,status,url))
                except Exception as e:
                    errors.append(f'{info.filename}: {str(e)}')
                finally:
                    try:extracted.unlink()
                    except:pass
                if job_id:set_publish_job(job_id,done=index,results=results,errors=errors,current=original)
        if not results and not errors:
            errors.append('ملف ZIP لا يحتوي ملفات صوت مدعومة')
        return results,errors
    finally:
        try:archive.unlink()
        except:pass

def publish_zip_background(job_id, archive, series, map_target='youtube'):
    try:
        results,errors=publish_zip_path(archive,series,map_target,job_id)
        set_publish_job(job_id,status='done',stage='done',results=results,errors=errors,finished_at=time.time(),current='')
        log(f'publish-clean {job_id}: done results={len(results)} errors={len(errors)}')
    except Exception as e:
        set_publish_job(job_id,status='failed',stage='failed',error=str(e)[-2500:],finished_at=time.time())
        log(f'publish-clean {job_id}: FAILED {str(e)[-1000:]}')
        try:Path(archive).unlink()
        except:pass

@app.get('/publish-clean-status/<job_id>')
def publish_clean_status(job_id):
    with PUBLISH_JOBS_LOCK:
        row=dict(PUBLISH_JOBS.get(job_id,{}) or {})
    if not row:
        return jsonify(ok=False,error='job_not_found'),404
    row.pop('archive',None)
    return jsonify(ok=True,**row)

@app.route('/publish-clean',methods=['GET','POST'])
def publish_clean():
    supplied=''
    vid=(request.values.get('id') or '').strip()
    series=(request.values.get('series') or '').strip()
    map_target=_map_target(request.values.get('map_target'))
    message=''
    if request.method=='POST':
        files=[x for x in request.files.getlist('file') if x and x.filename]
        if not files:
            message='<div class="err">اختر ملف صوت واحد على الأقل.</div>'
        else:
            # ZIP files can take minutes when they contain many episodes. Save the
            # archive first, answer the browser immediately, then publish in a
            # background job so a proxy/client timeout cannot abort the work.
            if len(files)==1 and Path(files[0].filename or '').suffix.lower()=='.zip':
                incoming=files[0]
                job_id=uuid.uuid4().hex
                archive=QDIR/f'publish_{job_id}.zip'
                incoming.save(archive)
                if not archive.exists() or archive.stat().st_size<1000:
                    try:archive.unlink()
                    except:pass
                    return jsonify(ok=False,error='ملف ZIP فارغ أو غير مكتمل'),400
                with PUBLISH_JOBS_LOCK:
                    PUBLISH_JOBS[job_id]={
                        'job_id':job_id,'status':'queued','stage':'waiting','series':series,'map_target':map_target,
                        'filename':incoming.filename,'size':archive.stat().st_size,'total':0,'done':0,
                        'current':'','results':[],'errors':[],'error':'',
                        'created_at':time.time(),'updated_at':time.time(),'archive':str(archive)
                    }
                threading.Thread(target=publish_zip_background,args=(job_id,archive,series,map_target),daemon=True,name=f'publish-{job_id[:8]}').start()
                log(f'publish-clean {job_id}: accepted zip size={archive.stat().st_size}')
                return jsonify(ok=True,status='queued',job_id=job_id),202

            results=[]
            errors=[]
            for incoming in files:
                if Path(incoming.filename or '').suffix.lower()=='.zip':
                    try:
                        zip_results,zip_errors=publish_zip_file(incoming,series,map_target)
                        results.extend(zip_results)
                        errors.extend(zip_errors)
                    except Exception as e:
                        errors.append(f'{incoming.filename}: {str(e)}')
                    continue

                this_vid=_derive_publish_id(vid if len(files)==1 else '',incoming.filename,map_target)
                if not this_vid:
                    errors.append(f'{incoming.filename}: تعذر معرفة Video ID من اسم الملف')
                    continue
                try:
                    _,existing_url=series_existing(this_vid,series,map_target)
                    if existing_url:
                        results.append((incoming.filename,this_vid,'exists',existing_url))
                        continue
                    status,url=publish_clean_file(incoming,this_vid,series,map_target)
                    results.append((incoming.filename,this_vid,status,url))
                except Exception as e:
                    errors.append(f'{incoming.filename}: {str(e)}')
            parts=[]
            if results:
                uploaded_count=sum(1 for _,_,s,_ in results if s in ('uploaded','published'))
                existing_count=sum(1 for _,_,s,_ in results if s in ('exists','skipped'))
                def _status_ar(s):
                    return '♻️ موجود مسبقًا — تم تخطيه' if s in ('exists','skipped') else '✅ تم رفعه وربطه'
                rows=''.join(
                    f'<div style="margin:9px 0;padding:9px 0;border-bottom:1px solid #385044"><b>{html.escape(v)}</b> — {_status_ar(s)}<br><a style="color:#ffd982" href="{html.escape(u)}">{html.escape(fn)}</a></div>'
                    for fn,v,s,u in results
                )
                summary=f'تم رفع {uploaded_count} جديد'
                if existing_count:
                    summary+=f' — وتخطي {existing_count} موجود مسبقًا'
                parts.append(f'<div class="ok">{summary} ✅{rows}</div>')
            if errors:
                parts.append('<div class="err">'+ '<br>'.join(html.escape(x) for x in errors) +'</div>')
            message=''.join(parts)
    return render_template_string(CLEAN_HTML,key=supplied,vid=html.escape(vid),series=html.escape(series),message=message)


STATUS_HTML='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>نبراس | حالة الصوت</title>
<style>
body{font-family:Arial;background:#15171b;color:#fff;max-width:920px;margin:30px auto;padding:18px}.c{background:#22262c;padding:24px;border-radius:18px}
input,button{padding:12px;border-radius:10px;border:1px solid #444;box-sizing:border-box;font-size:15px}input{background:#111;color:#fff}button{background:#f6c35f;color:#162436;font-weight:900;cursor:pointer}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.sum{background:#111;padding:14px;border-radius:12px;margin:16px 0}
table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #3a3d43;text-align:right}.yes{color:#8fda9b}.no{color:#ff9790}.mono{font-family:monospace;direction:ltr;text-align:left}
a{color:#ffd982}@media(max-width:650px){.grid{grid-template-columns:1fr}}
</style>
<div class=c><h2>حالة المقاطع المربوطة بالصوت</h2>
<form method=get><input type=hidden name=k value="{{key}}">
<div class=grid><input name=playlist value="{{playlist}}" placeholder="Playlist ID أو رابط يوتيوب" required><input name=series value="{{series}}" placeholder="مجلد المسلسل، مثال barbear"></div>
<button style="margin-top:10px;width:100%" type=submit>فحص القائمة</button></form>
{{body|safe}}
<p><a href="/publish-clean">رجوع لرفع الملفات</a></p></div></html>'''

def load_audio_map():
    try:
        _,old=gh_get(MAP_PATH)
        if not old:return {}
        raw=base64.b64decode(old.get('content','')).decode('utf-8')
        obj=json.loads(raw)
        return obj if isinstance(obj,dict) else {}
    except Exception:
        return {}

def playlist_id(value):
    value=str(value or '').strip()
    m=re.search(r'[?&]list=([A-Za-z0-9_-]+)',value)
    return m.group(1) if m else re.sub(r'[^A-Za-z0-9_-]','',value)

def playlist_entries(value):
    pid=playlist_id(value)
    if not pid:return []
    url=f'https://www.youtube.com/playlist?list={pid}'
    out=run(['yt-dlp','--flat-playlist','--dump-single-json','--no-warnings',url])
    obj=json.loads(out)
    return [{'id':str(x.get('id') or ''),'title':str(x.get('title') or '')} for x in (obj.get('entries') or []) if x and x.get('id')]


GPU_CLEAN_HTML='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>نبراس | إزالة الموسيقى بالـ GPU</title>
<style>
body{font-family:Arial;background:#15171b;color:#fff;max-width:760px;margin:30px auto;padding:18px}.c{background:#22262c;padding:24px;border-radius:18px}
label{display:block;margin:14px 0 7px;font-weight:700}input,select,button{width:100%;padding:13px;border-radius:10px;border:1px solid #444;box-sizing:border-box;font-size:16px}
input,select{background:#111;color:#fff}button{margin-top:18px;background:#7c5cff;color:#fff;font-weight:900;cursor:pointer}button:disabled{opacity:.55}
small{color:#aaa}.ok{background:#163b2b;padding:12px;border-radius:10px;margin-top:16px}.err{background:#4b1d23;padding:12px;border-radius:10px;margin-top:16px}
.job{background:#111;border:1px solid #3b4149;border-radius:12px;padding:12px;margin-top:10px}.bar{height:10px;background:#2b3037;border-radius:999px;overflow:hidden;margin-top:8px}
.fill{height:100%;width:5%;background:#8f7cff;transition:width .2s}.muted{color:#aaa;font-size:13px}
</style>
<div class=c><h2>نبراس | إزالة الموسيقى بالـ GPU</h2>
<div id=storageUsage class=job style="margin-top:0">
  <b>مساحة الصوتيات — Backblaze B2</b>
  <div id=storageText class=muted style="margin-top:7px">جاري حساب المساحة...</div>
  <div class=bar><div id=storageFill class=fill style="width:0%"></div></div>
</div>
<p>ارفع الصوت الخام، وسيتم فصل الموسيقى على RunPod ثم رفع الصوت النظيف وربطه تلقائيًا في الماب الذي تختاره.</p>
<form id=f enctype=multipart/form-data>
<label>مجلد المسلسل</label><input name=series placeholder="مثال: mshmsh" required>
<label>نوع الفيديو / الماب</label>
<select name=map_target required>
  <option value="youtube" selected>YouTube — audio-map.json</option>
  <option value="dailymotion">Dailymotion — dailymotion-audio-map.json</option>
  <option value="archive">Archive.org — archive-audio-map.json</option>
</select>
<label>الملفات</label><input type=file name=file multiple required accept=".m4a,.aac,.mp3,.wav,.flac,.ogg,.opus,.mp4,.webm,.zip,audio/*,video/mp4,application/zip">
<small>YouTube وDailymotion: يبدأ الاسم بمعرّف الفيديو. Archive.org: ارفع الملف باسم الحلقة الأصلي، وسيتم توليد مفتاح الربط تلقائيًا من الاسم. يدعم ZIP أيضًا.</small>
<button id=b type=submit>رفع وبدء إزالة الموسيقى</button>
</form>
<div id=uploadWrap style="display:none" class=job>
  <b id=uploadLabel>جاري رفع الملفات...</b>
  <div class=bar><div id=uploadFill class=fill style="width:0%"></div></div>
  <div id=uploadPct class=muted style="margin-top:6px">0%</div>
</div>
<div id=msg></div><div id=jobs></div>
<p style="margin-top:18px"><a style="color:#ffd982" href="/publish-clean">رفع صوت جاهز بدون موسيقى</a></p>
</div>
<script>
const f=document.getElementById('f'),b=document.getElementById('b'),msg=document.getElementById('msg'),jobs=document.getElementById('jobs'),uploadWrap=document.getElementById('uploadWrap'),uploadFill=document.getElementById('uploadFill'),uploadPct=document.getElementById('uploadPct'),uploadLabel=document.getElementById('uploadLabel');
function esc(v){const d=document.createElement('div');d.textContent=String(v||'');return d.innerHTML;}
function fmtGB(bytes){return (Number(bytes||0)/1024/1024/1024).toFixed(2)}
function loadStorageUsage(){
 fetch('/b2-usage',{cache:'no-store'}).then(r=>r.json()).then(j=>{
   const text=document.getElementById('storageText'),fill=document.getElementById('storageFill');
   if(!text||!fill)return;
   if(!j.ok){text.textContent='تعذر قراءة المساحة الآن';fill.style.width='0%';return}
   text.textContent='المستخدم '+fmtGB(j.bytes)+' GB من 10 GB — المتبقي '+fmtGB(j.remaining_bytes)+' GB — '+j.files+' ملف';
   fill.style.width=Math.max(1,Math.min(100,Number(j.percent||0)))+'%';
 }).catch(()=>{const text=document.getElementById('storageText');if(text)text.textContent='تعذر قراءة المساحة الآن'});
}
loadStorageUsage();
function pollBatch(id){
 const box=document.createElement('div');box.className='job';box.innerHTML='<b>ملف ZIP</b><div class=muted>جاري تجهيز الملفات...</div><div class=bar><div class=fill style="width:20%"></div></div>';jobs.prepend(box);
 const tick=()=>fetch('/gpu-clean-status/'+id).then(async r=>({status:r.status,body:await r.json()})).then(({status,body:j})=>{
   if(!j.ok){
     if(status===404&&j.error==='job_not_found'){box.innerHTML='<div class=err>تعذر استرجاع مهمة ZIP المحفوظة. حدّث الصفحة وحاول مرة أخرى.</div>';return}
     setTimeout(tick,2000);return
   }
   if(j.status==='failed'){box.innerHTML='<div class=err>فشل فك ZIP: '+esc(j.error||'خطأ')+'</div>';return}
   if(j.status==='done'){
     box.innerHTML='<div class=ok>تم فك ZIP وتجهيز '+(j.count||0)+' ملف ✅</div>';
     (j.jobs||[]).forEach(poll);return;
   }
   box.querySelector('.muted').textContent='جاري فك ZIP وتجهيز الملفات...';
   setTimeout(tick,2000);
 }).catch(()=>setTimeout(tick,2500));
 tick();
}
function poll(id){
 fetch('/gpu-clean-status/'+encodeURIComponent(id),{cache:'no-store'}).then(async r=>({status:r.status,body:await r.json()})).then(({status,body:j})=>{
   let box=document.getElementById('j_'+id); if(!box){box=document.createElement('div');box.className='job';box.id='j_'+id;jobs.appendChild(box);}
   if(!j.ok){
     if(status===404&&j.error==='job_not_found'){box.innerHTML='<div class=ok>الخدمة أعادت التشغيل — ملفك محفوظ، جاري استرجاع حالة المهمة...</div>';setTimeout(()=>poll(id),3000);return}
     box.innerHTML='<div class=err>'+esc(j.error||'تعذر قراءة حالة المهمة')+'</div>';return;
   }
   let p=8,label='بانتظار RunPod...';
   if(j.stage==='recovered'){p=55;label='تمت استعادة المهمة — جاري التحقق من النتيجة...'}
   else if(j.status==='processing'){p=45;label='جاري فصل الموسيقى على GPU...'}
   if(j.status==='publishing'){p=82;label='اكتمل الفصل — جاري الرفع إلى GitHub...'}
   if(j.status==='done'){p=100;const mapName=j.map_target==='archive'?'archive-audio-map.json':(j.map_target==='dailymotion'?'dailymotion-audio-map.json':'audio-map.json');label=(j.result==='exists'?'موجود مسبقًا — تم التخطي ✅':'اكتملت المعالجة وتم تحديث '+mapName+' ✅');loadStorageUsage()}
   if(j.status==='failed'){p=100;label='فشل ❌'}
   const profile=j.clean_profile?('<div class="muted" style="margin-top:5px">طريقة التنقية: '+esc(j.clean_profile)+(j.clean_profile==='vocals-only-min-music-v1'?' ✅':' ⚠️')+'</div>'):'';
   box.innerHTML='<b>'+esc(j.id||j.filename)+'</b><div class="muted">'+esc(label)+'</div>'+profile+'<div class=bar><div class=fill style="width:'+p+'%"></div></div>'+(j.url?'<div style="margin-top:8px"><a style="color:#ffd982" href="'+esc(j.url)+'">فتح الصوت النظيف</a></div>':'')+(j.error?'<div class=err>'+esc(j.error)+'</div>':'');
   if(j.status!=='done'&&j.status!=='failed')setTimeout(()=>poll(id),2000);
 }).catch(()=>setTimeout(()=>poll(id),3000));
}
f.addEventListener('submit',async e=>{
 e.preventDefault();b.disabled=true;msg.innerHTML='<div class=ok>جاري رفع الملفات إلى نبراس...</div>';
 const files=[...f.querySelector('input[type=file]').files];
 const series=f.querySelector('input[name=series]').value.trim();
 const mapTarget=f.querySelector('select[name=map_target]').value;
 const oneZip=files.length===1&&files[0].name.toLowerCase().endsWith('.zip');

 uploadWrap.style.display='block';uploadFill.style.width='0%';uploadPct.textContent='0%';uploadLabel.textContent='جاري رفع الملفات...';

 if(oneZip){
   try{
     const file=files[0],chunkSize=2*1024*1024,total=Math.ceil(file.size/chunkSize);
     const uploadId=(crypto.randomUUID?crypto.randomUUID().replaceAll('-',''):Array.from(crypto.getRandomValues(new Uint8Array(16))).map(x=>x.toString(16).padStart(2,'0')).join(''));
     let batchId=null;
     for(let i=0;i<total;i++){
       const fd=new FormData();
       fd.append('upload_id',uploadId);fd.append('series',series);fd.append('map_target',mapTarget);fd.append('filename',file.name);
       fd.append('index',String(i));fd.append('total',String(total));
       fd.append('chunk',file.slice(i*chunkSize,Math.min(file.size,(i+1)*chunkSize)),file.name+'.part');
       let j=null,lastError=null;
       for(let attempt=0;attempt<4;attempt++){
         let response=null;
         try{response=await fetch('/gpu-clean-chunk',{method:'POST',body:fd});}
         catch(err){lastError=err;}
         if(response){
           const body=await response.json().catch(()=>({ok:false,error:'استجابة غير متوقعة'}));
           if(response.ok&&body.ok){j=body;lastError=null;break;}
           lastError=new Error(body.error||('HTTP '+response.status));
           if(![502,503,504].includes(response.status))throw lastError;
         }
         if(attempt<3){
           uploadLabel.textContent='إعادة إرسال الجزء '+(i+1)+'...';
           await new Promise(resolve=>setTimeout(resolve,1000*(attempt+1)));
         }
       }
       if(!j)throw lastError||new Error('تعذر رفع الجزء بعد عدة محاولات');
       const p=Math.round(((i+1)/total)*100);
       uploadFill.style.width=p+'%';uploadPct.textContent=p+'%';
       uploadLabel.textContent='جاري رفع ZIP — الجزء '+(i+1)+' من '+total;
       if(j.batch_id)batchId=j.batch_id;
     }
     uploadLabel.textContent='تم رفع ZIP بالكامل ✅';msg.innerHTML='<div class=ok>تم الاستلام ✅ جاري فك الملف المضغوط وتجهيز المهام بالخلفية.</div>';
     if(batchId)pollBatch(batchId);
   }catch(err){
     msg.innerHTML='<div class=err>تعذر بدء المعالجة: '+esc(err.message||err)+'</div>';
   }finally{b.disabled=false}
   return;
 }

 const x=new XMLHttpRequest();x.open('POST','/gpu-clean',true);
 x.upload.onprogress=(e)=>{if(e.lengthComputable){const p=Math.max(0,Math.min(100,Math.round((e.loaded/e.total)*100)));uploadFill.style.width=p+'%';uploadPct.textContent=p+'%';if(p>=100)uploadLabel.textContent='اكتمل الرفع — جاري تجهيز المهام...';}};
 x.onload=()=>{b.disabled=false;try{const j=JSON.parse(x.responseText);if(x.status===202&&j.ok){uploadFill.style.width='100%';uploadPct.textContent='100%';uploadLabel.textContent='تم رفع الملفات بنجاح ✅';msg.innerHTML='<div class=ok>تم الاستلام ✅ تابع حالة كل ملف بالأسفل.</div>';if(j.batch_id){pollBatch(j.batch_id)}else{(j.jobs||[]).forEach(poll)}return;}msg.innerHTML='<div class=err>'+esc(j.error||'تعذر بدء المعالجة')+'</div>';}catch(e){msg.innerHTML='<div class=err>استجابة غير متوقعة من الخادم</div>';}};
 x.onerror=()=>{b.disabled=false;msg.innerHTML='<div class=err>تعذر الاتصال بالخادم</div>'};
 x.send(new FormData(f));
});
</script></html>'''

def set_runpod_job(job_id, **changes):
    with RUNPOD_JOBS_LOCK:
        row=RUNPOD_JOBS.get(job_id,{})
        row.update(changes)
        row['updated_at']=time.time()
        RUNPOD_JOBS[job_id]=row
        snapshot=dict(row)
    persist_runpod_job(snapshot)

def runpod_headers():
    return {'Authorization':f'Bearer {RUNPOD_API_KEY}','Content-Type':'application/json'}

def public_base_url():
    proto=(request.headers.get('X-Forwarded-Proto') or request.scheme or 'https').split(',')[0].strip()
    host=request.host
    if host.endswith('.up.railway.app'):
        proto='https'
    return f'{proto}://{host}'

def runpod_process_job(job_id):
    with RUNPOD_JOBS_LOCK: job=dict(RUNPOD_JOBS.get(job_id,{}) or {})
    if not job:
        job=load_persisted_runpod_job(job_id)
        if job:
            with RUNPOD_JOBS_LOCK: RUNPOD_JOBS[job_id]=job
    if not job:return
    final=None
    try:
        remote_id=job.get('remote_job_id')
        if not remote_id:
            set_runpod_job(job_id,status='processing',stage='submit')
            payload={'input':{'source_url':job['source_url'],'youtube_id':job['id'],'upload_url':job['callback_url']}}
            r=requests.post(f'{RUNPOD_BASE}/{RUNPOD_ENDPOINT_ID}/run',headers=runpod_headers(),json=payload,timeout=60)
            if r.status_code>=300:raise RuntimeError(f'RunPod submit {r.status_code}: {r.text[:700]}')
            remote_id=(r.json() or {}).get('id')
            if not remote_id:raise RuntimeError('RunPod لم يرجع Job ID')
            set_runpod_job(job_id,remote_job_id=remote_id,status='processing',stage='gpu')
        else:set_runpod_job(job_id,status='processing',stage='gpu')
        deadline=time.time()+7200; output=None
        while time.time()<deadline:
            with RUNPOD_JOBS_LOCK: latest=dict(RUNPOD_JOBS.get(job_id,{}) or {})
            if latest.get('status')=='done':return
            q=requests.get(f'{RUNPOD_BASE}/{RUNPOD_ENDPOINT_ID}/status/{remote_id}',headers=runpod_headers(),timeout=60)
            if q.status_code>=500:time.sleep(3);continue
            if q.status_code>=300:raise RuntimeError(f'RunPod status {q.status_code}: {q.text[:700]}')
            data=q.json() or {}; status=data.get('status')
            if status=='COMPLETED':output=data.get('output') or {};break
            if status in ('FAILED','CANCELLED','TIMED_OUT'):raise RuntimeError(f'RunPod انتهى بالحالة {status}: {str(data)[:900]}')
            time.sleep(3)
        if output is None:raise RuntimeError('انتهت مهلة انتظار RunPod')
        for _ in range(120):
            with RUNPOD_JOBS_LOCK: latest=dict(RUNPOD_JOBS.get(job_id,{}) or {})
            if latest.get('status')=='done':return
            time.sleep(1)
        if output.get('error'):raise RuntimeError(str(output.get('error')))
        b64=output.get('audio_base64')
        if b64:
            final=O/f'gpu_{job_id}_{job["id"]}.m4a'; final.write_bytes(base64.b64decode(b64))
            if final.stat().st_size<10000:raise RuntimeError('ملف RunPod الناتج غير مكتمل')
            clean_profile=(output.get('clean_profile') or 'legacy-vocals-only')
            set_runpod_job(job_id,status='publishing',stage='storage',clean_profile=clean_profile)
            status,url=upload_series(final,job['id'],job['series'],job.get('map_target','youtube'))
            set_runpod_job(job_id,status='done',stage='done',result=status,url=url,error='',finished_at=time.time())
            cleanup_gpu_source(job); log(f'gpu-clean {job_id} {job["id"]}: done {url}'); return
        raise RuntimeError('RunPod اكتمل لكن لم يسلّم ملف الصوت')
    except Exception as e:
        err=str(e)[-2500:]; set_runpod_job(job_id,status='failed',stage='failed',error=err,finished_at=time.time())
        log(f'gpu-clean {job_id}: FAILED {str(e)[-1000:]}')
    finally:
        if final:
            try:final.unlink()
            except:pass

def recover_pending_runpod_jobs():
    if not B2_KEY_ID or not B2_APPLICATION_KEY:return
    try:
        auth_data=b2_authorize()
        start=None; names=[]
        while True:
            payload={'bucketId':auth_data['_bucket_id'],'prefix':GPU_JOB_PREFIX+'/','maxFileCount':1000}
            if start:payload['startFileName']=start
            r=requests.post(f"{auth_data['apiUrl'].rstrip('/')}/b2api/v2/b2_list_file_names",
                headers={'Authorization':auth_data['authorizationToken']},json=payload,timeout=30)
            if r.status_code!=200:raise RuntimeError(f'HTTP {r.status_code}: {r.text[:300]}')
            data=r.json() or {}
            names.extend([x.get('fileName','') for x in data.get('files') or []])
            start=data.get('nextFileName')
            if not start:break
        ensure_runpod_worker()
        recovered=0
        for name in names:
            m=re.fullmatch(re.escape(GPU_JOB_PREFIX)+r'/([a-f0-9]{32})\.json',name or '')
            if not m:continue
            jid=m.group(1)
            row=load_persisted_runpod_job(jid)
            if not row or row.get('status') not in ('queued','processing','publishing'):continue
            with RUNPOD_JOBS_LOCK:
                if jid in RUNPOD_JOBS:continue
                RUNPOD_JOBS[jid]=row
            set_runpod_job(jid,status='queued',stage='recovered',error='')
            RUNPOD_QUEUE.put(jid);recovered+=1
        if recovered:log(f'recovered {recovered} GPU jobs after restart')
    except Exception as e:
        log(f'GPU recovery scan failed: {e}')


def _runpod_queue_worker():
    while True:
        job_id=RUNPOD_QUEUE.get()
        try:
            runpod_process_job(job_id)
        finally:
            RUNPOD_QUEUE.task_done()

def ensure_runpod_worker():
    global RUNPOD_WORKER_STARTED
    with RUNPOD_WORKER_LOCK:
        if RUNPOD_WORKER_STARTED:return
        threading.Thread(target=_runpod_queue_worker,daemon=True,name='runpod-serial-worker').start()
        RUNPOD_WORKER_STARTED=True

def _enqueue_gpu_source(original, source, series, base_url, map_target='youtube'):
    map_target=_map_target(map_target); vid=_derive_publish_id('',original,map_target); source=Path(source)
    if not vid:
        try:source.unlink()
        except:pass
        return None
    try:
        _,existing_url=series_existing(vid,series,map_target)
        if existing_url:
            try:source.unlink()
            except:pass
            skipped_id=uuid.uuid4().hex
            row={'job_id':skipped_id,'id':vid,'filename':original,'series':series,'map_target':map_target,
                 'status':'done','stage':'skipped','url':existing_url,'error':'','result':'exists',
                 'created_at':time.time(),'updated_at':time.time(),'finished_at':time.time()}
            with RUNPOD_JOBS_LOCK:RUNPOD_JOBS[skipped_id]=row
            persist_runpod_job(row);return skipped_id
    except Exception as e:log(f'gpu duplicate check failed {vid}: {e}')
    if not B2_KEY_ID or not B2_APPLICATION_KEY:
        try:source.unlink()
        except:pass
        raise RuntimeError('Backblaze مطلوب لحفظ ملفات GPU مؤقتًا بأمان')
    job_id=uuid.uuid4().hex; ext=source.suffix.lower() or '.source'
    temp_name=f'{GPU_TEMP_PREFIX}/{job_id}/{vid}{ext}'
    try:_,source_url=b2_upload(source,temp_name)
    except Exception:
        try:source.unlink()
        except:pass
        raise
    try:source.unlink()
    except:pass
    token=gpu_job_token(job_id); callback_url=f'{base_url}/gpu-result/{job_id}?t={token}'
    row={'job_id':job_id,'id':vid,'filename':original,'series':series,'map_target':map_target,'source':'',
         'source_b2_name':temp_name,'source_url':source_url,'base_url':base_url,'callback_url':callback_url,'token':token,
         'status':'queued','stage':'waiting','url':'','error':'','created_at':time.time(),'updated_at':time.time()}
    with RUNPOD_JOBS_LOCK:RUNPOD_JOBS[job_id]=row
    persist_runpod_job(row);ensure_runpod_worker();RUNPOD_QUEUE.put(job_id);return job_id

def _process_gpu_zip(zpath, series, base_url, batch_id, map_target='youtube'):
    jobs=[]
    try:
        with zipfile.ZipFile(zpath,'r') as z:
            for info in z.infolist():
                if info.is_dir(): continue
                original=Path(info.filename).name
                ext=Path(original).suffix.lower()
                if ext not in RUNPOD_AUDIO_EXTS: continue
                dst=QDIR/f'gpu_src_{uuid.uuid4().hex}{ext}'
                with z.open(info,'r') as src, open(dst,'wb') as out:
                    shutil.copyfileobj(src,out)
                jid=_enqueue_gpu_source(original,dst,series,base_url,map_target)
                if jid: jobs.append(jid)
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            row.update(status='done',stage='expanded',jobs=jobs,count=len(jobs),updated_at=time.time(),finished_at=time.time())
            RUNPOD_JOBS[batch_id]=row
    except Exception as e:
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            row.update(status='failed',stage='zip_error',error=str(e),updated_at=time.time(),finished_at=time.time())
            RUNPOD_JOBS[batch_id]=row
    finally:
        try: zpath.unlink()
        except: pass

def _process_gpu_files_background(saved_files, series, base_url, batch_id, map_target='youtube'):
    jobs=[]
    try:
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            row.update(status='processing',stage='staging',updated_at=time.time())
            RUNPOD_JOBS[batch_id]=row
        for original,path in saved_files:
            try:
                jid=_enqueue_gpu_source(original,Path(path),series,base_url,map_target)
                if jid: jobs.append(jid)
            except Exception as e:
                log(f'gpu batch {batch_id} enqueue failed {original}: {e}')
                try:Path(path).unlink()
                except:pass
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            if jobs:
                row.update(status='done',stage='expanded',jobs=jobs,count=len(jobs),updated_at=time.time(),finished_at=time.time())
            else:
                row.update(status='failed',stage='enqueue_failed',jobs=[],count=0,error='تعذر تجهيز الملفات للمعالجة',updated_at=time.time(),finished_at=time.time())
            RUNPOD_JOBS[batch_id]=row
    except Exception as e:
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            row.update(status='failed',stage='enqueue_failed',error=str(e)[-1200:],updated_at=time.time(),finished_at=time.time())
            RUNPOD_JOBS[batch_id]=row
        for _,path in saved_files:
            try:Path(path).unlink()
            except:pass

@app.route('/gpu-clean',methods=['GET','POST'])
def gpu_clean():
    if request.method=='GET':
        return render_template_string(GPU_CLEAN_HTML)
    if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
        return jsonify(ok=False,error='أضف RUNPOD_API_KEY و RUNPOD_ENDPOINT_ID في Railway أولًا'),503
    series=(request.form.get('series') or '').strip()
    map_target=_map_target(request.form.get('map_target'))
    if not series:return jsonify(ok=False,error='اكتب اسم مجلد المسلسل'),400
    uploaded=[x for x in request.files.getlist('file') if x and x.filename]
    if not uploaded:return jsonify(ok=False,error='اختر ملفًا واحدًا على الأقل'),400

    base_url=public_base_url()

    if len(uploaded)==1 and Path(uploaded[0].filename or '').suffix.lower()=='.zip':
        incoming=uploaded[0]
        batch_id=uuid.uuid4().hex
        zpath=QDIR/f'gpu_zip_{batch_id}.zip'
        incoming.save(zpath)
        with RUNPOD_JOBS_LOCK:
            RUNPOD_JOBS[batch_id]={
                'job_id':batch_id,'filename':incoming.filename,'series':series,'map_target':map_target,
                'status':'queued','stage':'expanding_zip','jobs':[],'count':0,
                'created_at':time.time(),'updated_at':time.time()
            }
        threading.Thread(
            target=_process_gpu_zip,
            args=(zpath,series,base_url,batch_id,map_target),
            daemon=True,
            name=f'gpu-zip-{batch_id[:8]}'
        ).start()
        return jsonify(ok=True,status='queued',batch_id=batch_id,jobs=[],count=0,zip_background=True),202

    saved=[]
    for incoming in uploaded:
        suffix=Path(incoming.filename or '').suffix.lower()
        if suffix not in RUNPOD_AUDIO_EXTS:
            continue
        dst=QDIR/f'gpu_src_{uuid.uuid4().hex}{suffix or ".source"}'
        incoming.save(dst)
        saved.append((incoming.filename,str(dst)))

    if not saved:
        return jsonify(ok=False,error='لم أجد ملفات صوت أو فيديو مدعومة'),400

    # Return to the browser immediately after the client upload finishes.
    # The second hop (Railway -> Backblaze) and RunPod enqueue now happen
    # asynchronously so the page never waits on storage/network processing.
    batch_id=uuid.uuid4().hex
    with RUNPOD_JOBS_LOCK:
        RUNPOD_JOBS[batch_id]={
            'job_id':batch_id,'filename':'batch','series':series,'map_target':map_target,
            'status':'queued','stage':'staging','jobs':[],'count':0,
            'created_at':time.time(),'updated_at':time.time()
        }
    threading.Thread(
        target=_process_gpu_files_background,
        args=(saved,series,base_url,batch_id,map_target),
        daemon=True,name=f'gpu-files-{batch_id[:8]}'
    ).start()
    return jsonify(ok=True,status='queued',batch_id=batch_id,jobs=[],count=len(saved),background_staging=True),202


def _assemble_gpu_chunks(chunk_dir,zpath,total,series,filename,base_url,batch_id,map_target='youtube'):
    chunk_dir=Path(chunk_dir); zpath=Path(zpath)
    try:
        missing=[i for i in range(total) if not (chunk_dir/f'{i:06d}.part').exists()]
        if missing:
            with RUNPOD_JOBS_LOCK:
                row=RUNPOD_JOBS.get(batch_id,{})
                row.update(status='failed',stage='zip_error',error=f'أجزاء ناقصة: {missing[:5]}',updated_at=time.time(),finished_at=time.time())
                RUNPOD_JOBS[batch_id]=row
            return
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            row.update(status='processing',stage='assembling',updated_at=time.time())
            RUNPOD_JOBS[batch_id]=row
        with open(zpath,'wb') as out:
            for i in range(total):
                part_path=chunk_dir/f'{i:06d}.part'
                with open(part_path,'rb') as src:
                    shutil.copyfileobj(src,out,1024*1024)
                try:part_path.unlink()
                except:pass
        try:chunk_dir.rmdir()
        except:pass
        _process_gpu_zip(zpath,series,base_url,batch_id,map_target)
    except Exception as e:
        with RUNPOD_JOBS_LOCK:
            row=RUNPOD_JOBS.get(batch_id,{})
            row.update(status='failed',stage='zip_error',error=str(e)[-1200:],updated_at=time.time(),finished_at=time.time())
            RUNPOD_JOBS[batch_id]=row
        try:zpath.unlink()
        except:pass
    finally:
        shutil.rmtree(chunk_dir,ignore_errors=True)

@app.post('/gpu-clean-chunk')
def gpu_clean_chunk():
    if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
        return jsonify(ok=False,error='إعداد RunPod غير مكتمل'),503
    upload_id=(request.form.get('upload_id') or '').strip()
    series=(request.form.get('series') or '').strip()
    map_target=_map_target(request.form.get('map_target'))
    filename=Path(request.form.get('filename') or 'upload.zip').name
    try:
        index=int(request.form.get('index','-1'))
        total=int(request.form.get('total','0'))
    except Exception:
        return jsonify(ok=False,error='بيانات الجزء غير صحيحة'),400
    if not upload_id or not re.fullmatch(r'[a-f0-9]{32}',upload_id):
        return jsonify(ok=False,error='معرّف الرفع غير صحيح'),400
    if not series or index<0 or total<1 or index>=total:
        return jsonify(ok=False,error='بيانات الرفع ناقصة'),400
    part=request.files.get('chunk')
    if not part:
        return jsonify(ok=False,error='الجزء غير موجود'),400

    chunk_dir=QDIR/f'gpu_chunks_{upload_id}'
    chunk_dir.mkdir(parents=True,exist_ok=True)
    part_path=chunk_dir/f'{index:06d}.part'
    part.save(part_path)

    if index != total-1:
        return jsonify(ok=True,status='chunk_saved',index=index,total=total),200

    # Use the upload id as the batch id so a retried final chunk is idempotent.
    batch_id=upload_id
    with RUNPOD_JOBS_LOCK:
        existing=RUNPOD_JOBS.get(batch_id)
        if existing and existing.get('stage') in ('assembling','expanding_zip','expanded','done'):
            return jsonify(ok=True,status='queued',batch_id=batch_id,zip_background=True),202
        RUNPOD_JOBS[batch_id]={
            'job_id':batch_id,'filename':filename,'series':series,'map_target':map_target,
            'status':'queued','stage':'assembling','jobs':[],'count':0,
            'created_at':time.time(),'updated_at':time.time()
        }
    zpath=QDIR/f'gpu_zip_{upload_id}.zip'
    base_url=public_base_url()
    threading.Thread(
        target=_assemble_gpu_chunks,
        args=(chunk_dir,zpath,total,series,filename,base_url,batch_id,map_target),
        daemon=True,name=f'gpu-assemble-{batch_id[:8]}'
    ).start()
    return jsonify(ok=True,status='queued',batch_id=batch_id,zip_background=True),202

@app.post('/gpu-result/<job_id>')
def gpu_result(job_id):
    token=(request.args.get('t') or '').strip()
    with RUNPOD_JOBS_LOCK:row=dict(RUNPOD_JOBS.get(job_id,{}) or {})
    if not row:
        row=load_persisted_runpod_job(job_id)
        if row:
            with RUNPOD_JOBS_LOCK:RUNPOD_JOBS[job_id]=row
    if not row or not token or not hmac.compare_digest(token,gpu_job_token(job_id)):
        return jsonify(ok=False,error='not_found'),404
    if row.get('status')=='done':return jsonify(ok=True,already_done=True,url=row.get('url','')),200
    f=request.files.get('file')
    if not f:return jsonify(ok=False,error='file_required'),400
    clean_profile=(request.form.get('clean_profile') or '').strip()[:80] or 'legacy-vocals-only'
    result_path=O/f'gpu_result_{job_id}_{row.get("id","audio")}.m4a'
    try:
        f.save(result_path)
        if result_path.stat().st_size<10000:return jsonify(ok=False,error='result_too_small'),400
        set_runpod_job(job_id,status='publishing',stage='storage',clean_profile=clean_profile)
        status,url=upload_series(result_path,row['id'],row['series'],row.get('map_target','youtube'))
        set_runpod_job(job_id,status='done',stage='done',result=status,url=url,error='',finished_at=time.time())
        cleanup_gpu_source(row);log(f'gpu-clean {job_id} {row["id"]}: callback published {url}')
        return jsonify(ok=True,size=result_path.stat().st_size,url=url),200
    except Exception as e:
        err=str(e)[-2500:];set_runpod_job(job_id,status='failed',stage='storage_failed',error=err,finished_at=time.time())
        return jsonify(ok=False,error=err),500
    finally:
        try:result_path.unlink()
        except:pass

@app.get('/gpu-source/<job_id>')
def gpu_source(job_id):
    token=(request.args.get('t') or '').strip()
    with RUNPOD_JOBS_LOCK:
        row=dict(RUNPOD_JOBS.get(job_id,{}) or {})
    if not row or not token or token!=row.get('token'):
        return 'not found',404
    path=Path(row.get('source',''))
    if not path.exists():return 'not found',404
    return send_file(path,as_attachment=False,download_name=row.get('filename') or path.name)

@app.get('/gpu-clean-status/<job_id>')
def gpu_clean_status(job_id):
    recovered=False
    with RUNPOD_JOBS_LOCK:
        row=dict(RUNPOD_JOBS.get(job_id,{}) or {})

    if not row:
        row=load_persisted_runpod_job(job_id)
        if row:
            recovered=True
            with RUNPOD_JOBS_LOCK:
                RUNPOD_JOBS[job_id]=row
            if row.get('status') in ('queued','processing','publishing'):
                set_runpod_job(job_id,status='queued',stage='recovered',error='')
                ensure_runpod_worker()
                RUNPOD_QUEUE.put(job_id)
                with RUNPOD_JOBS_LOCK:
                    row=dict(RUNPOD_JOBS.get(job_id,{}) or {})

    if not row:
        return jsonify(ok=False,error='job_not_found',retryable=True),404

    # Final storage/map wins over stale job state after a deploy or restart.
    if row.get('status') not in ('done','failed'):
        vid=row.get('id')
        series=row.get('series')
        map_target=row.get('map_target','youtube')
        if vid and series:
            try:
                _,final_url=series_existing(vid,series,map_target)
                if final_url:
                    set_runpod_job(
                        job_id,
                        status='done',
                        stage='done',
                        result='published',
                        url=final_url,
                        error='',
                        finished_at=time.time()
                    )
                    with RUNPOD_JOBS_LOCK:
                        row=dict(RUNPOD_JOBS.get(job_id,{}) or {})
                    cleanup_gpu_source(row)
                    log(f'gpu-clean {job_id} {vid}: reconciled done from final storage')
            except Exception as e:
                log(f'gpu status reconcile warning {job_id}: {e}')

    public=dict(row)
    public['recovered_after_restart']=recovered or public.get('stage')=='recovered'
    for k in ('source','source_url','source_b2_name','callback_url','token','remote_job_id'):
        public.pop(k,None)
    return jsonify(ok=True,**public)


@app.get('/compare-clean/<vid>')
def compare_clean(vid):
    try:
        mapping=load_audio_map()
        url=mapping.get(vid,'')
        if not url:return 'not found',404
        r=requests.get(url,timeout=120)
        if r.status_code!=200:return f'upstream {r.status_code}',502
        return Response(r.content,mimetype=r.headers.get('content-type','audio/mp4'))
    except Exception as e:
        return str(e),500

@app.get('/archive-debug/<identifier>')
def archive_debug(identifier):
    ident=str(identifier or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,200}',ident):
        return jsonify(ok=False,error='invalid_identifier'),400
    try:
        r=requests.get('https://archive.org/metadata/'+quote(ident,safe=''),timeout=30,headers={'User-Agent':'Nibras/1.0'})
        if r.status_code!=200:
            return jsonify(ok=False,error='archive_http_'+str(r.status_code)),502
        root=r.json() or {}
        files=[]
        for item in root.get('files') or []:
            if not isinstance(item,dict):
                continue
            name=str(item.get('name') or '').strip()
            if not name.lower().endswith(('.mp4','.m4v','.webm','.mkv','.ogv')):
                continue
            files.append({'name':name,'title':str(item.get('title') or '').strip(),'source':str(item.get('source') or '').strip()})
        return jsonify(ok=True,identifier=ident,files=files)
    except Exception as e:
        return jsonify(ok=False,error=str(e)),502

GITHUB_OIDC_JWKS=PyJWKClient('https://token.actions.githubusercontent.com/.well-known/jwks')

def _verify_github_release_oidc():
    auth=str(request.headers.get('Authorization') or '').strip()
    if not auth.lower().startswith('bearer '):
        raise RuntimeError('missing_oidc_token')
    token=auth.split(None,1)[1].strip()
    key=GITHUB_OIDC_JWKS.get_signing_key_from_jwt(token).key
    claims=jwt.decode(
        token,key,algorithms=['RS256'],
        audience='nibras-public-publisher',
        issuer='https://token.actions.githubusercontent.com'
    )
    if claims.get('repository')!='sloom059-svg/TV_KIDS':
        raise RuntimeError('wrong_repository')
    if claims.get('ref')!='refs/heads/islamic-nibras':
        raise RuntimeError('wrong_ref')
    workflow_ref=str(claims.get('workflow_ref') or '')
    allowed_workflows=(
        '/.github/workflows/nibras-release.yml@',
        '/.github/workflows/nibras-public-mirror.yml@'
    )
    if not any(x in workflow_ref for x in allowed_workflows):
        raise RuntimeError('wrong_workflow')
    if str(claims.get('event_name') or '') not in ('push','workflow_dispatch'):
        raise RuntimeError('wrong_event')
    return claims

@app.post('/publish-apk')
def publish_apk():
    try:
        claims=_verify_github_release_oidc()
    except Exception as e:
        log(f'Public APK OIDC rejected: {e}')
        return jsonify(ok=False,error='unauthorized'),401
    if not TOKEN:
        return jsonify(ok=False,error='github_token_missing'),503
    version_name=str(request.form.get('version_name') or '').strip()
    version_code=str(request.form.get('version_code') or '').strip()
    expected_sha=str(request.form.get('sha256') or '').strip().lower()
    notes=str(request.form.get('notes') or 'إصدار نبراس الرسمي.').strip()[:1000]
    required=str(request.form.get('required') or '').strip().lower() in ('1','true','yes','on')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}',version_name):
        return jsonify(ok=False,error='invalid_version_name'),400
    if not re.fullmatch(r'[0-9]{1,12}',version_code):
        return jsonify(ok=False,error='invalid_version_code'),400
    upload=request.files.get('apk')
    if upload is None:
        return jsonify(ok=False,error='apk_required'),400
    filename=f'Nibras-{version_name}.apk'
    tmp=Path(tempfile.mkdtemp(prefix='nibras-apk-'))/filename
    try:
        upload.save(tmp)
        actual=hashlib.sha256(tmp.read_bytes()).hexdigest()
        if expected_sha and not hmac.compare_digest(actual,expected_sha):
            return jsonify(ok=False,error='sha256_mismatch',actual_sha256=actual),400
        public_path=f'app-releases/{filename}'
        public_url=b2_upload_public_file(tmp,public_path,'application/vnd.android.package-archive',overwrite=True)
        metadata={
            'enabled':True,
            'required':required,
            'package':'com.nibras.kids',
            'version_code':int(version_code),
            'version_name':version_name,
            'min_sdk':17,
            'apk_url':public_url,
            'sha256':actual,
            'notes':notes
        }
        meta_path=tmp.parent/'nibras-update.json'
        meta_path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        metadata_url=b2_upload_public_file(meta_path,'app-releases/nibras-update.json','application/json',overwrite=True)
        log(f'Published public APK {filename} from {claims.get("run_id","github-actions")} -> {public_url}')
        return jsonify(ok=True,apk_url=public_url,metadata_url=metadata_url,sha256=actual,metadata=metadata)
    except Exception as e:
        log(f'Public APK publish failed: {e}')
        return jsonify(ok=False,error=str(e)),502
    finally:
        shutil.rmtree(tmp.parent,ignore_errors=True)

@app.get('/audio-status')
def audio_status():
    supplied=''
    value=(request.args.get('playlist') or '').strip()
    series=(request.args.get('series') or '').strip()
    body=''
    if value:
        try:
            entries=playlist_entries(value)
            mapping=load_audio_map()
            linked=[]
            for x in entries:
                url=mapping.get(x['id'],'')
                ok=bool(url)
                if series:
                    folder='/' + series_slug(series) + '/'
                    ok=ok and folder in url
                linked.append((x,ok,url))
            count=sum(1 for _,ok,_ in linked if ok)
            rows=''.join(
                '<tr><td>'+html.escape(x['title'] or 'بدون عنوان')+'</td><td class="mono">'+html.escape(x['id'])+'</td><td class="'+('yes' if ok else 'no')+'">'+('✅ مربوط' if ok else '❌ غير مربوط')+'</td></tr>'
                for x,ok,_ in linked
            )
            body=f'<div class="sum"><b>{count}</b> من <b>{len(linked)}</b> مقطع مربوط بالصوت النظيف.</div><div style="overflow:auto"><table><thead><tr><th>المقطع</th><th>Video ID</th><th>الحالة</th></tr></thead><tbody>{rows}</tbody></table></div>'
        except Exception as e:
            body='<div class="sum no">تعذر فحص القائمة: '+html.escape(str(e))+'</div>'
    return render_template_string(STATUS_HTML,key=supplied,playlist=html.escape(value),series=html.escape(series),body=body)

@app.get('/')
def home():return render_template_string(HTML)

@app.get('/health')
def health():
    ensure_worker()
    if not getattr(app,'_gpu_recovery_started',False):
        app._gpu_recovery_started=True
        threading.Thread(target=recover_pending_runpod_jobs,daemon=True,name='gpu-recovery').start()
    with JOBS_LOCK:
        active=sum(1 for x in JOBS.values() if x.get('status') in ('queued','processing'))
    return {'ok':True,'service':'nibras-audio','queue':True,'active_jobs':active,'waiting':JOB_QUEUE.qsize(),'folder':FOLDER,'map':MAP_PATH}

@app.post('/process-upload')
def process_upload():
    return enqueue_request()

@app.post('/enqueue-upload')
def enqueue_upload():
    return enqueue_request()

if B2_KEY_ID and B2_APPLICATION_KEY:
    threading.Thread(target=recover_pending_runpod_jobs,daemon=True,name='gpu-recovery-boot').start()

@app.get('/job-status/<job_id>')
def job_status(job_id):
    ensure_worker()
    with JOBS_LOCK:
        row=dict(JOBS.get(job_id,{}) or {})
    if not row:
        return jsonify(ok=False,error='job_not_found'),404
    row.pop('source',None)
    return jsonify(ok=True,**row)

@app.get('/video-status/<vid>')
def video_status(vid):
    vid=re.sub(r'[^A-Za-z0-9_-]','',vid)
    with JOBS_LOCK:
        candidates=[dict(x) for x in JOBS.values() if x.get('id')==vid]
    if candidates:
        row=sorted(candidates,key=lambda x:x.get('created_at',0))[-1]
        row.pop('source',None)
        return jsonify(ok=True,**row)
    try:
        path,_,old=gh_info(vid)
        if old:
            return jsonify(ok=True,id=vid,status='published',url=raw_url(path))
    except Exception as e:
        return jsonify(ok=False,error=str(e)),500
    return jsonify(ok=True,id=vid,status='unknown')


def _temporary_archive_probe():
    try:
        ident='01_20260301_20260301_1946'
        r=requests.get('https://archive.org/metadata/'+quote(ident,safe=''),timeout=30,headers={'User-Agent':'Nibras/1.0'})
        log(f'ARCHIVE_PROBE http={r.status_code}')
        if r.status_code==200:
            root=r.json() or {}
            videos=[]
            import unicodedata
            for item in root.get('files') or []:
                if not isinstance(item,dict): continue
                name=str(item.get('name') or '')
                low=name.lower()
                if not low.endswith(('.mp4','.m4v','.webm','.mkv','.ogv')): continue
                stem=Path(name).stem
                stem=re.sub(r'\s*\(\d+\)$','',stem)
                stem=re.sub(r'(?i)_vocals_split_by_lalalai$','',stem)
                stem=re.sub(r'(?i)_vocals$','',stem)
                stem=unicodedata.normalize('NFC',stem.strip())
                key='ia_'+hashlib.sha1(stem.encode('utf-8')).hexdigest()[:20]
                videos.append((name,key))
            for name,key in videos[:40]:
                log('ARCHIVE_PROBE_FILE '+key+' | '+name)
    except Exception as e:
        log('ARCHIVE_PROBE_ERROR '+repr(e))

threading.Thread(target=_temporary_archive_probe,daemon=True,name='archive-probe').start()
def _probe_runpod_endpoint_config():
    if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
        return
    try:
        hdr={'Authorization':f'Bearer {RUNPOD_API_KEY}','Content-Type':'application/json'}
        r=requests.get(f'https://rest.runpod.io/v1/endpoints/{RUNPOD_ENDPOINT_ID}',headers=hdr,timeout=30)
        log(f'RUNPOD_CONFIG http={r.status_code}')
        if r.status_code!=200:
            log('RUNPOD_CONFIG_ERROR '+(r.text or '')[:500])
            try:
                lr=requests.get('https://rest.runpod.io/v1/endpoints',headers=hdr,timeout=30)
                if lr.status_code==200:
                    rows=lr.json()
                    if isinstance(rows,dict):
                        rows=rows.get('data') or rows.get('endpoints') or []
                    safe_list=[]
                    for ep in (rows or []):
                        if isinstance(ep,dict):
                            safe_list.append({k:ep.get(k) for k in ('id','name','templateId','version') if k in ep})
                    log('RUNPOD_ENDPOINT_LIST_SAFE '+json.dumps(safe_list,ensure_ascii=False))
                else:
                    log(f'RUNPOD_ENDPOINT_LIST_ERROR http={lr.status_code} '+(lr.text or '')[:500])
            except Exception as le:
                log('RUNPOD_ENDPOINT_LIST_EXCEPTION '+repr(le))
            return
        obj=r.json() or {}
        safe={k:obj.get(k) for k in ('id','name','templateId','version','gpuIds','workersMin','workersMax','scalerType','scalerValue','idleTimeout') if k in obj}
        tpl=obj.get('template')
        if isinstance(tpl,dict):
            safe['template']={k:tpl.get(k) for k in ('id','name','imageName','containerDiskInGb','dockerStartCmd','dockerEntrypoint') if k in tpl}
        template_id=obj.get('templateId')
        if template_id:
            tr=requests.get(
                f'https://rest.runpod.io/v1/templates/{template_id}?includeEndpointBoundTemplates=true',
                headers=hdr,timeout=30
            )
            safe['template_http']=tr.status_code
            if tr.status_code==200:
                tobj=tr.json() or {}
                safe['template_detail']={k:tobj.get(k) for k in (
                    'id','name','imageName','containerDiskInGb','dockerStartCmd','dockerEntrypoint',
                    'isServerless','volumeInGb','volumeMountPath'
                ) if k in tobj}
            else:
                safe['template_error']=(tr.text or '')[:300]
        log('RUNPOD_CONFIG_SAFE '+json.dumps(safe,ensure_ascii=False))
    except Exception as e:
        log('RUNPOD_CONFIG_EXCEPTION '+repr(e))

threading.Thread(target=_probe_runpod_endpoint_config,daemon=True,name='runpod-config-probe').start()
threading.Thread(target=_cleanup_legacy_tom_jerry_tests_once,daemon=True,name='tom-jerry-test-cleanup').start()

def _trigger_runpod_github_release():
    # Runpod's GitHub integration rebuilds an endpoint when the connected repo
    # publishes a GitHub Release. Keep this idempotent so Railway restarts do
    # not create duplicate releases.
    tag='runpod-vocals-only-min-music-v1'
    if not TOKEN or not REPO or not RUNPOD_ENDPOINT_ID:
        return
    try:
        gh_headers=headers()
        check=requests.get(
            f'https://api.github.com/repos/{REPO}/releases/tags/{tag}',
            headers=gh_headers,timeout=30
        )
        if check.status_code==200:
            log(f'RUNPOD_RELEASE already_exists tag={tag}')
        elif check.status_code==404:
            payload={
                'tag_name':tag,
                'target_commitish':BRANCH,
                'name':'RunPod vocals only min music v1',
                'body':'Deploy vocals-only-min-music-v1 audio cleaning worker.',
                'draft':False,
                'prerelease':False
            }
            rr=requests.post(
                f'https://api.github.com/repos/{REPO}/releases',
                headers=gh_headers,json=payload,timeout=45
            )
            log(f'RUNPOD_RELEASE create_http={rr.status_code}')
            if rr.status_code not in (200,201):
                log('RUNPOD_RELEASE_ERROR '+(rr.text or '')[:700])
                return
        else:
            log(f'RUNPOD_RELEASE check_http={check.status_code} '+(check.text or '')[:500])
            return

        # Watch the bound template until Runpod swaps away from the old image.
        hdr={'Authorization':f'Bearer {RUNPOD_API_KEY}','Content-Type':'application/json'}
        old_image='registry.runpod.net/sloom059-svg-nibras-audio-runpod-v2-dockerfile-runpod:09414af13'
        for _ in range(90):
            try:
                er=requests.get(
                    f'https://rest.runpod.io/v1/endpoints/{RUNPOD_ENDPOINT_ID}',
                    headers=hdr,timeout=30
                )
                if er.status_code==200:
                    eo=er.json() or {}
                    tid=eo.get('templateId')
                    if tid:
                        tr=requests.get(
                            f'https://rest.runpod.io/v1/templates/{tid}?includeEndpointBoundTemplates=true',
                            headers=hdr,timeout=30
                        )
                        if tr.status_code==200:
                            image_name=(tr.json() or {}).get('imageName') or ''
                            if image_name and image_name!=old_image:
                                log('RUNPOD_RELEASE_ACTIVE image='+image_name)
                                return
            except Exception as pe:
                log('RUNPOD_RELEASE_POLL_WARNING '+repr(pe))
            time.sleep(20)
        log('RUNPOD_RELEASE_WAIT_TIMEOUT')
    except Exception as e:
        log('RUNPOD_RELEASE_EXCEPTION '+repr(e))

threading.Thread(target=_trigger_runpod_github_release,daemon=True,name='runpod-release-trigger').start()



if __name__=='__main__':
    ensure_worker()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
