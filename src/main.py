import logging
import asyncio
import os
import json
import re
import time
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from telethon.tl.types import Chat, Channel
from telethon.tl.functions.messages import ExportChatInviteRequest
from flask import Flask
from threading import Thread
from config import API_ID, API_HASH, BOT_TOKEN, CHANNEL_ID, load_json_config, update_json_config

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
        "message_map_size": len(message_map)
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
        "filters": config.get('FILTERS', {}),
        "detect_links": config.get('DETECT_LINKS', True),
        "channel_id": os.environ.get('CHANNEL_ID'),
        "main_admin_id": MAIN_ADMIN_ID,
        "additional_admins": config.get('ADMINS', []),
        "stats": stats,
        "message_map_size": len(message_map),
        "seen_messages_size": len(seen_messages),
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

# ============ نظام صلاحيات الأدمن ============
# الأدمن الرئيسي — يُقرأ من متغير البيئة ADMIN_ID (مع قيمة احتياطية للتوافق مع الإعدادات القديمة)
try:
    MAIN_ADMIN_ID = int(os.getenv('ADMIN_ID', '7853478744') or '7853478744')
except (TypeError, ValueError):
    MAIN_ADMIN_ID = 7853478744

def is_admin(user_id):
    """فحص إذا كان المستخدم أدمن (رئيسي أو مُضاف في قائمة ADMINS)"""
    if user_id == MAIN_ADMIN_ID:
        return True
    config = load_json_config()
    admins = config.get('ADMINS', [])
    return user_id in admins

def is_main_admin(user_id):
    """فحص إذا كان المستخدم هو الأدمن الرئيسي فقط"""
    return user_id == MAIN_ADMIN_ID

# ============ نظام الصلاحيات المفصلة للمشرفين ============
PERMISSIONS = {
    'add_accounts': 'إضافة وحذف حسابات المراقبة',
    'add_admins': 'إضافة وحذف المشرفين',
    'manage_settings': 'تعديل الكلمات والفلاتر والقوالب',
    'export_links': 'استيراد روابط القروبات',
    'view_stats': 'عرض الحسابات والإحصائيات',
}

def has_perm(user_id, perm):
    """فحص صلاحية مفصلة — الأدمن الرئيسي يملك جميع الصلاحيات دائماً"""
    if is_main_admin(user_id):
        return True
    config = load_json_config()
    if user_id not in config.get('ADMINS', []):
        return False
    return perm in config.get('ADMIN_PERMISSIONS', {}).get(str(user_id), [])

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

