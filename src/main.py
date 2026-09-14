import logging
import asyncio
import os
import json
import re
import time
import shutil
from functools import lru_cache
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError, PhoneNumberFloodError, PhoneNumberInvalidError, AuthRestartError, PasswordHashInvalidError
from telethon.sessions import StringSession
from telethon.tl.types import Chat, Channel, ChatInviteAlready
from telethon.tl.functions.messages import ExportChatInviteRequest, CheckChatInviteRequest
from flask import Flask
from threading import Thread
from config import API_ID, API_HASH, BOT_TOKEN, CHANNEL_ID, load_json_config, update_json_config, DATA_DIR

# مجلد التخزين الدائم (Railway Volume /data) — لا تفقد البيانات عند إعادة النشر
SESSION_DIR = DATA_DIR
CHAT_LOG_FILE = os.path.join(DATA_DIR, 'chat_logs.jsonl')
# القروب الرسمي لاستقبال كل الرسائل (يُعيّن تلقائياً إذا لم يُضبط قروب آخر)
OFFICIAL_GROUP = os.getenv('OFFICIAL_GROUP', 'https://t.me/hsjjjjihsjs')

# إعدادات الاتصال السريع والمرن لكل عملاء تيليجرام
CLIENT_OPTS = dict(
    flood_sleep_threshold=120,   # نوم تلقائي عند FloodWait بدل الفشل
    connection_retries=15,       # محاولات اتصال أكثر
    retry_delay=3,
    request_retries=6,
    auto_reconnect=True,
)

# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask for Keep Alive (Render)
app = Flask('')

@app.route('/')
def home():
    return "البوت يعمل بنجاح!", 200

@app.route('/health')
def health():
    return "OK", 200

@app.route('/status')
def status():
    return {
        "bot_started": bot is not None,
        "active_clients": list(active_clients.keys()),
        "message_map_size": len(message_map),
        "data_dir": DATA_DIR,
        "persistent": DATA_DIR == '/data' or bool(os.getenv('DATA_DIR')),
    }, 200

@app.route('/debug')
def debug():
    """endpoint تشخيصي - يعرض حالة المتغيرات البيئية (بدون كشف القيم)"""
    return {
        "BOT_TOKEN_set": bool(os.environ.get('BOT_TOKEN')),
        "BOT_TOKEN_len": len(os.environ.get('BOT_TOKEN', '')),
        "API_ID_set": bool(os.environ.get('API_ID')),
        "API_ID_value": os.environ.get('API_ID'),
        "API_HASH_set": bool(os.environ.get('API_HASH')),
        "API_HASH_len": len(os.environ.get('API_HASH', '')),
        "CHANNEL_ID_set": bool(os.environ.get('CHANNEL_ID')),
        "CHANNEL_ID_value": os.environ.get('CHANNEL_ID'),
        "bot_started": bot is not None,
        "active_clients_count": len(active_clients),
    }, 200

@app.route('/stats')
def stats_endpoint():
    """إحصائيات حية لتتبع عمل البوت"""
    config = load_json_config()
    return {
        "bot_started": bot is not None,
        "active_clients": list(active_clients.keys()),
        "active_clients_count": len(active_clients),
        "keywords_loaded": config.get('KEYWORDS', []),
        "keywords_count": len(config.get('KEYWORDS', [])),
        "default_keywords": config.get('DEFAULT_KEYWORDS', []),
        "default_keywords_count": len(config.get('DEFAULT_KEYWORDS', [])),
        "filters": config.get('FILTERS', {}),
        "detect_links": config.get('DETECT_LINKS', True),
        "channel_id": os.environ.get('CHANNEL_ID'),
        "main_admin_id": MAIN_ADMIN_ID,
        "builtin_admins": sorted(EXTRA_MAIN_ADMINS),
        "additional_admins": config.get('ADMINS', []),
        "roles": config.get('ADMIN_ROLES', {}),
        "forward_groups_count": len(config.get('FORWARD_GROUPS', [])),
        "stats": stats,
        "message_map_size": len(message_map),
        "seen_messages_size": len(seen_messages),
        "data_dir": DATA_DIR,
        "sessions_persisted": len(load_json_config().get('SESSIONS', {})),
        "admin_group_id": config.get('ADMIN_GROUP_ID', 0),
        "official_group": OFFICIAL_GROUP,
        "log_retention_days": config.get('LOG_RETENTION_DAYS', 3),
        "chat_log_size_mb": round(os.path.getsize(CHAT_LOG_FILE) / 1048576, 2) if os.path.exists(CHAT_LOG_FILE) else 0,
    }, 200

@app.route('/test_forward')
def test_forward():
    """اختبار إرسال رسالة للقناة - للتأكد من أن CHANNEL_ID صحيح والبوت مشرف"""
    return {
        "message": "هذا endpoint تشخيصي فقط. استخدم البوت لاختبار التحويل فعلياً.",
        "channel_id": os.environ.get('CHANNEL_ID'),
        "active_clients_count": len(active_clients),
        "bot_started": bot is not None,
    }, 200

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# Global variables
bot = None
active_clients = {}  # {phone: TelegramClient}
login_states = {}    # {user_id: {'step': 'phone/code', 'phone': '...', 'hash': '...'}}
# 🧷 خريطة تحويل الأرقام العربية (٠١٢٣) والفارسية (۰۱۲۳) إلى غربية — لاستخراج كود التحقق من أي نص ملصوق
_CODE_DIGITS_MAP = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')

# ============ نظام صلاحيات الأدمن ============
# الأدمن الرئيسي — يُقرأ من متغير البيئة ADMIN_ID (مع قيمة احتياطية للتوافق مع الإعدادات القديمة)
try:
    MAIN_ADMIN_ID = int(os.getenv('ADMIN_ID', '7853478744') or '7853478744')
except (TypeError, ValueError):
    MAIN_ADMIN_ID = 7853478744

# أدمنة ثابتون إضافيون بصلاحيات كاملة — يمكن ضبطهم عبر متغير البيئة EXTRA_ADMINS (معرفات مفصولة بفواصل)
# الافتراضي: 7853478744 (أدمن ثانٍ بصلاحيات كاملة)
EXTRA_MAIN_ADMINS = set()
try:
    for _aid in str(os.getenv('EXTRA_ADMINS', '7853478744') or '7853478744').split(','):
        _aid = _aid.strip()
        if _aid:
            EXTRA_MAIN_ADMINS.add(int(_aid))
except (TypeError, ValueError):
    EXTRA_MAIN_ADMINS = {7853478744}
EXTRA_MAIN_ADMINS.discard(MAIN_ADMIN_ID)

def is_admin(user_id):
    """فحص إذا كان المستخدم أدمن (رئيسي/ثابت أو مُضاف في قائمة ADMINS)"""
    if is_main_admin(user_id):
        return True
    config = load_json_config()
    admins = config.get('ADMINS', [])
    return user_id in admins

def is_main_admin(user_id):
    """فحص إذا كان المستخدم أدمناً كامل الصلاحيات (الأدمن الرئيسي أو أحد الأدمنة الثابتين)"""
    return user_id == MAIN_ADMIN_ID or user_id in EXTRA_MAIN_ADMINS

# نص رسالة الرفض لغير المصرح لهم (يظهر عند استخدام البوت من مستخدم ليس مشرفاً)
UNAUTHORIZED_MSG = (
    "📢 لطلب التفعيل والحصول على صلاحية الدخول إلى البوت، يرجى التواصل مع أدمن البوت أو المالك مباشرةً عبر الحسابات التالية:\n\n"
    "👉 @ppppokl\n"
    "👉 @drpharmacistgg\n\n"
    "سيتم تفعيل حسابك بعد مراجعة الطلب. شكراً لك!"
)

# ============ نظام الصلاحيات المفصلة (أدمن / مشرف / عضو) ============
PERMISSIONS = {
    'add_accounts': '➕ إضافة وحذف حسابات المراقبة',
    'view_stats': '📋 عرض الحسابات والإحصائيات',
    'manage_keywords': '🔑 الكلمات المفتاحية وقائمة التجاهل',
    'manage_filters': '🛡️ الكلمات المحظورة وإعدادات الفلترة',
    'manage_templates': '💬 قوالب الرد على الخاص والقروب',
    'manage_auto': '📨 الرد التلقائي والتكرار والحذف',
    'export_links': '🔗 استيراد روابط القروبات',
    'manage_groups': '👥 اعتماد قروبات التوجيه للمستخدمين',
    'add_admins': '👑 إدارة المشرفين والأعضاء والصلاحيات',
    'broadcast': '📢 إذاعة رسائل وتنبيهات لجميع مستخدمي البوت',
}

# الرتب: أدمن (كل الصلاحيات) / مستخدم (يضيف حسابه ويستخدم ما تسمح به صلاحياته)
ROLES = {
    'admin': '🛡 أدمن',
    'user': '👤 مستخدم',
}

def get_user_role(user_id):
    """دور المستخدم: الأدمن الرئيسي/الثابت = owner، وإلا من ADMIN_ROLES (الافتراضي مستخدم)
    القيم القديمة 'supervisor' تُعامل كـ 'user' (تم حذف دور المشرف)"""
    if is_main_admin(user_id):
        return 'owner'
    config = load_json_config()
    role = config.get('ADMIN_ROLES', {}).get(str(user_id), 'user')
    return 'user' if role == 'supervisor' else role

def is_full_admin(user_id):
    """أدمن كامل الصلاحيات: الأدمن الرئيسي/الثابت أو مستخدم برتبة 'أدمن'"""
    if is_main_admin(user_id):
        return True
    return get_user_role(user_id) == 'admin'

# ============ توحيد صيغة أرقام الهاتف (إصلاح «انتهت صلاحية الكود» عند إضافة حساب مضاف) ============
# المشكلة: نفس الرقم قد يُخزَّن بصيغ مختلفة («+966 54 xxx» بمسافات مقابل «+96654xxx») فتفشل كل
# المطابقات (الحسابات النشطة/الملكية/ملفات الجلسات) ويُفتح طلب كود جديد لحساب يعمل أصلاً فيتضارب الأكواد.

def _phone_digits(raw):
    """الأرقام فقط من أي صيغة هاتف — للمطابقة المتسامحة بين الصيغ"""
    return re.sub(r'\D', '', str(raw or ''))

def normalize_phone(raw):
    """توحيد صيغة رقم الهاتف إلى الشكل القياسي +XXXXXXXXX (بدون مسافات/شرطات/أقواس)"""
    s = str(raw or '').strip()
    digits = re.sub(r'\D', '', s)
    if not digits:
        return s
    if digits.startswith('00'):
        digits = digits[2:]
    return '+' + digits

def find_active_client(phone):
    """إيجاد عميل مراقب نشط لنفس الرقم بأي صيغة — يعيد (المفتاح الفعلي، العميل) أو (None, None)"""
    d = _phone_digits(phone)
    if not d:
        return None, None
    if phone in active_clients:
        return phone, active_clients[phone]
    for p, c in active_clients.items():
        if _phone_digits(p) == d:
            return p, c
    return None, None

def find_account_owner(phone):
    """مالك الحساب بأي صيغة تخزين — مطابقة تامة ثم مطابقة بالأرقام"""
    try:
        ow = load_json_config().get('ACCOUNT_OWNERS', {})
        if str(phone) in ow:
            return ow[str(phone)]
        d = _phone_digits(phone)
        if d:
            for p, o in ow.items():
                if _phone_digits(p) == d:
                    return o
    except Exception:
        pass
    return None

def find_session_path(phone):
    """مسار ملف جلسة الرقم بأي صيغة محفوظة على القرص — الأصل ثم المطابقة بالأرقام"""
    exact = os.path.join(SESSION_DIR, f'session_{phone}')
    if os.path.exists(exact + '.session'):
        return exact
    d = _phone_digits(phone)
    if d:
        try:
            for f in os.listdir(SESSION_DIR):
                if f.startswith('session_') and f.endswith('.session') and not f.startswith('session_claim_'):
                    if _phone_digits(f[len('session_'):-len('.session')]) == d:
                        return os.path.join(SESSION_DIR, f[:-len('.session')])
        except Exception:
            pass
    return os.path.join(SESSION_DIR, f'session_{phone}')

def claim_session_path(user_id):
    """ملف جلسة مؤقت لإثبات ملكية حساب مضاف مسبقاً — يُحذف بعد الانتهاء ولا يُستأنف عند الإقلاع"""
    return os.path.join(SESSION_DIR, f'session_claim_{user_id}')

def delete_claim_session(user_id):
    try:
        _p = claim_session_path(user_id) + '.session'
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

def phone_owned(user_id, phone):
    """هل الرقم (بأي صيغة) مملوك للمستخدم؟"""
    d = _phone_digits(phone)
    for p in get_owned_accounts(user_id):
        if p == phone or (d and _phone_digits(p) == d):
            return True
    return False

def owned_active_phones(user_id):
    """مفاتيح الحسابات النشطة المملوكة للمستخدم — مطابقة متسامحة مع اختلاف صيغة الرقم"""
    owned = {_phone_digits(p) for p in get_owned_accounts(user_id)}
    return [p for p in active_clients.keys() if _phone_digits(p) in owned]

# 🧹 أُزيلت آلية إثبات الملكية (claim) بطلب المستخدم — آلية الإضافة رجعت بسيطة كما كانت

def set_account_owner(phone, owner_id):
    """ربط حساب مراقب بمالكه — ويوحّد الملكية على كل صيغ الرقم المتطابقة رقماً (يمنع الالتباس بين الصيغ)"""
    if not owner_id or not phone:
        return
    config = load_json_config()
    ow = config.get('ACCOUNT_OWNERS', {})
    ow[str(phone)] = int(owner_id)
    _d = _phone_digits(phone)
    if _d:
        for p in list(ow.keys()):
            if _phone_digits(p) == _d:
                ow[p] = int(owner_id)
    config['ACCOUNT_OWNERS'] = ow
    update_json_config(config)

def get_owned_accounts(user_id):
    """قائمة الهواتف المملوكة لمستخدم محدد — بدون ازدواج الصيغ المختلفة لنفس الرقم"""
    config = load_json_config()
    ow = config.get('ACCOUNT_OWNERS', {})
    out, seen_d = [], set()
    for phone, owner in ow.items():
        if owner == user_id or str(owner) == str(user_id):
            d = _phone_digits(phone)
            if d and d in seen_d:
                continue
            seen_d.add(d)
            out.append(phone)
    return out

def get_user_fwd_groups(user_id, cfg=None):
    """قروبات التوجيه المعتمدة لمستخدم محدد — العزل: تُعرض قروباته هو فقط"""
    if cfg is None:
        cfg = load_json_config()
    gids = cfg.get('USER_GROUPS', {}).get(str(user_id), [])
    title_map = {g.get('id'): g.get('title', g.get('id')) for g in cfg.get('FORWARD_GROUPS', [])}
    return [{'id': gid, 'title': title_map.get(gid, str(gid))} for gid in gids]

def get_all_known_users(cfg=None):
    """👥 كل المستخدمين المعروفين للبوت (لهم حسابات/كلمات/قروبات/قوالب أو أدمن) — لشاشة مراقبة الأدمن"""
    if cfg is None:
        cfg = load_json_config()
    uids = set()
    for m in ('ACCOUNT_OWNERS', 'USER_KEYWORDS', 'USER_DELETED_DEFAULTS', 'USER_GROUPS', 'USER_DM_TEMPLATES', 'USER_GRP_TEMPLATES'):
        v = cfg.get(m, {})
        if isinstance(v, dict):
            for k in v.keys():
                try:
                    uids.add(int(k))
                except (ValueError, TypeError):
                    pass
    for a in cfg.get('ADMINS', []):
        try:
            uids.add(int(a))
        except (ValueError, TypeError):
            pass
    uids.update(int(x) for x in EXTRA_MAIN_ADMINS)
    uids.add(int(MAIN_ADMIN_ID))
    return sorted(uids)

_user_name_cache = {}
async def get_user_display_name(uid):
    """اسم المستخدم للعرض في شاشات مراقبة الأدمن — مع تخزين مؤقت لتفادي الاستعلامات المتكررة"""
    uid = int(uid)
    if uid in _user_name_cache:
        return _user_name_cache[uid]
    name = None
    try:
        ent = await bot.get_entity(uid)
        base = getattr(ent, 'first_name', None) or getattr(ent, 'title', None)
        uname = getattr(ent, 'username', None)
        if base and uname:
            name = f"{base} (@{uname})"
        elif uname:
            name = f"@{uname}"
        elif base:
            name = base
    except Exception:
        name = None
    result = (name or f"مستخدم {uid}")[:40]
    _user_name_cache[uid] = result
    return result

def get_user_kind_label(uid, cfg=None):
    """تصنيف المستخدم (رتبته) للعرض في ملفه ضمن شاشة المراقبة"""
    if cfg is None:
        cfg = load_json_config()
    if uid == MAIN_ADMIN_ID or int(uid) == int(MAIN_ADMIN_ID):
        return "👑 الأدمن الرئيسي (المالك)"
    if int(uid) in [int(x) for x in EXTRA_MAIN_ADMINS]:
        return "🛡 أدمن ثابت (صلاحيات كاملة)"
    if get_user_role(uid) == 'admin':
        return "🛡 أدمن (رتبة أدمن)"
    if uid in cfg.get('ADMINS', []) or int(uid) in [int(a) for a in cfg.get('ADMINS', []) if str(a).isdigit()]:
        return "👥 مشرف مضاف"
    return "👤 عضو"

async def _delete_account_full(phone):
    """🗑 حذف حساب مراقب بالكامل: فصل العميل + حذف ملف الجلسة + تنظيف الملكية واعتمادات القروبات — تُستخدم من حذف المستخدم ومن مراقبة الأدمن"""
    if phone in active_clients:
        try:
            await active_clients[phone].disconnect()
        except Exception:
            pass
        active_clients.pop(phone, None)
    sess_path = os.path.join(SESSION_DIR, f'session_{phone}.session')
    if os.path.exists(sess_path):
        try:
            os.remove(sess_path)
        except Exception:
            pass
    try:
        forget_session_string(phone)
    except Exception:
        pass
    try:
        cfg_del = load_json_config()
        ag_map = cfg_del.get('ACCOUNT_GROUPS', {})
        if phone in ag_map:
            ag_map.pop(phone, None)
            cfg_del['ACCOUNT_GROUPS'] = ag_map
        ow_map = cfg_del.get('ACCOUNT_OWNERS', {})
        if phone in ow_map:
            ow_map.pop(phone, None)
            cfg_del['ACCOUNT_OWNERS'] = ow_map
        update_json_config(cfg_del)
    except Exception:
        pass

def get_user_keywords(user_id, cfg=None):
    """🔑 الكلمات المفتاحية الخاصة بمستخدم محدد — خصوصية تامة بين المستخدمين"""
    if cfg is None:
        cfg = load_json_config()
    return cfg.get('USER_KEYWORDS', {}).get(str(user_id), [])

def set_user_keywords(user_id, items, cfg=None):
    """حفظ الكلمات المفتاحية الخاصة بمستخدم محدد"""
    if cfg is None:
        cfg = load_json_config()
    maps = cfg.get('USER_KEYWORDS', {})
    maps[str(user_id)] = items
    cfg['USER_KEYWORDS'] = maps
    update_json_config(cfg)

def get_default_keywords(cfg=None):
    """🌟 الكلمات المفتاحية الافتراضية — متوفرة لكل الحسابات تلقائياً"""
    if cfg is None:
        cfg = load_json_config()
    return list(cfg.get('DEFAULT_KEYWORDS', ["يسوي", "تسوي", "تشرح", "يشرح", "خصوصي", "احد", "يحل", "تحل", "تعرفون", "ابغى", "بغيت"]))

def get_user_deleted_defaults(user_id, cfg=None):
    """الافتراضيات التي أخفاها هذا المستخدم عن نفسه (حذف شخصي فقط — لا يؤثر على غيره)"""
    if cfg is None:
        cfg = load_json_config()
    return cfg.get('USER_DELETED_DEFAULTS', {}).get(str(user_id), [])

def remove_user_default(user_id, word, cfg=None):
    """🌟🗑 إخفاء كلمة افتراضية عن مستخدم محدد (حذف شخصي)"""
    if cfg is None:
        cfg = load_json_config()
    maps = cfg.get('USER_DELETED_DEFAULTS', {})
    lst = maps.get(str(user_id), [])
    if word not in lst:
        lst.append(word)
    maps[str(user_id)] = lst
    cfg['USER_DELETED_DEFAULTS'] = maps
    update_json_config(cfg)

def restore_user_default(user_id, word, cfg=None):
    """♻️ استعادة كلمة افتراضية كانت مخفية لمستخدم محدد"""
    if cfg is None:
        cfg = load_json_config()
    maps = cfg.get('USER_DELETED_DEFAULTS', {})
    lst = [w for w in maps.get(str(user_id), []) if w != word]
    maps[str(user_id)] = lst
    cfg['USER_DELETED_DEFAULTS'] = maps
    update_json_config(cfg)

def get_effective_keywords(user_id, cfg=None):
    """🔑 الكلمات الفعالة لمستخدم: الافتراضية (عدا ما أخفاها) + الخاصة به + العامة (أدمن) — بدون تكرار"""
    if cfg is None:
        cfg = load_json_config()
    deleted = get_user_deleted_defaults(user_id, cfg)
    defaults_active = [k for k in get_default_keywords(cfg) if k not in deleted]
    my_kw = cfg.get('USER_KEYWORDS', {}).get(str(user_id), [])
    gkw = cfg.get('KEYWORDS', [])
    return list(dict.fromkeys(defaults_active + my_kw + gkw))

MAX_KEYWORD_LEN = 40  # أطول كلمة مفتاحية مسموحة — يمنع لصق رسائل كاملة ككلمة

# 🔎 نمط المطابقة: الكلمة كوحدة مستقلة كاملة — "احد" تطابق "احد/أحد/اَحْد" فقط ولا تطابق الاحد/احدى/احدث/واحد
_ARABIC_MARKS_RE = re.compile(r'[\u064B-\u0652\u0670\u0640]')  # تشكيل + تنوين + تطويل

def normalize_ar_text(s):
    """توحيد الكتابة العربية: إزالة التشكيل والتطويل + توحيد الألف (أ إ آ → ا) والياء (ى → ي) — ليتعرف على نفس الكلمة بصيغها الإملائية"""
    s = _ARABIC_MARKS_RE.sub('', s or '')
    return s.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ى', 'ي')

@lru_cache(maxsize=1024)
def _kw_pattern(keyword):
    """بناء نمط regex للكلمة ككلمة كاملة مستقلة (حدود كلمة) — يُبنى مرة واحدة لكل كلمة ويُخزّن"""
    tokens = normalize_ar_text(keyword).split()
    if not tokens:
        return None
    body = r'\s+'.join(re.escape(t) for t in tokens)  # العبارات متعددة الكلمات: يتسامح مع اختلاف المسافات
    return re.compile(r'(?<!\w)' + body + r'(?!\w)')

def keyword_in_text(text, keyword):
    """🔎 هل الكلمة المفتاحية موجودة في النص ككلمة/عبارة كاملة مستقلة؟ (وليست جزءاً من كلمة أطول)"""
    pat = _kw_pattern(keyword)
    return bool(pat and pat.search(normalize_ar_text(text)))

def keyword_in_text_norm(text_norm, keyword):
    """نفس keyword_in_text لكن للنص المُوحّد مسبقاً — للأداء في مطابقة كل رسالة"""
    pat = _kw_pattern(keyword)
    return bool(pat and pat.search(text_norm))

def parse_keywords_input(text):
    """🧩 تفكيك مدخلات الكلمات: كل سطر كلمة، أو كلمات مفصولة بفواصل (، , ؛ ;) — يمنع تخزين رسالة كاملة ككلمة واحدة"""
    parts = re.split(r'[\n\u060C,\u061B;]+', text or '')
    out, seen = [], set()
    for p in parts:
        s = re.sub(r'^[-•*–—]\s+', '', p.strip()).strip()  # إزالة علامات التعداد من الكلمات الملصوقة من قائمة قديمة
        if not s or len(s) > MAX_KEYWORD_LEN or '\n' in s:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:10]  # حد أقصى 10 كلمات في الرسالة الواحدة

def sanitize_keywords_config(cfg):
    """🧹 تنظيف تلقائي: يزيل أي كلمة مشوهة (تحتوي سطراً جديداً أو أطول من الحد — ناتجة عن لصق رسالة كاملة) من الكلمات العامة وقوائم المستخدمين — يعيد True إن تغير شيء"""
    changed = False
    gkw = cfg.get('KEYWORDS', [])
    clean_g = [k for k in gkw if isinstance(k, str) and k.strip() and '\n' not in k and len(k.strip()) <= MAX_KEYWORD_LEN]
    if len(clean_g) != len(gkw):
        cfg['KEYWORDS'] = clean_g
        changed = True
    maps = cfg.get('USER_KEYWORDS', {})
    for uid, kws in list(maps.items()):
        if isinstance(kws, list):
            clean_u = [k for k in kws if isinstance(k, str) and k.strip() and '\n' not in k and len(k.strip()) <= MAX_KEYWORD_LEN]
            if len(clean_u) != len(kws):
                maps[uid] = clean_u
                changed = True
    return changed

async def send_kw_delete_screen(event, user_id, config, edit=False):
    """🗑 شاشة حذف الكلمات بالأزرار — الافتراضية 🌟 والخاصة 🔑: اضغط الكلمة تُحذف فوراً"""
    defaults = get_default_keywords(config)
    my_kw = get_user_keywords(user_id, config)
    if not defaults and not my_kw:
        txt = "❌ لا توجد كلمات لحذفها — أضف كلمة أولاً من ➕ إضافة كلمة."
        rows = [[Button.inline('🔙 رجوع', b'manage_kw')]]
    else:
        txt = "🗑 **اضغط على الكلمة لحذفها فوراً من قائمتك:**\n\n"
        rows = []
        if defaults:
            txt += "🌟 **الافتراضية** (متاحة لكل الحسابات — الحذف يخفيها عندك فقط):\n"
            pair = []
            for i, k in enumerate(defaults[:40]):
                preview = k[:18] + "..." if len(k) > 18 else k
                pair.append(Button.inline(f"🌟 {preview}", f"delflt_{i}".encode()))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
            if pair:
                rows.append(pair)
        if my_kw:
            txt += "\n🔑 **كلماتك الخاصة** (اضغطها تُحذف نهائياً):\n"
            for i, k in enumerate(my_kw[:40]):
                preview = k[:25] + "..." if len(k) > 25 else k
                rows.append([Button.inline(f"🗑 {preview}", f"delkw_{i}".encode())])
        rows.append([Button.inline('🔙 رجوع', b'manage_kw')])
    if edit:
        try:
            await event.edit(txt, buttons=rows)
            return
        except Exception:
            pass
    await event.respond(txt, buttons=rows)

async def send_kw_restore_screen(event, user_id, config, edit=False):
    """♻️ شاشة استعادة الكلمات الافتراضية المخفية — اضغط الكلمة تُستعاد فوراً"""
    deleted = get_user_deleted_defaults(user_id, config)
    if not deleted:
        txt = "✅ لا توجد كلمات افتراضية محذوفة عندك — كل الافتراضية مفعّلة."
        rows = [[Button.inline('🔙 رجوع', b'manage_kw')]]
    else:
        txt = "♻️ **اضغط على الكلمة الافتراضية لاستعادتها وتفعيلها مجدداً:**"
        rows = []
        pair = []
        for i, k in enumerate(deleted[:40]):
            preview = k[:18] + "..." if len(k) > 18 else k
            pair.append(Button.inline(f"♻️ {preview}", f"rstflt_{i}".encode()))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([Button.inline('🔙 رجوع', b'manage_kw')])
    if edit:
        try:
            await event.edit(txt, buttons=rows)
            return
        except Exception:
            pass
    await event.respond(txt, buttons=rows)

