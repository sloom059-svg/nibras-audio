import os, re, base64, shutil, subprocess, threading
from pathlib import Path
import requests
from flask import Flask, request, render_template_string, jsonify

WORK=Path("/tmp/nibras"); D=WORK/"downloads"; S=WORK/"separated"; O=WORK/"output"
for p in (D,S,O): p.mkdir(parents=True,exist_ok=True)
TOKEN=os.getenv("GITHUB_TOKEN","").strip()
REPO=os.getenv("GITHUB_REPO","").strip()
BRANCH=os.getenv("GITHUB_BRANCH","main").strip()
FOLDER=os.getenv("GITHUB_FOLDER","audio").strip().strip("/")
UPLOAD_KEY=os.getenv("UPLOAD_KEY","").strip()
app=Flask(__name__)
app.config["MAX_CONTENT_LENGTH"]=512*1024*1024
JOB_LOCK=threading.Lock()
HTML="""<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>نبراس</title>
<style>body{font-family:Arial;background:#15171b;color:white;max-width:800px;margin:40px auto;padding:20px}.c{background:#22262c;padding:25px;border-radius:18px}input,button{padding:14px;font-size:17px;border:0;border-radius:10px}input{width:90%;margin:12px 0}button{cursor:pointer}pre{background:#111;padding:15px;border-radius:10px;white-space:pre-wrap}.hint{color:#bbb;font-size:14px}</style>
<div class=c><h2>نبراس | إزالة الموسيقى</h2><p>ضع رابط YouTube للمطابقة، وأرفق الفيديو/الصوت إذا طلب YouTube تسجيل الدخول.</p><input id="u" placeholder="YouTube URL"><br><input id="f" type="file" accept="audio/*,video/*"><p class="hint">الرابط يحدد مكان الملف في الكتالوج، والملف المرفق هو مصدر المعالجة.</p><button onclick="go()">بدء المعالجة</button><pre id="s">جاهز</pre></div>
<script>
function getId(raw){try{const x=new URL(raw);if(x.hostname==="youtu.be")return x.pathname.slice(1).split("/")[0];if(x.searchParams.get("v"))return x.searchParams.get("v");const m=x.pathname.match(/\/(?:shorts|embed|live)\/([^/?]+)/);return m?m[1]:""}catch(e){return""}}
async function go(){
 const url=document.getElementById("u").value.trim(), file=document.getElementById("f").files[0], s=document.getElementById("s"), id=getId(url);
 if(!id){s.textContent="❌ ضع رابط YouTube صحيحًا للمطابقة";return}
 s.textContent=file?"⏳ جاري رفع الملف وفصل الصوت...":"⏳ جاري السحب والمعالجة...";
 try{
  let r;
  if(file){const fd=new FormData();fd.append("id",id);fd.append("source_url",url);fd.append("file",file,file.name);r=await fetch("/process-upload-ui",{method:"POST",body:fd})}
  else{r=await fetch("/process",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})})}
  const d=await r.json();s.textContent=d.ok?"✅ "+d.message:"❌ "+(d.error||"حدث خطأ")
 }catch(e){s.textContent="❌ تعذر الاتصال بالخدمة"}
}
</script></html>"""