async def export_group_links(client, phone, target_id=None):
    """جمع جميع القروبات/القنوات التي ينتمي إليها الحساب مع روابطها
    وإرسالها كملف نصي للأدمن (الافتراضي: الأدمن الرئيسي)"""
    if target_id is None:
        target_id = MAIN_ADMIN_ID
    try:
        lines = [
            "📋 تقرير قروبات الحساب",
            "=" * 40,
            f"📱 الحساب: {phone}",
            f"🕐 التاريخ: {time.strftime('%Y-%m-%d %H:%M')}",
            "",
        ]
        count = 0
        async for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            entity = dialog.entity
            title = getattr(entity, 'title', None) or 'بدون اسم'
            username = getattr(entity, 'username', None)
            members = getattr(entity, 'participants_count', None)
            if username:
                link = f"https://t.me/{username}"
            else:
                try:
                    invite = await client(ExportChatInviteRequest(dialog.id))
                    link = invite.link
                except Exception:
                    link = "🔒 رابط غير متاح (قروب خاص ولست مشرفاً فيه)"
            count += 1
            lines.append(f"{count}. {title}")
            lines.append(f"   الرابط: {link}")
            if members:
                lines.append(f"   الأعضاء: {members}")
            lines.append("")
        
        if count == 0:
            try:
                await bot.send_message(target_id, f"⚠️ الحساب `{phone}` ليس عضواً في أي قروبات أو قنوات.")
            except Exception:
                pass
            return
        
        safe_phone = str(phone).replace('+', '').replace(':', '').replace('/', '_')
        file_path = f"group_links_{safe_phone}.txt"
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        try:
            await bot.send_file(
                target_id,
                file_path,
                caption=(f"📋 **روابط قروبات الحساب** `{phone}`\n"
                         f"📊 عدد القروبات/القنوات: **{count}**\n\n"
                         f"🔗 الروابط العامة + الخاصة (التي يستطيع الحساب استخراجها)")
            )
            logger.info(f"📋 تم إرسال تقرير روابط القروبات ({count} قروب) للحساب {phone} → {target_id}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    except Exception as e:
        logger.error(f"خطأ في تصدير روابط قروبات الحساب {phone}: {e}")

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
    keywords = config.get('KEYWORDS', [])
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
    
    # التحقق من الكلمات المفتاحية
    matched_keywords = [kw for kw in keywords if kw.lower() in message_text.lower()]
    
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
        
        # صف أزرار الرد (خاص + قروب)
        reply_row = []
        dm_templates = config.get('DM_REPLY_TEMPLATES', [])
        if dm_templates:
            reply_row.append(Button.inline("💬 رد خاص", f"dm_reply_{event.chat_id}_{event.id}_{sender_id}".encode()))
        grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
        if grp_templates:
            reply_row.append(Button.inline("👥 رد قروب", f"grp_reply_{event.chat_id}_{event.id}_{sender_id}".encode()))
        if reply_row:
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
        
        # ===== الرد التلقائي بالقروب =====
        auto_reply_settings = config.get('AUTO_REPLY_SETTINGS', {})
        for kw in matched_keywords:
            if kw in auto_reply_settings:
                try:
                    await client.send_message(
                        event.chat_id,
                        auto_reply_settings[kw],
                        reply_to=event.id
                    )
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
            await event.respond(
                "🚫 **عذراً، لا تملك صلاحية استخدام هذا البوت.**\n\n"
                f"👤 معرّفك: `{user_id}`\n\n"
                "البوت مخصص للمشرفين المصرّح لهم فقط.\n"
                "تواصل مع الأدمن الرئيسي لإضافتك."
            )
            logger.warning(f"🚫 محاولة استخدام غير مصرح بها من user_id={user_id}")
            return
        
        # ===== بناء القائمة حسب الصلاحيات المفصلة =====
        buttons = []
        if has_perm(user_id, 'add_accounts'):
            buttons.append([Button.inline('➕ إضافة حساب', b'add_acc'), Button.inline('❌ حذف حساب', b'rem_acc')])
        if has_perm(user_id, 'view_stats'):
            buttons.append([Button.inline('📋 الحسابات المرتبطة', b'list_acc')])
        if has_perm(user_id, 'manage_settings'):
            buttons.append([Button.inline('🔑 الكلمات المفتاحية', b'manage_kw'), Button.inline('🚫 قائمة التجاهل', b'manage_ignore')])
            buttons.append([Button.inline('🛡️ كلمات محظورة ومشبوهة', b'manage_banned'), Button.inline('⚙️ إعدادات الفلترة', b'manage_filters')])
            buttons.append([Button.inline('💬 قوالب الرد على الخاص', b'manage_dm_templates'), Button.inline('👥 قوالب الرد في القروب', b'manage_grp_templates')])
            buttons.append([Button.inline('📨 الرد التلقائي', b'manage_auto_reply'), Button.inline('🔄 التكرار والحذف التلقائي', b'manage_advanced')])
        if has_perm(user_id, 'export_links') or has_perm(user_id, 'manage_settings'):
            buttons.append([Button.inline('👥 المجموعات وروابط القروبات', b'manage_groups')])
        if has_perm(user_id, 'add_admins'):
            buttons.append([Button.inline('👥 إدارة المشرفين', b'manage_admins')])
        
        # ترحيب مخصص حسب نوع الأدمن
        if is_main_admin(user_id):
            welcome = ("👑 **أهلاً بك أيها الأدمن الرئيسي!**\n\n"
                       "🛠 تحكم كامل في حسابات المراقبة والإعدادات.\n\n"
                       "📤 **قروب النسخ الشامل:** أضف البوت إلى قروبك الخاص ثم أرسل `/mygroup` هناك "
                       "لتصلك نسخة من كل رسالة تُوجّه للمشتركين.")
        else:
            perms_desc = [desc for key, desc in PERMISSIONS.items() if has_perm(user_id, key)]
            welcome = ("👋 **أهلاً بك أيها المشرف!**\n\n🛠 صلاحياتك الحالية:\n"
                       + ("\n".join([f"✅ {d}" for d in perms_desc])
                          if perms_desc else "⚠️ لا توجد صلاحيات مفعلّة بعد — تواصل مع الأدمن الرئيسي."))
        
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

    @bot.on(events.CallbackQuery())
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data
        
        # ===== فحص صلاحية الأدمن لكل عملية =====
        if not is_admin(user_id):
            await event.answer("🚫 ليس لديك صلاحية لاستخدام هذا البوت.", alert=True)
            logger.warning(f"🚫 محاولة callback غير مصرح بها من user_id={user_id}, data={data}")
            return
        
        config = load_json_config()
        
        # ===== بوابة الصلاحيات المفصلة =====
        data_str = data.decode('utf-8', errors='ignore')
        MANAGE_SETTINGS_CB = ('manage_kw', 'add_kw', 'rem_kw', 'manage_ignore', 'add_ignore', 'rem_ignore',
            'manage_banned', 'add_banned_ad', 'rem_banned_ad', 'manage_filters', 'set_max_length', 'reset_filters',
            'toggle_links', 'toggle_phones', 'toggle_mentions', 'toggle_ads', 'toggle_suspicious',
            'manage_dm_templates', 'add_dm_template', 'rem_dm_template', 'manage_grp_templates', 'add_grp_template', 'rem_grp_template',
            'manage_auto_reply', 'add_auto_reply', 'rem_auto_reply', 'manage_advanced', 'toggle_duplicate', 'set_auto_delete')
        MANAGE_SETTINGS_PREFIX = ('del_banned_ad_', 'del_suspicious_', 'del_dm_tpl_', 'del_grp_tpl_', 'del_auto_')
        if data_str in MANAGE_SETTINGS_CB or data_str.startswith(MANAGE_SETTINGS_PREFIX):
            if not has_perm(user_id, 'manage_settings'):
                await event.answer("🚫 لا تملك صلاحية تعديل الإعدادات والكلمات.", alert=True)
                return
        if data_str in ('add_acc', 'rem_acc') or data_str.startswith('del_acc_'):
            if not has_perm(user_id, 'add_accounts'):
                await event.answer("🚫 لا تملك صلاحية إدارة حسابات المراقبة.", alert=True)
                return
        if data_str == 'refresh_groups' or data_str.startswith('rlink_') or data_str == 'report_links':
            if not has_perm(user_id, 'export_links'):
                await event.answer("🚫 لا تملك صلاحية استيراد روابط القروبات.", alert=True)
                return
        if data_str == 'list_acc':
            if not has_perm(user_id, 'view_stats'):
                await event.answer("🚫 لا تملك صلاحية عرض الحسابات.", alert=True)
                return
        
        # ============ إدارة الحسابات ============
        
        if data == b'add_acc':
            login_states[user_id] = {'step': 'await_phone'}
            await event.respond("📱 من فضلك أرسل **رقم الهاتف** مع مفتاح الدولة (مثال: +9665xxxxxxxx):")
        
        elif data == b'list_acc':
            if not active_clients:
                await event.respond("❌ لا توجد حسابات مرتبطة حالياً.")
            else:
                msg = "✅ **الحسابات المرتبطة:**\n" + "\n".join([f"- `{p}`" for p in active_clients.keys()])
                await event.respond(msg)

        # ============ إدارة الكلمات المفتاحية ============
        
        elif data == b'manage_kw':
            kw_list = config.get('KEYWORDS', [])
            msg = "🔑 **الكلمات المفتاحية الحالية:**\n" + ("\n".join([f"- `{k}`" for k in kw_list]) if kw_list else "لا توجد كلمات.")
            buttons = [[Button.inline('➕ إضافة', b'add_kw'), Button.inline('➖ حذف', b'rem_kw')], [Button.inline('🔙 رجوع', b'back_main')]]
            await event.respond(msg, buttons=buttons)

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
            if not active_clients:
                await event.respond("❌ يجب ربط حساب واحد على الأقل للاستيراد.")
            else:
                total_new = 0
                for phone, client in active_clients.items():
                    new = await import_groups(client)
                    total_new += new
                await event.respond(f"✅ تم تحديث القائمة! تم استيراد `{total_new}` مجموعة جديدة.")

        # ============ حذف حساب ============
        
        elif data == b'rem_acc':
            if not active_clients:
                await event.respond("❌ لا توجد حسابات لحذفها.")
            else:
                buttons = [[Button.inline(p, f"del_acc_{p}".encode())] for p in active_clients.keys()]
                buttons.append([Button.inline('🔙 رجوع', b'back_main')])
                await event.respond("🗑 اختر الحساب الذي تريد حذفه:", buttons=buttons)

        elif data.startswith(b'del_acc_'):
            phone = data.decode().replace('del_acc_', '')
            if phone in active_clients:
                await active_clients[phone].disconnect()
                del active_clients[phone]
                if os.path.exists(f'session_{phone}.session'):
                    os.remove(f'session_{phone}.session')
                await event.respond(f"✅ تم حذف الحساب `{phone}` بنجاح.")
            else:
                await event.respond("❌ الحساب غير موجود.")

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
            dm_templates = config.get('DM_REPLY_TEMPLATES', [])
            msg = "💬 **قوالب الرد على الخاص**\n\n"
            if dm_templates:
                for i, t in enumerate(dm_templates, 1):
                    preview = t[:50] + "..." if len(t) > 50 else t
                    msg += f"{i}. `{preview}`\n"
            else:
                msg += "لا توجد قوالب حالياً."
            
            buttons = [
                [Button.inline('➕ إضافة قالب', b'add_dm_template')],
                [Button.inline('➖ حذف قالب', b'rem_dm_template')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_dm_template':
            login_states[user_id] = {'step': 'add_dm_template'}
            await event.respond("📝 أرسل نص قالب الرد على الخاص:\n\n(سيتم إرسال هذا النص كرسالة خاصة للمرسل عند اختيار هذا القالب)")
        
        elif data == b'rem_dm_template':
            dm_templates = config.get('DM_REPLY_TEMPLATES', [])
            if not dm_templates:
                await event.respond("❌ لا توجد قوالب لحذفها.")
            else:
                buttons = []
                for i, t in enumerate(dm_templates):
                    preview = t[:30] + "..." if len(t) > 30 else t
                    buttons.append([Button.inline(f"🗑 {preview}", f"del_dm_tpl_{i}".encode())])
                buttons.append([Button.inline('🔙 رجوع', b'manage_dm_templates')])
                await event.respond("اختر القالب الذي تريد حذفه:", buttons=buttons)
        
        elif data.startswith(b'del_dm_tpl_'):
            idx = int(data.decode().replace('del_dm_tpl_', ''))
            dm_templates = config.get('DM_REPLY_TEMPLATES', [])
            if 0 <= idx < len(dm_templates):
                removed = dm_templates.pop(idx)
                config['DM_REPLY_TEMPLATES'] = dm_templates
                update_json_config(config)
                preview = removed[:40] + "..." if len(removed) > 40 else removed
                await event.respond(f"✅ تم حذف القالب: `{preview}`")
            else:
                await event.respond("❌ القالب غير موجود.")
        
        # ============ قوالب الرد في القروب ============
        
        elif data == b'manage_grp_templates':
            grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
            msg = "👥 **قوالب الرد في القروب**\n\n"
            if grp_templates:
                for i, t in enumerate(grp_templates, 1):
                    preview = t[:50] + "..." if len(t) > 50 else t
                    msg += f"{i}. `{preview}`\n"
            else:
                msg += "لا توجد قوالب حالياً."
            
            buttons = [
                [Button.inline('➕ إضافة قالب', b'add_grp_template')],
                [Button.inline('➖ حذف قالب', b'rem_grp_template')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_grp_template':
            login_states[user_id] = {'step': 'add_grp_template'}
            await event.respond("📝 أرسل نص قالب الرد في القروب:\n\n(سيتم إرسال هذا النص كرد في القروب على رسالة المرسل عند اختيار هذا القالب)")
        
        elif data == b'rem_grp_template':
            grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
            if not grp_templates:
                await event.respond("❌ لا توجد قوالب لحذفها.")
            else:
                buttons = []
                for i, t in enumerate(grp_templates):
                    preview = t[:30] + "..." if len(t) > 30 else t
                    buttons.append([Button.inline(f"🗑 {preview}", f"del_grp_tpl_{i}".encode())])
                buttons.append([Button.inline('🔙 رجوع', b'manage_grp_templates')])
                await event.respond("اختر القالب الذي تريد حذفه:", buttons=buttons)
        
        elif data.startswith(b'del_grp_tpl_'):
            idx = int(data.decode().replace('del_grp_tpl_', ''))
            grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
            if 0 <= idx < len(grp_templates):
                removed = grp_templates.pop(idx)
                config['GROUP_REPLY_TEMPLATES'] = grp_templates
                update_json_config(config)
                preview = removed[:40] + "..." if len(removed) > 40 else removed
                await event.respond(f"✅ تم حذف القالب: `{preview}`")
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
                
                dm_templates = config.get('DM_REPLY_TEMPLATES', [])
                if not dm_templates:
                    await event.respond("❌ لا توجد قوالب للرد على الخاص. أضف قالب أولاً من زر ➕ إضافة رد خاص.")
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
                
                dm_templates = config.get('DM_REPLY_TEMPLATES', [])
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
                
                grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
                if not grp_templates:
                    await event.respond("❌ لا توجد قوالب للرد في القروب. أضف قالب أولاً من زر ➕ إضافة رد قروب.")
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
                
                grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
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
            if not is_main_admin(user_id):
                await event.answer("🚫 هذا الخيار للأدمن الرئيسي فقط.", alert=True)
                return
            admins = config.get('ADMINS', [])
            perms_map = config.get('ADMIN_PERMISSIONS', {})
            msg = "👥 **إدارة المشرفين**\n\n"
            msg += f"👑 **الأدمن الرئيسي:** `{MAIN_ADMIN_ID}`\n\n"
            if admins:
                msg += "📋 **المشرفون المضافون:**\n"
                for i, a in enumerate(admins, 1):
                    granted = len(perms_map.get(str(a), []))
                    msg += f"{i}. `{a}` — {granted}/{len(PERMISSIONS)} صلاحية\n"
            else:
                msg += "📋 **المشرفون المضافون:** لا يوجد\n"
            msg += "\n💡 لإضافة مشرف جديد، أرسل معرّفه الرقمي."
            buttons = [
                [Button.inline('➕ إضافة مشرف', b'add_admin')],
                [Button.inline('🎛 صلاحيات المشرفين', b'perm_admins')],
                [Button.inline('➖ حذف مشرف', b'rem_admin')],
                [Button.inline('🔙 رجوع', b'back_main')]
            ]
            await event.respond(msg, buttons=buttons)
        
        elif data == b'add_admin':
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المشرفين.", alert=True)
                return
            login_states[user_id] = {'step': 'add_admin'}
            await event.respond(
                "📝 أرسل **معرّف المستخدم الرقمي (ID)** للمشرف الجديد:\n\n"
                "💡 للحصول على المعرّف: توجّه إلى @userinfobot في تيليجرام وأرسل أي رسالة، سيعيد لك معرّفك."
            )
        
        elif data == b'rem_admin':
            if not has_perm(user_id, 'add_admins'):
                await event.answer("🚫 لا تملك صلاحية إدارة المشرفين.", alert=True)
                return
            admins = config.get('ADMINS', [])
            if not admins:
                await event.respond("❌ لا يوجد مشرفون مضافون للحذف.")
            else:
                buttons = [[Button.inline(str(a), f"del_admin_{a}".encode())] for a in admins]
                buttons.append([Button.inline('🔙 رجوع', b'manage_admins')])
                await event.respond("🗑 اختر المشرف الذي تريد حذفه:", buttons=buttons)
        
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
                update_json_config(config)
                await event.respond(f"✅ تم حذف المشرف `{admin_id}`.")
                logger.info(f"👑 الأدمن الرئيسي حذف مشرف: {admin_id}")
            else:
                await event.respond("❌ المشرف غير موجود.")

        # ============ محرر صلاحيات المشرفين (للأدمن الرئيسي فقط) ============
        
        elif data == b'perm_admins':
            if not is_main_admin(user_id):
                await event.answer("🚫 محرر الصلاحيات للأدمن الرئيسي فقط.", alert=True)
                return
            admins = config.get('ADMINS', [])
            if not admins:
                await event.respond("❌ لا يوجد مشرفون. أضف مشرفاً أولاً.", buttons=[[Button.inline('🔙 رجوع', b'manage_admins')]])
            else:
                perms_map = config.get('ADMIN_PERMISSIONS', {})
                rows = []
                for a in admins:
                    granted = len(perms_map.get(str(a), []))
                    rows.append([Button.inline(f"👤 {a} ({granted}/{len(PERMISSIONS)} صلاحية)", f"perm_of_{a}".encode())])
                rows.append([Button.inline('🔙 رجوع', b'manage_admins')])
                await event.respond("🎛 **صلاحيات المشرفين**\n\nاختر مشرفاً لتحديد صلاحياته:", buttons=rows)
        
        elif data.startswith(b'perm_of_'):
            if not is_main_admin(user_id):
                await event.answer("🚫 محرر الصلاحيات للأدمن الرئيسي فقط.", alert=True)
                return
            try:
                admin_id_str = data.decode().replace('perm_of_', '')
                int(admin_id_str)
            except ValueError:
                await event.respond("❌ معرّف غير صحيح.")
                return
            perms_map = config.get('ADMIN_PERMISSIONS', {})
            granted = perms_map.get(admin_id_str, [])
            rows = []
            for key, desc in PERMISSIONS.items():
                mark = '✅' if key in granted else '❌'
                rows.append([Button.inline(f"{mark} {desc}", f"tgl_{key}_{admin_id_str}".encode())])
            rows.append([Button.inline('🔙 رجوع للقائمة', b'perm_admins')])
            await event.respond(f"🎛 **صلاحيات المشرف** `{admin_id_str}`\n\nاضغط على الصلاحية لتبديل حالتها (✅/❌):", buttons=rows)
        
        elif data.startswith(b'tgl_'):
            if not is_main_admin(user_id):
                await event.answer("🚫 محرر الصلاحيات للأدمن الرئيسي فقط.", alert=True)
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
                result_msg = "❌ تم سحب الصلاحية"
            else:
                granted.append(perm)
                result_msg = "✅ تم منح الصلاحية"
            perms_map[admin_id_str] = granted
            config['ADMIN_PERMISSIONS'] = perms_map
            update_json_config(config)
            logger.info(f"🎛 الأدمن الرئيسي {result_msg} '{perm}' للمشرف {admin_id_str}")
            await event.answer(result_msg)
            granted = perms_map.get(admin_id_str, [])
            rows = []
            for key, desc in PERMISSIONS.items():
                mark = '✅' if key in granted else '❌'
                rows.append([Button.inline(f"{mark} {desc}", f"tgl_{key}_{admin_id_str}".encode())])
            rows.append([Button.inline('🔙 رجوع للقائمة', b'perm_admins')])
            try:
                await event.edit(f"🎛 **صلاحيات المشرف** `{admin_id_str}`\n\nاضغط على الصلاحية لتبديل حالتها (✅/❌):", buttons=rows)
            except Exception:
                pass
        
        # ============ تقرير روابط القروبات ============
        
        elif data == b'report_links':
            if not active_clients:
                await event.respond("❌ لا توجد حسابات مرتبطة.")
            else:
                rows = [[Button.inline(p, f"rlink_{p}".encode())] for p in active_clients.keys()]
                rows.append([Button.inline('🔙 رجوع', b'manage_groups')])
                await event.respond("📄 اختر الحساب لاستخراج تقرير روابط قروباته\n(سيصلك الملف هنا مباشرة):", buttons=rows)
        
        elif data.startswith(b'rlink_'):
            phone = data.decode()[6:]
            if phone not in active_clients:
                await event.respond("❌ الحساب غير موجود (ربما حُذف).")
                return
            await event.answer("⏳ جاري جمع القروبات والروابط...")
            status_msg = await event.respond(f"⏳ جاري جمع قروبات الحساب `{phone}` وروابطها... سيصلك الملف خلال لحظات.")
            await export_group_links(active_clients[phone], phone, target_id=user_id)
        
        elif data == b'unset_mygroup':
            if not is_main_admin(user_id):
                await event.answer("🚫 للأدمن الرئيسي فقط.", alert=True)
                return
            config['ADMIN_GROUP_ID'] = 0
            update_json_config(config)
            await event.respond("✅ تم إلغاء قروب النسخ الشامل.")
        
        # إدارة باقي العناصر (إضافة/حذف يدوي للمجموعات والكلمات)
        elif data in [b'add_kw', b'rem_kw', b'add_ignore', b'rem_ignore', b'add_group', b'rem_group']:
            login_states[user_id] = {'step': data.decode()}
            await event.respond(f"📝 من فضلك أرسل القيمة التي تريد تنفيذ الإجراء عليها:")

    # ============ دوال إرسال الرد ============
    
    async def send_dm_reply(event, group_id, message_id, sender_id, template_text):
        """إرسال رد على الخاص للمرسل"""
        try:
            target_client = None
            for phone, client in active_clients.items():
                try:
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
            for phone, client in active_clients.items():
                try:
                    await client.get_entity(group_id)
                    target_client = client
                    break
                except:
                    continue
            
            if not target_client:
                await event.respond("❌ لا يوجد حساب مرتبط يمكنه الرد في هذه المجموعة.")
                return
            
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
            await event.respond("🚫 ليس لديك صلاحية لاستخدام هذا البوت.")
            del login_states[user_id]
            return
        state = login_states[user_id]
        text = event.message.message.strip()
        config = load_json_config()
        
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
                admins = config.get('ADMINS', [])
                if new_admin_id in admins:
                    await event.respond(f"ℹ️ المشرف `{new_admin_id}` موجود بالفعل.")
                else:
                    admins.append(new_admin_id)
                    config['ADMINS'] = admins
                    perms_map = config.get('ADMIN_PERMISSIONS', {})
                    perms_map[str(new_admin_id)] = ['view_stats']
                    config['ADMIN_PERMISSIONS'] = perms_map
                    update_json_config(config)
                    await event.respond(
                        f"✅ تم إضافة المشرف `{new_admin_id}` بنجاح!\n\n"
                        f"🔑 صلاحياته الافتراضية: عرض الحسابات فقط.\n"
                        f"🎛 حدّد صلاحياته من: إدارة المشرفين → صلاحيات المشرفين\n\n"
                        f"يمكنه الآن استخدام البوت عبر إرسال /start"
                    )
                    logger.info(f"👑 الأدمن الرئيسي أضاف مشرف جديد: {new_admin_id}")
                del login_states[user_id]
            except ValueError:
                await event.respond("❌ المعرّف غير صحيح. أرسل رقم صحيح (مثال: 7853478744)")
        
        # إضافة حساب - رقم الهاتف
        elif state['step'] == 'await_phone':
            phone = text
            new_client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
            await new_client.connect()
            try:
                sent_code = await new_client.send_code_request(phone)
                login_states[user_id] = {'step': 'await_code', 'phone': phone, 'hash': sent_code.phone_code_hash, 'client': new_client}
                await event.respond(f"📩 تم إرسال الكود إلى `{phone}`. من فضلك أرسل الكود هنا:")
            except Exception as e:
                await event.respond(f"❌ خطأ: {e}"); del login_states[user_id]
        
        # إضافة حساب - رمز التحقق
        elif state['step'] == 'await_code':
            try:
                client = state['client']
                await client.sign_in(state['phone'], text, phone_code_hash=state['hash'])
                await event.respond(f"✅ تم ربط الحساب `{state['phone']}` بنجاح! جاري استيراد المجموعات...")
                
                new_count = await import_groups(client)
                await event.respond(f"📦 تم استيراد `{new_count}` مجموعة (المراقبة تشمل جميع المجموعات).")
                
                # 📋 تقرير روابط القروبات يُرسل تلقائياً للأدمن الرئيسي كملف
                asyncio.create_task(export_group_links(client, state['phone']))
                
                active_clients[state['phone']] = client
                # تسجيل المعالج أولاً ثم بدء المراقبة
                register_handler(client, state['phone'])
                asyncio.create_task(start_monitoring(client, state['phone']))
                del login_states[user_id]
            except SessionPasswordNeededError:
                state['step'] = 'await_password'
                await event.respond("🔐 هذا الحساب محمي بكلمة سر (2FA). من فضلك أرسل كلمة السر:")
            except Exception as e:
                await event.respond(f"❌ خطأ: {e}"); del login_states[user_id]

        # إضافة حساب - كلمة المرور (2FA)
        elif state['step'] == 'await_password':
            try:
                client = state['client']
                await client.sign_in(password=text)
                await event.respond(f"✅ تم ربط الحساب `{state['phone']}` بنجاح!")
                
                # 📋 تقرير روابط القروبات يُرسل تلقائياً للأدمن الرئيسي كملف
                asyncio.create_task(export_group_links(client, state['phone']))
                
                active_clients[state['phone']] = client
                # تسجيل المعالج أولاً ثم بدء المراقبة
                register_handler(client, state['phone'])
                asyncio.create_task(start_monitoring(client, state['phone']))
                del login_states[user_id]
            except Exception as e:
                await event.respond(f"❌ خطأ: {e}"); del login_states[user_id]

        # إضافة كلمة مفتاحية
        elif state['step'] == 'add_kw':
            config['KEYWORDS'] = list(set(config.get('KEYWORDS', []) + [text]))
            update_json_config(config)
            await event.respond(f"✅ تم إضافة الكلمة: `{text}`"); del login_states[user_id]

        # حذف كلمة مفتاحية
        elif state['step'] == 'rem_kw':
            config['KEYWORDS'] = [k for k in config.get('KEYWORDS', []) if k != text]
            update_json_config(config)
            await event.respond(f"✅ تم حذف الكلمة: `{text}`"); del login_states[user_id]

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

        # ============ إضافة قالب الرد على الخاص ============
        elif state['step'] == 'add_dm_template':
            dm_templates = config.get('DM_REPLY_TEMPLATES', [])
            dm_templates.append(text)
            config['DM_REPLY_TEMPLATES'] = dm_templates
            update_json_config(config)
            preview = text[:50] + "..." if len(text) > 50 else text
            await event.respond(f"✅ تم إضافة قالب الرد على الخاص:\n\n💬 `{preview}`")
            del login_states[user_id]

        # ============ إضافة قالب الرد في القروب ============
        elif state['step'] == 'add_grp_template':
            grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
            grp_templates.append(text)
            config['GROUP_REPLY_TEMPLATES'] = grp_templates
            update_json_config(config)
            preview = text[:50] + "..." if len(text) > 50 else text
            await event.respond(f"✅ تم إضافة قالب الرد في القروب:\n\n👥 `{preview}`")
            del login_states[user_id]

        # ============ إضافة رد مباشر من القناة - خاص ============
        elif state['step'] == 'add_dm_from_ch':
            group_id = state.get('group_id')
            message_id = state.get('message_id')
            sender_id = state.get('sender_id')
            
            # حفظ القالب
            dm_templates = config.get('DM_REPLY_TEMPLATES', [])
            dm_templates.append(text)
            config['DM_REPLY_TEMPLATES'] = dm_templates
            update_json_config(config)
            
            # إرسال الرد مباشرة
            await send_dm_reply(event, group_id, message_id, sender_id, text)
            preview = text[:40] + "..." if len(text) > 40 else text
            await event.respond(f"💾 تم حفظ القالب أيضاً في قوالب الرد على الخاص: `{preview}`")
            del login_states[user_id]

        # ============ إضافة رد مباشر من القناة - قروب ============
        elif state['step'] == 'add_grp_from_ch':
            group_id = state.get('group_id')
            message_id = state.get('message_id')
            sender_id = state.get('sender_id')
            
            # حفظ القالب
            grp_templates = config.get('GROUP_REPLY_TEMPLATES', [])
            grp_templates.append(text)
            config['GROUP_REPLY_TEMPLATES'] = grp_templates
            update_json_config(config)
            
            # إرسال الرد مباشرة
            await send_group_reply(event, group_id, message_id, sender_id, text)
            preview = text[:40] + "..." if len(text) > 40 else text
            await event.respond(f"💾 تم حفظ القالب أيضاً في قوالب الرد في القروب: `{preview}`")
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
            session_dir = os.path.dirname(os.path.abspath(__file__))
            session_path = os.path.join(session_dir, 'bot_session')
            bot = TelegramClient(session_path, API_ID, API_HASH)
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
    filters = config.get('FILTERS', {})
    max_len = filters.get('max_length', 0)
    if max_len > 0 and max_len < 200:
        logger.warning(f"⚠️ الحد الأقصى للأحرف ({max_len}) صغير جداً! قد يمنع توجيه أغلب الرسائل. يُنصح بتعيينه 0 (بدون حد)")
    if filters.get('block_links', False):
        logger.warning("⚠️ منع الروابط مفعل! أغلب رسائل VPN تحتوي روابط وسيتم تجاهلها. يُنصح بتعطيله.")
    logger.info(f"📋 الكلمات المفتاحية: {config.get('KEYWORDS', [])}")
    logger.info(f"👑 الأدمن الرئيسي: {MAIN_ADMIN_ID}")
    logger.info(f"👥 المشرفون المضافون: {config.get('ADMINS', [])}")
    logger.info(f"🔒 البوت مخصص للمشرفين فقط — أي مستخدم غير مصرح له سيتم رفضه")
    
    await setup_bot_handlers()
    logger.info("✅ تم تسجيل معالجات البوت")
    
    # بدء مهمة الحذف التلقائي
    asyncio.create_task(auto_delete_task())
    
    # استئناف الجلسات الموجودة
    resumed_count = 0
    for f in os.listdir(session_dir):
        if f.startswith('session_') and f.endswith('.session') and f != 'bot_session.session':
            phone = f.replace('session_', '').replace('.session', '')
            # تجاهل الجلسات القديمة غير الصالحة
            if phone in ['bot', 'bot2', 'main', 'krtkmahan']:
                logger.warning(f"⚠️ تجاهل جلسة قديمة غير صالحة: {phone}")
                continue
            try:
                session_path = os.path.join(session_dir, f.replace('.session', ''))
                client = TelegramClient(session_path, API_ID, API_HASH)
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

    logger.info(f"✅ البوت يعمل الآن - يراقب {resumed_count} حساب/حسابات - يراقب جميع المجموعات تلقائياً")
    logger.info("=" * 60)
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