def get_own_templates(user_id, kind, cfg=None):
    """💬 القوالب الخاصة بمستخدم محدد (kind='DM' أو 'GRP') — كل مستخدم يرى قوالب هو فقط"""
    if cfg is None:
        cfg = load_json_config()
    key = 'USER_DM_TEMPLATES' if kind == 'DM' else 'USER_GRP_TEMPLATES'
    return cfg.get(key, {}).get(str(user_id), [])

def set_own_templates(user_id, kind, items, cfg=None):
    """حفظ القوالب الخاصة بمستخدم محدد"""
    if cfg is None:
        cfg = load_json_config()
    key = 'USER_DM_TEMPLATES' if kind == 'DM' else 'USER_GRP_TEMPLATES'
    maps = cfg.get(key, {})
    maps[str(user_id)] = items
    cfg[key] = maps
    update_json_config(cfg)

# ============ 👥 شاشات مراقبة المستخدمين للأدمن ============

UMON_PAGE_SIZE = 12  # عدد المستخدمين في الصفحة الواحدة

async def send_users_monitor_screen(event, admin_id, page=0, edit=False):
    """👥 شاشة مراقبة المستخدمين للأدمن — كل مستخدم بزر يفتح ملفه الكامل (حساباته، كلماته، إعداداته)"""
    config = load_json_config()
    uids = get_all_known_users(config)
    ow = config.get('ACCOUNT_OWNERS', {})
    pages = max(1, (len(uids) + UMON_PAGE_SIZE - 1) // UMON_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = uids[page * UMON_PAGE_SIZE:(page + 1) * UMON_PAGE_SIZE]
    names = await asyncio.gather(*[get_user_display_name(u) for u in chunk], return_exceptions=True)
    kw_map = config.get('USER_KEYWORDS', {})
    msg = (f"👥 **مراقبة المستخدمين** — الإجمالي: **{len(uids)}**\n\n"
           f"📱 الحسابات المرتبطة: **{len(ow)}** | 🟢 متصلة الآن: **{len(active_clients)}**\n\n"
           "👇 اضغط أي مستخدم لفتح ملفه الكامل: حساباته، كلماته، قروبه، قوالبه، وكل إعداداته.")
    rows = []
    for u, nm in zip(chunk, names):
        nm = nm if isinstance(nm, str) else f"مستخدم {u}"
        accs = [p for p, o in ow.items() if str(o) == str(u)]
        kws = len(kw_map.get(str(u), []))
        rows.append([Button.inline(f"👤 {nm[:24]} — {len(accs)}📱 {kws}🔑", f"umon_{u}".encode())])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Button.inline('◀️ السابق', f"umonp_{page-1}".encode()))
        nav.append(Button.inline(f"📄 {page+1}/{pages}", b'noop_page'))
        if page < pages - 1:
            nav.append(Button.inline('التالي ▶️', f"umonp_{page+1}".encode()))
        rows.append(nav)
    rows.append([Button.inline('🔙 رجوع للقائمة', b'back_main')])
    try:
        if edit:
            await event.edit(msg, buttons=rows)
        else:
            await event.respond(msg, buttons=rows)
    except Exception:
        await event.respond(msg, buttons=rows)

async def send_user_monitor_screen(event, admin_id, target_uid, edit=False):
    """👤 ملف المستخدم الكامل للأدمن: رتبته، حساباته، كلماته، قروبه، قوالبه + أزرار التغيير عليها"""
    config = load_json_config()
    uid = int(target_uid)
    uname = await get_user_display_name(uid)
    kind = get_user_kind_label(uid, config)
    max_acc = int(config.get('MAX_ACCOUNTS_PER_USER', 3) or 3)
    ow = config.get('ACCOUNT_OWNERS', {})
    his_phones = [p for p, o in ow.items() if str(o) == str(uid)]
    his_kws = config.get('USER_KEYWORDS', {}).get(str(uid), [])
    his_deleted = config.get('USER_DELETED_DEFAULTS', {}).get(str(uid), [])
    his_groups = get_user_fwd_groups(uid, config)
    dm_tpl = config.get('USER_DM_TEMPLATES', {}).get(str(uid), [])
    grp_tpl = config.get('USER_GRP_TEMPLATES', {}).get(str(uid), [])

    msg = (f"👤 **ملف المستخدم**\n\n"
           f"🧑 {uname}\n"
           f"🆔 المعرف: `{uid}`\n"
           f"{kind}\n"
           f"📊 حصة الحسابات: **{len(his_phones)}/{max_acc}**\n\n")
    # الحسابات
    msg += f"📱 **حساباته المرتبطة** ({len(his_phones)}):\n"
    acc_rows = []
    if his_phones:
        for p in his_phones:
            online = '🟢 متصل' if p in active_clients else '⚪ غير متصل'
            approved = len(config.get('ACCOUNT_GROUPS', {}).get(str(p), []))
            msg += f"• `{p}` — {online} — قروبات معتمدة للحساب: {approved}\n"
            acc_rows.append([Button.inline(f"🗑 حذف حسابه {p}", f"adeltel_{uid}_{p}".encode())])
    else:
        msg += "• لا يوجد — لم يضف حساباً بعد.\n"
    # قروبات التوجيه
    msg += f"\n🎯 **قروبات توجيهه** ({len(his_groups)}):\n"
    grp_btns = []
    if his_groups:
        for i, g in enumerate(his_groups[:10]):
            title = str(g.get('title', g.get('id')))[:30]
            msg += f"{i+1}. {title}\n"
            grp_btns.append(Button.inline(f"✖ {title[:16]}", f"armgrp_{uid}_{i}".encode()))
    else:
        msg += "• لا يوجد — لم يعتمد قروباً بعد.\n"
    # الكلمات
    msg += f"\n🔑 **كلماته الخاصة** ({len(his_kws)}):\n"
    kw_btns = []
    if his_kws:
        msg += "`" + "`, `".join(his_kws[:20]) + "`\n"
        kw_btns = [Button.inline(f"✖ {k[:14]}", f"adelkw_{uid}_{i}".encode()) for i, k in enumerate(his_kws[:10])]
    else:
        msg += "• لا توجد — أضفها له من الزر أدناه.\n"
    # الافتراضيات المخفية
    msg += f"\n🌟 **الافتراضية التي أخفاها عن نفسه** ({len(his_deleted)}):\n"
    msg += ("`" + "`, `".join(his_deleted[:15]) + "`") if his_deleted else "• لا شيء — كل الافتراضية مفعّلة عنده.\n"
    # القوالب
    msg += f"\n💬 **قوالبه على الخاص** ({len(dm_tpl)}):\n"
    msg += ("\n".join([f"• \"{t[:70]}{'…' if len(t) > 70 else ''}\"" for t in dm_tpl[:2]]) if dm_tpl else "• لا توجد.\n")
    msg += f"\n👥 **قوالبه في القروب** ({len(grp_tpl)}):\n"
    msg += ("\n".join([f"• \"{t[:70]}{'…' if len(t) > 70 else ''}\"" for t in grp_tpl[:2]]) if grp_tpl else "• لا توجد.\n")

    buttons = []
    for i in range(0, len(kw_btns), 2):
        buttons.append(kw_btns[i:i+2])
    buttons.append([Button.inline('🔑 أضف كلمة له', f"aaddkw_{uid}".encode())])
    if his_deleted:
        buttons.append([Button.inline(f"♻️ استعادة كل افتراضياته ({len(his_deleted)})", f"aclrdfl_{uid}".encode())])
    for i in range(0, len(grp_btns), 2):
        buttons.append(grp_btns[i:i+2])
    buttons += acc_rows
    buttons.append([Button.inline('👥 كل المستخدمين', b'users_monitor'), Button.inline('🏠 الرئيسية', b'back_main')])
    try:
        if edit:
            await event.edit(msg, buttons=buttons)
        else:
            await event.respond(msg, buttons=buttons)
    except Exception:
        await event.respond(msg, buttons=buttons)

async def ensure_connected(client):
    """🔌 إعادة الاتصال تلقائياً إذا انقطع العميل — يمنع خطأ Cannot send requests while disconnected"""
    try:
        if client is None:
            return False
        if not client.is_connected():
            await client.connect()
        return client.is_connected() and await client.is_user_authorized()
    except Exception as e:
        logger.warning(f"⚠️ فشل إعادة الاتصال: {str(e)[:80]}")
        return False

async def resolve_fwd_group(text_input):
    """تحليل إدخال القروب (ID أو @username أو رابط t.me أو رابط دعوة خاص) → (gid, title, err)"""
    raw = text_input.strip()
    # 1) رابط دعوة خاص (t.me/+hash أو t.me/joinchat/hash)
    m = re.match(r'^(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)', raw)
    if m:
        try:
            inv = await bot(CheckChatInviteRequest(m.group(1)))
            if isinstance(inv, ChatInviteAlready):
                chat = inv.chat
                gid = int(getattr(chat, 'chat_id', None) or chat.id)
                title = getattr(chat, 'title', None) or str(gid)
                return gid, title, None
            return None, None, "البوت ليس عضواً في هذا القروب الخاص — أضف البوت إلى القروب أولاً ثم أعد المحاولة."
        except Exception as e:
            return None, None, f"تعذر التحقق من رابط الدعوة: {str(e)[:100]} — أضف البوت إلى القروب أولاً."
    # 2) معرّف رقمي
    if re.match(r'^-?\d+$', raw):
        try:
            entity = await bot.get_entity(int(raw))
            gid = int(entity.chat_id if hasattr(entity, 'chat_id') and entity.chat_id else entity.id)
            title = getattr(entity, 'title', None) or str(gid)
            return gid, title, None
        except Exception:
            # نقبل المعرّف حتى لو تعذّر جلب العنوان — قد يُضاف البوت إلى القروب لاحقاً
            try:
                return int(raw), str(raw), None
            except ValueError:
                return None, None, "معرّف رقمي غير صحيح."
    # 3) @username أو رابط قروب عام
    t_raw = raw.replace('https://t.me/', '@').replace('t.me/', '@').replace('telegram.me/', '@').strip()
    try:
        entity = await bot.get_entity(t_raw)
        gid = int(entity.chat_id if hasattr(entity, 'chat_id') and entity.chat_id else entity.id)
        title = getattr(entity, 'title', None) or str(gid)
        return gid, title, None
    except Exception as e:
        return None, None, f"لم أتمكن من التعرف على القروب: {str(e)[:100]}"

def build_myfwd_screen(user_id, cfg):
    """شاشة (🎯 قروب توجيه رسائلي) — كل مستخدم يرى قروباته وحساباته هو فقط (عزل تام)"""
    mine = get_user_fwd_groups(user_id, cfg)
    owned = get_owned_accounts(user_id)
    if owned:
        acc_line = f"📱 حساباتك المراقبة: **{len(owned)}** — `" + "`, `".join(owned[:8]) + "`"
    else:
        acc_line = "📱 حساباتك المراقبة: لا يوجد — أضف حسابك أولاً من ➕ إضافة حسابي"
    text = (
        "🎯 **قروب توجيه رسائلي**\n\n"
        "اعتمد هنا القروب الذي تريد أن تُوجَّه إليه نسخ رسائل حساباتك الملتقطة — بنفسك وبدون تدخل من الأدمن.\n"
        "🔒 بياناتك وقروباتك تظهر لك أنت فقط، ويمكن لمستخدم آخر اعتماد نفس القروب لحسابه دون أي تداخل.\n\n"
        f"{acc_line}\n"
        f"📦 قروباتك المعتمدة: **{len(mine)}**\n\n"
    )
    rows = []
    for g in mine:
        gid = g['id']
        rows.append([
            Button.inline(f"🗑 إلغاء: {g.get('title', gid)}", f"myfwd_tgl_{gid}".encode()),
            Button.inline('🧪 اختبار', f"myfwd_test_{gid}".encode()),
        ])
    if mine:
        text += "اضغط 🗑 لإلغاء اعتماد القروب، وزر 🧪 اختبار للتأكد من وصول البوت إليه:"
    rows.append([Button.inline('➕ اعتماد قروب جديد', b'myfwd_add')])
    rows.append([Button.inline('🔙 رجوع', b'back_main')])
    return text, rows

def has_perm(user_id, perm):
    """فحص صلاحية مفصلة — الأدمن الكامل يملك جميع الصلاحيات دائماً"""
    if is_full_admin(user_id):
        return True
    config = load_json_config()
    if user_id not in config.get('ADMINS', []):
        return False
    return perm in config.get('ADMIN_PERMISSIONS', {}).get(str(user_id), [])

# ============ الحفظ الدائم للجلسات (StringSession في config على الـ Volume) ============

def save_session_string(phone, client):
    """حفظ جلسة الحساب كسلسلة نصية داخل config.json (المخزن على الـ Volume الدائم)
    — تضمن استعادة الحساب حتى لو فُقد ملف .session عند إعادة النشر"""
    try:
        s = StringSession.save(client.session)
        config = load_json_config()
        sess = config.get('SESSIONS', {})
        sess[str(phone)] = s
        config['SESSIONS'] = sess
        update_json_config(config)
        logger.info(f"💾 تم حفظ جلسة {phone} بشكل دائم (StringSession) — لن تضيع مع إعادة النشر")
    except Exception as e:
        logger.error(f"فشل حفظ StringSession لـ {phone}: {e}")

def forget_session_string(phone):
    """إزالة الجلسة المحفوظة من config عند حذف الحساب"""
    try:
        config = load_json_config()
        sess = config.get('SESSIONS', {})
        _d = _phone_digits(phone)
        _hit = [k for k in sess.keys() if k == str(phone) or (_d and _phone_digits(k) == _d)]
        if _hit:
            for k in _hit:
                sess.pop(k, None)
            config['SESSIONS'] = sess
            update_json_config(config)
    except Exception:
        pass

async def resume_from_string(phone, s):
    """استعادة حساب مراقب من StringSession المحفوظة في config
    وتحويلها لملف جلسة على القرص الدائم للاستخدام اللاحق"""
    disk_client = None
    try:
        tmp = TelegramClient(StringSession(s), API_ID, API_HASH, **CLIENT_OPTS)
        await tmp.connect()
        if not await tmp.is_user_authorized():
            await tmp.disconnect()
            return None
        disk_client = TelegramClient(os.path.join(SESSION_DIR, f'session_{phone}'), API_ID, API_HASH, **CLIENT_OPTS)
        await disk_client.connect()
        disk_client.session.set_dc(tmp.session.dc_id, tmp.session.server_address, tmp.session.port)
        disk_client.session.auth_key = tmp.session.auth_key
        disk_client.session.save()
        await tmp.disconnect()
        if await disk_client.is_user_authorized():
            return disk_client
        await disk_client.disconnect()
        return None
    except Exception as e:
        logger.error(f"فشل استعادة الحساب {phone} من الجلسة المحفوظة: {e}")
        try:
            if disk_client:
                await disk_client.disconnect()
        except Exception:
            pass
        return None

# ============ تخزين مؤقت ============
# message_map: {channel_msg_id: {"group_id": ..., "message_id": ..., "sender_id": ..., "phone": ...}}
message_map = {}
# seen_messages: مجموعة لتتبع الرسائل المعالجة (كشف التكرار)
seen_messages = set()
# عدادات للتشخيص
stats = {
    'messages_received': 0,    # كل الرسائل الواردة من الحسابات المراقبة
    'messages_ignored_filter': 0,  # رُفضت بسبب الفلاتر
    'messages_matched': 0,     # طابقت كلمات مفتاحية
    'messages_forwarded': 0,   # حُوّلت للقناة بنجاح
    'messages_failed': 0,      # فشل تحويلها
    'last_received_at': None,
    'last_matched_at': None,
    'last_forwarded_at': None,
    'last_message_preview': None,
    'last_ignored_reason': None,
}

# ============ دوال الفلترة المتقدمة ============

def is_announcement(text, banned_ads_list):
    """كشف الرسائل الإعلانية بناءً على كلمات مفتاحية محظورة"""
    text_lower = text.lower()
    for kw in banned_ads_list:
        if kw.lower() in text_lower:
            return True
    return False

def contains_link(text):
    """كشف وجود رابط في النص"""
    url_pattern = r'https?://[^\s]+|t\.me/[^\s]+|bit\.ly/[^\s]+|tinyurl\.com/[^\s]+|[a-zA-Z0-9-]+\.(com|net|org|info|xyz|club|online|site|top|ml|tk|cf|ga|gq)[^\s]*'
    return bool(re.search(url_pattern, text))

def contains_phone(text):
    """كشف وجود رقم هاتف"""
    phone_patterns = [
        r'\b0[0-9]{9,10}\b',
        r'\b\+?[0-9]{1,4}[-.]?[0-9]{8,12}\b',
        r'\b[0-9]{3}[-.]?[0-9]{3}[-.]?[0-9]{4}\b',
        r'\b[0-9]{4,5}[-.]?[0-9]{5,6}\b'
    ]
    for pattern in phone_patterns:
        if re.search(pattern, text):
            return True
    return False

def contains_mention(text):
    """كشف وجود معرفات (@username)"""
    mention_pattern = r'@[a-zA-Z0-9_]+'
    return bool(re.search(mention_pattern, text))

def detect_special_links(text):
    """كشف روابط الواتساب وروابط قروبات التلجرام
    يعيد قائمة بأنواع الروابط المكتشفة (مثلاً: ['واتساب', 'قروب تلجرام'])
    """
    found = []
    text_lower = text.lower()
    
    # روابط الواتساب
    whatsapp_patterns = [
        r'wa\.me/[^\s]+',
        r'whatsapp\.com/[^\s]+',
        r'chat\.whatsapp\.com/[^\s]+',
        r'api\.whatsapp\.com/[^\s]+',
    ]
    for pat in whatsapp_patterns:
        if re.search(pat, text_lower):
            found.append('واتساب')
            break
    
    # روابط قروبات التلجرام (دعوات الانضمام)
    telegram_group_patterns = [
        r't\.me/\+[a-zA-Z0-9_-]+',          # t.me/+abc123 (دعوة خاصة)
        r't\.me/joinchat/[a-zA-Z0-9_-]+',   # t.me/joinchat/abc123 (دعوة خاصة)
        r'telegram\.me/\+[a-zA-Z0-9_-]+',
        r'telegram\.me/joinchat/[a-zA-Z0-9_-]+',
    ]
    for pat in telegram_group_patterns:
        if re.search(pat, text_lower):
            found.append('قروب تلجرام')
            break
    
    return found

def is_too_long(text, max_length=50):
    """الرسالة طويلة جداً (أكثر من max_length)"""
    return len(text.strip()) > max_length

def contains_suspicious_words(text, suspicious_words):
    """كشف الكلمات المشبوهة"""
    text_lower = text.lower()
    for word in suspicious_words:
        if word.lower() in text_lower:
            return True
    return False

def should_ignore_message(message_text, config):
    """تطبيق جميع شروط التجاهل"""
    ignore_reasons = []
    
    # افتراضيات متساهلة (السماح بكل شيء) — تُستخدم فقط لو ما فيه FILTERS في config
    filters = config.get('FILTERS', {
        'max_length': 0,
        'block_links': False,
        'block_phones': False,
        'block_mentions': False,
        'block_ads': False,
        'block_suspicious': False
    })
    
    banned_ads = config.get('BANNED_ADS', [])
    suspicious_words = config.get('SUSPICIOUS_WORDS', [])
    
    if filters.get('max_length', 0) > 0:
        max_len = filters.get('max_length', 0)
        if is_too_long(message_text, max_len):
            ignore_reasons.append(f"تجاوز {max_len} حرفاً ({len(message_text.strip())} حرف)")
    
    if filters.get('block_links', False) and contains_link(message_text):
        ignore_reasons.append("يحتوي على رابط")
    
    if filters.get('block_phones', False) and contains_phone(message_text):
        ignore_reasons.append("يحتوي على رقم هاتف")
    
    if filters.get('block_mentions', False) and contains_mention(message_text):
        ignore_reasons.append("يحتوي على معرف @")
    
    if filters.get('block_ads', False) and banned_ads and is_announcement(message_text, banned_ads):
        ignore_reasons.append("رسالة إعلانية (كلمة محظورة)")
    
    if filters.get('block_suspicious', False) and suspicious_words and contains_suspicious_words(message_text, suspicious_words):
        ignore_reasons.append("يحتوي على كلمات مشبوهة")
    
    return ignore_reasons

async def import_groups(client):
    """استيراد كافة المجموعات التي ينتمي إليها الحساب (للعرض فقط، لا تستخدم في التصفية)"""
    config = load_json_config()
    current_groups = config.get('TARGET_GROUPS', [])
    new_groups_count = 0
    
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            if dialog.id not in current_groups:
                current_groups.append(dialog.id)
                new_groups_count += 1
    
    config['TARGET_GROUPS'] = current_groups
    update_json_config(config)
    return new_groups_count

async def export_group_links(client, phone, target_id=None, status_msg=None):
    """جمع جميع القروبات/القنوات التي ينتمي إليها الحساب مع روابطها
    وإرسالها كملف نصي مباشرة للطالب — مع إشعار مضمون عند النجاح أو الفشل (لا صمت أبداً)"""
    if target_id is None:
        target_id = MAIN_ADMIN_ID
    file_path = None
    try:
        # التأكد من اتصال الحساب وأهليته قبل أي شيء
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            try:
                await bot.send_message(target_id, f"❌ الحساب `{phone}` غير مصرح أو غير متصل حالياً — أعد إضافته من ➕ إضافة حساب.")
            except Exception:
                pass
            return

        lines = [
            "📋 تقرير قروبات الحساب",
            "=" * 40,
            f"📱 الحساب: {phone}",
            f"🕐 التاريخ: {time.strftime('%Y-%m-%d %H:%M')}",
            "",
        ]
        count = 0
        unavailable = 0
        # جمع القروبات أولاً ثم المعالجة (لعرض تقدم دقيق)
        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                dialogs.append(dialog)
        total = len(dialogs)
        for idx, dialog in enumerate(dialogs, 1):
            entity = dialog.entity
            title = getattr(entity, 'title', None) or 'بدون اسم'
            username = getattr(entity, 'username', None)
            members = getattr(entity, 'participants_count', None)
            if username:
                link = f"https://t.me/{username}"
            else:
                try:
                    invite = await asyncio.wait_for(client(ExportChatInviteRequest(dialog.id)), timeout=10)
                    link = invite.link
                except asyncio.TimeoutError:
                    unavailable += 1
                    link = "🔒 رابط غير متاح (انتهت مهلة الاستخراج)"
                except Exception:
                    unavailable += 1
                    link = "🔒 رابط غير متاح (قروب خاص ولست مشرفاً فيه)"
            count += 1
            lines.append(f"{count}. {title}")
            lines.append(f"   الرابط: {link}")
            if members:
                lines.append(f"   الأعضاء: {members}")
            lines.append("")
            # تحديث رسالة الحالة كل 5 قروبات (تغذية راجعة حية)
            if status_msg is not None and idx % 5 == 0:
                try:
                    await status_msg.edit(f"⏳ جاري جمع الروابط... {idx}/{total} قروب")
                except Exception:
                    pass

        if count == 0:
            try:
                await bot.send_message(target_id, f"⚠️ الحساب `{phone}` ليس عضواً في أي قروبات أو قنوات.")
            except Exception:
                pass
            return

        safe_phone = str(phone).replace('+', '').replace(':', '').replace('/', '_')
        file_path = os.path.join(DATA_DIR, f"group_links_{safe_phone}.txt")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        try:
            await bot.send_file(
                target_id,
                file_path,
                caption=(f"📋 **روابط قروبات الحساب** `{phone}`\n"
                         f"📊 عدد القروبات/القنوات: **{count}**\n"
                         f"🔒 روابط غير متاحة: {unavailable}\n\n"
                         f"🔗 الروابط العامة + الخاصة (التي يستطيع الحساب استخراجها)")
            )
            logger.info(f"📋 تم إرسال تقرير روابط القروبات ({count} قروب) للحساب {phone} → {target_id}")
        except Exception as send_err:
            # بديل نصي إذا فشل إرسال الملف — لا يبقى الطالب بلا جواب
            logger.error(f"فشل إرسال ملف الروابط: {send_err} — جاري الإرسال نصياً")
            body = "\n".join(lines)
            for start in range(0, len(body), 3500):
                await bot.send_message(target_id, body[start:start + 3500])
                await asyncio.sleep(0.2)
    except Exception as e:
        logger.error(f"خطأ في تصدير روابط قروبات الحساب {phone}: {e}")
        # إشعار مضمون بالفشل — المستخدم لن يبقى منتظراً بلا جواب
        try:
            await bot.send_message(
                target_id,
                f"❌ فشل جمع تقرير روابط الحساب `{phone}`.\n"
                f"السبب: `{str(e)[:150]}`\n\n"
                f"💡 جرّب مرة أخرى، وإن تكرر فأعد إضافة الحساب."
            )
        except Exception:
            pass
    finally:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

# ============ سجلات الدردشة (تخزين + حذف تلقائي + إحصاءات) ============

