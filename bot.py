import os, re, base64, shutil, subprocess, threading, json, queue, uuid, time, html, zipfile
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
PUBLISH_KEY=os.getenv('PUBLISH_KEY','').strip()
MAP_PATH=os.getenv('AUDIO_MAP_PATH','audio-map.json').strip().strip('/') or 'audio-map.json'

app=Flask(__name__)

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
input,button{width:100%;padding:13px;border-radius:10px;border:1px solid #444;box-sizing:border-box;font-size:16px}
input{background:#111;color:#fff}button{margin-top:18px;background:#f6c35f;color:#162436;font-weight:900;cursor:pointer}
small{color:#aaa}.ok{background:#163b2b;padding:12px;border-radius:10px;margin-top:16px}.err{background:#4b1d23;padding:12px;border-radius:10px;margin-top:16px}
</style>
<div class=c><h2>نبراس | نشر الصوت الجاهز</h2>
<p>ارفع ملف LALAL الجاهز وسيُربط تلقائيًا بمعرّف فيديو YouTube داخل التطبيق.</p>
<form method=post enctype=multipart/form-data>
<input type=hidden name=k value="{{key}}">
<label>معرّف الفيديو</label><input name=id value="{{vid}}" placeholder="مثال: aJ3zGhhMuxE">
<small>إذا تركته فارغًا سأحاول أخذه من بداية اسم الملف.</small>
<label>مجلد المسلسل</label><input name=series value="{{series}}" placeholder="مثال: barbear" required>
<small>كل مسلسل يكون داخل مجلد مستقل للتنظيم، مثل audio/barbear/.</small>
<label>ملفات الصوت الجاهزة</label><input type=file name=file accept=".zip,.m4a,.aac,.mp3,.wav,.flac,.ogg,.opus,audio/mp4,audio/x-m4a,audio/aac,audio/mpeg,audio/wav,audio/x-wav,audio/flac,audio/ogg,audio/opus,application/zip" multiple required>
<small>يدعم ZIP و M4A و AAC و MP3 و WAV و FLAC و OGG و OPUS. إذا رفعت ZIP سأفكه تلقائيًا وألتقط كل ملفات الصوت داخله، وإذا أسماء الملفات تبدأ بـ Video ID مثل aJ3zGhhMuxE_... راح أربطها تلقائيًا.</small>
<button type=submit>رفع وربط الآن</button>
</form>
<p style="margin-top:18px"><a style="color:#ffd982" href="/audio-status?k={{key}}">عرض حالة المقاطع المربوطة</a></p>
{{message|safe}}
</div></html>'''

def _derive_video_id(value, filename):
    raw=re.sub(r'[^A-Za-z0-9_-]','',str(value or '').strip())
    if raw:return raw
    name=Path(filename or '').stem
    m=re.match(r'^([A-Za-z0-9_-]{11})(?:_|$)',name)
    return m.group(1) if m else ''

def series_slug(value):
    value=re.sub(r'[^A-Za-z0-9_-]','-',str(value or '').strip()).strip('-_').lower()
    return value or 'general'

def upload_series(file,vid,series):
    folder=series_slug(series)
    path=f'{FOLDER}/{folder}/{vid}.m4a' if FOLDER else f'{folder}/{vid}.m4a'
    api,old=gh_get(path)
    if old:
        return 'skipped', update_audio_map(vid,path)
    data=base64.b64encode(Path(file).read_bytes()).decode()
    r=requests.put(api,headers=headers(),json={'message':f'Add cleaned audio {folder}/{vid}','content':data,'branch':BRANCH},timeout=240)
    if r.status_code not in (200,201):
        raise RuntimeError(f'GitHub upload {r.status_code}: {r.text[:1000]}')
    return 'uploaded', update_audio_map(vid,path)

AUDIO_EXTS={'.m4a','.aac','.mp3','.wav','.flac','.ogg','.opus'}

def publish_clean_path(source_path, original_name, vid, series):
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
        return upload_series(final,vid,series)
    finally:
        try:work.unlink()
        except:pass
        try:final.unlink()
        except:pass

def publish_zip_file(incoming, series):
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
                vid=_derive_video_id('',original)
                if not vid:
                    errors.append(f'{info.filename}: تعذر معرفة Video ID من اسم الملف')
                    continue
                extracted=QDIR/f'zipitem_{uuid.uuid4().hex}{Path(original).suffix.lower()}'
                try:
                    with z.open(info,'r') as src, open(extracted,'wb') as dst:
                        shutil.copyfileobj(src,dst)
                    status,url=publish_clean_path(extracted,original,vid,series)
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

def publish_clean_file(incoming, vid, series):
    suffix=Path(incoming.filename or '').suffix.lower() or '.source'
    source=QDIR/f'incoming_{uuid.uuid4().hex}{suffix}'
    incoming.save(source)
    try:
        return publish_clean_path(source,incoming.filename,vid,series)
    finally:
        try:source.unlink()
        except:pass

@app.route('/publish-clean',methods=['GET','POST'])
def publish_clean():
    supplied=(request.values.get('k') or '').strip()
    if PUBLISH_KEY and supplied!=PUBLISH_KEY:
        return 'Unauthorized',401
    vid=(request.values.get('id') or '').strip()
    series=(request.values.get('series') or '').strip()
    message=''
    if request.method=='POST':
        files=[x for x in request.files.getlist('file') if x and x.filename]
        if not files:
            message='<div class="err">اختر ملف صوت واحد على الأقل.</div>'
        else:
            results=[]
            errors=[]
            for incoming in files:
                if Path(incoming.filename or '').suffix.lower()=='.zip':
                    try:
                        zip_results,zip_errors=publish_zip_file(incoming,series)
                        results.extend(zip_results)
                        errors.extend(zip_errors)
                    except Exception as e:
                        errors.append(f'{incoming.filename}: {str(e)}')
                    continue

                this_vid=_derive_video_id(vid if len(files)==1 else '',incoming.filename)
                if not this_vid:
                    errors.append(f'{incoming.filename}: تعذر معرفة Video ID من اسم الملف')
                    continue
                try:
                    status,url=publish_clean_file(incoming,this_vid,series)
                    results.append((incoming.filename,this_vid,status,url))
                except Exception as e:
                    errors.append(f'{incoming.filename}: {str(e)}')
            parts=[]
            if results:
                rows=''.join(
                    f'<div style="margin:7px 0"><b>{html.escape(v)}</b> — {html.escape(s)}<br><a style="color:#ffd982" href="{html.escape(u)}">{html.escape(fn)}</a></div>'
                    for fn,v,s,u in results
                )
                parts.append(f'<div class="ok">تم نشر {len(results)} ملف ✅{rows}</div>')
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
<p><a href="/publish-clean?k={{key}}">رجوع لرفع الملفات</a></p></div></html>'''

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

@app.get('/audio-status')
def audio_status():
    supplied=(request.args.get('k') or '').strip()
    if PUBLISH_KEY and supplied!=PUBLISH_KEY:return 'Unauthorized',401
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
