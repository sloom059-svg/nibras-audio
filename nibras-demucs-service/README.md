# Nibras Demucs Service

خدمة مستقلة لإزالة الموسيقى من ملفات نبراس باستخدام Demucs على RunPod GPU، مع واجهة تحكم خفيفة على Railway.

## المجلدات

- `app.py`: واجهة Railway والـ API.
- `runpod-worker/`: عامل GPU الذي يعمل داخل RunPod Serverless.
- `Dockerfile`: حاوية Railway.
- `railway.toml`: إعدادات Railway.

## متغيرات Railway

ضع هذه القيم داخل خدمة Railway، ولا تضع الأسرار داخل GitHub:

- `RUNPOD_API_KEY` — مفتاح RunPod.
- `RUNPOD_ENDPOINT_ID` — رقم Endpoint الخاص بعامل Demucs.

## RunPod

ابنِ صورة Docker من `runpod-worker/` وانشرها في Registry، ثم أنشئ Serverless Endpoint في RunPod منها. استخدم GPU مناسب مثل RTX 4090/L4/A5000.

العامل يقبل:

```json
{
  "input": {
    "source_url": "https://...",
    "youtube_id": "optional-id"
  }
}
```

ويرجع ملف M4A في `audio_base64`.

## Railway API

- `GET /health`
- `GET /` واجهة بسيطة.
- `POST /process` نموذج ويب مباشر.
- `POST /api/process` يرجع job id.
- `GET /api/status/{job_id}`

مثال:

```json
{
  "source_url": "https://youtu.be/VIDEO_ID",
  "youtube_id": "VIDEO_ID"
}
```

> ملاحظة: النسخة الأولى ترجع الملف مباشرة للمستخدم بعد المعالجة. رفع الناتج تلقائياً إلى GitHub يمكن إضافته لاحقاً بمفتاح GitHub خاص بالخدمة، بدون وضعه في المستودع.
