#!/bin/bash
# 🚀 سكربت بدء التشغيل — يستخدمه Railpack عند النشر على Railway
set -e
cd "$(dirname "$0")"

# 📋 ضمان وجود config.json في src/ (يُترحل تلقائياً إلى /data عند أول تشغيل على Volume جديد)
if [ -f config.json ] && [ ! -f src/config.json ]; then
  cp config.json src/config.json
  echo "📋 تم نسخ config.json إلى src/"
fi

# ▶️ تشغيل البوت
exec python src/main.py