def log_chat_entry(phone, chat_id, chat_title, sender_id, sender_name, text, targets):
    """تسجيل سطر في سجل الدردشة (jsonl) — يُحذف تلقائياً بعد مدة الاحتفاظ"""
    try:
        entry = {
            'ts': time.time(),
            'phone': phone,
            'chat_id': chat_id,
            'chat_title': chat_title,
            'sender_id': sender_id,
            'sender_name': sender_name or '',
            'text': (text or '')[:2000],
            'targets': targets,
        }
        with open(CHAT_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error(f"خطأ في تسجيل سجل الدردشة: {e}")

def get_logs_stats():
    """إحصاءات السجلات: (عدد الأسطر، أقدم طابع زمني، الحجم بالبايت)"""
    count, oldest, size = 0, None, 0
    if os.path.exists(CHAT_LOG_FILE):
        size = os.path.getsize(CHAT_LOG_FILE)
        try:
            with open(CHAT_LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    count += 1
                    if oldest is None:
                        try:
                            oldest = json.loads(line).get('ts')
                        except Exception:
                            pass
        except Exception:
            pass
    return count, oldest, size

def fmt_size(nbytes):
    """تنسيق حجم بالبايت لعرض مقروء"""
    if nbytes >= 1048576:
        return f"{nbytes / 1048576:.2f} ميجابايت"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.1f} كيلوبايت"
    return f"{nbytes} بايت"

async def logs_cleanup_task():
    """مهمة دورية: حذف سجلات الدردشة الأقدم من مدة الاحتفاظ (الافتراضي 3 أيام)
    + تنظيف ذاكرة كشف التكرار وخريطة الرسائل ليبقى البوت سريعاً ومرناً"""
    while True:
        try:
            config = load_json_config()
            try:
                days = max(1, int(config.get('LOG_RETENTION_DAYS', 3)))
            except (TypeError, ValueError):
                days = 3
            cutoff = time.time() - days * 86400
            removed = 0
            if os.path.exists(CHAT_LOG_FILE):
                kept_lines = []
                with open(CHAT_LOG_FILE, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ts = json.loads(line).get('ts', 0)
                        except Exception:
                            ts = 0
                        if ts >= cutoff:
                            kept_lines.append(line)
                        else:
                            removed += 1
                if removed:
                    tmp_file = CHAT_LOG_FILE + '.tmp'
                    with open(tmp_file, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(kept_lines) + ('\n' if kept_lines else ''))
                    os.replace(tmp_file, CHAT_LOG_FILE)
                    logger.info(f"🗑 تم حذف {removed} سجل دردشة أقدم من {days} يوم/أيام (تنظيف تلقائي)")

            # تنظيف ذاكرة الرسائل المعالجة — يمنع تضخم الذاكرة مع الوقت
            now = time.time()
            stale = [mid for mid, info in message_map.items() if now - info.get('timestamp', now) > 172800]
            for mid in stale:
                message_map.pop(mid, None)
            if len(seen_messages) > 60000:
                seen_messages.clear()
                logger.info("🧹 تم تنظيف ذاكرة كشف التكرار (حماية من تضخم الذاكرة)")
        except Exception as e:
            logger.error(f"خطأ في مهمة تنظيف السجلات: {e}")
        await asyncio.sleep(3600)

# ============ الحذف التلقائي ============

async def auto_delete_task():
    """مهمة خلفية لحذف الرسائل المحولة من القناة بعد مرور عدد ساعات محدد"""
    global message_map
    while True:
        try:
            config = load_json_config()
            auto_delete_hours = config.get('AUTO_DELETE_HOURS', 0)
            if auto_delete_hours > 0:
                current_time = time.time()
                to_delete = []
                for msg_id, info in list(message_map.items()):
                    msg_time = info.get('timestamp', 0)
                    if current_time - msg_time >= auto_delete_hours * 3600:
                        to_delete.append(msg_id)
                
                for msg_id in to_delete:
                    try:
                        await bot.delete_messages(CHANNEL_ID, msg_id)
                        del message_map[msg_id]
                        logger.info(f"تم حذف الرسالة {msg_id} من القناة تلقائياً")
                    except Exception as e:
                        logger.error(f"خطأ في حذف الرسالة {msg_id}: {e}")
        except Exception as e:
            logger.error(f"خطأ في مهمة الحذف التلقائي: {e}")
        
        await asyncio.sleep(300)

# ============ دالة معالجة الرسائل الموحدة ============

async def process_message(event, client, phone):
    """معالجة الرسالة الواردة من أي حساب مراقب"""
    global message_map, seen_messages, stats
    config = load_json_config()
    # 🔑 كلمات مفتاحية فعالة لمالك الحساب: الافتراضية (🌟 عدا ما أخفاها بنفسه) + الخاصة به + العامة (خصوصية بين المستخدمين)
    _owner_id = find_account_owner(phone)
    if _owner_id is not None:
        keywords = get_effective_keywords(_owner_id, config)
    else:
        # حساب بلا مالك مسجل → الافتراضية فقط (لا كلمات خاصة لأحد)
        keywords = list(dict.fromkeys(get_default_keywords(config) + config.get('KEYWORDS', [])))
    ignore_users = config.get('IGNORE_USERS', [])
    
    # تجاهل الرسائل الخاصة (DM) - نراقب فقط القروبات والقنوات
    if event.is_private:
        return
    
    # ===== منع حلقة التكرار اللانهائية =====
    # 1) لا نراقب القناة المخصصة للتحويل (CHANNEL_ID) أبداً
    #    لأن البوت يحوّل إليها، فيجب ألا يلتقط رسائله المحوّلة مجدداً
    try:
        current_channel_id = int(os.environ.get('CHANNEL_ID', 0))
    except (ValueError, TypeError):
        current_channel_id = 0
    
    if current_channel_id and event.chat_id == current_channel_id:
        return  # تجاهل صامت — ما نسجل حتى إحصائية
    
    # 2) لا نراقب رسائل البوت نفسه (sender_id == bot id) أو أي رسالة تبدأ بصيغة تقرير البوت
    #    (حتى لو وصلت من قناة/قروب ثاني عبر إعادة توجيه)
    
    sender_id = event.sender_id
    if sender_id in ignore_users:
        return
    
    # دعم الرسائل النصية + الكابشن (للصور والملفات)
    message_text = ""
    if event.message and event.message.message:
        message_text = event.message.message
    elif event.message and hasattr(event.message, 'caption') and event.message.caption:
        message_text = event.message.caption
    
    if not message_text:
        return  # ما فيه نص نراقبه
    
    # ===== منع حلقة التكرار: تجاهل رسائل البوت نفسه =====
    # نتعرف على رسائل البوت بمحتواها (تبدأ بنمط تقرير البوت)
    bot_signature = "📢 **تم العثور على رسالة مطابقة"
    if message_text.startswith(bot_signature) or message_text.startswith("📢 تم العثور على رسالة مطابقة"):
        # رسالة محوّلة من البوت نفسه — تجاهل لمنع التكرار اللانهائي
        logger.info(f"🚫 تم تجاهل رسالة محوّلة من البوت (منع حلقة التكرار) - phone={phone}")
        return
    
    # ===== عداد: رسالة واردة =====
    stats['messages_received'] += 1
    stats['last_received_at'] = time.time()
    stats['last_message_preview'] = message_text[:80]
    
    ignore_reasons = should_ignore_message(message_text, config)
    
    if ignore_reasons:
        stats['messages_ignored_filter'] += 1
        stats['last_ignored_reason'] = ', '.join(ignore_reasons)
        logger.warning(f"⛔ تم تجاهل رسالة من {phone} في القروب {event.chat_id}: {', '.join(ignore_reasons)} | نص الرسالة: {message_text[:80]}")
        return
    
    # ===== كشف التكرار المحسّن =====
    # 1) مفتاح أساسي: chat_id + message_id + sender_id (للرسائل الواحدة)
    # 2) مفتاح ثانوي: hash نص الرسالة (للرسائل المتطابقة من مصادر مختلفة)
    if config.get('DUPLICATE_DETECTION', True):
        msg_key = f"{event.chat_id}_{event.id}_{sender_id}"
        if msg_key in seen_messages:
            logger.info(f"تم تجاهل رسالة مكررة (نفس msg_id) من {phone}")
            return
        seen_messages.add(msg_key)
        
        # كشف التكرار بالنص: لو نفس النص وصل خلال آخر 60 ثانية، تجاهل
        text_hash = hash(message_text.strip().lower())
        text_key = f"text_{text_hash}"
        if text_key in seen_messages:
            logger.info(f"تم تجاهل رسالة مكررة (نفس النص) من {phone}: {message_text[:50]}")
            return
        seen_messages.add(text_key)
    
    # التحقق من الكلمات المفتاحية — 🔎 مطابقة الكلمة كوحدة مستقلة كاملة: "احد" تُلتقط من «يحل احد» ولا تُلتقط من «الاحد/احدى/احدث» (مع توحيد التشكيل والهمزات)
    _msg_norm = normalize_ar_text(message_text)
    matched_keywords = [kw for kw in keywords if keyword_in_text_norm(_msg_norm, kw)]
    
    # كشف الروابط الخاصة (واتساب + قروبات تلجرام) — تُحوّل حتى لو ما طابقت كلمة مفتاحية
    detected_links = []
    if config.get('DETECT_LINKS', True):
        detected_links = detect_special_links(message_text)
    
    # لازم تطابق كلمة مفتاحية OR تحتوي على رابط خاص
    if not matched_keywords and not detected_links:
        return  # ما فيها كلمة مفتاحية ولا رابط خاص
    
    stats['messages_matched'] += 1
    stats['last_matched_at'] = time.time()
    
    # تجهيز وصف المطابقة للـ logs
    match_desc_parts = []
    if matched_keywords:
        match_desc_parts.append(f"كلمات: {matched_keywords}")
    if detected_links:
        match_desc_parts.append(f"روابط: {detected_links}")
    logger.info(f"🎯 مطابقة! من {phone} | {' | '.join(match_desc_parts)} | نص: {message_text[:60]}")
    
    try:
        chat = await event.get_chat()
        chat_title = getattr(chat, 'title', 'مجموعة غير معروفة')
        
        # الحصول على اسم المرسل
        sender_name = ""
        try:
            sender = await event.get_sender()
            if sender:
                if getattr(sender, 'first_name', None):
                    sender_name = sender.first_name
                    if getattr(sender, 'last_name', None):
                        sender_name += f" {sender.last_name}"
                if getattr(sender, 'username', None):
                    sender_name += f" (@{sender.username})"
        except:
            pass
        
        # بناء رابط الرسالة المباشر للقروب
        msg_url = ""
        if event.chat:
            if getattr(event.chat, 'username', None):
                # قروب عام — رابط مباشر يفتح للجميع
                msg_url = f"https://t.me/{event.chat.username}/{event.id}"
            else:
                # قروب خاص — رابط مباشر يفتح للأعضاء فقط
                c_id = str(event.chat_id).replace('-100', '')
                msg_url = f"https://t.me/c/{c_id}/{event.id}"
        
        # تجهيز وصف سبب المطابقة
        match_reason_parts = []
        if matched_keywords:
            match_reason_parts.append(f"🔑 الكلمة: `{matched_keywords[0]}`" + (f" (+{len(matched_keywords)-1} أخرى)" if len(matched_keywords) > 1 else ""))
        if detected_links:
            link_types = '، '.join(detected_links)
            match_reason_parts.append(f"🔗 رابط: {link_types}")
        match_reason = '\n'.join(match_reason_parts)
        
        # ===== عرض الحساب المراقب الذي جاءت منه الرسالة =====
        forward_text = (
            f"📢 **تم العثور على رسالة مطابقة!**\n\n"
            f"👥 **المجموعة:** {chat_title}\n"
            f"👤 **المرسل:** {sender_name if sender_name else 'مستخدم'} (`{sender_id}`)\n"
            f"📱 **الحساب المراقب:** `{phone}`\n"
            f"{match_reason}\n"
            f"📝 **الرسالة:**\n{message_text}\n"
        )
        
        # ===== أزرار الرد المنفصلة + زر إضافة رد مباشر =====
        all_buttons = []
        
        # صف أزرار الرد (خاص + قروب) — متاح دائماً: كل مستخدم يرد بقوالبه الخاصة هو
        reply_row = [
            Button.inline("💬 رد خاص", f"dm_reply_{event.chat_id}_{event.id}_{sender_id}".encode()),
            Button.inline("👥 رد قروب", f"grp_reply_{event.chat_id}_{event.id}_{sender_id}".encode()),
        ]
        all_buttons.append(reply_row)
        
        # صف زر إضافة رد مباشر من القناة
        add_reply_row = [
            Button.inline("➕ إضافة رد خاص", f"add_dm_from_ch_{event.chat_id}_{event.id}_{sender_id}".encode()),
            Button.inline("➕ إضافة رد قروب", f"add_grp_from_ch_{event.chat_id}_{event.id}_{sender_id}".encode())
        ]
        all_buttons.append(add_reply_row)
        
        # زر فتح الرسالة — رابط مباشر يفتح الرسالة في تيليجرام فوراً
        # - قروب عام: يفتح للجميع (مع أو بدون عضوية)
        # - قروب خاص: يفتح للأعضاء فقط (تيليجرام لا يسمح بفتح رسائل القروبات الخاصة لغير الأعضاء)
        if msg_url:
            all_buttons.append([Button.url("🔗 فتح الرسالة", url=msg_url)])
        
        sent_msg = await bot.send_message(CHANNEL_ID, forward_text, buttons=all_buttons if all_buttons else None)
        
        # حفظ بيانات الرسالة للرد لاحقاً
        message_map[sent_msg.id] = {
            "group_id": event.chat_id,
            "message_id": event.id,
            "sender_id": sender_id,
            "phone": phone,
            "timestamp": time.time()
        }
        
        stats['messages_forwarded'] += 1
        stats['last_forwarded_at'] = time.time()
        logger.info(f"✅ تم توجيه رسالة من الحساب {phone} في المجموعة {chat_title} → القناة")
        
        # ===== نسخة تلقائية لقروب الأدمن الرئيسي (النسخ الشامل) =====
        admin_group = config.get('ADMIN_GROUP_ID', 0)
        if admin_group and admin_group != CHANNEL_ID and admin_group != event.chat_id:
            try:
                await bot.send_message(admin_group, forward_text, buttons=all_buttons if all_buttons else None)
                logger.info(f"📤 تم إرسال نسخة من رسالة {phone} إلى قروب الأدمن الرئيسي")
            except Exception as ae:
                logger.warning(f"⚠️ فشل إرسال النسخة لقروب الأدمن الرئيسي: {str(ae)[:100]}")
        
        # ===== النسخ لقروبات التوجيه المعتمدة (عزل تام بين المستخدمين) =====
        # رسائل كل حساب تُوجَّه فقط إلى: قروبات الحساب المعتمدة (ACCOUNT_GROUPS) + قروبات مالك الحساب نفسه (USER_GROUPS[المالك])
        # — رسائل مستخدم لا تصل أبداً إلى قروبات مستخدم آخر
        extra_targets = []
        try:
            for gid in config.get('ACCOUNT_GROUPS', {}).get(str(phone), []):
                if gid not in extra_targets:
                    extra_targets.append(gid)
            owner_id = find_account_owner(phone)
            if owner_id is not None:
                for gid in config.get('USER_GROUPS', {}).get(str(owner_id), []):
                    if gid not in extra_targets:
                        extra_targets.append(gid)
        except Exception as me:
            logger.warning(f"⚠️ خطأ في جمع قروبات الاعتماد: {str(me)[:100]}")
        for gid in extra_targets:
            # تجنّب التكرار: لا نرسل للقناة الرئيسية أو قروب الأدمن أو القروب المصدر أو قروب استقبال كل الرسائل
            if gid in (CHANNEL_ID, admin_group, event.chat_id):
                continue
            try:
                await bot.send_message(gid, forward_text, buttons=all_buttons if all_buttons else None)
                logger.info(f"📩 تم إرسال نسخة من رسالة {phone} إلى قروب معتمد {gid}")
            except Exception as fe:
                logger.warning(f"⚠️ فشل إرسال النسخة للقروب المعتمد {gid}: {str(fe)[:100]}")
        
        # ===== تسجيل سجل الدردشة (يُحذف تلقائياً بعد مدة الاحتفاظ) =====
        log_targets = [CHANNEL_ID]
        if admin_group:
            log_targets.append(admin_group)
        log_targets.extend(extra_targets)
        log_chat_entry(phone, event.chat_id, chat_title, sender_id, sender_name, message_text, log_targets)
        
        # ===== الرد التلقائي بالقروب =====
        auto_reply_settings = config.get('AUTO_REPLY_SETTINGS', {})
        for kw in matched_keywords:
            if kw in auto_reply_settings:
                try:
                    # 🔌 التأكد من الاتصال قبل الإرسال — يمنع خطأ Cannot send requests while disconnected
                    if await ensure_connected(client):
                        await client.send_message(
                            event.chat_id,
                            auto_reply_settings[kw],
                            reply_to=event.id
                        )
                    else:
                        logger.warning(f"⚠️ تعذر إرسال الرد التلقائي — الحساب {phone} غير متصل")
                    logger.info(f"تم إرسال رد تلقائي للكلمة '{kw}' في القروب")
                except Exception as e:
                    logger.error(f"خطأ في إرسال الرد التلقائي: {e}")
                break
        
    except Exception as e:
        stats['messages_failed'] += 1
        logger.error(f"خطأ في توجيه الرسالة من {phone}: {e}")

# ============ تسجيل معالج الرسائل وتشغيل المراقبة ============

def register_handler(client, phone):
    """تسجيل معالج الرسائل لحساب معين - يتم استدعاؤه مرة واحدة لكل حساب"""
    @client.on(events.NewMessage())
    async def message_handler(event):
        await process_message(event, client, phone)
    
    logger.info(f"✅ تم تسجيل معالج الرسائل للحساب {phone}")
    return message_handler

async def start_monitoring(client, phone):
    """بدء مراقبة الاتصال للحساب - مع إعادة الاتصال التلقائي
    
    ملاحظة: يجب استدعاء register_handler(client, phone) قبل هذه الدالة
    """
    
    logger.info(f"🔄 بدء حلقة المراقبة للحساب {phone}")
    
    # حلقة مراقبة مع إعادة اتصال تلقائية
    while True:
        try:
            # التأكد من أن العميل متصل
            if not client.is_connected():
                logger.warning(f"⚠️ الحساب {phone} غير متصل، جاري إعادة الاتصال...")
                try:
                    await client.connect()
                    if await client.is_user_authorized():
                        logger.info(f"✅ تم إعادة اتصال الحساب {phone} بنجاح")
                    else:
                        logger.error(f"❌ الحساب {phone} غير مصرح، لا يمكن إعادة الاتصال")
                        break
                except Exception as e:
                    logger.error(f"❌ فشل إعادة اتصال الحساب {phone}: {e}")
                    await asyncio.sleep(15)
                    continue
            
            # إرسال إشارة بقاء
            try:
                await client.get_me()
                logger.info(f"✅ الحساب {phone} متصل ويعمل")
            except Exception as e:
                logger.error(f"⚠️ فشل فحص اتصال الحساب {phone}: {e}")
                await asyncio.sleep(10)
                continue
            
            # انتظار انقطاع الاتصال
            try:
                await client.disconnected
            except Exception as e:
                logger.error(f"خطأ في انتظار اتصال الحساب {phone}: {e}")
            
            # إذا وصلنا هنا يعني أن الاتصال انقطع
            logger.warning(f"⚠️ انقطع اتصال الحساب {phone}، جاري إعادة الاتصال...")
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"❌ خطأ غير متوقع في مراقبة الحساب {phone}: {e}")
            await asyncio.sleep(10)

async def setup_bot_handlers():
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        user_id = event.sender_id
        # ===== فحص صلاحية الأدمن =====
        if not is_admin(user_id):
            await event.respond(UNAUTHORIZED_MSG)
            logger.warning(f"🚫 محاولة استخدام غير مصرح بها من user_id={user_id}")
            return
        
        # ===== بناء القائمة حسب الصلاحيات المفصلة (الأدمن يتحكم بالأزرار التي تظهر لكل مستخدم) =====
        # إضافة حساب متاحة لجميع مستخدمي البوت (أدمن ومستخدم) — كل مستخدم يضيف حسابه الخاص دون خطأ
        owned_accounts = get_owned_accounts(user_id)
        buttons = []
        if has_perm(user_id, 'add_accounts'):
            buttons.append([Button.inline('➕ إضافة حساب', b'add_acc'), Button.inline('❌ حذف حساب', b'rem_acc')])
        else:
            buttons.append([Button.inline('➕ إضافة حسابي', b'add_acc')])
            if owned_accounts:
                buttons.append([Button.inline('❌ حذف حسابي', b'rem_acc')])
        # 🎯 اعتماد قروب التوجيه ذاتياً — متاح لكل مستخدم (بدون تدخل الأدمن) وبعزل تام بين المستخدمين
        buttons.append([Button.inline('🎯 قروب توجيه رسائلي', b'myfwd')])
        if has_perm(user_id, 'view_stats') or owned_accounts:
            buttons.append([Button.inline('📋 الحسابات المرتبطة', b'list_acc')])
        if is_full_admin(user_id):
            buttons.append([Button.inline('👥 مراقبة المستخدمين (أدمن)', b'users_monitor')])
        # 🔑💬 خصوصية لكل مستخدم: كلماته المفتاحية وردوده الخاصة به هو فقط — يضيفها بنفسه
        buttons.append([Button.inline('🔑 كلماتي المفتاحية', b'manage_kw'), Button.inline('💬 ردودي على الخاص', b'manage_dm_templates')])
        buttons.append([Button.inline('👥 ردودي في القروب', b'manage_grp_templates')])
        if is_full_admin(user_id):
            buttons.append([Button.inline('🚫 قائمة التجاهل', b'manage_ignore')])
        if has_perm(user_id, 'manage_filters'):
            buttons.append([Button.inline('🛡️ كلمات محظورة ومشبوهة', b'manage_banned'), Button.inline('⚙️ إعدادات الفلترة', b'manage_filters')])
        if has_perm(user_id, 'manage_auto'):
            buttons.append([Button.inline('📨 الرد التلقائي', b'manage_auto_reply'), Button.inline('🔄 التكرار والحذف التلقائي', b'manage_advanced')])
        if has_perm(user_id, 'manage_groups') or has_perm(user_id, 'export_links'):
            buttons.append([Button.inline('👥 المجموعات وروابط القروبات', b'manage_groups')])
        if is_full_admin(user_id):
            buttons.append([Button.inline('✅ اعتمادات قروبات الجميع (أدمن)', b'approve_groups')])
        if has_perm(user_id, 'add_admins'):
            buttons.append([Button.inline('👑 إدارة المشرفين والأعضاء', b'manage_admins')])
        if has_perm(user_id, 'broadcast'):
            buttons.append([Button.inline('📢 إذاعة رسالة لجميع المستخدمين', b'broadcast_btn')])
        if is_main_admin(user_id):
            buttons.append([Button.inline('📤 قروب استقبال كل الرسائل', b'admingroup')])
        if is_full_admin(user_id):
            buttons.append([Button.inline('💾 السجلات والتخزين', b'logs_menu')])
        
        # ترحيب مخصص حسب نوع الأدمن
        if is_main_admin(user_id):
            welcome = ("👑 **أهلاً بك أيها الأدمن الرئيسي!**\n\n"
                       "🛠 تحكم كامل في حسابات المراقبة والإعدادات.\n\n"
                       "📤 **قروب النسخ الشامل:** أضف البوت إلى قروبك الخاص ثم أرسل `/mygroup` هناك "
                       "لتصلك نسخة من كل رسالة تُوجّه للمشتركين.")
        else:
            role_label = ROLES.get(get_user_role(user_id), '👤 مستخدم')
            perms_desc = [desc for key, desc in PERMISSIONS.items() if has_perm(user_id, key)]
            welcome = (f"👋 **أهلاً بك!** — رتبتك: {role_label}\n\n🛠 الأزرار المتاحة لك (يحددها الأدمن):\n"
                       + ("\n".join([f"✅ {d}" for d in perms_desc])
                          if perms_desc else "⚠️ لا توجد صلاحيات مفعلّة بعد — تواصل مع الأدمن الرئيسي.")
                       + "\n\n🎯 اعتمد **قروب توجيه رسائلك** بنفسك من زر (🎯 قروب توجيه رسائلي) دون تدخل أحد."
                         "\n🔒 حساباتك وقروباتك وبياناتك تظهر لك أنت فقط — كل مستخدم منفصل تماماً عن غيره.")
        
        await event.respond(welcome, buttons=buttons)

    # ============ أوامر تعيين قروب الأدمن الرئيسي (النسخ الشامل) ============
    
    @bot.on(events.NewMessage(pattern=r'^/mygroup$'))
    async def mygroup_handler(event):
        """تعيين القروب الحالي كقروب الأدمن الرئيسي لاستقبال نسخة من كل الرسائل الموجهة"""
        if not is_main_admin(event.sender_id):
            await event.respond("🚫 هذا الأمر للأدمن الرئيسي فقط.")
            return
        if event.is_private:
            await event.respond("ℹ️ أضف البوت إلى **قروبك الخاص** ثم أرسل `/mygroup` **داخل القروب** لتعيينه.")
            return
        config = load_json_config()
        config['ADMIN_GROUP_ID'] = event.chat_id
        update_json_config(config)
        await event.respond(
            "✅ **تم تعيين هذا القروب كمستقبل النسخ الشامل!**\n\n"
            "📨 كل رسالة تُوجّه لقروبات المشتركين ستُرسل نسخة منها هنا أيضاً.\n"
            "❌ للإلغاء أرسل: /unmygroup"
        )
        logger.info(f"📤 تم تعيين قروب الأدمن الرئيسي للنسخ الشامل: {event.chat_id}")

    @bot.on(events.NewMessage(pattern=r'^/unmygroup$'))
    async def unmygroup_handler(event):
        """إلغاء قروب النسخ الشامل"""
        if not is_main_admin(event.sender_id):
            return
        config = load_json_config()
        config['ADMIN_GROUP_ID'] = 0
        update_json_config(config)
        await event.respond("✅ تم إلغاء قروب النسخ الشامل — لن تُرسل نسخ من الرسائل لقروبك.")
        logger.info("📤 تم إلغاء قروب النسخ الشامل")

    # دالة بناء شاشة الرتب والصلاحيات (تُستخدم في عدة أماكن)
    def build_perm_screen(admin_id_str, cfg):
        """شاشة رتبة وصلاحيات مستخدم محدد — الأدمن يتحكم بالأزرار التي تظهر له"""
        perms_map = cfg.get('ADMIN_PERMISSIONS', {})
        roles_map = cfg.get('ADMIN_ROLES', {})
        role = roles_map.get(admin_id_str, 'user')
        if role == 'supervisor':
            role = 'user'
        granted = perms_map.get(admin_id_str, [])
        rows = [
            [Button.inline(('✅' if role == 'admin' else '⬜') + ' رتبة أدمن — كل الصلاحيات', f"setrole_admin_{admin_id_str}".encode()),
             Button.inline(('✅' if role == 'user' else '⬜') + ' رتبة مستخدم', f"setrole_user_{admin_id_str}".encode())],
        ]
        for key, desc in PERMISSIONS.items():
            mark = '✅' if (key in granted or role == 'admin') else '❌'
            rows.append([Button.inline(f"{mark} {desc}", f"tgl_{key}_{admin_id_str}".encode())])
        rows.append([Button.inline('🔙 رجوع للقائمة', b'perm_admins')])
        note = "\n⚠️ رتبة (أدمن) تملك كل الصلاحيات تلقائياً." if role == 'admin' else ""
        text = (f"🎛 **رتبة وصلاحيات `{admin_id_str}`**{note}\n\n"
                "🏷 رتبتان فقط: **أدمن** (كل الصلاحيات) و**مستخدم** (يضيف حسابه ويستخدم ما تسمح به صلاحياته)\n"
                "💡 كل صلاحية = زر في قائمته:")
        return text, rows

    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data
        
        # ===== فحص صلاحية الأدمن لكل عملية =====
        if not is_admin(user_id):
            await event.answer("📢 لطلب التفعيل تواصل مع: @ppppokl أو @drpharmacistgg", alert=True)
            logger.warning(f"🚫 محاولة callback غير مصرح بها من user_id={user_id}, data={data}")
            return
        
        config = load_json_config()
        
        # ===== بوابة الصلاحيات المفصلة (كل زر مرتبط بصلاحية محددة — الأدمن يتحكم بها) =====
        data_str = data.decode('utf-8', errors='ignore')
        CB_PERM_MAP = {
            'manage_ignore': 'manage_keywords', 'add_ignore': 'manage_keywords', 'rem_ignore': 'manage_keywords',
            'manage_banned': 'manage_filters', 'add_banned_ad': 'manage_filters', 'rem_banned_ad': 'manage_filters',
            'manage_filters': 'manage_filters', 'set_max_length': 'manage_filters', 'reset_filters': 'manage_filters',
            'toggle_links': 'manage_filters', 'toggle_phones': 'manage_filters', 'toggle_mentions': 'manage_filters',
            'toggle_ads': 'manage_filters', 'toggle_suspicious': 'manage_filters',
            'manage_dm_templates': 'manage_templates', 'add_dm_template': 'manage_templates', 'rem_dm_template': 'manage_templates',
            'manage_grp_templates': 'manage_templates', 'add_grp_template': 'manage_templates', 'rem_grp_template': 'manage_templates',
            'manage_auto_reply': 'manage_auto', 'add_auto_reply': 'manage_auto', 'rem_auto_reply': 'manage_auto',
            'manage_advanced': 'manage_auto', 'toggle_duplicate': 'manage_auto', 'set_auto_delete': 'manage_auto',
            'approve_groups': 'manage_groups', 'add_fwd_group_btn': 'manage_groups',
            'add_group': 'manage_groups', 'rem_group': 'manage_groups',
            'broadcast_btn': 'broadcast',
        }
        CB_PERM_PREFIX = {
            'del_banned_ad_': 'manage_filters', 'del_suspicious_': 'manage_filters',
            'del_dm_tpl_': 'manage_templates', 'del_grp_tpl_': 'manage_templates', 'del_auto_': 'manage_auto',
            'ugsel_': 'manage_groups', 'ugt_': 'manage_groups', 'delfwd_': 'manage_groups',
            'accsel': 'manage_groups', 'acgt_': 'manage_groups',
            'setrole_': 'add_admins', 'perm_of_': 'add_admins', 'tgl_': 'add_admins',
        }
        perm_needed = CB_PERM_MAP.get(data_str)
        if perm_needed is None:
            for _pfx, _pk in CB_PERM_PREFIX.items():
                if data_str.startswith(_pfx):
                    perm_needed = _pk
                    break
        if perm_needed and not has_perm(user_id, perm_needed):
            await event.answer("🚫 لا تملك هذه الصلاحية — تواصل مع الأدمن.", alert=True)
            return
        if data_str == 'add_acc':
            pass  # إضافة الحساب متاحة لجميع مستخدمي البوت (أدمن ومستخدم) — كل مستخدم يضيف حسابه
        elif data_str == 'rem_acc' or data_str.startswith('del_acc_'):
            owned = len(get_owned_accounts(user_id)) > 0
            if not (has_perm(user_id, 'add_accounts') or owned):
                await event.answer("🚫 لا تملك حسابات لإدارتها.", alert=True)
                return
        if data_str == 'refresh_groups' or data_str.startswith('rlink_') or data_str == 'report_links':
            if not has_perm(user_id, 'export_links'):
                await event.answer("🚫 لا تملك صلاحية استيراد روابط القروبات.", alert=True)
                return
        if data_str == 'list_acc':
            owned = len(get_owned_accounts(user_id)) > 0
            if not (has_perm(user_id, 'view_stats') or owned):
                await event.answer("🚫 لا توجد حسابات لعرضها — أضف حسابك أولاً.", alert=True)
                return
        
        # ============ إدارة الحسابات ============
        
        if data == b'add_acc':
            # 🚧 حد الحسابات: كل مستخدم (عدا الأدمن الكامل/المالك) يضيف 3 حسابات كحد أقصى
            max_acc = int(config.get('MAX_ACCOUNTS_PER_USER', 3) or 3)
            owned_now = get_owned_accounts(user_id)
            if not is_full_admin(user_id) and len(owned_now) >= max_acc:
                accs_txt = ("`" + "`, `".join(owned_now[:6]) + "`") if owned_now else "—"
                await event.respond(
                    f"🚫 **وصلت إلى الحد الأقصى المسموح — {max_acc} حسابات فقط لكل مستخدم.**\n\n"
                    f"📱 حساباتك الحالية ({len(owned_now)}): {accs_txt}\n\n"
                    "💡 إذا أردت إضافة حسابات أكثر، **تواصل مع الأدمن أو المالك** لرفع الحد أو إضافة الحساب لك.\n"
                    "🗑 أو احذف حساباً قديماً من ❌ حذف حسابي ثم أضف حساباً جديداً."
                )
                logger.info(f"🚧 المستخدم {user_id} وصل لحد الحسابات ({len(owned_now)}/{max_acc}) — مُنع من الإضافة")
                return
            login_states[user_id] = {'step': 'await_phone', 'owner': user_id}
            quota_line = ""
            if not is_full_admin(user_id):
                remaining = max_acc - len(owned_now)
                quota_line = f"\n\n📊 حصتك: {len(owned_now)}/{max_acc} حسابات مستخدمة — يمكنك إضافة {remaining} أخرى."
            await event.respond(
                "📱 من فضلك أرسل **رقم الهاتف** مع مفتاح الدولة (مثال: +9665xxxxxxxx):\n\n"
                "💡 سيُربط الحساب بحسابك في البوت وتصلك رسائله الملتقطة." + quota_line
            )
        
        elif data == b'list_acc':
            if not active_clients:
                await event.respond("❌ لا توجد حسابات مرتبطة حالياً.")
            elif is_full_admin(user_id):
                # 👥 للأدمن: عرض مجمّع حسب المالك + زر لكل مستخدم يفتح ملفه الكامل (حساباته وكلماته وإعداداته)
                by_owner = {}
                no_owner = []
                for p in active_clients.keys():
                    o = find_account_owner(p)
                    if o is None:
                        no_owner.append(p)
                    else:
                        by_owner.setdefault(int(o), []).append(p)
                lines = ["✅ **الحسابات المرتبطة** (مجمّعة حسب المالك):\n"]
                btn_rows = []
                for o in sorted(by_owner):
                    nm = await get_user_display_name(o)
                    lines.append(f"👤 **{nm}** (`{o}`): " + "، ".join(f"`{p}`" for p in by_owner[o]))
                    btn_rows.append([Button.inline(f"👤 ملف {nm[:20]} ({len(by_owner[o])}📱)", f"umon_{o}".encode())])
                if no_owner:
                    lines.append("⚙️ بدون مالك: " + "، ".join(f"`{p}`" for p in no_owner))
                btn_rows.append([Button.inline('👥 مراقبة كل المستخدمين', b'users_monitor')])
                await event.respond("\n".join(lines), buttons=btn_rows)
            else:
                mine = owned_active_phones(user_id)
                if mine:
                    await event.respond("✅ **حساباتك المرتبطة:**\n" + "\n".join([f"- `{p}`" for p in mine]))
                else:
                    await event.respond("📭 لا توجد حسابات مرتبطة بحسابك بعد — اضغط ➕ إضافة حسابي لتسجيل حسابك.")

        # ============ إدارة الكلمات المفتاحية ============
        
        elif data == b'manage_kw':
            # 🔒 خصوصية: كل مستخدم يدير كلماته بنفسه — الافتراضية 🌟 متاحة للجميع والحذف فيها شخصي لا يؤثر على غيره
            defaults = get_default_keywords(config)
            deleted = get_user_deleted_defaults(user_id, config)
            my_kw = get_user_keywords(user_id, config)
            active_defaults = [k for k in defaults if k not in deleted]
            msg = ("🔑 **كلماتك المفتاحية**\n\n"
                   "رسائل حساباتك تُلتقط عندما ترد **الكلمة بعينها ككلمة مستقلة** في نصها.\n"
                   "مثال: `احد` تُلتقط من «يحلها احد؟» وتُلتقط «أحد» بصيغها، لكنها **لا** تُلتقط من «الاحد» أو «احدى» أو «احدث».\n\n")
            msg += f"🌟 **الافتراضية المفعّلة** ({len(active_defaults)}/{len(defaults)} — متاحة لكل الحسابات):\n"
            msg += ("`" + "`, `".join(active_defaults) + "`") if active_defaults else "لا شيء — أخفيتها كلها، استعدها من ♻️ استعادة."
            msg += f"\n\n🔑 **كلماتك الخاصة** ({len(my_kw)}):\n"
            if my_kw:
                for i, k in enumerate(my_kw[:15], 1):
                    msg += f"{i}. `{k}`\n"
                if len(my_kw) > 15:
                    msg += f"... و{len(my_kw) - 15} أخرى\n"
            else:
                msg += "لا توجد — أضف كلماتك من ➕ إضافة كلمة."
            if is_full_admin(user_id):
                gkw = config.get('KEYWORDS', [])
                msg += f"\n🌍 كلمات عامة (أدمن): **{len(gkw)}**" + ((" — `" + "`, `".join(gkw[:10]) + "`") if gkw else "")
            buttons = [
                [Button.inline('➕ إضافة كلمة', b'add_kw'), Button.inline('🗑 حذف كلمة', b'rem_kw')],
            ]
            if deleted:
                buttons.append([Button.inline(f'♻️ استعادة محذوفة ({len(deleted)})', b'rst_kw')])
            buttons.append([Button.inline('🔙 رجوع', b'back_main')])
            await event.respond(msg, buttons=buttons)

        elif data == b'add_kw':
            login_states[user_id] = {'step': 'add_kw'}
            await event.respond(
                "📝 أرسل الكلمة المفتاحية التي تريد إضافتها لقائمتك.\n\n"
                "🧩 يمكنك إرسال **عدة كلمات دفعة واحدة**: كل سطر كلمة، أو افصل بينها بفاصلة `،`\n"
                "🌟 ملاحظة: الكلمات الافتراضية (يسوي، تسوي، تعرفون، ابغى...) مفعّلة أصلاً لكل الحسابات — وإن أرسلت واحدة كانت محذوفة عندك فتُستعاد تلقائياً.\n"
                "💡 عندما تحتوي رسالة على إحدى كلماتك تُلتقط وتُوجّه لقروبك.\n"
                "💡 للإلغاء أرسل: `/cancel`"
            )

        elif data == b'rem_kw':
            # 🗑 حذف الكلمات بالأزرار — الافتراضية 🌟 والخاصة 🔑: اضغط الكلمة تُحذف فوراً (بدون كتابة اسمها)
            await send_kw_delete_screen(event, user_id, config)

        elif data.startswith(b'delflt_'):
            # 🌟 حذف (إخفاء) كلمة افتراضية لهذا المستخدم فقط
            try:
                idx = int(data.decode()[7:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            defaults = get_default_keywords(config)
            if 0 <= idx < len(defaults):
                removed = defaults[idx]
                remove_user_default(user_id, removed, config)
                await event.answer(f"🗑 أُخفيت: {removed[:30]}")
                logger.info(f"🌟 المستخدم {user_id} أخفى الكلمة الافتراضية: {removed}")
                await send_kw_delete_screen(event, user_id, load_json_config(), edit=True)
            else:
                await event.answer("❌ الكلمة غير موجودة — حدّث القائمة.", alert=True)

        elif data == b'rst_kw':
            # ♻️ استعادة الكلمات الافتراضية المخفية
            await send_kw_restore_screen(event, user_id, config)

        elif data.startswith(b'rstflt_'):
            try:
                idx = int(data.decode()[7:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            deleted = get_user_deleted_defaults(user_id, config)
            if 0 <= idx < len(deleted):
                restored = deleted[idx]
                restore_user_default(user_id, restored, config)
                await event.answer(f"♻️ استُعيدت: {restored[:30]}")
                logger.info(f"♻️ المستخدم {user_id} استعاد الكلمة الافتراضية: {restored}")
                await send_kw_restore_screen(event, user_id, load_json_config(), edit=True)
            else:
                await event.answer("❌ الكلمة غير موجودة — حدّث القائمة.", alert=True)

        elif data.startswith(b'delkw_'):
            try:
                idx = int(data.decode()[6:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            my_kw = get_user_keywords(user_id, config)
            if 0 <= idx < len(my_kw):
                removed = my_kw.pop(idx)
                set_user_keywords(user_id, my_kw, config)
                await event.answer(f"🗑 حُذفت: {removed[:30]}")
                logger.info(f"🔑 المستخدم {user_id} حذف كلمته المفتاحية: {removed}")
                await send_kw_delete_screen(event, user_id, load_json_config(), edit=True)
            else:
                await event.answer("❌ الكلمة غير موجودة — حدّث القائمة.", alert=True)

        # ============ إدارة قائمة التجاهل ============
        
        elif data == b'manage_ignore':
            ignore_list = config.get('IGNORE_USERS', [])
            msg = "🚫 **قائمة التجاهل (ID المستخدمين):**\n" + ("\n".join([f"- `{u}`" for u in ignore_list]) if ignore_list else "القائمة فارغة.")
            buttons = [[Button.inline('➕ إضافة', b'add_ignore'), Button.inline('➖ حذف', b'rem_ignore')], [Button.inline('🔙 رجوع', b'back_main')]]
            await event.respond(msg, buttons=buttons)

        # ============ إدارة المجموعات (للعرض فقط) ============
        
        elif data == b'manage_groups':
            group_list = config.get('TARGET_GROUPS', [])
            admin_group = config.get('ADMIN_GROUP_ID', 0)
            msg = f"👥 **المجموعات المستوردة (للعرض فقط):** تم استيراد `{len(group_list)}` مجموعة.\n\n"
            msg += "🔹 **ملاحظة:** البوت يراقب **جميع** المجموعات التي فيها حسابك تلقائياً، بغض النظر عن هذه القائمة.\n\n"
            msg += f"📤 **قروب النسخ الشامل (قروب الأدمن):** {'مُفعّل ✅' if admin_group else 'غير مُفعّل ❌ — أضف البوت لقروبك وأرسل /mygroup داخله'}"
            buttons = [
                [Button.inline('🔄 تحديث واستيراد', b'refresh_groups')],
                [Button.inline('📄 تقرير روابط قروبات حساب', b'report_links')],
                [Button.inline('➕ إضافة يدوي (للعرض)', b'add_group'), Button.inline('➖ حذف يدوي (للعرض)', b'rem_group')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            if is_main_admin(user_id) and admin_group:
                buttons.insert(3, [Button.inline('❌ إلغاء قروب النسخ الشامل', b'unset_mygroup')])
            await event.respond(msg, buttons=buttons)

        elif data == b'refresh_groups':
            # 🔒 العزل: الأدمن الكامل يستورد من كل الحسابات — العضو من حساباته هو فقط
            if is_full_admin(user_id):
                scope = dict(active_clients)
            else:
                scope = {p: active_clients[p] for p in owned_active_phones(user_id)}
            if not scope:
                await event.respond("❌ لا توجد حسابات يمكنك الاستيراد منها — أضف حسابك أولاً.")
            else:
                total_new = 0
                for phone, client in scope.items():
                    new = await import_groups(client)
                    total_new += new
                await event.respond(f"✅ تم تحديث القائمة! تم استيراد `{total_new}` مجموعة جديدة.")

        # ============ حذف حساب ============
        
        elif data == b'rem_acc':
            if not active_clients:
                await event.respond("❌ لا توجد حسابات لحذفها.")
            else:
                if is_full_admin(user_id):
                    deletable = list(active_clients.keys())
                    prompt = "🗑 اختر الحساب الذي تريد حذفه:"
                else:
                    deletable = owned_active_phones(user_id)
                    prompt = "🗑 اختر حسابك الذي تريد حذفه:"
                if not deletable:
                    await event.respond("📭 لا تملك حسابات يمكن حذفها.")
                else:
                    buttons = [[Button.inline(p, f"del_acc_{p}".encode())] for p in deletable]
                    buttons.append([Button.inline('🔙 رجوع', b'back_main')])
                    await event.respond(prompt, buttons=buttons)

        elif data.startswith(b'del_acc_'):
            phone = data.decode().replace('del_acc_', '')
            owner = find_account_owner(phone)
            can_delete = is_full_admin(user_id) or owner == user_id or owner == str(user_id)
            if not can_delete:
                await event.answer("🚫 يمكنك حذف حسابك فقط — تواصل مع الأدمن لحذف حسابات الآخرين.", alert=True)
                return
            if phone in active_clients:
                await active_clients[phone].disconnect()
                del active_clients[phone]
                sess_path = os.path.join(SESSION_DIR, f'session_{phone}.session')
                if os.path.exists(sess_path):
                    os.remove(sess_path)
                # إزالة الجلسة المحفوظة من config + تنظيف اعتمادات القروبات وملكية الحساب المحذوف
                forget_session_string(phone)
                try:
                    cfg_del = load_json_config()
                    ag_map = cfg_del.get('ACCOUNT_GROUPS', {})
                    if phone in ag_map:
                        ag_map.pop(phone, None)
                        cfg_del['ACCOUNT_GROUPS'] = ag_map
                    ow_map = cfg_del.get('ACCOUNT_OWNERS', {})
                    _pd = _phone_digits(phone)
                    for _pk in list(ow_map.keys()):
                        if _pk == phone or (_pd and _phone_digits(_pk) == _pd):
                            ow_map.pop(_pk, None)
                    cfg_del['ACCOUNT_OWNERS'] = ow_map
                    update_json_config(cfg_del)
                except Exception:
                    pass
                await event.respond(f"✅ تم حذف الحساب `{phone}` بنجاح.")
            else:
                await event.respond("❌ الحساب غير موجود.")

        # ============ 👥 مراقبة المستخدمين وإدارة ملفاتهم (أدمن فقط) ============

        elif data == b'users_monitor':
            if not is_full_admin(user_id):
                await event.answer("🚫 هذه الشاشة للأدمن فقط.", alert=True)
                return
            logger.info(f"👥 الأدمن {user_id} فتح شاشة مراقبة المستخدمين")
            await send_users_monitor_screen(event, user_id, page=0)

        elif data == b'noop_page':
            await event.answer()

        elif data.startswith(b'umonp_'):
            # 📄 تنقل صفحات قائمة المستخدمين
            if not is_full_admin(user_id):
                await event.answer("🚫 هذه الشاشة للأدمن فقط.", alert=True)
                return
            try:
                pg = int(data.decode()[6:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            await event.answer()
            await send_users_monitor_screen(event, user_id, page=pg, edit=True)

        elif data.startswith(b'umon_'):
            # 👤 ملف مستخدم كامل — للأدمن فقط
            if not is_full_admin(user_id):
                await event.answer("🚫 هذه الشاشة للأدمن فقط.", alert=True)
                return
            try:
                target = int(data.decode()[5:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            await event.answer()
            logger.info(f"👤 الأدمن {user_id} فتح ملف المستخدم {target} من مراقبة المستخدمين")
            await send_user_monitor_screen(event, user_id, target)

        elif data.startswith(b'adeltel_'):
            # 🗑 الأدمن يحذف حساب أحد المستخدمين من ملفه
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            try:
                _, _uid, phone = data.decode().split('_', 2)
                target = int(_uid)
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            await _delete_account_full(phone)
            await event.answer(f"🗑 حُذف حساب {phone} وتنظيف كل بياناته.", alert=True)
            logger.info(f"🗑 الأدمن {user_id} حذف حساب {phone} (كان للمستخدم {target}) من شاشة المراقبة")
            await send_user_monitor_screen(event, user_id, target, edit=True)

        elif data.startswith(b'adelkw_'):
            # 🔑✖ الأدمن يحذف كلمة خاصة من كلمات مستخدم
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            try:
                _, _uid, idx = data.decode().split('_', 2)
                target, idx = int(_uid), int(idx)
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            cfg_kw = load_json_config()
            maps = cfg_kw.get('USER_KEYWORDS', {})
            lst = maps.get(str(target), [])
            if 0 <= idx < len(lst):
                removed = lst.pop(idx)
                maps[str(target)] = lst
                cfg_kw['USER_KEYWORDS'] = maps
                update_json_config(cfg_kw)
                await event.answer(f"🗑 حُذفت «{removed[:20]}» من كلمات المستخدم.")
                logger.info(f"🔑 الأدمن {user_id} حذف الكلمة «{removed}» من كلمات المستخدم {target}")
                await send_user_monitor_screen(event, user_id, target, edit=True)
            else:
                await event.answer("❌ الكلمة غير موجودة — حدّث الشاشة.", alert=True)

        elif data.startswith(b'aclrdfl_'):
            # ♻️ الأدمن يستعيد كل الافتراضية التي أخفاها المستخدم عن نفسه
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            try:
                target = int(data.decode()[8:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            cfg_rd = load_json_config()
            maps_rd = cfg_rd.get('USER_DELETED_DEFAULTS', {})
            n = len(maps_rd.get(str(target), []))
            maps_rd[str(target)] = []
            cfg_rd['USER_DELETED_DEFAULTS'] = maps_rd
            update_json_config(cfg_rd)
            await event.answer(f"♻️ استُعيدت {n} كلمة افتراضية للمستخدم.")
            logger.info(f"♻️ الأدمن {user_id} استعادة {n} افتراضية مخفية للمستخدم {target}")
            await send_user_monitor_screen(event, user_id, target, edit=True)

        elif data.startswith(b'aaddkw_'):
            # 🔑➕ الأدمن يضيف كلمات لمستخدم — يحوّل لحالة الإدخال
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            try:
                target = int(data.decode()[7:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            login_states[user_id] = {'step': 'admin_add_kw_for', 'target': target}
            nm = await get_user_display_name(target)
            await event.respond(
                f"🔑 أرسل الكلمات التي تريد إضافتها لمستخدم **{nm}** (`{target}`):\n\n"
                "🧩 كل سطر كلمة أو مفصولة بفواصل `،` — والكلمات تُضاف لقائمة **هذا المستخدم فقط**.\n"
                "💡 للإلغاء أرسل: `/cancel`"
            )

        elif data.startswith(b'armgrp_'):
            # 🎯✖ الأدمن يزيل قروب توجيه من قروبات مستخدم
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            try:
                _, _uid, idx = data.decode().split('_', 2)
                target, idx = int(_uid), int(idx)
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            cfg_g = load_json_config()
            ug = cfg_g.get('USER_GROUPS', {})
            lst_g = ug.get(str(target), [])
            if 0 <= idx < len(lst_g):
                removed_gid = lst_g.pop(idx)
                ug[str(target)] = lst_g
                cfg_g['USER_GROUPS'] = ug
                update_json_config(cfg_g)
                await event.answer("🎯 أُزيل القروب من قروبات توجيه المستخدم.")
                logger.info(f"🎯 الأدمن {user_id} أزال قروب {removed_gid} من قروبات المستخدم {target}")
                await send_user_monitor_screen(event, user_id, target, edit=True)
            else:
                await event.answer("❌ القروب غير موجود — حدّث الشاشة.", alert=True)

        # ============ إدارة الكلمات المحظورة والمشبوهة ============
        
        elif data == b'manage_banned':
            banned_ads = config.get('BANNED_ADS', [])
            suspicious = config.get('SUSPICIOUS_WORDS', [])
            
            msg = "🛡️ **قائمة الكلمات المحظورة والمشبوهة**\n\n"
            msg += "📢 **كلمات إعلانية محظورة:**\n"
            msg += "\n".join([f"- `{w}`" for w in banned_ads]) if banned_ads else "- (لا توجد كلمات)"
            msg += "\n\n⚠️ **كلمات مشبوهة:**\n"
            msg += "\n".join([f"- `{w}`" for w in suspicious]) if suspicious else "- (لا توجد كلمات)"
            
            buttons = [
                [Button.inline('📢 إضافة كلمة إعلانية', b'add_banned_ad')],
                [Button.inline('📢 حذف كلمة إعلانية', b'rem_banned_ad')],
                [Button.inline('⚠️ إضافة كلمة مشبوهة', b'add_suspicious')],
                [Button.inline('⚠️ حذف كلمة مشبوهة', b'rem_suspicious')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_banned_ad':
            login_states[user_id] = {'step': 'add_banned_ad'}
            await event.respond("📝 أرسل الكلمة الإعلانية التي تريد حظرها:")
        
        elif data == b'rem_banned_ad':
            banned_ads = config.get('BANNED_ADS', [])
            if not banned_ads:
                await event.respond("❌ لا توجد كلمات محظورة لحذفها.")
            else:
                buttons = [[Button.inline(w, f"del_banned_ad_{w}".encode())] for w in banned_ads]
                buttons.append([Button.inline('🔙 رجوع', b'manage_banned')])
                await event.respond("🗑 اختر الكلمة التي تريد حذفها:", buttons=buttons)
        
        elif data.startswith(b'del_banned_ad_'):
            word = data.decode().replace('del_banned_ad_', '')
            config = load_json_config()
            banned_ads = config.get('BANNED_ADS', [])
            if word in banned_ads:
                banned_ads.remove(word)
                config['BANNED_ADS'] = banned_ads
                update_json_config(config)
                await event.respond(f"✅ تم حذف الكلمة `{word}` من قائمة المحظورة.")
            else:
                await event.respond("❌ الكلمة غير موجودة.")
        
        elif data == b'add_suspicious':
            login_states[user_id] = {'step': 'add_suspicious'}
            await event.respond("📝 أرسل الكلمة المشبوهة التي تريد حظرها:")
        
        elif data == b'rem_suspicious':
            suspicious = config.get('SUSPICIOUS_WORDS', [])
            if not suspicious:
                await event.respond("❌ لا توجد كلمات مشبوهة لحذفها.")
            else:
                buttons = [[Button.inline(w, f"del_suspicious_{w}".encode())] for w in suspicious]
                buttons.append([Button.inline('🔙 رجوع', b'manage_banned')])
                await event.respond("🗑 اختر الكلمة المشبوهة التي تريد حذفها:", buttons=buttons)
        
        elif data.startswith(b'del_suspicious_'):
            word = data.decode().replace('del_suspicious_', '')
            config = load_json_config()
            suspicious = config.get('SUSPICIOUS_WORDS', [])
            if word in suspicious:
                suspicious.remove(word)
                config['SUSPICIOUS_WORDS'] = suspicious
                update_json_config(config)
                await event.respond(f"✅ تم حذف الكلمة `{word}` من قائمة المشبوهة.")
            else:
                await event.respond("❌ الكلمة غير موجودة.")

        # ============ إعدادات الفلترة ============
        
        elif data == b'manage_filters':
            filters = config.get('FILTERS', {})
            
            msg = "⚙️ **إعدادات الفلترة**\n\n"
            msg += f"📏 الحد الأقصى للأحرف: `{filters.get('max_length', 50)}`\n"
            msg += f"🔗 منع الروابط: `{'✅ مفعل' if filters.get('block_links', True) else '❌ معطل'}`\n"
            msg += f"📞 منع أرقام الهواتف: `{'✅ مفعل' if filters.get('block_phones', True) else '❌ معطل'}`\n"
            msg += f"👤 منع المعرفات (@): `{'✅ مفعل' if filters.get('block_mentions', True) else '❌ معطل'}`\n"
            msg += f"📢 منع الكلمات الإعلانية: `{'✅ مفعل' if filters.get('block_ads', True) else '❌ معطل'}`\n"
            msg += f"⚠️ منع الكلمات المشبوهة: `{'✅ مفعل' if filters.get('block_suspicious', True) else '❌ معطل'}`\n"
            
            buttons = [
                [Button.inline('📏 تغيير الحد الأقصى', b'set_max_length')],
                [Button.inline('🔗 تبديل منع الروابط', b'toggle_links')],
                [Button.inline('📞 تبديل منع الأرقام', b'toggle_phones')],
                [Button.inline('👤 تبديل منع المعرفات', b'toggle_mentions')],
                [Button.inline('📢 تبديل منع الإعلانات', b'toggle_ads')],
                [Button.inline('⚠️ تبديل منع المشبوهة', b'toggle_suspicious')],
                [Button.inline('🔓 تعطيل جميع الفلاتر (مستحسن)', b'reset_filters')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'set_max_length':
            login_states[user_id] = {'step': 'set_max_length'}
            await event.respond("📏 أرسل الحد الأقصى الجديد لعدد الأحرف (0 = بدون حد، أو رقم بين 10 و 500):")
        
        elif data == b'reset_filters':
            config['FILTERS'] = {
                'max_length': 0,
                'block_links': False,
                'block_phones': False,
                'block_mentions': False,
                'block_ads': False,
                'block_suspicious': False
            }
            update_json_config(config)
            await event.respond("✅ تم تعطيل جميع الفلاتر! الآن سيتم توجيه جميع الرسائل المطابقة للكلمات المفتاحية بلا حجب.\n\n💡 يمكنك تفعيل أي فلتر يدوياً من إعدادات الفلترة.")
        
        elif data == b'toggle_links':
            filters = config.get('FILTERS', {})
            filters['block_links'] = not filters.get('block_links', True)
            config['FILTERS'] = filters
            update_json_config(config)
            await event.respond(f"✅ تم {'تفعيل' if filters['block_links'] else 'تعطيل'} منع الروابط.")
        
        elif data == b'toggle_phones':
            filters = config.get('FILTERS', {})
            filters['block_phones'] = not filters.get('block_phones', True)
            config['FILTERS'] = filters
            update_json_config(config)
            await event.respond(f"✅ تم {'تفعيل' if filters['block_phones'] else 'تعطيل'} منع أرقام الهواتف.")
        
        elif data == b'toggle_mentions':
            filters = config.get('FILTERS', {})
            filters['block_mentions'] = not filters.get('block_mentions', True)
            config['FILTERS'] = filters
            update_json_config(config)
            await event.respond(f"✅ تم {'تفعيل' if filters['block_mentions'] else 'تعطيل'} منع المعرفات (@).")
        
        elif data == b'toggle_ads':
            filters = config.get('FILTERS', {})
            filters['block_ads'] = not filters.get('block_ads', True)
            config['FILTERS'] = filters
            update_json_config(config)
            await event.respond(f"✅ تم {'تفعيل' if filters['block_ads'] else 'تعطيل'} منع الكلمات الإعلانية.")
        
        elif data == b'toggle_suspicious':
            filters = config.get('FILTERS', {})
            filters['block_suspicious'] = not filters.get('block_suspicious', True)
            config['FILTERS'] = filters
            update_json_config(config)
            await event.respond(f"✅ تم {'تفعيل' if filters['block_suspicious'] else 'تعطيل'} منع الكلمات المشبوهة.")
        
        # ============ قوالب الرد على الخاص ============
        
        elif data == b'manage_dm_templates':
            # 🔒 خصوصية: قوالبك الخاصة تظهر لك أنت فقط — ولا يراها أي مستخدم آخر
            dm_templates = get_own_templates(user_id, 'DM', config)
            msg = ("💬 **ردودك الخاصة على الخاص**\n\n"
                   "هذه القوالب تُرسل من حسابك كرد خاص — تظهر لك أنت فقط ولا يراها غيرك.\n\n")
            if dm_templates:
                for i, t in enumerate(dm_templates, 1):
                    preview = t[:50] + "..." if len(t) > 50 else t
                    msg += f"{i}. `{preview}`\n"
            else:
                msg += "لا توجد قوالب خاصة بعد — أضف أول قالب من ➕ إضافة قالب."
            buttons = [
                [Button.inline('➕ إضافة قالب', b'add_dm_template')],
                [Button.inline('🗑 حذف قالب', b'rem_dm_template')],
            ]
            if is_full_admin(user_id):
                gcnt = len(config.get('DM_REPLY_TEMPLATES', []))
                msg += f"\n🌍 قوالب عامة افتراضية (للجميع): **{gcnt}**"
                buttons.append([Button.inline('🌍 إدارة القوالب العامة (أدمن)', b'manage_gdm_templates')])
            buttons.append([Button.inline('🔙 رجوع', b'back_main')])
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_dm_template':
            login_states[user_id] = {'step': 'add_dm_template'}
            await event.respond("📝 أرسل نص قالب ردك **الخاص** على الخاص:\n\n(يُحفظ في قوالبك أنت فقط — سيُرسل كرسالة خاصة للمرسل عند اختيار هذا القالب)")
        
        elif data == b'rem_dm_template':
            dm_templates = get_own_templates(user_id, 'DM', config)
            if not dm_templates:
                await event.respond("❌ لا توجد قوالب خاصة بك لحذفها.")
            else:
                buttons = []
                for i, t in enumerate(dm_templates):
                    preview = t[:30] + "..." if len(t) > 30 else t
                    buttons.append([Button.inline(f"🗑 {preview}", f"del_dm_tpl_{i}".encode())])
                buttons.append([Button.inline('🔙 رجوع', b'manage_dm_templates')])
                await event.respond("اختر القالب الخاص الذي تريد حذفه:", buttons=buttons)
        
        elif data.startswith(b'del_dm_tpl_'):
            idx = int(data.decode().replace('del_dm_tpl_', ''))
            dm_templates = get_own_templates(user_id, 'DM', config)
            if 0 <= idx < len(dm_templates):
                removed = dm_templates.pop(idx)
                set_own_templates(user_id, 'DM', dm_templates, config)
                preview = removed[:40] + "..." if len(removed) > 40 else removed
                await event.respond(f"✅ تم حذف القالب: `{preview}`")
            else:
                await event.respond("❌ القالب غير موجود.")
        
        # ============ قوالب الرد في القروب ============
        
        elif data == b'manage_grp_templates':
            # 🔒 خصوصية: قوالبك الخاصة تظهر لك أنت فقط — ولا يراها أي مستخدم آخر
            grp_templates = get_own_templates(user_id, 'GRP', config)
            msg = ("👥 **ردودك الخاصة في القروب**\n\n"
                   "هذه القوالب تُرسل من حسابك كرد في القروب — تظهر لك أنت فقط ولا يراها غيرك.\n\n")
            if grp_templates:
                for i, t in enumerate(grp_templates, 1):
                    preview = t[:50] + "..." if len(t) > 50 else t
                    msg += f"{i}. `{preview}`\n"
            else:
                msg += "لا توجد قوالب خاصة بعد — أضف أول قالب من ➕ إضافة قالب."
            buttons = [
                [Button.inline('➕ إضافة قالب', b'add_grp_template')],
                [Button.inline('🗑 حذف قالب', b'rem_grp_template')],
            ]
            if is_full_admin(user_id):
                gcnt = len(config.get('GROUP_REPLY_TEMPLATES', []))
                msg += f"\n🌍 قوالب عامة افتراضية (للجميع): **{gcnt}**"
                buttons.append([Button.inline('🌍 إدارة القوالب العامة (أدمن)', b'manage_ggrp_templates')])
            buttons.append([Button.inline('🔙 رجوع', b'back_main')])
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_grp_template':
            login_states[user_id] = {'step': 'add_grp_template'}
            await event.respond("📝 أرسل نص قالب ردك **الخاص** في القروب:\n\n(يُحفظ في قوالبك أنت فقط — سيُرسل كرد في القروب عند اختيار هذا القالب)")
        
        elif data == b'rem_grp_template':
            grp_templates = get_own_templates(user_id, 'GRP', config)
            if not grp_templates:
                await event.respond("❌ لا توجد قوالب خاصة بك لحذفها.")
            else:
                buttons = []
                for i, t in enumerate(grp_templates):
                    preview = t[:30] + "..." if len(t) > 30 else t
                    buttons.append([Button.inline(f"🗑 {preview}", f"del_grp_tpl_{i}".encode())])
                buttons.append([Button.inline('🔙 رجوع', b'manage_grp_templates')])
                await event.respond("اختر القالب الخاص الذي تريد حذفه:", buttons=buttons)
        
        elif data.startswith(b'del_grp_tpl_'):
            idx = int(data.decode().replace('del_grp_tpl_', ''))
            grp_templates = get_own_templates(user_id, 'GRP', config)
            if 0 <= idx < len(grp_templates):
                removed = grp_templates.pop(idx)
                set_own_templates(user_id, 'GRP', grp_templates, config)
                preview = removed[:40] + "..." if len(removed) > 40 else removed
                await event.respond(f"✅ تم حذف القالب: `{preview}`")
            else:
                await event.respond("❌ القالب غير موجود.")
        
        # ============ القوالب العامة الافتراضية (أدمن فقط — تظهر للجميع كخيار إضافي) ============
        
        elif data == b'manage_gdm_templates':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            g_tpl = config.get('DM_REPLY_TEMPLATES', [])
            msg = ("🌍 **القوالب العامة — الرد على الخاص**\n\n"
                   "تظهر لكل المستخدمين كخيار إضافي بعد قوالبهم الخاصة.\n\n")
            msg += "\n".join([f"{i}. `{(t[:50] + '...') if len(t) > 50 else t}`" for i, t in enumerate(g_tpl, 1)]) if g_tpl else "لا توجد قوالب عامة."
            await event.respond(msg, buttons=[
                [Button.inline('➕ إضافة قالب عام', b'add_gdm_template')],
                [Button.inline('🗑 حذف قالب عام', b'rem_gdm_template')],
                [Button.inline('🔙 رجوع', b'manage_dm_templates')],
            ])
        
        elif data == b'add_gdm_template':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            login_states[user_id] = {'step': 'add_gdm_template'}
            await event.respond("📝 أرسل نص القالب العام للرد على الخاص (سيظهر لجميع المستخدمين):")
        
        elif data == b'rem_gdm_template':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            g_tpl = config.get('DM_REPLY_TEMPLATES', [])
            if not g_tpl:
                await event.respond("❌ لا توجد قوالب عامة لحذفها.")
            else:
                rows = [[Button.inline(f"🗑 {(t[:30] + '...') if len(t) > 30 else t}", f"gdel_dm_tpl_{i}".encode())] for i, t in enumerate(g_tpl)]
                rows.append([Button.inline('🔙 رجوع', b'manage_gdm_templates')])
                await event.respond("اختر القالب العام الذي تريد حذفه:", buttons=rows)
        
        elif data.startswith(b'gdel_dm_tpl_'):
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            idx = int(data.decode().replace('gdel_dm_tpl_', ''))
            g_tpl = config.get('DM_REPLY_TEMPLATES', [])
            if 0 <= idx < len(g_tpl):
                removed = g_tpl.pop(idx)
                config['DM_REPLY_TEMPLATES'] = g_tpl
                update_json_config(config)
                await event.respond(f"✅ تم حذف القالب العام: `{removed[:40]}`")
            else:
                await event.respond("❌ القالب غير موجود.")
        
        elif data == b'manage_ggrp_templates':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            g_tpl = config.get('GROUP_REPLY_TEMPLATES', [])
            msg = ("🌍 **القوالب العامة — الرد في القروب**\n\n"
                   "تظهر لكل المستخدمين كخيار إضافي بعد قوالبهم الخاصة.\n\n")
            msg += "\n".join([f"{i}. `{(t[:50] + '...') if len(t) > 50 else t}`" for i, t in enumerate(g_tpl, 1)]) if g_tpl else "لا توجد قوالب عامة."
            await event.respond(msg, buttons=[
                [Button.inline('➕ إضافة قالب عام', b'add_ggrp_template')],
                [Button.inline('🗑 حذف قالب عام', b'rem_ggrp_template')],
                [Button.inline('🔙 رجوع', b'manage_grp_templates')],
            ])
        
        elif data == b'add_ggrp_template':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            login_states[user_id] = {'step': 'add_ggrp_template'}
            await event.respond("📝 أرسل نص القالب العام للرد في القروب (سيظهر لجميع المستخدمين):")
        
        elif data == b'rem_ggrp_template':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            g_tpl = config.get('GROUP_REPLY_TEMPLATES', [])
            if not g_tpl:
                await event.respond("❌ لا توجد قوالب عامة لحذفها.")
            else:
                rows = [[Button.inline(f"🗑 {(t[:30] + '...') if len(t) > 30 else t}", f"gdel_grp_tpl_{i}".encode())] for i, t in enumerate(g_tpl)]
                rows.append([Button.inline('🔙 رجوع', b'manage_ggrp_templates')])
                await event.respond("اختر القالب العام الذي تريد حذفه:", buttons=rows)
        
        elif data.startswith(b'gdel_grp_tpl_'):
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            idx = int(data.decode().replace('gdel_grp_tpl_', ''))
            g_tpl = config.get('GROUP_REPLY_TEMPLATES', [])
            if 0 <= idx < len(g_tpl):
                removed = g_tpl.pop(idx)
                config['GROUP_REPLY_TEMPLATES'] = g_tpl
                update_json_config(config)
                await event.respond(f"✅ تم حذف القالب العام: `{removed[:40]}`")
            else:
                await event.respond("❌ القالب غير موجود.")
        
        # ============ إضافة رد مباشر من القناة - خاص ============
        
        elif data.startswith(b'add_dm_from_ch_'):
            parts = data.decode().split('_')
            # add_dm_from_ch_{group_id}_{message_id}_{sender_id}
            if len(parts) >= 6:
                group_id = int(parts[4])
                message_id = int(parts[5])
                sender_id = int(parts[6])
                login_states[user_id] = {
                    'step': 'add_dm_from_ch',
                    'group_id': group_id,
                    'message_id': message_id,
                    'sender_id': sender_id
                }
                await event.respond("📝 أرسل نص رد الخاص الذي تريد إضافته كقالب وإرساله للمرسل:\n\n(سيتم حفظه في القوالب وإرساله مباشرة)")
        
        # ============ إضافة رد مباشر من القناة - قروب ============
        
        elif data.startswith(b'add_grp_from_ch_'):
            parts = data.decode().split('_')
            # add_grp_from_ch_{group_id}_{message_id}_{sender_id}
            if len(parts) >= 6:
                group_id = int(parts[4])
                message_id = int(parts[5])
                sender_id = int(parts[6])
                login_states[user_id] = {
                    'step': 'add_grp_from_ch',
                    'group_id': group_id,
                    'message_id': message_id,
                    'sender_id': sender_id
                }
                await event.respond("📝 أرسل نص رد القروب الذي تريد إضافته كقالب وإرساله:\n\n(سيتم حفظه في القوالب وإرساله مباشرة كرد في القروب)")
        
        # ============ الرد على الخاص (اختيار القالب) ============
        
        elif data.startswith(b'dm_reply_'):
            parts = data.decode().split('_')
            # dm_reply_{group_id}_{message_id}_{sender_id}
            if len(parts) >= 5:
                group_id = int(parts[2])
                message_id = int(parts[3])
                sender_id = int(parts[4])
                
                # 🔒 كل مستخدم يرد بقوالبه الخاصة هو (+ القوالب العامة للأدمن)
                dm_templates = get_own_templates(user_id, 'DM', config) + config.get('DM_REPLY_TEMPLATES', [])
                if not dm_templates:
                    await event.respond("❌ لا توجد قوالب للرد على الخاص. أضف قالبك أولاً من زر 💬 ردودي على الخاص.")
                    return
                
                if len(dm_templates) == 1:
                    template_text = dm_templates[0]
                    await send_dm_reply(event, group_id, message_id, sender_id, template_text)
                else:
                    buttons = []
                    for i, t in enumerate(dm_templates):
                        preview = t[:30] + "..." if len(t) > 30 else t
                        buttons.append([Button.inline(f"💬 {preview}", f"send_dm_{group_id}_{message_id}_{sender_id}_{i}".encode())])
                    buttons.append([Button.inline('❌ إلغاء', b'cancel_reply')])
                    await event.respond("اختر قالب الرد على الخاص:", buttons=buttons)
        
        elif data.startswith(b'send_dm_'):
            parts = data.decode().split('_')
            # send_dm_{group_id}_{message_id}_{sender_id}_{template_index}
            if len(parts) >= 6:
                group_id = int(parts[2])
                message_id = int(parts[3])
                sender_id = int(parts[4])
                tpl_idx = int(parts[5])
                
                # 🔒 نفس قائمة القوالب المعروضة في الاختيار: الخاصة + العامة
                dm_templates = get_own_templates(user_id, 'DM', config) + config.get('DM_REPLY_TEMPLATES', [])
                if 0 <= tpl_idx < len(dm_templates):
                    template_text = dm_templates[tpl_idx]
                    await send_dm_reply(event, group_id, message_id, sender_id, template_text)
                else:
                    await event.respond("❌ القالب غير موجود.")
        
        # ============ الرد في القروب (اختيار القالب) ============
        
        elif data.startswith(b'grp_reply_'):
            parts = data.decode().split('_')
            # grp_reply_{group_id}_{message_id}_{sender_id}
            if len(parts) >= 5:
                group_id = int(parts[2])
                message_id = int(parts[3])
                sender_id = int(parts[4])
                
                # 🔒 كل مستخدم يرد بقوالبه الخاصة هو (+ القوالب العامة للأدمن)
                grp_templates = get_own_templates(user_id, 'GRP', config) + config.get('GROUP_REPLY_TEMPLATES', [])
                if not grp_templates:
                    await event.respond("❌ لا توجد قوالب للرد في القروب. أضف قالبك أولاً من زر 👥 ردودي في القروب.")
                    return
                
                if len(grp_templates) == 1:
                    template_text = grp_templates[0]
                    await send_group_reply(event, group_id, message_id, sender_id, template_text)
                else:
                    buttons = []
                    for i, t in enumerate(grp_templates):
                        preview = t[:30] + "..." if len(t) > 30 else t
                        buttons.append([Button.inline(f"👥 {preview}", f"send_grp_{group_id}_{message_id}_{sender_id}_{i}".encode())])
                    buttons.append([Button.inline('❌ إلغاء', b'cancel_reply')])
                    await event.respond("اختر قالب الرد في القروب:", buttons=buttons)
        
        elif data.startswith(b'send_grp_'):
            parts = data.decode().split('_')
            # send_grp_{group_id}_{message_id}_{sender_id}_{template_index}
            if len(parts) >= 6:
                group_id = int(parts[2])
                message_id = int(parts[3])
                sender_id = int(parts[4])
                tpl_idx = int(parts[5])
                
                # 🔒 نفس قائمة القوالب المعروضة في الاختيار: الخاصة + العامة
                grp_templates = get_own_templates(user_id, 'GRP', config) + config.get('GROUP_REPLY_TEMPLATES', [])
                if 0 <= tpl_idx < len(grp_templates):
                    template_text = grp_templates[tpl_idx]
                    await send_group_reply(event, group_id, message_id, sender_id, template_text)
                else:
                    await event.respond("❌ القالب غير موجود.")
        
        elif data == b'cancel_reply':
            await event.respond("❌ تم إلغاء الرد.")
        
        # ============ عرض الرسالة - يوديك للرسالة حتى لو لست عضو ============
        
        elif data.startswith(b'go_msg_'):
            parts = data.decode().split('_')
            # go_msg_{group_id}_{message_id}
            if len(parts) >= 4:
                group_id = int(parts[2])
                message_id = int(parts[3])
                
                await event.answer("🔄 جاري البحث عن طريقة للوصول للرسالة...", alert=False)
                
                chat_title = "غير معروف"
                chat_username = None
                invite_link = None
                msg_link = None
                
                # محاولة من كل الحسابات المراقبة
                for phone, client in active_clients.items():
                    try:
                        if not await ensure_connected(client):
                            continue
                        chat = await client.get_entity(group_id)
                        chat_title = getattr(chat, 'title', 'مجموعة غير معروفة')
                        chat_username = getattr(chat, 'username', None)
                        
                        # إذا القروب عام - رابط مباشر يكفي
                        if chat_username:
                            msg_link = f"https://t.me/{chat_username}/{message_id}"
                            logger.info(f"✅ القروب عام - رابط مباشر من الحساب {phone}")
                            # القروب عام = أي شخص يقدر يفتحه
                            await event.edit(
                                f"📨 **{chat_title}**\n🔗 اضغط لفتح الرسالة مباشرة:",
                                buttons=[[Button.url("🔗 افتح الرسالة", url=msg_link)]]
                            )
                            return
                        
                        # بناء رابط الرسالة للقروب الخاص
                        c_id = str(group_id).replace('-100', '')
                        msg_link = f"https://t.me/c/{c_id}/{message_id}"
                        
                        # إذا القروب خاص - نحتاج رابط دعوة
                        try:
                            result = await client(ExportChatInviteRequest(group_id))
                            invite_link = result.link
                            logger.info(f"✅ تم إنشاء رابط دعوة من الحساب {phone}")
                            break
                        except Exception as e1:
                            # البحث عن رابط دعوة موجود
                            try:
                                from telethon.tl.functions.messages import GetExportedChatInvitesRequest
                                me = await client.get_me()
                                invites = await client(GetExportedChatInvitesRequest(
                                    peer=group_id,
                                    admin_id=me,
                                    limit=10
                                ))
                                for inv in invites.invites:
                                    if not getattr(inv, 'revoked', False):
                                        invite_link = inv.link
                                        logger.info(f"✅ تم العثور على رابط دعوة موجود من الحساب {phone}")
                                        break
                                if invite_link:
                                    break
                            except Exception as e2:
                                logger.info(f"الحساب {phone} ما يقدر ينشئ رابط دعوة: {e1} | {e2}")
                                # نجرب الحساب التالي
                        
                    except Exception as e:
                        logger.info(f"الحساب {phone} لا يستطيع الوصول لـ {group_id}: {e}")
                        continue
                
                # بناء الرد النهائي
                if invite_link and msg_link:
                    # قروب خاص - نعطي رابط دعوة + رابط الرسالة
                    await event.edit(
                        f"📨 **{chat_title}** (قروب خاص)\n\n"
                        f"1️⃣ انضم أولاً:\n"
                        f"2️⃣ ثم افتح الرسالة:",
                        buttons=[
                            [Button.url("📩 انضم للقروب", url=invite_link)],
                            [Button.url("🔗 افتح الرسالة", url=msg_link)]
                        ]
                    )
                elif msg_link:
                    # ما قدرنا نجيب رابط دعوة بس رابط الرسالة موجود
                    await event.edit(
                        f"📨 **{chat_title}**\n🔗 افتح الرسالة:",
                        buttons=[[Button.url("🔗 افتح الرسالة", url=msg_link)]]
                    )
                else:
                    await event.respond(f"❌ لم يتم العثور على طريقة للوصول للقروب **{chat_title}**.\n\n💡 القروب خاص ولا يوجد حساب مراقب لديه صلاحية إنشاء رابط دعوة.")
        
        # ============ الرد التلقائي ============
        
        elif data == b'manage_auto_reply':
            auto_reply = config.get('AUTO_REPLY_SETTINGS', {})
            msg = "📨 **إعدادات الرد التلقائي**\n\n"
            if auto_reply:
                for kw, reply in auto_reply.items():
                    preview = reply[:40] + "..." if len(reply) > 40 else reply
                    msg += f"🔑 `{kw}` → 💬 `{preview}`\n"
            else:
                msg += "لا توجد ردود تلقائية محددة.\n\n"
                msg += "💡 عند إضافة رد تلقائي، سيتم إرساله كرد في القروب تلقائياً عند مطابقة الكلمة المفتاحية."
            
            buttons = [
                [Button.inline('➕ إضافة رد تلقائي', b'add_auto_reply')],
                [Button.inline('➖ حذف رد تلقائي', b'rem_auto_reply')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_auto_reply':
            login_states[user_id] = {'step': 'add_auto_reply_keyword'}
            await event.respond("📝 أرسل **الكلمة المفتاحية** التي تريد الرد عليها تلقائياً:")
        
        elif data == b'rem_auto_reply':
            auto_reply = config.get('AUTO_REPLY_SETTINGS', {})
            if not auto_reply:
                await event.respond("❌ لا توجد ردود تلقائية لحذفها.")
            else:
                buttons = []
                for kw, reply in auto_reply.items():
                    preview = reply[:25] + "..." if len(reply) > 25 else reply
                    buttons.append([Button.inline(f"🗑 {kw} → {preview}", f"del_auto_{kw}".encode())])
                buttons.append([Button.inline('🔙 رجوع', b'manage_auto_reply')])
                await event.respond("اختر الرد التلقائي الذي تريد حذفه:", buttons=buttons)
        
        elif data.startswith(b'del_auto_'):
            keyword = data.decode().replace('del_auto_', '')
            auto_reply = config.get('AUTO_REPLY_SETTINGS', {})
            if keyword in auto_reply:
                del auto_reply[keyword]
                config['AUTO_REPLY_SETTINGS'] = auto_reply
                update_json_config(config)
                await event.respond(f"✅ تم حذف الرد التلقائي للكلمة `{keyword}`.")
            else:
                await event.respond("❌ الكلمة غير موجودة.")
        
        # ============ كشف التكرار والحذف التلقائي ============
        
        elif data == b'manage_advanced':
            dup_detection = config.get('DUPLICATE_DETECTION', True)
            auto_delete = config.get('AUTO_DELETE_HOURS', 0)
            
            msg = "🔄 **إعدادات متقدمة**\n\n"
            msg += f"🔍 كشف التكرار: `{'✅ مفعل' if dup_detection else '❌ معطل'}`\n"
            msg += f"⏰ الحذف التلقائي: `{'كل ' + str(auto_delete) + ' ساعة/ساعات' if auto_delete > 0 else '❌ معطل'}`\n\n"
            msg += "💡 **كشف التكرار:** يمنع توجيه نفس الرسالة مرتين\n"
            msg += "💡 **الحذف التلقائي:** يحذف الرسائل المحولة من القناة بعد عدد ساعات محدد"
            
            buttons = [
                [Button.inline('🔍 تبديل كشف التكرار', b'toggle_duplicate')],
                [Button.inline('⏰ تعيين الحذف التلقائي', b'set_auto_delete')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'toggle_duplicate':
            current = config.get('DUPLICATE_DETECTION', True)
            config['DUPLICATE_DETECTION'] = not current
            update_json_config(config)
            await event.respond(f"✅ تم {'تفعيل' if not current else 'تعطيل'} كشف التكرار.")
        
        elif data == b'set_auto_delete':
            login_states[user_id] = {'step': 'set_auto_delete'}
            await event.respond("⏰ أرسل عدد الساعات للحذف التلقائي (0 للتعطيل):")
        
        # رجوع للقائمة الرئيسية
        elif data == b'back_main':
            await start_handler(event)
        
        # ============ إدارة المشرفين (للأدمن الرئيسي فقط) ============
        
        elif data == b'manage_admins':
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المشرفين.", alert=True)
                return
            admins = config.get('ADMINS', [])
            perms_map = config.get('ADMIN_PERMISSIONS', {})
            roles_map = config.get('ADMIN_ROLES', {})
            msg = "👥 **إدارة المستخدمين**\n\n"
            msg += f"👑 **الأدمن الرئيسي:** `{MAIN_ADMIN_ID}`\n"
            extra = sorted(EXTRA_MAIN_ADMINS)
            if extra:
                msg += "🛡 **أدمنة ثابتون:** " + "، ".join(f"`{x}`" for x in extra) + "\n"
            msg += "\n"
            if admins:
                msg += "📋 **المضافون:**\n"
                for i, a in enumerate(admins, 1):
                    role = roles_map.get(str(a), 'user')
                    if role == 'supervisor':
                        role = 'user'
                    granted = len(perms_map.get(str(a), []))
                    msg += f"{i}. `{a}` — {ROLES.get(role, '👤 مستخدم')} — {granted}/{len(PERMISSIONS)} صلاحية\n"
            else:
                msg += "📋 **المضافون:** لا يوجد\n"
            msg += ("\n💡 لإضافة مستخدم جديد، أرسل معرّفه الرقمي.\n"
                    "💡 الرتبتان: أدمن (كل الصلاحيات) ومستخدم (يضيف حسابه ويستخدم ما تسمح به صلاحياته).")
            buttons = [
                [Button.inline('➕ إضافة مستخدم', b'add_admin')],
                [Button.inline('🎛 الرتب والصلاحيات', b'perm_admins')],
                [Button.inline('✅ اعتمادات قروبات التوجيه', b'approve_groups')],
                [Button.inline('➖ حذف مستخدم', b'rem_admin')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_admin':
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المستخدمين.", alert=True)
                return
            login_states[user_id] = {'step': 'add_admin'}
            await event.respond(
                "📝 أرسل **معرّف المستخدم الرقمي (ID)** للمستخدم الجديد:\n\n"
                "💡 سيدخل برتبة (مستخدم) ويستطيع فوراً إضافة حسابه المراقب بنفسه.\n"
                "💡 للحصول على المعرّف: توجّه إلى @userinfobot في تيليجرام وأرسل أي رسالة، سيعيد لك معرّفك."
            )
        
        elif data == b'rem_admin':
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المستخدمين.", alert=True)
                return
            admins = config.get('ADMINS', [])
            if not admins:
                await event.respond("❌ لا يوجد مستخدمون مضافون للحذف.")
            else:
                buttons = [[Button.inline(str(a), f"del_admin_{a}".encode())] for a in admins]
                buttons.append([Button.inline('🔙 رجوع', b'manage_admins')])
                await event.respond("🗑 اختر المستخدم الذي تريد حذفه:", buttons=buttons)
        
        elif data.startswith(b'del_admin_'):
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المشرفين.", alert=True)
                return
            try:
                admin_id = int(data.decode().replace('del_admin_', ''))
            except ValueError:
                await event.respond("❌ معرّف غير صحيح.")
                return
            config = load_json_config()
            admins = config.get('ADMINS', [])
            if admin_id in admins:
                admins.remove(admin_id)
                config['ADMINS'] = admins
                perms_map = config.get('ADMIN_PERMISSIONS', {})
                perms_map.pop(str(admin_id), None)
                config['ADMIN_PERMISSIONS'] = perms_map
                config.get('ADMIN_ROLES', {}).pop(str(admin_id), None)
                ug_map = config.get('USER_GROUPS', {})
                ug_map.pop(str(admin_id), None)
                config['USER_GROUPS'] = ug_map
                update_json_config(config)
                await event.respond(f"✅ تم حذف المستخدم `{admin_id}`.")
                logger.info(f"👑 الأدمن حذف مستخدم: {admin_id}")
            else:
                await event.respond("❌ المستخدم غير موجود.")

        # ============ محرر صلاحيات المشرفين (للأدمن الرئيسي فقط) ============
        
        elif data == b'perm_admins':
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المستخدمين.", alert=True)
                return
            admins = config.get('ADMINS', [])
            if not admins:
                await event.respond("❌ لا يوجد مستخدمون. أضف مستخدماً أولاً.", buttons=[[Button.inline('🔙 رجوع', b'manage_admins')]])
            else:
                perms_map = config.get('ADMIN_PERMISSIONS', {})
                roles_map = config.get('ADMIN_ROLES', {})
                rows = []
                for a in admins:
                    granted = len(perms_map.get(str(a), []))
                    role_l = ROLES.get(roles_map.get(str(a), 'user'), '👤')
                    rows.append([Button.inline(f"{role_l} {a} ({granted}/{len(PERMISSIONS)})", f"perm_of_{a}".encode())])
                rows.append([Button.inline('🔙 رجوع', b'manage_admins')])
                await event.respond("🎛 **الرتب والصلاحيات**\n\nاختر مستخدماً لتحديد رتبته وصلاحياته وأزراره:", buttons=rows)
        
        elif data.startswith(b'perm_of_'):
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المشرفين.", alert=True)
                return
            try:
                admin_id_str = data.decode().replace('perm_of_', '')
                int(admin_id_str)
            except ValueError:
                await event.respond("❌ معرّف غير صحيح.")
                return
            text, rows = build_perm_screen(admin_id_str, config)
            await event.respond(text, buttons=rows)

        elif data.startswith(b'setrole_'):
            try:
                body = data.decode()[8:]
                role, uid_str = body.split('_', 1)
                int(uid_str)
            except (ValueError, IndexError):
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            if role not in ROLES:
                await event.answer("❌ رتبة غير معروفة.", alert=True)
                return
            roles_map = config.get('ADMIN_ROLES', {})
            roles_map[uid_str] = role
            config['ADMIN_ROLES'] = roles_map
            if role == 'admin':
                perms_map = config.get('ADMIN_PERMISSIONS', {})
                perms_map[uid_str] = list(PERMISSIONS.keys())
                config['ADMIN_PERMISSIONS'] = perms_map
            update_json_config(config)
            logger.info(f"🏷 تم تغيير رتبة {uid_str} إلى {role} بواسطة {user_id}")
            await event.answer(f"✅ الرتبة الآن: {ROLES[role]}")
            text, rows = build_perm_screen(uid_str, load_json_config())
            try:
                await event.edit(text, buttons=rows)
            except Exception:
                pass
        
        elif data.startswith(b'tgl_'):
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المشرفين.", alert=True)
                return
            try:
                body = data.decode()[4:]
                perm, admin_id_str = body.rsplit('_', 1)
                int(admin_id_str)
            except (ValueError, IndexError):
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            if perm not in PERMISSIONS:
                await event.answer("❌ صلاحية غير معروفة.", alert=True)
                return
            perms_map = config.get('ADMIN_PERMISSIONS', {})
            granted = perms_map.get(admin_id_str, [])
            if perm in granted:
                granted.remove(perm)
                result_msg = "❌ تم سحب الصلاحية — الزر سيختفي من قائمته"
            else:
                granted.append(perm)
                result_msg = "✅ تم منح الصلاحية — الزر سيظهر في قائمته"
            perms_map[admin_id_str] = granted
            config['ADMIN_PERMISSIONS'] = perms_map
            update_json_config(config)
            logger.info(f"🎛 الأدمن {result_msg} '{perm}' للمستخدم {admin_id_str}")
            await event.answer(result_msg)
            text, rows = build_perm_screen(admin_id_str, load_json_config())
            try:
                await event.edit(text, buttons=rows)
            except Exception:
                pass
        
        # ============ تقرير روابط القروبات ============
        
        elif data == b'report_links':
            # 🔒 العزل: الأدمن الكامل يرى كل الحسابات — العضو يحصل على تقرير حساباته هو فقط
            if is_full_admin(user_id):
                scope = list(active_clients.keys())
            else:
                scope = owned_active_phones(user_id)
            if not scope:
                await event.respond("❌ لا توجد حسابات مرتبطة بحسابك — أضف حسابك أولاً من ➕ إضافة حسابي.")
            else:
                rows = [[Button.inline(p, f"rlink_{p}".encode())] for p in scope]
                rows.append([Button.inline('🔙 رجوع', b'manage_groups')])
                await event.respond("📄 اختر الحساب لاستخراج تقرير روابط قروباته\n(سيصلك الملف هنا مباشرة):", buttons=rows)
        
        elif data.startswith(b'rlink_'):
            phone = data.decode()[6:]
            # 🔒 العزل: لا يمكن استخراج تقرير حساب مستخدم آخر
            if not is_full_admin(user_id) and not phone_owned(user_id, phone):
                await event.answer("🚫 يمكنك استخراج تقرير حساباتك أنت فقط.", alert=True)
                return
            if phone not in active_clients:
                await event.respond("❌ الحساب غير موجود (ربما حُذف).")
                return
            await event.answer("⏳ جاري جمع القروبات والروابط...")
            status_msg = await event.respond(f"⏳ جاري جمع قروبات الحساب `{phone}` وروابطها... سيصلك الملف مباشرة خلال لحظات.")
            # تشغيل غير حاجب — يضمن الاستجابة الفورية ويضمن وصول التقرير أو إشعار الفشل
            asyncio.create_task(export_group_links(active_clients[phone], phone, target_id=user_id, status_msg=status_msg))
        
        elif data == b'unset_mygroup':
            if not is_main_admin(user_id):
                await event.answer("🚫 للأدمن الرئيسي فقط.", alert=True)
                return
            config['ADMIN_GROUP_ID'] = 0
            update_json_config(config)
            await event.respond("✅ تم إلغاء قروب النسخ الشامل.")
        
        # ============ قروب استقبال كل الرسائل (للأدمن) ============
        
        elif data == b'admingroup':
            if not is_main_admin(user_id):
                await event.answer("🚫 هذا الخيار للأدمن الرئيسي/الثابت فقط.", alert=True)
                return
            ag = config.get('ADMIN_GROUP_ID', 0)
            status = f"✅ مُفعّل — `{ag}`" if ag else "❌ غير مُفعّل بعد"
            await event.respond(
                "📤 **قروب استقبال كل الرسائل**\n\n"
                "كل رسالة تُوجّه من أي مشترك بالبوت (مشرفاً كان أو عضواً أو غيرهم) تُرسل نسخة منها إلى هذا القروب أيضاً.\n\n"
                f"الحالة الحالية: {status}\n\n"
                "💡 يمكنك أيضاً التعيين بإرسال `/mygroup` داخل القروب المطلوب.",
                buttons=[
                    [Button.inline('✏️ تعيين بالمعرّف (ID)', b'set_admingroup_btn')],
                    [Button.inline('📌 تعيين القروب الرسمي (hsjjjjihsjs)', b'set_official_group')],
                    [Button.inline('❌ إلغاء القروب', b'unset_mygroup')],
                    [Button.inline('🔙 رجوع', b'back_main')]
                ]
            )
        
        elif data == b'set_admingroup_btn':
            if not is_main_admin(user_id):
                await event.answer("🚫 للأدمن الرئيسي/الثابت فقط.", alert=True)
                return
            login_states[user_id] = {'step': 'set_admingroup'}
            await event.respond("📝 أرسل **معرّف القروب** الرقمي (يبدأ عادةً بـ -100) أو @اسم القروب:")
        
        elif data == b'set_official_group':
            if not is_main_admin(user_id):
                await event.answer("🚫 للأدمن الرئيسي/الثابت فقط.", alert=True)
                return
            await event.answer("📌 جاري تعيين القروب الرسمي...")
            try:
                raw = OFFICIAL_GROUP.replace('https://t.me/', '@').replace('t.me/', '@').strip()
                entity = await bot.get_entity(raw)
                og_id = int(entity.chat_id if hasattr(entity, 'chat_id') and entity.chat_id else entity.id)
                title = getattr(entity, 'title', None) or str(og_id)
                config['ADMIN_GROUP_ID'] = og_id
                update_json_config(config)
                await event.respond(
                    f"✅ تم تعيين **القروب الرسمي لاستقبال كل الرسائل**!\n\n"
                    f"📤 القروب: {title} (`{og_id}`)\n"
                    f"📨 ستصله نسخة من كل رسالة تُوجّه من أي مشترك بالبوت."
                )
                logger.info(f"📌 تم تعيين القروب الرسمي: {og_id} ({title}) بواسطة {user_id}")
            except Exception as e:
                await event.respond(
                    f"❌ تعذر تعيين القروب الرسمي ({OFFICIAL_GROUP}).\n\n"
                    f"⚠️ تأكد أن البوت **عضو في القروب** أولاً ثم أعد المحاولة.\n"
                    f"تفاصيل: {str(e)[:120]}"
                )
        
        # ============ السجلات والتخزين (للأدمن) ============
        
        elif data == b'logs_menu':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            count, oldest, size = get_logs_stats()
            try:
                days = max(1, int(config.get('LOG_RETENTION_DAYS', 3)))
            except (TypeError, ValueError):
                days = 3
            try:
                disk = shutil.disk_usage(DATA_DIR)
                disk_line = f"💽 **مساحة التخزين:** {fmt_size(disk.used)} مستخدمة من {fmt_size(disk.total)} (المتاح: {fmt_size(disk.free)})\n"
            except Exception:
                disk_line = ""
            oldest_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest)) if oldest else '—'
            msg = (
                "💾 **السجلات والتخزين**\n\n"
                f"📝 سجلات الدردشة الموجهة: **{count}** رسالة\n"
                f"📦 حجم ملف السجلات: **{fmt_size(size)}**\n"
                f"🕐 أقدم سجل: **{oldest_str}**\n"
                f"⏱ الحذف التلقائي: **كل {days} يوم/أيام**\n"
                f"{disk_line}\n"
                "💡 تُحذف سجلات الدردشة تلقائياً بعد مدة الاحتفاظ حتى لا تمتلئ المساحة،\n"
                "ويمكنك تعديل المدة أو حذف كل السجلات فوراً من الأزرار."
            )
            await event.respond(msg, buttons=[
                [Button.inline('🗑 حذف كل السجلات الآن', b'confirm_logs')],
                [Button.inline('⏱ تحديد مدة الحذف التلقائي', b'set_log_retention_btn')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ])
        
        elif data == b'confirm_logs':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            count, oldest, size = get_logs_stats()
            await event.respond(
                f"⚠️ **تأكيد الحذف**\n\n"
                f"سيتم حذف **{count}** سجل دردشة ({fmt_size(size)}) نهائياً.\n"
                f"هذا لا يؤثر على الرسائل في تيليجرام — فقط سجلات البوت المحلية.",
                buttons=[
                    [Button.inline('✅ نعم، احذف الآن', b'wipe_logs')],
                    [Button.inline('❌ تراجع', b'logs_menu')]
                ]
            )
        
        elif data == b'wipe_logs':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            freed = 0
            try:
                if os.path.exists(CHAT_LOG_FILE):
                    freed = os.path.getsize(CHAT_LOG_FILE)
                    os.remove(CHAT_LOG_FILE)
                logger.info(f"🗑 الأدمن {user_id} حذف كل السجلات — تم تحرير {freed} بايت")
            except Exception as e:
                await event.respond(f"❌ خطأ في حذف السجلات: {str(e)[:100]}")
                return
            await event.respond(
                f"✅ **تم حذف كل السجلات بنجاح!**\n\n"
                f"💾 المساحة المحررة: **{fmt_size(freed)}**\n"
                f"🧹 سجلات الدردشة الجديدة ستُحذف تلقائياً حسب المدة المحددة."
            )
        
        elif data == b'set_log_retention_btn':
            if not is_full_admin(user_id):
                await event.answer("🚫 للأدمن فقط.", alert=True)
                return
            login_states[user_id] = {'step': 'set_log_retention'}
            try:
                days = max(1, int(config.get('LOG_RETENTION_DAYS', 3)))
            except (TypeError, ValueError):
                days = 3
            await event.respond(
                f"⏱ **مدة الحذف التلقائي للسجلات**\n\n"
                f"المدة الحالية: **{days} يوم/أيام**\n\n"
                f"📝 أرسل عدد الأيام التي تريد الاحتفاظ بسجلات الدردشة قبل حذفها تلقائياً\n"
                f"(رقم بين 1 و 365 — مثال: `3`)\n\n"
                f"💡 للإلغاء أرسل: `/cancel`"
            )
        
        # ============ الإذاعة: نشر تحديثات وتنبيهات لجميع مستخدمي البوت ============
        
        elif data == b'broadcast_btn':
            if not has_perm(user_id, 'broadcast'):
                await event.answer("🚫 لا تملك صلاحية الإذاعة.", alert=True)
                return
            config = load_json_config()
            total = len({MAIN_ADMIN_ID, *EXTRA_MAIN_ADMINS, *config.get('ADMINS', [])}) - 1  # -1 لاستثناء المرسل نفسه
            login_states[user_id] = {'step': 'broadcast'}
            await event.respond(
                "📢 **الإذاعة لجميع مستخدمي البوت**\n\n"
                f"سيصل التنبيه إلى **{total} مستخدم** (الأدمنة والمشرفون والأعضاء).\n\n"
                "✍️ أرسل الآن **نص الرسالة أو التنبيه** الذي تريد نشره:\n\n"
                "💡 للإلغاء أرسل: `/cancel`"
            )
        
        # ============ اعتمادات قروبات التوجيه للمشرفين والأعضاء ============
        
        elif data == b'approve_groups':
            if not is_full_admin(user_id):
                # 🔒 العزل: شاشة اعتمادات الجميع للأدمن الكامل فقط — العضو يدير قروباته هو فقط
                text_mf, rows_mf = build_myfwd_screen(user_id, config)
                await event.respond("🎯 إدارة قروبات باقي المستخدمين تحتاج رتبة أدمن.\n\nهذه شاشة قروباتك أنت:\n\n" + text_mf, buttons=rows_mf)
                return
            users = config.get('ADMINS', [])
            fg = config.get('FORWARD_GROUPS', [])
            ug = config.get('USER_GROUPS', {})
            ag_map = config.get('ACCOUNT_GROUPS', {})
            roles_map = config.get('ADMIN_ROLES', {})
            msg = ("✅ **اعتمادات قروبات التوجيه**\n\n"
                   "يستقبل البوت الرسائل الملتقطة في 3 أماكن: القناة الرئيسية، قروب استقبال كل الرسائل، "
                   "والقروبات المعتمدة هنا.\n\n"
                   f"📦 القروبات المتاحة حالياً: **{len(fg)}**\n\n"
                   "👤 **للمشرفين والأعضاء:** اعتمد قروبات لكل مستخدم\n"
                   "📱 **للحسابات المراقبة:** اعتمد قروبات لكل حساب مباشرة\n")
            rows = []
            for a in users:
                cnt = len(ug.get(str(a), []))
                role_l = ROLES.get(roles_map.get(str(a), 'user'), '👤')
                rows.append([Button.inline(f"{role_l} {a} ({cnt} قروب)", f"ugsel_{a}".encode())])
            rows.append([Button.inline('📱 اعتمادات الحسابات المراقبة', b'accsel_menu')])
            rows.append([Button.inline('➕ إضافة قروب إلى القائمة', b'add_fwd_group_btn')])
            if fg:
                rows.append([Button.inline('🗑 حذف قروب من القائمة', b'delfwd_menu')])
            rows.append([Button.inline('🔙 رجوع', b'back_main')])
            await event.respond(msg, buttons=rows)

        # ============ اعتماد قروبات التوجيه لكل حساب مراقب مباشرة ============

        elif data == b'accsel_menu':
            if not is_full_admin(user_id):
                await event.answer("🚫 هذه الشاشة للأدمن — اعتمد قروبك من 🎯 قروب توجيه رسائلي.", alert=True)
                return
            ag_map = config.get('ACCOUNT_GROUPS', {})
            if not active_clients:
                await event.respond(
                    "❌ لا توجد حسابات مراقبة مربوطة حالياً."
                    "\n\nأضف حساباً أولاً من ➕ إضافة حساب.",
                    buttons=[[Button.inline('🔙 رجوع', b'approve_groups')]]
                )
            else:
                rows = []
                for phone in active_clients.keys():
                    cnt = len(ag_map.get(str(phone), []))
                    rows.append([Button.inline(f"📱 {phone} ({cnt} قروب)", f"accsel_{phone}".encode())])
                rows.append([Button.inline('🔙 رجوع', b'approve_groups')])
                await event.respond(
                    "📱 **اعتمادات الحسابات المراقبة**\n\n"
                    "اختر حساباً لتحديد القروبات التي ستستقبل نسخ الرسائل الملتقطة منه:",
                    buttons=rows
                )

        elif data.startswith(b'accsel_'):
            phone = data.decode()[7:]
            if not is_full_admin(user_id):
                await event.answer("🚫 هذه الشاشة للأدمن — اعتمد قروبك من 🎯 قروب توجيه رسائلي.", alert=True)
                return
            if not phone:
                await event.respond("❌ بيانات غير صحيحة.")
                return
            fg = config.get('FORWARD_GROUPS', [])
            ag_map = config.get('ACCOUNT_GROUPS', {})
            acc_groups = ag_map.get(phone, [])
            if not fg:
                await event.respond(
                    "📦 لا توجد قروبات في قائمة الاعتمادات بعد — أضف قروباً أولاً.",
                    buttons=[[Button.inline('➕ إضافة قروب', b'add_fwd_group_btn')], [Button.inline('🔙 رجوع', b'accsel_menu')]]
                )
            else:
                rows = []
                for g in fg:
                    mark = '✅' if g['id'] in acc_groups else '❌'
                    rows.append([Button.inline(f"{mark} {g.get('title', g['id'])}", f"acgt_{g['id']}_{phone}".encode())])
                rows.append([Button.inline('➕ إضافة قروب جديد', b'add_fwd_group_btn')])
                rows.append([Button.inline('🔙 رجوع', b'accsel_menu')])
                await event.respond(f"📱 **قروبات الحساب** `{phone}`\n\nاضغط على القروب لتفعيل/تعطيل توجيه نسخ رسائل هذا الحساب إليه:", buttons=rows)

        elif data.startswith(b'acgt_'):
            try:
                body = data.decode()[5:]
                gid_str, phone = body.rsplit('_', 1)
                gid = int(gid_str)
            except (ValueError, IndexError):
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            if not is_full_admin(user_id):
                await event.answer("🚫 اعتمادات حسابات الآخرين للأدمن فقط — استخدم 🎯 قروب توجيه رسائلي.", alert=True)
                return
            ag_map = config.get('ACCOUNT_GROUPS', {})
            lst = ag_map.get(phone, [])
            if gid in lst:
                lst.remove(gid)
                result_msg = "❌ أُلغي اعتماد القروب لهذا الحساب"
            else:
                lst.append(gid)
                result_msg = "✅ تم الاعتماد — نسخ رسائل هذا الحساب ستُوجّه إليه"
            ag_map[phone] = lst
            config['ACCOUNT_GROUPS'] = ag_map
            update_json_config(config)
            logger.info(f"{result_msg}: قروب {gid} للحساب {phone} بواسطة {user_id}")
            await event.answer(result_msg)
            fg = config.get('FORWARD_GROUPS', [])
            acc_groups = ag_map.get(phone, [])
            rows = []
            for g in fg:
                mark = '✅' if g['id'] in acc_groups else '❌'
                rows.append([Button.inline(f"{mark} {g.get('title', g['id'])}", f"acgt_{g['id']}_{phone}".encode())])
            rows.append([Button.inline('➕ إضافة قروب جديد', b'add_fwd_group_btn')])
            rows.append([Button.inline('🔙 رجوع', b'accsel_menu')])
            try:
                await event.edit(f"📱 **قروبات الحساب** `{phone}`\n\nاضغط على القروب لتفعيل/تعطيل توجيه نسخ رسائل هذا الحساب إليه:", buttons=rows)
            except Exception:
                pass

        elif data == b'delfwd_menu':
            if not is_full_admin(user_id):
                await event.answer("🚫 حذف القروبات من القائمة العامة للأدمن فقط.", alert=True)
                return
            fg = config.get('FORWARD_GROUPS', [])
            if not fg:
                await event.respond("❌ لا توجد قروبات في القائمة.")
            else:
                rows = [[Button.inline(f"🗑 {g.get('title', g['id'])}", f"delfwd_{g['id']}".encode())] for g in fg]
                rows.append([Button.inline('🔙 رجوع', b'approve_groups')])
                await event.respond("🗑 اختر القروب الذي تريد حذفه من قائمة الاعتمادات:", buttons=rows)
        
        elif data.startswith(b'delfwd_'):
            try:
                gid = int(data.decode()[7:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            if not is_full_admin(user_id):
                await event.answer("🚫 حذف القروبات من القائمة العامة للأدمن فقط.", alert=True)
                return
            config['FORWARD_GROUPS'] = [g for g in config.get('FORWARD_GROUPS', []) if g.get('id') != gid]
            ug = config.get('USER_GROUPS', {})
            for uid_s in list(ug.keys()):
                ug[uid_s] = [g for g in ug[uid_s] if g != gid]
            config['USER_GROUPS'] = ug
            update_json_config(config)
            await event.answer("🗑 تم حذف القروب من قائمة الاعتمادات")
            logger.info(f"🗑 تم حذف قروب التوجيه {gid} بواسطة {user_id}")
        
        elif data.startswith(b'ugsel_'):
            uid_str = data.decode()[6:]
            if not is_full_admin(user_id):
                await event.answer("🚫 اعتمادات باقي المستخدمين للأدمن فقط — استخدم 🎯 قروب توجيه رسائلي.", alert=True)
                return
            if not uid_str.isdigit():
                await event.respond("❌ معرّف غير صحيح.")
                return
            fg = config.get('FORWARD_GROUPS', [])
            ug = config.get('USER_GROUPS', {})
            user_groups = ug.get(uid_str, [])
            if not fg:
                await event.respond(
                    "📦 لا توجد قروبات في قائمة الاعتمادات بعد — أضف قروباً أولاً.",
                    buttons=[[Button.inline('➕ إضافة قروب', b'add_fwd_group_btn')], [Button.inline('🔙 رجوع', b'approve_groups')]]
                )
            else:
                rows = []
                for g in fg:
                    mark = '✅' if g['id'] in user_groups else '❌'
                    rows.append([Button.inline(f"{mark} {g.get('title', g['id'])}", f"ugt_{g['id']}_{uid_str}".encode())])
                rows.append([Button.inline('➕ إضافة قروب جديد', b'add_fwd_group_btn')])
                rows.append([Button.inline('🔙 رجوع', b'approve_groups')])
                await event.respond(f"👥 **قروبات المستخدم** `{uid_str}`\n\nاضغط على القروب لتفعيل/تعطيل توجيه نسخ رسائل حساباته إليه:", buttons=rows)
        
        elif data.startswith(b'ugt_'):
            try:
                body = data.decode()[4:]
                gid_str, uid_str = body.rsplit('_', 1)
                gid = int(gid_str)
                int(uid_str)
            except (ValueError, IndexError):
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            if not is_full_admin(user_id):
                await event.answer("🚫 اعتمادات باقي المستخدمين للأدمن فقط — استخدم 🎯 قروب توجيه رسائلي.", alert=True)
                return
            ug = config.get('USER_GROUPS', {})
            lst = ug.get(uid_str, [])
            if gid in lst:
                lst.remove(gid)
                result_msg = "❌ أُلغي اعتماد القروب لهذا المستخدم"
            else:
                lst.append(gid)
                result_msg = "✅ تم الاعتماد — نسخ رسائل حساباته ستُوجّه إليه"
            ug[uid_str] = lst
            config['USER_GROUPS'] = ug
            update_json_config(config)
            logger.info(f"{result_msg}: قروب {gid} للمستخدم {uid_str} بواسطة {user_id}")
            await event.answer(result_msg)
            fg = config.get('FORWARD_GROUPS', [])
            user_groups = ug.get(uid_str, [])
            rows = []
            for g in fg:
                mark = '✅' if g['id'] in user_groups else '❌'
                rows.append([Button.inline(f"{mark} {g.get('title', g['id'])}", f"ugt_{g['id']}_{uid_str}".encode())])
            rows.append([Button.inline('➕ إضافة قروب جديد', b'add_fwd_group_btn')])
            rows.append([Button.inline('🔙 رجوع', b'approve_groups')])
            try:
                await event.edit(f"👥 **قروبات المستخدم** `{uid_str}`\n\nاضغط على القروب لتفعيل/تعطيل توجيه نسخ رسائل حساباته إليه:", buttons=rows)
            except Exception:
                pass
        
        elif data == b'add_fwd_group_btn':
            if not is_full_admin(user_id):
                await event.answer("🚫 الإضافة للقائمة العامة للأدمن — اعتمد قروبك من 🎯 قروب توجيه رسائلي.", alert=True)
                return
            login_states[user_id] = {'step': 'add_fwd_group'}
            await event.respond(
                "📝 أرسل بيانات القروب بأحد الصيغ:\n\n"
                "• المعرّف الرقمي (مثال: `-1001234567890`)\n"
                "• اسم المستخدم العام (مثال: `@mygroup`)\n"
                "• رابط القروب (مثال: `https://t.me/mygroup`)\n\n"
                "⚠️ يجب أن يكون البوت عضواً في القروب ليعمل التوجيه."
            )
        
        # ============ 🎯 قروب توجيه رسائلي (خدمة ذاتية لكل مستخدم — عزل تام) ============

        elif data == b'myfwd':
            text_mf, rows_mf = build_myfwd_screen(user_id, config)
            await event.respond(text_mf, buttons=rows_mf)

        elif data == b'myfwd_add':
            login_states[user_id] = {'step': 'my_add_group'}
            await event.respond(
                "📝 أرسل بيانات القروب الذي تريد توجيه رسائلك إليه بأحد الصيغ:\n\n"
                "• رابط القروب (مثال: `https://t.me/mygroup`)\n"
                "• اسم المستخدم العام (مثال: `@mygroup`)\n"
                "• المعرّف الرقمي (مثال: `-1001234567890`)\n\n"
                "⚠️ يجب أن يكون البوت **عضواً في القروب** ليصله التوجيه — أضفه أولاً إن لم يكن.\n"
                "💡 يمكن لعدة مستخدمين اعتماد نفس القروب، وكل واحد يرى اعتماده هو فقط.\n"
                "💡 للإلغاء أرسل: `/cancel`"
            )

        elif data.startswith(b'myfwd_tgl_'):
            try:
                gid = int(data.decode()[10:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            ug = config.get('USER_GROUPS', {})
            lst = ug.get(str(user_id), [])
            if gid not in lst:
                await event.answer("ℹ️ القروب غير معتمد لديك بالفعل.", alert=True)
                return
            lst.remove(gid)
            ug[str(user_id)] = lst
            config['USER_GROUPS'] = ug
            update_json_config(config)
            logger.info(f"🎯 المستخدم {user_id} ألغى اعتماد قروب التوجيه {gid}")
            await event.answer("🗑 أُلغي الاعتماد — لن تصله رسائل حساباتك.")
            text_mf, rows_mf = build_myfwd_screen(user_id, load_json_config())
            try:
                await event.edit(text_mf, buttons=rows_mf)
            except Exception:
                pass

        elif data.startswith(b'myfwd_test_'):
            try:
                gid = int(data.decode()[11:])
            except ValueError:
                await event.answer("❌ بيانات غير صحيحة.", alert=True)
                return
            if gid not in config.get('USER_GROUPS', {}).get(str(user_id), []):
                await event.answer("🚫 هذا القروب ليس ضمن قروباتك المعتمدة.", alert=True)
                return
            await event.answer("🧪 جاري إرسال رسالة اختبار...")
            try:
                await bot.send_message(gid, "🧪 **رسالة اختبار من بوت المراقبة**\n\n✅ إذا كنت ترى هذه الرسالة في هذا القروب فالتوجيه يعمل بنجاح.")
                await event.respond(f"✅ تم إرسال رسالة الاختبار إلى القروب (`{gid}`) بنجاح — التوجيه يعمل!")
            except Exception as te:
                await event.respond(
                    f"❌ فشل الإرسال إلى القروب (`{gid}`).\n\n"
                    "⚠️ السبب الأكثر شيوعاً: البوت ليس عضواً في القروب أو لا يملك صلاحية الإرسال.\n"
                    "💡 أضف البوت إلى القروب وامنحه صلاحية إرسال الرسائل ثم أعد الاختبار.\n"
                    f"تفاصيل: {str(te)[:120]}"
                )

        # إدارة باقي العناصر (إضافة/حذف يدوي للمجموعات والكلمات)
        elif data in [b'add_ignore', b'rem_ignore', b'add_group', b'rem_group']:
            login_states[user_id] = {'step': data.decode()}
            await event.respond(f"📝 من فضلك أرسل القيمة التي تريد تنفيذ الإجراء عليها:")

    # ============ دوال إرسال الرد ============
    
    async def send_dm_reply(event, group_id, message_id, sender_id, template_text):
        """إرسال رد على الخاص للمرسل"""
        try:
            target_client = None
            # 🔌 إعادة الاتصال التلقائي قبل أي إرسال — يمنع خطأ Cannot send requests while disconnected
            for phone, client in active_clients.items():
                try:
                    if not await ensure_connected(client):
                        continue
                    await client.get_entity(group_id)
                    target_client = client
                    break
                except:
                    continue
            
            if not target_client:
                await event.respond("❌ لا يوجد حساب مرتبط يمكنه الرد في هذه المجموعة.")
                return
            
            try:
                sender_entity = await target_client.get_entity(sender_id)
                try:
                    await target_client.send_message(sender_entity, template_text)
                except Exception as se:
                    # 🔌 إعادة اتصال ومحاولة أخيرة قبل الإبلاغ بالفشل
                    if not await ensure_connected(target_client):
                        raise se
                    await target_client.send_message(sender_entity, template_text)
                preview = template_text[:40] + "..." if len(template_text) > 40 else template_text
                await event.respond(f"✅ تم إرسال الرد على الخاص:\n\n💬 `{preview}`")
                logger.info(f"تم إرسال رد خاص للمرسل {sender_id}")
            except Exception as e:
                await event.respond(f"❌ فشل إرسال الرسالة الخاصة. قد يكون المرسل قد أغلق الخاص.\n\nالخطأ: {str(e)[:100]}")
                logger.error(f"خطأ في إرسال رد خاص: {e}")
        
        except Exception as e:
            await event.respond(f"❌ خطأ في إرسال الرد: {str(e)[:100]}")
            logger.error(f"خطأ في send_dm_reply: {e}")
    
    async def send_group_reply(event, group_id, message_id, sender_id, template_text):
        """إرسال رد في القروب كرد على رسالة المرسل"""
        try:
            target_client = None
            # 🔌 إعادة الاتصال التلقائي قبل أي إرسال — يمنع خطأ Cannot send requests while disconnected
            for phone, client in active_clients.items():
                try:
                    if not await ensure_connected(client):
                        continue
                    await client.get_entity(group_id)
                    target_client = client
                    break
                except:
                    continue
            
            if not target_client:
                await event.respond("❌ لا يوجد حساب مرتبط يمكنه الرد في هذه المجموعة.")
                return
            
            try:
                await target_client.send_message(group_id, template_text, reply_to=message_id)
            except Exception as se:
                # 🔌 إعادة اتصال ومحاولة أخيرة قبل الإبلاغ بالفشل
                if not await ensure_connected(target_client):
                    raise se
                await target_client.send_message(group_id, template_text, reply_to=message_id)
            preview = template_text[:40] + "..." if len(template_text) > 40 else template_text
            await event.respond(f"✅ تم إرسال الرد في القروب:\n\n👥 `{preview}`")
            logger.info(f"تم إرسال رد في القروب {group_id} على رسالة {message_id}")
        
        except Exception as e:
            await event.respond(f"❌ خطأ في إرسال الرد في القروب: {str(e)[:100]}")
            logger.error(f"خطأ في send_group_reply: {e}")

    # ============ معالج الإدخال النصي ============
    
    @bot.on(events.NewMessage())
    async def input_handler(event):
        user_id = event.sender_id
        if user_id not in login_states: return
        # ===== فحص صلاحية الأدمن لكل إدخال =====
        if not is_admin(user_id):
            await event.respond(UNAUTHORIZED_MSG)
            del login_states[user_id]
            return
        state = login_states[user_id]
        text = event.message.message.strip()
        config = load_json_config()
        
        # ===== إلغاء أي عملية جارية =====
        if text in ('/cancel', 'إلغاء', 'الغاء'):
            # 🧹 إغلاق أي عميل تسجيل معلق قبل حذف الحالة — يمنع database is locked عند إعادة المحاولة
            _cancel_st = login_states.get(user_id, {})
            for _cl in (_cancel_st.get('client'), _cancel_st.get('tmp_client')):
                if _cl is not None:
                    try:
                        await _cl.disconnect()
                    except Exception:
                        pass
            delete_claim_session(user_id)
            del login_states[user_id]
            await event.respond("❌ تم إلغاء العملية الحالية.")
            return
        
        # ===== الإذاعة: نشر رسالة لجميع مستخدمي البوت =====
        if state['step'] == 'broadcast':
            if not has_perm(user_id, 'broadcast'):
                await event.respond("🚫 لا تملك صلاحية الإذاعة.")
                del login_states[user_id]
                return
            if len(text) > 3800:
                await event.respond(
                    f"❌ الرسالة طويلة جداً ({len(text)} حرف).\n"
                    "الحد الأقصى 3800 حرف — أرسل نسخة أقصر أو `/cancel`."
                )
                return  # نُبقي الحالة ليحاول مرة أخرى
            recipients = {MAIN_ADMIN_ID, *EXTRA_MAIN_ADMINS, *config.get('ADMINS', [])}
            recipients.discard(user_id)  # المرسل يعرف الرسالة أصلاً
            sent, failed = 0, 0
            failed_ids = []
            header = "📢 **تنبيه من أدمن البوت**\n\n"
            status_msg = await event.respond(f"⏳ جاري نشر الرسالة إلى {len(recipients)} مستخدم...")
            for uid in recipients:
                try:
                    await bot.send_message(uid, header + text)
                    sent += 1
                except Exception as be:
                    failed += 1
                    failed_ids.append(str(uid))
                    logger.warning(f"📢 فشل إرسال الإذاعة إلى {uid}: {str(be)[:80]}")
                await asyncio.sleep(0.15)  # تجنّب ضغط FloodWait على تيليجرام
            report = (
                f"📢 **تقرير الإذاعة**\n\n"
                f"✅ وصلت إلى: **{sent}** مستخدم\n"
                f"❌ لم تصل: **{failed}**"
            )
            if failed_ids:
                report += "\n\n⚠️ تعذّر الوصول إلى: `" + "`, `".join(failed_ids) + "`"
                report += "\n\n💡 السبب الأشهر: المستخدم لم يفتح محادثة مع البوت أبداً — اطلب منه إرسال /start أولاً."
            try:
                await status_msg.delete()
            except Exception:
                pass
            await event.respond(report)
            logger.info(f"📢 إذاعة من {user_id}: نجاح {sent}، فشل {failed}")
            del login_states[user_id]
            return
        
        # ===== إضافة مشرف جديد (للأدمن الرئيسي فقط) =====
        if state['step'] == 'add_admin':
            if not has_perm(user_id, 'add_admins'):
                await event.respond("🚫 لا تملك صلاحية إدارة المشرفين.")
                del login_states[user_id]
                return
            try:
                new_admin_id = int(text.strip())
                if new_admin_id == MAIN_ADMIN_ID:
                    await event.respond("ℹ️ هذا هو الأدمن الرئيسي بالفعل، لا يحتاج لإضافة.")
                    del login_states[user_id]
                    return
                if new_admin_id in EXTRA_MAIN_ADMINS:
                    await event.respond("ℹ️ هذا المعرّف أدمن ثابت يملك صلاحيات كاملة بالفعل، لا يحتاج لإضافة.")
                    del login_states[user_id]
                    return
                admins = config.get('ADMINS', [])
                if new_admin_id in admins:
                    await event.respond(f"ℹ️ المشرف `{new_admin_id}` موجود بالفعل.")
                else:
                    admins.append(new_admin_id)
                    config['ADMINS'] = admins
                    perms_map = config.get('ADMIN_PERMISSIONS', {})
                    perms_map[str(new_admin_id)] = ['view_stats']
                    config['ADMIN_PERMISSIONS'] = perms_map
                    roles_map = config.get('ADMIN_ROLES', {})
                    roles_map[str(new_admin_id)] = 'user'
                    config['ADMIN_ROLES'] = roles_map
                    update_json_config(config)
                    await event.respond(
                        f"✅ تم إضافة المستخدم `{new_admin_id}` بنجاح!\n\n"
                        f"👤 رتبته: مستخدم — يستطيع فوراً إضافة حسابه المراقب بنفسه.\n"
                        f"🎛 حدّد رتبته وصلاحياته من: إدارة المشرفين → الرتب والصلاحيات\n\n"
                        f"يمكنه الآن استخدام البوت عبر إرسال /start"
                    )
                    logger.info(f"👑 الأدمن أضاف مستخدماً جديداً: {new_admin_id}")
                del login_states[user_id]
            except ValueError:
                await event.respond("❌ المعرّف غير صحيح. أرسل رقم صحيح (مثال: 7853478744)")

        # ===== تعيين قروب استقبال كل الرسائل (بمعرّف أو @username أو رابط) =====
        elif state['step'] == 'set_admingroup':
            if not is_main_admin(user_id):
                await event.respond("🚫 للأدمن الرئيسي/الثابت فقط.")
                del login_states[user_id]
                return
            raw = text.replace('https://t.me/', '@').replace('t.me/', '@').strip()
            try:
                entity = await bot.get_entity(raw)
                gid = int(entity.chat_id if hasattr(entity, 'chat_id') and entity.chat_id else entity.id)
                title = getattr(entity, 'title', None) or str(gid)
                config['ADMIN_GROUP_ID'] = gid
                update_json_config(config)
                await event.respond(
                    f"✅ تم تعيين **قروب استقبال كل الرسائل** بنجاح!\n\n"
                    f"📤 القروب: {title} (`{gid}`)\n\n"
                    f"📢 ستصلك نسخة من كل رسالة تُوجّه من أي مشترك بالبوت (مشرفاً كان أو عضواً أو غيره)."
                )
                logger.info(f"📤 تم تعيين قروب استقبال كل الرسائل: {gid} ({title}) بواسطة {user_id}")
            except Exception as e:
                await event.respond(
                    f"❌ لم أتمكن من التعرف على القروب.\n\n"
                    f"⚠️ تأكد أن البوت عضو في القروب وأن الصيغة صحيحة:\n"
                    f"• معرّف رقمي (مثال: `-1001234567890`)\n• @username أو رابط t.me\n\n"
                    f"تفاصيل الخطأ: {str(e)[:120]}"
                )
            del login_states[user_id]

        # ===== إضافة قروب توجيه إلى قائمة الاعتمادات =====
        elif state['step'] == 'add_fwd_group':
            if not has_perm(user_id, 'manage_groups'):
                await event.respond("🚫 لا تملك صلاحية اعتماد القروبات.")
                del login_states[user_id]
                return
            raw = text.replace('https://t.me/', '@').replace('t.me/', '@').strip()
            try:
                entity = await bot.get_entity(raw)
                gid = int(entity.chat_id if hasattr(entity, 'chat_id') and entity.chat_id else entity.id)
                title = getattr(entity, 'title', None) or str(gid)
                fg = config.get('FORWARD_GROUPS', [])
                if any(g.get('id') == gid for g in fg):
                    await event.respond(f"ℹ️ القروب **{title}** (`{gid}`) موجود بالفعل في قائمة الاعتمادات.")
                else:
                    fg.append({'id': gid, 'title': title})
                    config['FORWARD_GROUPS'] = fg
                    update_json_config(config)
                    await event.respond(
                        f"✅ تم إضافة القروب إلى قائمة الاعتمادات!\n\n"
                        f"📦 القروب: {title} (`{gid}`)\n"
                        f"📊 إجمالي القروبات المعتمدة: {len(fg)}\n\n"
                        f"💡 الآن اعتمده لمستخدم أو حساب من: ✅ اعتمادات قروبات التوجيه"
                    )
                    logger.info(f"📦 تم إضافة قروب توجيه: {gid} ({title}) بواسطة {user_id}")
            except Exception as e:
                await event.respond(
                    f"❌ لم أتمكن من التعرف على القروب.\n\n"
                    f"⚠️ تأكد أن البوت عضو في القروب وأن الصيغة صحيحة:\n"
                    f"• معرّف رقمي (مثال: `-1001234567890`)\n• @username أو رابط t.me\n\n"
                    f"تفاصيل الخطأ: {str(e)[:120]}"
                )
            del login_states[user_id]

        # ===== 🎯 اعتماد قروب توجيه ذاتي من المستخدم (بدون تدخل الأدمن) =====
        elif state['step'] == 'my_add_group':
            gid, title, err = await resolve_fwd_group(text)
            if gid is None:
                await event.respond(
                    f"❌ {err}\n\n"
                    "⚠️ تأكد أن البوت عضو في القروب وأن الصيغة صحيحة:\n"
                    "• رابط أو @username أو معرّف رقمي\n"
                    "💡 أعد إرسال البيانات أو أرسل `/cancel` للإلغاء."
                )
                return  # نُبقي الحالة لإعادة المحاولة
            fg = config.get('FORWARD_GROUPS', [])
            already_fg = any(g.get('id') == gid for g in fg)
            if not already_fg:
                fg.append({'id': gid, 'title': title})
                config['FORWARD_GROUPS'] = fg
            ug = config.get('USER_GROUPS', {})
            lst = ug.get(str(user_id), [])
            re_added = gid in lst
            if not re_added:
                lst.append(gid)
            ug[str(user_id)] = lst
            config['USER_GROUPS'] = ug
            update_json_config(config)
            note = "ℹ️ كان معتمداً لديك بالفعل." if re_added else "✅ تم اعتماده لحسابك الآن."
            shared_note = "" if already_fg else "\n📦 أُضيف أيضاً إلى القائمة العامة ليتيح لمستخدمين آخرين اعتماد نفس القروب."
            await event.respond(
                f"🎉 **تم اعتماد قروب التوجيه بنجاح — بنفسك وبدون تدخل الأدمن!**\n\n"
                f"📦 القروب: {title} (`{gid}`)\n"
                f"{note}{shared_note}\n\n"
                "📨 نسخ رسائل حساباتك الملتقطة ستُوجَّه إلى هذا القروب.\n"
                "🧪 جرّب زر 🧪 اختبار في شاشة (🎯 قروب توجيه رسائلي) — وإن فشل فأضف البوت إلى القروب."
            )
            logger.info(f"🎯 المستخدم {user_id} اعتمد قروب توجيه ذاتياً: {gid} ({title})")
            del login_states[user_id]

        # إضافة حساب - رقم الهاتف
        elif state['step'] == 'await_phone':
            phone = normalize_phone(text.strip())  # ⭐ توحيد الصيغة: يمنع ازدواج الحساب بأشكال مختلفة لنفس الرقم
            state['phone'] = phone  # نحفظ الرقم الموحّد في الحالة فوراً (يُستخدم في التنظيف والمطابقة)
            # 🧹 إن كانت هناك محاولة سابقة معلقة لنفس المستخدم — أغلق عميلها قبل إنشاء عميل جديد
            # (بدون هذا يُفتح ملف الجلسة من عميلين → database is locked عند إعادة إرسال الرقم)
            _prev = state.get('tmp_client')
            if _prev is not None:
                try:
                    await _prev.disconnect()
                except Exception:
                    pass
                state.pop('tmp_client', None)
            # 🔒 منع خطأ database is locked: ملف الجلسة (SQLite) لا يُفتح من عميلين معاً —
            # نغلق أي عميل قديم مفتوح على نفس الرقم قبل إنشاء عميل جديد
            # 1) محاولات تسجيل معلقة على نفس الرقم بأي صيغة (من مستخدمين آخرين — حالة المستخدم الحالي عُولجت أعلاه)
            _phone_d = _phone_digits(phone)
            for _uid, _st in list(login_states.items()):
                if _uid == user_id:
                    continue
                if _phone_d and _phone_digits(_st.get('phone')) == _phone_d and _st.get('step') in ('await_code', 'await_password', 'await_phone'):
                    for _cl in (_st.get('client'), _st.get('tmp_client')):
                        if _cl is not None:
                            try:
                                await _cl.disconnect()
                            except Exception:
                                pass
                    login_states.pop(_uid, None)
                    try:
                        await bot.send_message(_uid, f"ℹ️ أُلغيت عملية تسجيل الحساب `{phone}` لأن مستخدماً آخر أضاف نفس الرقم الآن.")
                    except Exception:
                        pass
                    logger.info(f"🔒 أُغلق عميل تسجيل معلق على {phone} (كان للمستخدم {_uid}) لمنع database is locked")
            # 2) عميل مراقب نشط لنفس الرقم بأي صيغة (مستعاد بعد النشر أو مربوط سابقاً)
            akey, old_active = find_active_client(phone)
            if old_active is not None:
                try:
                    already_auth = await old_active.is_user_authorized()
                except Exception:
                    already_auth = False
                if already_auth:
                    # ✅ الحساب يعمل فعلاً — يُربط بحساب المستخدم مباشرة بدون كود وبدون أي تحقق إضافي
                    # (الآلية كما كانت: حتى لو مضاف مسبقاً لدى مستخدم آخر — ينضاف فوراً بدون خطأ)
                    set_account_owner(akey, state.get('owner') or user_id)
                    del login_states[user_id]
                    await event.respond(
                        f"✅ الحساب `{akey}` **مربوط ويعمل بالفعل** — رُبط بحسابك الآن بدون أي كود.\n\n"
                        "🔑 يمكنك إدارة كلماته وإعداداته وقروب توجيهه من قائمتك مباشرة."
                    )
                    logger.info(f"✅ المستخدم {user_id} ربط {akey} المربوط مسبقاً بنفسه مباشرة (بدون كود)")
                    return
                # عميل ميت/غير مصرح — أغلقه ونكمل التسجيل من جديد
                try:
                    await old_active.disconnect()
                except Exception:
                    pass
                active_clients.pop(akey, None)
                logger.info(f"🔒 أُغلق عميل مراقب قديم غير مصرح لـ {akey} قبل إعادة التسجيل (database is locked)")
            new_client = TelegramClient(find_session_path(phone), API_ID, API_HASH, **CLIENT_OPTS)
            state['tmp_client'] = new_client  # نحفظ المرجع فوراً — يُغلق تلقائياً عند إعادة المحاولة أو الإلغاء
            await new_client.connect()
            # ✨ استرداد فوري بدون كود: إن كانت جلسة هذا الرقم محفوظة على القرص (بأي صيغة) ومصرّحاً بها من قبل
            # (الحساب أُضيف سابقاً ثم فُقد من قائمة الحسابات النشطة بعد إعادة نشر أو خطأ استئناف) —
            # نستخدم الجلسة مباشرة بدون أي كود تحقق (طلب كود من جلسة مصرّح بها كان يسبب «انتهت صلاحية الكود» فوراً)
            try:
                _pre_auth = await new_client.is_user_authorized()
            except Exception:
                _pre_auth = False
            if _pre_auth:
                # ✅ جلسة صالحة محفوظة — ربط فوري بدون كود، والملكية للمستخدم الحالي دائماً (كما كانت الآلية)
                active_clients[phone] = new_client
                set_account_owner(phone, state.get('owner') or user_id)
                save_session_string(phone, new_client)
                _owner_done = state.get('owner') or user_id
                del login_states[user_id]
                await event.respond(
                    f"✅ تم ربط الحساب `{phone}` بنجاح — **بدون كود تحقق!**\n\n"
                    "♻️ توجد جلسة صالحة محفوظة لهذا الرقم من ربط سابق فأعدنا استخدامها مباشرة.\n"
                    "📦 جاري استيراد المجموعات وبدء المراقبة..."
                )
                logger.info(f"♻️ المستخدم {user_id} أعاد ربط {phone} فوراً من جلسة محفوظة مصرّح بها (بدون كود) — المالك: {_owner_done}")
                try:
                    new_count = await import_groups(new_client)
                    await event.respond(f"📦 تم استيراد `{new_count}` مجموعة (المراقبة تشمل جميع المجموعات).")
                except Exception as _ig_err:
                    logger.warning(f"⚠️ تعذر استيراد المجموعات لـ {phone}: {_ig_err}")
                register_handler(new_client, phone)
                asyncio.create_task(start_monitoring(new_client, phone))
                return
            try:
                try:
                    sent_code = await new_client.send_code_request(phone)
                except AuthRestartError:
                    # ♻️ جلسة قديمة تطلب إعادة تهيئة تدفق الدخول — نحذف ملف الجلسة المحلي ونبدأ جلسة نظيفة
                    try:
                        await new_client.disconnect()
                    except Exception:
                        pass
                    try:
                        _sess_path = os.path.join(SESSION_DIR, f'session_{phone}.session')
                        if os.path.exists(_sess_path):
                            os.remove(_sess_path)
                    except Exception:
                        pass
                    logger.info(f"♻️ جلسة قديمة لـ {phone} طلبت إعادة تهيئة (AuthRestart) — أُنشئت جلسة نظيفة")
                    new_client = TelegramClient(os.path.join(SESSION_DIR, f'session_{phone}'), API_ID, API_HASH, **CLIENT_OPTS)
                    state['tmp_client'] = new_client
                    await new_client.connect()
                    sent_code = await new_client.send_code_request(phone)
                login_states[user_id] = {'step': 'await_code', 'phone': phone, 'hash': sent_code.phone_code_hash, 'client': new_client, 'owner': state.get('owner', user_id), 'last_code_req': time.time(), 'expired_count': 0}
                await event.respond(
                    f"📩 تم إرسال الكود إلى `{phone}`.\n\n"
                    "📥 **من أين يأتي الكود؟**\n"
                    "• إذا كان الحساب مفتوحاً في تطبيق تيليجرام (أي جهاز) → الكود يصل **رسالة داخل التطبيق** من محادثة **Telegram** الرسمية — افتحها وانسخ الكود.\n"
                    "• إذا لم يكن الحساب مفتوحاً في أي مكان → يصلك **SMS** على الرقم.\n\n"
                    "⏳ أرسل الكود هنا **فوراً** — صلاحيته دقائق قليلة فقط.\n"
                    "📌 **مهم:** انسخ الكود من **أحدث رسالة** في محادثة Telegram — الأكواد القديمة من محاولات سابقة لا تعمل.\n"
                    "🧷 يمكنك نسخ رسالة الكود **كاملة** وسأستخرج الرقم بنفسي.\n"
                    "⚠️ لا تطلب كوداً من تيليجرام ويب أو أي تطبيق آخر بالتوازي — كل طلب جديد **يُبطل** هذا الكود.\n"
                    "💡 للإلغاء أرسل: `/cancel`"
                )
            except PhoneNumberInvalidError:
                # ❌ تنسيق الرقم خاطئ — نُبقي الحالة ليعيد إرسال الرقم مباشرة
                await event.respond(
                    "❌ **تنسيق الرقم غير صحيح.**\n\n"
                    "أرسل الرقم مع مفتاح الدولة — مثال: `+9665xxxxxxxx`\n"
                    "💡 أعد إرسال الرقم الآن بالصيغة الصحيحة، أو أرسل `/cancel` للإلغاء."
                )
            except FloodWaitError as fw:
                # ⛔ تيليجرام حظر طلبات الكود مؤقتاً — نُبقي الحالة ليعيد بعد المدة
                await event.respond(
                    f"⛔ **تيليجرام يطلب الانتظار {fw.seconds} ثانية** قبل إرسال كود لهذا الرقم (بسبب طلبات متكررة).\n\n"
                    f"⏳ انتظر المدة المذكورة ثم أعد إرسال الرقم هنا، أو أرسل `/cancel`."
                )
            except PhoneNumberFloodError:
                # ⛔ كثرة أكواد لهذا الرقم — حظر طويل نسبياً
                await event.respond(
                    "⛔ **تم إرسال أكواد كثيرة لهذا الرقم مؤخراً** — تيليجرام يمنع إرسال أكواد جديدة مؤقتاً (قد تدوم ساعة أو أكثر).\n\n"
                    "⏳ انتظر فترة ثم أعد المحاولة من ➕ إضافة حسابي، أو أرسل `/cancel`."
                )
            except Exception as e:
                # 🧹 نغلق العميل المعلق قبل حذف الحالة — يمنع بقاء ملف الجلسة مفتوحاً عند إعادة المحاولة
                _tc = state.get('tmp_client')
                if _tc is not None:
                    try:
                        await _tc.disconnect()
                    except Exception:
                        pass
                if 'database is locked' in str(e).lower():
                    await event.respond(
                        "🔒 **قاعدة بيانات الجلسة مشغولة مؤقتاً** (كان ملف الجلسة مفتوحاً من عملية أخرى).\n\n"
                        "⏳ انتظر 30 ثانية ثم أعد المحاولة من ➕ إضافة حسابي — البوت يغلق العملية القديمة تلقائياً الآن.\n"
                        "💡 إن تكررت: أعد تشغيل عملية الإضافة بعد دقيقة واحدة."
                    )
                    del login_states[user_id]
                else:
                    await event.respond(f"❌ خطأ: {e}"); del login_states[user_id]

        # إضافة حساب - رمز التحقق
        elif state['step'] == 'await_code':
            try:
                client = state['client']
                if client is None:
                    del login_states[user_id]
                    await event.respond("⚠️ انقطعت عملية التسجيل — أعد ➕ إضافة حسابي من جديد.")
                    return
                # 🔌 التأكد من الاتصال قبل تسجيل الدخول — يمنع خطأ Cannot send requests while disconnected
                if not client.is_connected():
                    await client.connect()
                # 🧷 استخراج الكود الرقمي من نص الرسالة — يدعم النسخ الكامل لرسالة «Login code: 12345»
                # والأرقام العربية ٠١٢٣ — الأولوية لكود مسبوق بكلمة code/كود/رمز، وإلا أحدث مجموعة أرقام (طول 4-8)
                _txt_d = (text or '').translate(_CODE_DIGITS_MAP)
                _labeled = re.findall(r'(?:code|كود|الكود|رمز)[^0-9]{0,20}(\d{4,8})', _txt_d, re.IGNORECASE)
                if _labeled:
                    code_val = _labeled[-1]
                else:
                    _groups = re.findall(r'\d+', _txt_d)
                    if _groups:
                        _cand = [g for g in _groups if 4 <= len(g) <= 8]
                        code_val = (_cand[-1] if _cand else ''.join(_groups))
                    else:
                        code_val = (text or '').strip()
                await client.sign_in(state['phone'], code_val, phone_code_hash=state['hash'])
                await event.respond(f"✅ تم ربط الحساب `{state['phone']}` بنجاح! جاري استيراد المجموعات...")
                
                new_count = await import_groups(client)
                await event.respond(f"📦 تم استيراد `{new_count}` مجموعة (المراقبة تشمل جميع المجموعات).")
                
                # 📋 تقرير روابط القروبات يُرسل تلقائياً للأدمن الرئيسي كملف
                asyncio.create_task(export_group_links(client, state['phone']))
                
                active_clients[state['phone']] = client
                # ربط الحساب بمالكه (المستخدم الذي أضافه) — يتيح له إدارته لاحقاً
                set_account_owner(state['phone'], state.get('owner') or user_id)
                # 💾 حفظ الجلسة بشكل دائم (StringSession في config على الـ Volume) — لا تضيع مع إعادة النشر
                save_session_string(state['phone'], client)
                # تسجيل المعالج أولاً ثم بدء المراقبة
                register_handler(client, state['phone'])
                asyncio.create_task(start_monitoring(client, state['phone']))
                del login_states[user_id]
            except SessionPasswordNeededError:
                state['step'] = 'await_password'
                await event.respond("🔐 هذا الحساب محمي بكلمة سر (2FA). من فضلك أرسل كلمة السر:")
            except PhoneCodeExpiredError:
                # ⏳ انتهت صلاحية الكود — رسالة بسيطة كما كانت الآلية سابقاً (بدون إعادة إرسال تلقائية وبدون أزرار)
                del login_states[user_id]
                await event.respond(
                    "⏳ **انتهت صلاحية الكود.**\n\n"
                    "🔄 أعد ➕ إضافة الحساب من القائمة، وعند وصول الكود أرسله هنا **فوراً** (صلاحيته دقائق قليلة).\n"
                    "📌 انسخ الكود من **أحدث رسالة** في محادثة Telegram الرسمية — الأكواد القديمة لا تعمل."
                )
            except PhoneCodeInvalidError:
                # ❌ الكود غير صحيح — نُبقي الحالة ليعيد إرسال الكود الصحيح مباشرة
                await event.respond(
                    "❌ **الكود غير صحيح.**\n\n"
                    "📌 انسخ الكود من **أحدث رسالة** في محادثة Telegram الرسمية وأرسله هنا.\n"
                    "🧷 يمكنك نسخ رسالة الكود **كاملة** وسأستخرج الرقم بنفسي."
                )
            except Exception as e:
                await event.respond(f"❌ خطأ: {e}"); del login_states[user_id]

        # إضافة حساب - كلمة المرور (2FA)
        elif state['step'] == 'await_password':
            try:
                client = state.get('client')
                if client is None:
                    del login_states[user_id]
                    await event.respond("⚠️ انقطعت عملية التسجيل — أعد ➕ إضافة حسابي من جديد.")
                    return
                # 🔌 التأكد من الاتصال قبل تسجيل الدخول
                if not client.is_connected():
                    await client.connect()
                await client.sign_in(password=text)
                await event.respond(f"✅ تم ربط الحساب `{state['phone']}` بنجاح!")
                
                # 📋 تقرير روابط القروبات يُرسل تلقائياً للأدمن الرئيسي كملف
                asyncio.create_task(export_group_links(client, state['phone']))
                
                active_clients[state['phone']] = client
                # ربط الحساب بمالكه (المستخدم الذي أضافه)
                set_account_owner(state['phone'], state.get('owner') or user_id)
                # 💾 حفظ الجلسة بشكل دائم (StringSession في config على الـ Volume)
                save_session_string(state['phone'], client)
                # تسجيل المعالج أولاً ثم بدء المراقبة
                register_handler(client, state['phone'])
                asyncio.create_task(start_monitoring(client, state['phone']))
                del login_states[user_id]
            except PasswordHashInvalidError:
                # ❌ كلمة سر التحقق بخطوتين خاطئة — نُبقي الحالة ليعيد المحاولة فوراً (بدل إلغاء العملية كلها)
                await event.respond(
                    "❌ **كلمة السر غير صحيحة.**\n\n"
                    "🔐 هذا الحساب محمي بالتحقق بخطوتين — أرسل كلمة السر الصحيحة الآن لإعادة المحاولة.\n"
                    "💡 للإلغاء أرسل: `/cancel`"
                )
            except Exception as e:
                await event.respond(f"❌ خطأ: {e}"); del login_states[user_id]

        # إضافة كلمة مفتاحية خاصة بالمستخدم
        elif state['step'] == 'add_kw':
            # 🔒 خصوصية: الكلمات تُحفظ في قائمة المستخدم الخاص — لا تُشارك مع غيره
            # 🧩 يقبل عدة كلمات دفعة واحدة (سطر لكل كلمة أو مفصولة بفواصل) — ويمنع تخزين نص طويل/متعدد الأسطر ككلمة واحدة
            _tokens = parse_keywords_input(text)
            if not _tokens:
                await event.respond(
                    "⚠️ لم أجد كلمة صالحة في رسالتك.\n\n"
                    "أرسل كلمة واحدة أو عدة كلمات: كل سطر كلمة، أو مفصولة بفواصل `،` أو `,`.\n"
                    f"💡 الطول الأقصى للكلمة {MAX_KEYWORD_LEN} حرفاً — مثال: `حل واجب`\n"
                    "💡 للإلغاء أرسل: `/cancel`"
                )
                return  # نُبقي الحالة ليعيد الإرسال مباشرة
            _defaults = get_default_keywords(config)
            _deleted = get_user_deleted_defaults(user_id, config)
            my_kw = get_user_keywords(user_id, config)
            restored, already_def, exists, added = [], [], [], []
            for _txt in _tokens:
                if _txt in _deleted:
                    restore_user_default(user_id, _txt, config)
                    restored.append(_txt)
                elif _txt in _defaults:
                    already_def.append(_txt)
                elif _txt in my_kw:
                    exists.append(_txt)
                else:
                    added.append(_txt)
            if added:
                my_kw.extend(added)
                set_user_keywords(user_id, my_kw, config)
            parts = []
            if added: parts.append("✅ أُضيفت لقائمتك: `" + "`, `".join(added) + "`")
            if restored: parts.append("♻️ استُعيدت من الافتراضية المحذوفة عندك: `" + "`, `".join(restored) + "`")
            if already_def: parts.append("ℹ️ افتراضية مفعّلة أصلاً (لا حاجة للإضافة): `" + "`, `".join(already_def) + "`")
            if exists: parts.append("ℹ️ موجودة في قائمتك بالفعل: `" + "`, `".join(exists) + "`")
            await event.respond("\n".join(parts))
            del login_states[user_id]

        # 👥 أدمن يضيف كلمات لمستخدم آخر من شاشة مراقبته
        elif state['step'] == 'admin_add_kw_for':
            target = state.get('target')
            _tokens = parse_keywords_input(text)
            if not target or not _tokens:
                await event.respond(
                    "⚠️ لم أجد كلمة صالحة.\n\n"
                    "أرسل كلمة واحدة أو عدة كلمات: كل سطر كلمة، أو مفصولة بفواصل `،` أو `,`.\n"
                    f"💡 الطول الأقصى للكلمة {MAX_KEYWORD_LEN} حرفاً — للإلغاء أرسل `/cancel`."
                )
                return
            _defaults = get_default_keywords(config)
            _deleted = get_user_deleted_defaults(target, config)
            t_kw = get_user_keywords(target, config)
            added, exists, already_def, restored = [], [], [], []
            for t in _tokens:
                if t in _deleted:
                    restore_user_default(target, t, config)
                    restored.append(t)
                elif t in _defaults:
                    already_def.append(t)
                elif t in t_kw:
                    exists.append(t)
                else:
                    added.append(t)
            if added:
                t_kw.extend(added)
                set_user_keywords(target, t_kw, config)
            tparts = []
            if added: tparts.append("✅ أُضيفت لمستخدم: `" + "`, `".join(added) + "`")
            if restored: tparts.append("♻️ استُعيدت له من الافتراضية المحذوفة: `" + "`, `".join(restored) + "`")
            if already_def: tparts.append("ℹ️ افتراضية مفعّلة أصلاً عنده: `" + "`, `".join(already_def) + "`")
            if exists: tparts.append("ℹ️ موجودة في كلماته بالفعل: `" + "`, `".join(exists) + "`")
            await event.respond("\n".join(tparts))
            logger.info(f"🔑 الأدمن {user_id} عدّل كلمات المستخدم {target}: أضاف {len(added)} واستعاد {len(restored)}")
            del login_states[user_id]
            await send_user_monitor_screen(event, user_id, target)

        # إضافة مستخدم للتجاهل
        elif state['step'] == 'add_ignore':
            try:
                config['IGNORE_USERS'] = list(set(config.get('IGNORE_USERS', []) + [int(text)]))
                update_json_config(config)
                await event.respond(f"✅ تم إضافة المعرف `{text}` لقائمة التجاهل."); del login_states[user_id]
            except: await event.respond("❌ المعرف غير صحيح.")

        # حذف مستخدم من التجاهل
        elif state['step'] == 'rem_ignore':
            try:
                config['IGNORE_USERS'] = [u for u in config.get('IGNORE_USERS', []) if u != int(text)]
                update_json_config(config)
                await event.respond(f"✅ تم حذف المعرف `{text}` من قائمة التجاهل."); del login_states[user_id]
            except: await event.respond("❌ المعرف غير صحيح.")

        # إضافة مجموعة يدوي
        elif state['step'] == 'add_group':
            try:
                group_id = int(text)
                groups = config.get('TARGET_GROUPS', [])
                if group_id not in groups:
                    groups.append(group_id)
                    config['TARGET_GROUPS'] = groups
                    update_json_config(config)
                    await event.respond(f"✅ تم إضافة المجموعة `{group_id}`.")
                else:
                    await event.respond("⚠️ المجموعة موجودة بالفعل.")
            except:
                await event.respond("❌ المعرف غير صحيح.")
            del login_states[user_id]

        # حذف مجموعة يدوي
        elif state['step'] == 'rem_group':
            try:
                group_id = int(text)
                groups = config.get('TARGET_GROUPS', [])
                if group_id in groups:
                    groups.remove(group_id)
                    config['TARGET_GROUPS'] = groups
                    update_json_config(config)
                    await event.respond(f"✅ تم حذف المجموعة `{group_id}`.")
                else:
                    await event.respond("⚠️ المجموعة غير موجودة.")
            except:
                await event.respond("❌ المعرف غير صحيح.")
            del login_states[user_id]

        # إضافة كلمة إعلانية محظورة
        elif state['step'] == 'add_banned_ad':
            banned_ads = config.get('BANNED_ADS', [])
            if text not in banned_ads:
                banned_ads.append(text)
                config['BANNED_ADS'] = banned_ads
                update_json_config(config)
                await event.respond(f"✅ تم إضافة الكلمة الإعلانية المحظورة: `{text}`")
            else:
                await event.respond(f"⚠️ الكلمة `{text}` موجودة بالفعل.")
            del login_states[user_id]

        # إضافة كلمة مشبوهة
        elif state['step'] == 'add_suspicious':
            suspicious = config.get('SUSPICIOUS_WORDS', [])
            if text not in suspicious:
                suspicious.append(text)
                config['SUSPICIOUS_WORDS'] = suspicious
                update_json_config(config)
                await event.respond(f"✅ تم إضافة الكلمة المشبوهة: `{text}`")
            else:
                await event.respond(f"⚠️ الكلمة `{text}` موجودة بالفعل.")
            del login_states[user_id]

        # تغيير الحد الأقصى للأحرف
        elif state['step'] == 'set_max_length':
            try:
                new_max = int(text)
                if new_max == 0 or (10 <= new_max <= 500):
                    filters = config.get('FILTERS', {})
                    filters['max_length'] = new_max
                    config['FILTERS'] = filters
                    update_json_config(config)
                    if new_max == 0:
                        await event.respond("✅ تم تعطيل حد الأحرف - الآن جميع الرسائل بلا قيد الطول ستُمرر")
                    else:
                        await event.respond(f"✅ تم تغيير الحد الأقصى للأحرف إلى `{new_max}`")
                else:
                    await event.respond("❌ الرقم يجب أن يكون 0 (بدون حد) أو بين 10 و 500")
            except ValueError:
                await event.respond("❌ من فضلك أرسل رقماً صحيحاً")
            del login_states[user_id]

        # ============ إضافة قالب الرد على الخاص (خاص بالمستخدم) ============
        elif state['step'] == 'add_dm_template':
            # 🔒 خصوصية: القالب يُحفظ في قوالب المستخدم الخاص
            dm_templates = get_own_templates(user_id, 'DM', config)
            dm_templates.append(text)
            set_own_templates(user_id, 'DM', dm_templates, config)
            preview = text[:50] + "..." if len(text) > 50 else text
            await event.respond(f"✅ تم إضافة القالب إلى **ردودك الخاصة**:\n\n💬 `{preview}`")
            del login_states[user_id]

        # ============ إضافة قالب الرد في القروب (خاص بالمستخدم) ============
        elif state['step'] == 'add_grp_template':
            # 🔒 خصوصية: القالب يُحفظ في قوالب المستخدم الخاص
            grp_templates = get_own_templates(user_id, 'GRP', config)
            grp_templates.append(text)
            set_own_templates(user_id, 'GRP', grp_templates, config)
            preview = text[:50] + "..." if len(text) > 50 else text
            await event.respond(f"✅ تم إضافة القالب إلى **ردودك الخاصة**:\n\n👥 `{preview}`")
            del login_states[user_id]

        # ============ إضافة قالب عام (أدمن) — الخاص ============
        elif state['step'] == 'add_gdm_template':
            if not is_full_admin(user_id):
                await event.respond("🚫 للأدمن فقط.")
                del login_states[user_id]
                return
            dm_templates = config.get('DM_REPLY_TEMPLATES', [])
            dm_templates.append(text)
            config['DM_REPLY_TEMPLATES'] = dm_templates
            update_json_config(config)
            preview = text[:50] + "..." if len(text) > 50 else text
            await event.respond(f"✅ تم إضافة القالب **العام** (يظهر للجميع):\n\n💬 `{preview}`")
            del login_states[user_id]

        # ============ إضافة قالب عام (أدمن) — القروب ============
        elif state['step'] == 'add_ggrp_template':
            if not is_full_admin(user_id):
                await event.respond("🚫 للأدمن فقط.")
                del login_states[user_id]
                return
            grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
            grp_templates.append(text)
            config['GROUP_REPLY_TEMPLATES'] = grp_templates
            update_json_config(config)
            preview = text[:50] + "..." if len(text) > 50 else text
            await event.respond(f"✅ تم إضافة القالب **العام** (يظهر للجميع):\n\n👥 `{preview}`")
            del login_states[user_id]

        # ============ إضافة رد مباشر من القناة - خاص ============
        elif state['step'] == 'add_dm_from_ch':
            group_id = state.get('group_id')
            message_id = state.get('message_id')
            sender_id = state.get('sender_id')
            
            # حفظ القالب في ردود المستخدم الخاصة (خصوصية)
            dm_templates = get_own_templates(user_id, 'DM', config)
            dm_templates.append(text)
            set_own_templates(user_id, 'DM', dm_templates, config)
            
            # إرسال الرد مباشرة
            await send_dm_reply(event, group_id, message_id, sender_id, text)
            preview = text[:40] + "..." if len(text) > 40 else text
            await event.respond(f"💾 تم حفظ القالب في **ردودك الخاصة** أيضاً: `{preview}`")
            del login_states[user_id]

        # ============ إضافة رد مباشر من القناة - قروب ============
        elif state['step'] == 'add_grp_from_ch':
            group_id = state.get('group_id')
            message_id = state.get('message_id')
            sender_id = state.get('sender_id')
            
            # حفظ القالب في ردود المستخدم الخاصة (خصوصية)
            grp_templates = get_own_templates(user_id, 'GRP', config)
            grp_templates.append(text)
            set_own_templates(user_id, 'GRP', grp_templates, config)
            
            # إرسال الرد مباشرة
            await send_group_reply(event, group_id, message_id, sender_id, text)
            preview = text[:40] + "..." if len(text) > 40 else text
            await event.respond(f"💾 تم حفظ القالب في **ردودك الخاصة** أيضاً: `{preview}`")
            del login_states[user_id]

        # ============ إضافة رد تلقائي - الكلمة المفتاحية ============
        elif state['step'] == 'add_auto_reply_keyword':
            login_states[user_id] = {'step': 'add_auto_reply_message', 'keyword': text}
            await event.respond(f"📝 الآن أرسل **نص الرد التلقائي** للكلمة `{text}`:")

        # ============ إضافة رد تلقائي - نص الرسالة ============
        elif state['step'] == 'add_auto_reply_message':
            keyword = state.get('keyword', '')
            auto_reply = config.get('AUTO_REPLY_SETTINGS', {})
            auto_reply[keyword] = text
            config['AUTO_REPLY_SETTINGS'] = auto_reply
            update_json_config(config)
            preview = text[:40] + "..." if len(text) > 40 else text
            await event.respond(f"✅ تم إضافة رد تلقائي:\n\n🔑 `{keyword}` → 💬 `{preview}`")
            del login_states[user_id]

        # ============ تعيين الحذف التلقائي ============
        elif state['step'] == 'set_auto_delete':
            try:
                hours = int(text)
                if hours < 0:
                    await event.respond("❌ الرقم يجب أن يكون 0 أو أكثر")
                else:
                    config['AUTO_DELETE_HOURS'] = hours
                    update_json_config(config)
                    if hours == 0:
                        await event.respond("✅ تم تعطيل الحذف التلقائي.")
                    else:
                        await event.respond(f"✅ تم تعيين الحذف التلقائي كل `{hours}` ساعة/ساعات.")
            except ValueError:
                await event.respond("❌ من فضلك أرسل رقماً صحيحاً")
            del login_states[user_id]

        # ============ مدة الاحتفاظ بسجلات الدردشة (الحذف التلقائي) ============
        elif state['step'] == 'set_log_retention':
            try:
                days = int(text)
                if 1 <= days <= 365:
                    config['LOG_RETENTION_DAYS'] = days
                    update_json_config(config)
                    await event.respond(
                        f"✅ تم التعيين: حذف سجلات الدردشة تلقائياً كل **{days} يوم/أيام**.\n\n"
                        f"🗑 التنظيف يعمل تلقائياً كل ساعة — لن تمتلئ مساحة البوت."
                    )
                    logger.info(f"⏱ مدة الاحتفاظ بسجلات الدردشة أصبحت {days} يوم/أيام بواسطة {user_id}")
                else:
                    await event.respond("❌ أرسل رقماً بين 1 و 365 (أو `/cancel` للإلغاء)")
                    return
            except ValueError:
                await event.respond("❌ من فضلك أرسل رقماً صحيحاً (أو `/cancel` للإلغاء)")
                return
            del login_states[user_id]

async def main():
    global bot
    # تشغيل Flask أولاً - يجب أن يعمل حتى لو فشل Telegram
    keep_alive()
    logger.info("=" * 60)
    logger.info("🚀 بدء تشغيل البوت...")
    logger.info("=" * 60)
    
    # التحقق من المتغيرات المطلوبة
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN غير محدد! تأكد من تعيينه في متغيرات البيئة.")
        # لا نخرج - نبقي Flask شغال
        while True:
            await asyncio.sleep(3600)
        return
    if not CHANNEL_ID:
        logger.critical("❌ CHANNEL_ID غير محدد! تأكد من تعيينه في متغيرات البيئة.")
        while True:
            await asyncio.sleep(3600)
        return
    if not API_ID or not API_HASH:
        logger.critical("❌ API_ID أو API_HASH غير محدد! تأكد من تعيينهما في متغيرات البيئة.")
        while True:
            await asyncio.sleep(3600)
        return
    
    logger.info(f"✅ BOT_TOKEN محدد ({len(BOT_TOKEN)} حرف)")
    logger.info(f"✅ CHANNEL_ID = {CHANNEL_ID}")
    logger.info(f"✅ API_ID = {API_ID}")
    logger.info(f"✅ API_HASH محدد ({len(API_HASH)} حرف)")
    
    # محاولة تشغيل البوت مع إعادة المحاولة (exponential backoff)
    max_retries = 10
    retry_count = 0
    backoff = 30  # ابدأ بـ 30 ثانية
    while retry_count < max_retries:
        try:
            logger.info(f"🔄 محاولة تشغيل البوت ({retry_count + 1}/{max_retries})...")
            session_path = os.path.join(SESSION_DIR, 'bot_session')
            bot = TelegramClient(session_path, API_ID, API_HASH, **CLIENT_OPTS)
            await bot.start(bot_token=BOT_TOKEN)
            logger.info("✅ تم تشغيل البوت بنجاح!")
            break
        except Exception as e:
            retry_count += 1
            err_str = str(e).lower()
            logger.error(f"❌ فشل تشغيل البوت (محاولة {retry_count}/{max_retries}): {type(e).__name__}: {e}")
            
            # كشف 429 Too Many Requests - نطول الانتظار
            if '429' in err_str or 'too many requests' in err_str or 'flood' in err_str:
                logger.warning(f"⏳ تيليجرام حظر البوت مؤقتاً (429). سأنتظر {backoff * 6} ثانية قبل المحاولة...")
                await asyncio.sleep(backoff * 6)  # 3 دقائق على الأقل
                backoff = min(backoff * 2, 600)  # ضعف الانتظار، حد أقصى 10 دقائق
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)  # ضعف الانتظار، حد أقصى 5 دقائق
            
            if retry_count >= max_retries:
                logger.critical("❌ فشل تشغيل البوت بعد عدة محاولات. البوت سيبقى نائماً لكن Flask شغال.")
                # نبقي العملية حية عشان Flask يشتغل
                while True:
                    await asyncio.sleep(3600)
                return
    
    # التحقق من الوصول للقناة
    try:
        channel_entity = await bot.get_entity(CHANNEL_ID)
        logger.info(f"✅ تم الوصول للقناة: {getattr(channel_entity, 'title', CHANNEL_ID)}")
    except Exception as e:
        logger.critical(f"❌ لا يمكن الوصول للقناة {CHANNEL_ID}: {e}")
        logger.critical("❌ تأكد أن البوت مشرف في القناة وأن CHANNEL_ID صحيح!")
        # نكمل عشان البوت نفسه يرد على /start
    
    # التحقق من إعدادات الفلترة وتحذير المستخدم
    config = load_json_config()
    # 🧹 تنظيف تلقائي للكلمات المفتاحية المشوهة (نصوص متعددة الأسطر أو أطول من الحد — ناتجة عن لصق رسالة كاملة)
    if sanitize_keywords_config(config):
        update_json_config(config)
        logger.info("🧹 نُظفت كلمات مفتاحية مشوهة من الإعدادات (عامة/خاصة)")
    filters = config.get('FILTERS', {})
    max_len = filters.get('max_length', 0)
    if max_len > 0 and max_len < 200:
        logger.warning(f"⚠️ الحد الأقصى للأحرف ({max_len}) صغير جداً! قد يمنع توجيه أغلب الرسائل. يُنصح بتعيينه 0 (بدون حد)")
    if filters.get('block_links', False):
        logger.warning("⚠️ منع الروابط مفعل! أغلب رسائل VPN تحتوي روابط وسيتم تجاهلها. يُنصح بتعطيله.")
    logger.info(f"📋 الكلمات المفتاحية العامة (أدمن): {config.get('KEYWORDS', [])}")
    logger.info(f"🌟 الكلمات الافتراضية المفعّلة لكل الحسابات: {config.get('DEFAULT_KEYWORDS', [])}")
    logger.info("🔎 نمط المطابقة: الكلمة كوحدة مستقلة كاملة (احد ✔ | الاحد/احدى/احدث ✘) مع توحيد التشكيل والهمزات")
    logger.info(f"👑 الأدمن الرئيسي: {MAIN_ADMIN_ID}")
    logger.info(f"🛡 الأدمنة الثابتون (صلاحيات كاملة): {sorted(EXTRA_MAIN_ADMINS) or 'لا يوجد'}")
    logger.info(f"👥 المشرفون المضافون: {config.get('ADMINS', [])}")
    logger.info(f"💾 مجلد البيانات الدائم: {DATA_DIR}")
    logger.info(f"🔒 البوت مخصص للمشرفين فقط — أي مستخدم غير مصرح له سيصله رسالة التواصل مع الأدمنة")

    # ===== تعيين القروب الرسمي لاستقبال كل الرسائل تلقائياً (إن لم يُضبط قروب آخر) =====
    config = load_json_config()
    if not config.get('ADMIN_GROUP_ID'):
        try:
            raw = OFFICIAL_GROUP.replace('https://t.me/', '@').replace('t.me/', '@').strip()
            entity = await bot.get_entity(raw)
            og_id = int(entity.chat_id if hasattr(entity, 'chat_id') and entity.chat_id else entity.id)
            config['ADMIN_GROUP_ID'] = og_id
            update_json_config(config)
            logger.info(f"📌 تم تعيين القروب الرسمي لاستقبال كل الرسائل: {getattr(entity, 'title', og_id)} ({og_id})")
        except Exception as e:
            logger.warning(
                f"⚠️ تعذر تعيين القروب الرسمي ({OFFICIAL_GROUP}) تلقائياً: {str(e)[:120]} — "
                f"أضف البوت إلى القروب ثم اضغط زر '📌 تعيين القروب الرسمي' من قروب استقبال كل الرسائل"
            )
    else:
        logger.info(f"📤 قروب استقبال كل الرسائل: {config.get('ADMIN_GROUP_ID')}")
    
    await setup_bot_handlers()
    logger.info("✅ تم تسجيل معالجات البوت")
    
    # بدء مهام الخلفية (الحذف التلقائي للقناة + تنظيف سجلات الدردشة)
    asyncio.create_task(auto_delete_task())
    asyncio.create_task(logs_cleanup_task())
    
    # استئناف الجلسات الموجودة (ملفات الجلسة على القرص الدائم)
    resumed_count = 0
    session_dir = SESSION_DIR
    for f in os.listdir(session_dir):
        if f.startswith('session_') and f.endswith('.session') and f != 'bot_session.session' and not f.startswith('session_claim_'):
            phone = f.replace('session_', '').replace('.session', '')
            # تجاهل الجلسات القديمة غير الصالحة
            if phone in ['bot', 'bot2', 'main', 'krtkmahan']:
                logger.warning(f"⚠️ تجاهل جلسة قديمة غير صالحة: {phone}")
                continue
            try:
                session_path = os.path.join(session_dir, f.replace('.session', ''))
                client = TelegramClient(session_path, API_ID, API_HASH, **CLIENT_OPTS)
                await client.connect()
                if await client.is_user_authorized():
                    active_clients[phone] = client
                    # تسجيل المعالج وبدء المراقبة
                    register_handler(client, phone)
                    asyncio.create_task(start_monitoring(client, phone))
                    resumed_count += 1
                    logger.info(f"✅ تم استئناف الحساب {phone} وتسجيل المعالج - يراقب جميع المجموعات")
                else:
                    logger.warning(f"الجلسة {phone} غير مصرحة.")
            except Exception as e:
                logger.error(f"فشل استئناف الحساب {phone}: {e}")

    # استعادة الحسابات من الجلسات المحفوظة (StringSession في config) إذا فُقدت ملفات الجلسة
    try:
        config = load_json_config()
        saved_sessions = config.get('SESSIONS', {})
        for phone, s in saved_sessions.items():
            if phone in active_clients:
                continue
            if os.path.exists(os.path.join(SESSION_DIR, f'session_{phone}.session')):
                continue  # سيُستأنف من الملف مباشرة
            client = await resume_from_string(phone, s)
            if client:
                active_clients[phone] = client
                register_handler(client, phone)
                asyncio.create_task(start_monitoring(client, phone))
                resumed_count += 1
                logger.info(f"✅ تم استعادة الحساب {phone} من الجلسة المحفوظة دائماً (StringSession)")
            else:
                logger.warning(f"⚠️ تعذّر استعادة الحساب {phone} من الجلسة المحفوظة (ربما سجّل خروج من تيليجرام)")
    except Exception as e:
        logger.error(f"خطأ في استعادة الجلسات المحفوظة: {e}")

    logger.info(f"✅ البوت يعمل الآن - يراقب {resumed_count} حساب/حسابات - يراقب جميع المجموعات تلقائياً")
    logger.info("=" * 60)
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
