# غابة الطب — خلاصة الوصول والاسترجاع

وثيقة سريعة: لو حصل أي شيء ومحتاجين نعيد تشغيل كل شي من الصفر، أو شخص ثاني يحتاج يفهم الهيكلة بسرعة.

## أين يعيش كل شي

| الشيء | وين |
|---|---|
| كود البوت + الموقع | GitHub: `kau-forest-bot` (public repo) |
| تشغيل البوت (24/7) | Railway — نفس المشروع اسمه kau-forest-bot |
| الموقع المباشر | GitHub Pages، من فولدر `/docs` بالـ repo |
| القروب | تيليجرام، GROUP_CHAT_ID = -5488109639 |
| البوت نفسه | @KAUForestBot على تيليجرام (عبر BotFather) |

## المتغيرات المهمة (Railway → Variables)

- `BOT_TOKEN` — من BotFather، يفتح اتصال البوت بتيليجرام
- `GROUP_CHAT_ID` — -5488109639
- `TZ_OFFSET_HOURS` — 3
- `MANAGER_IDS` — الآيدي الرقمي حقك بتيليجرام (من @userinfobot)
- `LAUNCH_DATE` — 2026-10-03
- `GITHUB_TOKEN` / `GITHUB_REPO` — الـ repo العام (الموقع)
- `BACKUP_GITHUB_REPO` — repo **خاص** منفصل للنسخ الاحتياطي. بدونه النسخ الاحتياطي ما يشتغل أبدًا

## لو احتجنا نعيد النشر من الصفر

1. Railway → نفس المشروع → تأكد إنه متصل بـ GitHub repo
2. Settings → Custom Start Command: `python forest_bot.py`
3. تأكد Dockerfile موجود بالـ repo (يثبت محرك OCR الحقيقي، بدونه القراءة تفشل)
4. تأكد كل المتغيرات أعلاه موجودة
5. أي كوميت جديد يعيد النشر تلقائيًا

## لو انقطع الوصول عن حسين

- الداتابيس (كل بيانات الطلاب) تتنسخ احتياطيًا تلقائيًا كل يوم 4 الفجر إلى الـ repo **الخاص** المحدد في `BACKUP_GITHUB_REPO` — أبدًا ما تنحط بالـ repo العام
- أهم شي: ضيفوا شخص ثاني (الصديق المؤسس المشارك) كـ member على مشروع Railway وكـ collaborator على الـ GitHub repo، عشان ما يصير كل شي معلّق على حساب وحد

## التوكنات الحساسة

- BOT_TOKEN موجود حاليًا بمحادثة Claude القديمة — يُفضّل تجديده من BotFather بعد ما يستقر كل شي (BotFather → /mybots → اختر البوت → API Token → Revoke current token)
