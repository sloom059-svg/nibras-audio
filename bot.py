import os, re, base64, shutil, subprocess, threading, json
from pathlib import Path
from urllib.parse import quote
import requests
from flask import Flask, request, render_template_string, jsonify

WORK=Path('/tmp/nibras'); D=WORK/'downloads'; S=WORK/'separated'; O=WORK/'output'
for p in (D,S,O): p.mkdir(parents=True,exist_ok=True)
TOKEN=os.getenv('GITHUB_TOKEN','').strip()
REPO=os.getenv('GITHUB_REPO','').strip()
BRANCH=os.getenv('GITHUB_BRANCH','main').strip()
FOLDER=os.getenv('GITHUB_FOLDER','processed-audio').strip().strip('/')
UPLOAD_KEY=os.getenv('UPLOAD_KEY','').strip()
MAP_PATH=os.getenv('AUDIO_MAP_PATH','audio-map.json').strip().strip('/') or 'audio-map.json'
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=512*1024*1024
JOB_LOCK=threading.Lock()
HTML='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>نبراس</title>
<style>body{font-family:Arial;background:#15171b;color:white;max-width:800px;margin:40px auto;padding:20px}.c{background:#22262c;padding:25px;border-radius:18px}pre{background:#111;padding:15px;border-radius:10px;white-space:pre-wrap}</style>
<div class=c><h2>نبراس | معالج الصوت</h2><p>الخدمة تستقبل الصوت من برنامج الكمبيوتر، تزيل الموسيقى، ثم ترفع الناتج وتحدّث audio-map.json.</p><pre>جاهز</pre></div></html>'''

def run(c):
    p=subprocess.run(c,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if p.returncode: raise RuntimeError(p.stdout[-7000:])
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
    if not TOKEN or '/' not in REPO:
        raise RuntimeError('GitHub settings are missing')
    api,old=gh_get(MAP_PATH)
    mapping={}
    sha=None
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
        update_audio_map(vid,path)
        return 'skipped', raw_url(path)
    data=base64.b64encode(Path(file).read_bytes()).decode()
    r=requests.put(api,headers=headers(),json={'message':f'Add cleaned audio {vid}','content':data,'branch':BRANCH},timeout=240)
    if r.status_code not in (200,201):raise RuntimeError(f'GitHub upload {r.status_code}: {r.text[:1000]}')
    url=update_audio_map(vid,path)
    return 'uploaded',url

def cleanup_vid(vid):
    for p in (D/f'{vid}.wav', O/f'{vid}.m4a'):
        try:p.unlink()
        except:pass
    for p in S.glob(f'**/{vid}'):
        if p.is_dir():shutil.rmtree(p,ignore_errors=True)

def one_uploaded(vid, file_path):
    path,_,old=gh_info(vid)
    if old:
        return 'skipped', update_audio_map(vid,path)
    source=Path(file_path)
    wav=D/f'{vid}.wav'
    final=O/f'{vid}.m4a'
    try:
        run(['ffmpeg','-y','-i',str(source),'-vn','-ar','44100','-ac','2','-c:a','pcm_s16le',str(wav)])
        run(['python','-m','demucs','--two-stems=vocals','-n','mdx_q','--segment','4','-j','1','-o',str(S),str(wav)])
        cand=list(S.glob(f'**/{vid}/vocals.wav'))
        if not cand:raise RuntimeError('Demucs vocals output missing')
        run(['ffmpeg','-y','-i',str(cand[0]),'-vn','-c:a','aac','-b:a','128k',str(final)])
        if not final.exists() or final.stat().st_size < 10000:
            raise RuntimeError('Final clean audio was not created correctly')
        return upload(final,vid)
    finally:
        cleanup_vid(vid)

@app.get('/')
def home():return render_template_string(HTML)

@app.get('/health')
def health():
    return {'ok':True,'service':'nibras-audio','folder':FOLDER,'map':MAP_PATH,'busy':JOB_LOCK.locked()}

@app.post('/process-upload')
def proc_upload():
    if UPLOAD_KEY and request.headers.get('X-Upload-Key','') != UPLOAD_KEY:
        return jsonify(ok=False,error='مفتاح الرفع غير صحيح'),401
    if not TOKEN or '/' not in REPO:
        return jsonify(ok=False,error='GitHub settings are missing on Railway'),500
    if not JOB_LOCK.acquire(blocking=False):
        return jsonify(ok=False,error='busy',message='هناك ملف آخر قيد المعالجة'),429
    source=None
    try:
        vid=re.sub(r'[^A-Za-z0-9_-]','',str(request.form.get('id','')).strip())
        title=str(request.form.get('title','')).strip()[:300]
        playlist=str(request.form.get('playlist','')).strip()[:500]
        incoming=request.files.get('file')
        if not vid or incoming is None or not incoming.filename:
            return jsonify(ok=False,error='أرسل id وملفًا باسم file'),400
        suffix=Path(incoming.filename).suffix.lower() or '.source'
        source=D/f'{vid}{suffix}'
        incoming.save(source)
        status,url=one_uploaded(vid,source)
        return jsonify(ok=True,status=status,id=vid,title=title,playlist=playlist,url=url,map=MAP_PATH,folder=FOLDER)
    except Exception as e:
        return jsonify(ok=False,error=str(e)),500
    finally:
        if source is not None:
            try:source.unlink()
            except:pass
        JOB_LOCK.release()

@app.post('/process-upload-ui')
def proc_upload_ui():
    return proc_upload()

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