def run(c):
    p=subprocess.run(c,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if p.returncode: raise RuntimeError(p.stdout[-5000:])
    return p.stdout

def headers():
    return {"Authorization":f"Bearer {TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def gh_info(vid):
    path=f"{FOLDER}/{vid}.m4a" if FOLDER else f"{vid}.m4a"
    api=f"https://api.github.com/repos/{REPO}/contents/{path}"
    r=requests.get(api,headers=headers(),params={"ref":BRANCH},timeout=30)
    if r.status_code==200:return path,api,r.json()
    if r.status_code==404:return path,api,None
    raise RuntimeError(f"GitHub check {r.status_code}: {r.text[:600]}")

def upload(file,vid):
    path,api,old=gh_info(vid)
    if old:return "skipped"
    data=base64.b64encode(Path(file).read_bytes()).decode()
    r=requests.put(api,headers=headers(),json={"message":f"Add cleaned audio {vid}","content":data,"branch":BRANCH},timeout=180)
    if r.status_code not in (200,201):raise RuntimeError(f"GitHub upload {r.status_code}: {r.text[:800]}")
    return "uploaded"

def ytdlp_base():
    # YouTube now requires yt-dlp-ejs plus an explicit JavaScript runtime.
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        raise RuntimeError("Node.js runtime is unavailable in the container")
    return [
        "yt-dlp",
        "--js-runtimes", f"node:{node}",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=tv_downgraded,android_vr",
        "--retries", "5",
        "--fragment-retries", "5",
        "--sleep-requests", "1",
    ]

def entries(url):
    out=run(ytdlp_base()+["--flat-playlist","--ignore-errors","--print","%(id)s|%(webpage_url)s",url])
    a=[]
    for x in out.splitlines():
        if "|" in x:
            vid,page=x.split("|",1)
            if vid.strip():a.append((vid.strip(),page.strip() or f"https://www.youtube.com/watch?v={vid.strip()}"))
    if not a:
        vid=run(ytdlp_base()+["--no-playlist","--print","%(id)s",url]).strip().splitlines()[0];a=[(vid,url)]
    return a

def one(vid,url):
    _,_,old=gh_info(vid)
    if old:return "skipped"
    run(ytdlp_base()+["--no-playlist","-x","--audio-format","wav","-o",str(D/"%(id)s.%(ext)s"),url])
    wav=D/f"{vid}.wav"
    run(["python","-m","demucs","--two-stems=vocals","-n","mdx_q","--segment","4","-j","1","-o",str(S),str(wav)])
    cand=list(S.glob(f"**/{vid}/vocals.wav"))
    if not cand:raise RuntimeError("Demucs vocals output missing")
    final=O/f"{vid}.m4a"
    run(["ffmpeg","-y","-i",str(cand[0]),"-c:a","aac","-b:a","128k",str(final)])
    status=upload(final,vid)
    try:wav.unlink();final.unlink()
    except:pass
    for p in S.glob(f"**/{vid}"):
        if p.is_dir():shutil.rmtree(p,ignore_errors=True)
    return status

def one_direct(vid, source_url):
    _,_,old=gh_info(vid)
    if old:return "skipped"
    source=D/f"{vid}.m4a"
    wav=D/f"{vid}.wav"
    final=O/f"{vid}.m4a"
    try:
        with requests.get(
            source_url,
            headers={"User-Agent":"Mozilla/5.0"},
            stream=True,
            timeout=(30,180),
        ) as r:
            r.raise_for_status()
            with source.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        run(["ffmpeg","-y","-i",str(source),"-ar","44100","-ac","2","-c:a","pcm_s16le",str(wav)])
        run(["python","-m","demucs","--two-stems=vocals","-n","mdx_q","--segment","4","-j","1","-o",str(S),str(wav)])
        cand=list(S.glob(f"**/{vid}/vocals.wav"))
        if not cand:raise RuntimeError("Demucs vocals output missing")
        run(["ffmpeg","-y","-i",str(cand[0]),"-c:a","aac","-b:a","128k",str(final)])
        return upload(final,vid)
    finally:
        for p in (source,wav,final):
            try:p.unlink()
            except:pass
        for p in S.glob(f"**/{vid}"):
            if p.is_dir():shutil.rmtree(p,ignore_errors=True)

def one_uploaded(vid, file_path):
    _,_,old=gh_info(vid)
    if old:return "skipped"
    source=Path(file_path)
    wav=D/f"{vid}.wav"
    final=O/f"{vid}.m4a"
    try:
        run(["ffmpeg","-y","-i",str(source),"-ar","44100","-ac","2","-c:a","pcm_s16le",str(wav)])
        run(["python","-m","demucs","--two-stems=vocals","-n","mdx_q","--segment","4","-j","1","-o",str(S),str(wav)])
        cand=list(S.glob(f"**/{vid}/vocals.wav"))
        if not cand:raise RuntimeError("Demucs vocals output missing")
        run(["ffmpeg","-y","-i",str(cand[0]),"-c:a","aac","-b:a","128k",str(final)])
        return upload(final,vid)
    finally:
        for p in (wav,final):
            try:p.unlink()
            except:pass
        for p in S.glob(f"**/{vid}"):
            if p.is_dir():shutil.rmtree(p,ignore_errors=True)

def publish_uploaded(vid, file_path):
    _,_,old=gh_info(vid)
    if old:return "skipped"
    source=Path(file_path)
    final=O/f"{vid}.m4a"
    try:
        run(["ffmpeg","-y","-i",str(source),"-vn","-c:a","aac","-b:a","128k",str(final)])
        return upload(final,vid)
    finally:
        try:final.unlink()
        except:pass

def process_all(url):
    if not TOKEN or "/" not in REPO:raise RuntimeError("أضف GITHUB_TOKEN و GITHUB_REPO في Railway Variables")
    es=entries(url); up=[]; skip=[]; fail=[]
    for vid,page in es:
        try:
            x=one(vid,page); (up if x=="uploaded" else skip).append(vid)
        except Exception as e:fail.append((vid,str(e)[-700:]))
    msg=f"انتهى — الإجمالي {len(es)}\n✅ مرفوع: {len(up)}\n⏭️ موجود مسبقًا: {len(skip)}\n❌ فشل: {len(fail)}"
    if up:msg+="\n\n"+ "\n".join(f"✅ {x}.m4a" for x in up)
    if fail:msg+="\n\n"+ "\n".join(f"❌ {v}: {e}" for v,e in fail)
    return msg

@app.get("/")
def home():return render_template_string(HTML)
@app.get("/health")
def health():return {"ok":True}
@app.post("/process-direct")
def proc_direct():
    d=request.get_json(silent=True) or {}
    vid=re.sub(r"[^A-Za-z0-9_-]","",str(d.get("id","")).strip())
    source=(d.get("source_url") or "").strip()
    if not vid or not re.match(r"^https?://",source):
        return jsonify(ok=False,error="معرّف أو رابط بث غير صحيح"),400
    if not TOKEN or "/" not in REPO:
        return jsonify(ok=False,error="أضف GITHUB_TOKEN و GITHUB_REPO في Railway Variables"),500
    try:
        status=one_direct(vid,source)
        return jsonify(ok=True,message=f"انتهى — {status}: {vid}.m4a")
    except Exception as e:
        return jsonify(ok=False,error=str(e)),500

@app.post("/publish-audio-ui")
def publish_audio_ui():
    if not JOB_LOCK.acquire(blocking=False):
        return jsonify(ok=False,error="هناك عملية أخرى جارية، حاول بعد انتهائها"),429
    source=None
    try:
        vid=re.sub(r"[^A-Za-z0-9_-]","",str(request.form.get("id","")).strip())
        incoming=request.files.get("file")
        if not vid or incoming is None or not incoming.filename:
            return jsonify(ok=False,error="أرسل id وملفًا باسم file"),400
        if not TOKEN or "/" not in REPO:
            return jsonify(ok=False,error="أضف GITHUB_TOKEN و GITHUB_REPO في Railway Variables"),500
        source=D/f"{vid}.clean"
        incoming.save(source)
        status=publish_uploaded(vid,source)
        return jsonify(ok=True,message=f"انتهى — {status}: {vid}.m4a")
    except Exception as e:
        return jsonify(ok=False,error=str(e)),500
    finally:
        if source is not None:
            try:source.unlink()
            except:pass
        JOB_LOCK.release()

@app.post("/process-upload-ui")
def proc_upload_ui():
    if not JOB_LOCK.acquire(blocking=False):
        return jsonify(ok=False,error="هناك عملية أخرى جارية، حاول بعد انتهائها"),429
    source=None
    try:
        vid=re.sub(r"[^A-Za-z0-9_-]","",str(request.form.get("id","")).strip())
        incoming=request.files.get("file")
        if not vid or incoming is None or not incoming.filename:
            return jsonify(ok=False,error="أرسل رابطًا وملفًا للمطابقة"),400
        if not TOKEN or "/" not in REPO:
            return jsonify(ok=False,error="أضف GITHUB_TOKEN و GITHUB_REPO في Railway Variables"),500
        source=D/f"{vid}.source"
        incoming.save(source)
        status=one_uploaded(vid,source)
        return jsonify(ok=True,message=f"انتهى — {status}: {vid}.m4a")
    except Exception as e:
        return jsonify(ok=False,error=str(e)),500
    finally:
        if source is not None:
            try:source.unlink()
            except:pass
        JOB_LOCK.release()

@app.post("/process-upload")
def proc_upload():
    if UPLOAD_KEY and request.headers.get("X-Upload-Key","") != UPLOAD_KEY:
        return jsonify(ok=False,error="مفتاح الرفع غير صحيح"),401
    vid=re.sub(r"[^A-Za-z0-9_-]","",str(request.form.get("id","")).strip())
    incoming=request.files.get("file")
    if not vid or incoming is None or not incoming.filename:
        return jsonify(ok=False,error="أرسل id وملفًا باسم file"),400
    if not TOKEN or "/" not in REPO:
        return jsonify(ok=False,error="أضف GITHUB_TOKEN و GITHUB_REPO في Railway Variables"),500
    source=D/f"{vid}.source"
    try:
        incoming.save(source)
        status=one_uploaded(vid,source)
        return jsonify(ok=True,message=f"انتهى — {status}: {vid}.m4a")
    except Exception as e:
        return jsonify(ok=False,error=str(e)),500
    finally:
        try:source.unlink()
        except:pass

@app.post("/process")
def proc():
    u=(request.get_json(silent=True) or {}).get("url","").strip()
    if not re.match(r"^https?://",u):return jsonify(ok=False,error="رابط غير صحيح"),400
    try:return jsonify(ok=True,message=process_all(u))
    except Exception as e:return jsonify(ok=False,error=str(e)),500

if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
