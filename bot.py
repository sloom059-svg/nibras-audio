import os, re, base64, shutil, subprocess, threading, json, queue, uuid, time
from pathlib import Path
from urllib.parse import quote
import requests
from flask import Flask, request, render_template_string, jsonify

WORK=Path('/tmp/nibras'); D=WORK/'downloads'; S=WORK/'separated'; O=WORK/'output'; QDIR=WORK/'queued'
for p in (D,S,O,QDIR): p.mkdir(parents=True,exist_ok=True)

TOKEN=os.getenv('GITHUB_TOKEN','').strip()
REPO=os.getenv('GITHUB_REPO','').strip()
BRANCH=os.getenv('GITHUB_BRANCH','main').strip()
FOLDER=os.getenv('GITHUB_FOLDER','processed-audio').strip().strip('/')
UPLOAD_KEY=os.getenv('UPLOAD_KEY','').strip()
MAP_PATH=os.getenv('AUDIO_MAP_PATH','audio-map.json').strip().strip('/') or 'audio-map.json'

app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=512*1024*1024

JOB_QUEUE=queue.Queue()
JOBS={}
JOBS_LOCK=threading.Lock()
WORKER_STARTED=False
WORKER_START_LOCK=threading.Lock()

HTML='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>نبراس</title>
<style>body{font-family:Arial;background:#15171b;color:white;max-width:800px;margin:40px auto;padding:20px}.c{background:#22262c;padding:25px;border-radius:18px}pre{background:#111;padding:15px;border-radius:10px;white-space:pre-wrap}</style>
<div class=c><h2>نبراس | معالج الصوت</h2><p>Queue مفعّلة: يستقبل الملف فورًا، ثم يعالج الملفات واحدًا واحدًا بالخلفية ويرفع الناتج تلقائيًا.</p><pre>جاهز</pre></div></html>'''

def log(msg):
    print(f'[NIBRAS] {msg}', flush=True)

def run(c):
    p=subprocess.run(c,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if p.returncode:
        raise RuntimeError(p.stdout[-7000:])
    return p.stdout

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

def update_audio_map(vid, path):
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
    url=raw_url(path)
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
        run(['python','-m','demucs','--two-stems=vocals','-n','htdemucs','--segment','4','-j','1','-o',str(S),str(wav)])
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

@app.get('/')
def home():return render_template_string(HTML)

@app.get('/health')
def health():
    ensure_worker()
    with JOBS_LOCK:
        active=sum(1 for x in JOBS.values() if x.get('status') in ('queued','processing'))
    return {'ok':True,'service':'nibras-audio','queue':True,'active_jobs':active,'waiting':JOB_QUEUE.qsize(),'folder':FOLDER,'map':MAP_PATH}

@app.post('/process-upload')
def process_upload():
    return enqueue_request()

@app.post('/enqueue-upload')
def enqueue_upload():
    return enqueue_request()

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

if __name__=='__main__':
    ensure_worker()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
