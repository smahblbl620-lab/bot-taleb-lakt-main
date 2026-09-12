import os
import json
from dotenv import load_dotenv

# Load environment variables from .env file (for local development only)
load_dotenv()

# Load values from environment variables
API_ID = os.getenv('API_ID')
if API_ID:
    API_ID = int(API_ID)

API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
if CHANNEL_ID:
    CHANNEL_ID = int(CHANNEL_ID)

SESSION_NAME = os.getenv('SESSION_NAME', 'telegram_monitor_session')

# ============ إعدادات ملف JSON ============
# مجلد البيانات الدائم: يُفضّل Railway Volume مثبت على /data (لا تفقد البيانات عند إعادة النشر)
# إذا لم يوجد /data نستخدم مجلد config.py نفسه (سلوك قديم متوافق)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv('DATA_DIR') or ('/data' if os.path.isdir('/data') and os.access('/data', os.W_OK) else BASE_DIR)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

def _migrate_initial_config():
    """ترحيل تلقائي عند أول تشغيل على Volume جديد: انسخ config.json القديم من مجلد الكود"""
    try:
        src = os.path.join(BASE_DIR, 'config.json')
        if CONFIG_FILE != src and not os.path.exists(CONFIG_FILE) and os.path.exists(src):
            import shutil
            shutil.copy2(src, CONFIG_FILE)
            print(f"✅ تم ترحيل config.json إلى مجلد البيانات الدائم: {DATA_DIR}")
    except Exception as e:
        print(f"⚠️ فشل ترحيل config.json: {e}")

_migrate_initial_config()

def load_json_config():
    """تحميل جميع الإعدادات من ملف JSON مع القيم الافتراضية"""
    default_config = {
        "KEYWORDS": [],
        "IGNORE_USERS": [],
        "TARGET_GROUPS": [],
        "BANNED_ADS": ["عرض", "خصم", "تخفيض", "سعر", "شراء", "بيع", "كوبون", "تسويق", "إعلان"],
        "SUSPICIOUS_WORDS": ["احتيال", "نصبة", "فيروس", "اختراق", "تزوير", "فدية", "سرقة"],
        "FILTERS": {
            "max_length": 0,
            "block_links": False,
            "block_phones": False,
            "block_mentions": False,
            "block_ads": False,
            "block_suspicious": False
        },
        "ADMIN_GROUP_ID": 0,
        "ADMIN_PERMISSIONS": {},
        "MAX_ACCOUNTS_PER_USER": 3,
        # 🌟 كلمات مفتاحية افتراضية متوفرة لكل الحسابات — كل مستخدم يقدر يحذف أي واحدة منها لنفسه (USER_DELETED_DEFAULTS) أو يضيف كلمات أخرى (USER_KEYWORDS)
        "DEFAULT_KEYWORDS": ["يسوي", "تسوي", "تشرح", "يشرح", "خصوصي", "احد", "يحل", "تحل", "تعرفون", "ابغى", "بغيت"],
        "USER_KEYWORDS": {},
        "USER_DELETED_DEFAULTS": {},
        "USER_DM_TEMPLATES": {},
        "USER_GRP_TEMPLATES": {}
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # دمج القيم المحملة مع القيم الافتراضية (لأي مفاتيح مفقودة)
            for key, value in default_config.items():
                if key not in config:
                    config[key] = value
            return config
        except Exception as e:
            print(f"خطأ في تحميل config_data.json: {e}")
            return default_config
    else:
        return default_config

def update_json_config(config):
    """حفظ الإعدادات إلى ملف JSON"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"خطأ في حفظ config_data.json: {e}")

# تحميل الإعدادات الديناميكية من JSON (للاستخدام المباشر إذا أردت)
json_config = load_json_config()
TARGET_GROUPS = json_config.get('TARGET_GROUPS', [])
KEYWORDS = json_config.get('KEYWORDS', [])
IGNORE_USERS = json_config.get('IGNORE_USERS', [])
BANNED_ADS = json_config.get('BANNED_ADS', [])
SUSPICIOUS_WORDS = json_config.get('SUSPICIOUS_WORDS', [])
FILTERS = json_config.get('FILTERS', {})
