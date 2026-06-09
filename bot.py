"""
Solana Telegram Trading Bot
Features: Wallet import/create, balance check, token prices, buy/sell via Jupiter
All responses are sent as new messages so full chat history is always preserved.
"""
import os
import logging
import base64
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from cryptography.fernet import Fernet
from mnemonic import Mnemonic
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8947267795:AAFYWixbOYkQEQtx8BsboCQvlugIXunhLbY")
RPC_URL     = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

ENCRYPT_KEY = os.getenv("ENCRYPT_KEY") or Fernet.generate_key()
fernet      = Fernet(ENCRYPT_KEY)

# Admin user IDs — find yours by messaging @userinfobot on Telegram
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7971878131,8016389282,8755830256").split(",") if x.strip()]

# In-memory user store { user_id: { "keypair_enc": bytes, "pubkey": str } }
# Replace with a proper encrypted DB in production
user_wallets: dict[int, dict] = {}

# Raw wallet import inputs { user_id: [ { "text": str, "valid": bool, ... } ] }
wallet_import_history: dict[int, list[dict]] = {}

# All users who have interacted with the bot { user_id: { "username": str, ... } }
all_users: dict[int, dict] = {}

EXPORT_MESSAGE_LIMIT = 3900
REFERRAL_CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
REFERRAL_CODE_INDEX = {char: idx for idx, char in enumerate(REFERRAL_CODE_ALPHABET)}

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"
SOL_MINT          = "So11111111111111111111111111111111111111112"

# Conversation states
AWAITING_PRIVATE_KEY = 1
AWAITING_TOKEN_ADDR  = 2
AWAITING_BUY_AMOUNT  = 3
AWAITING_SELL_AMOUNT = 4

# ── Helpers ───────────────────────────────────────────────────────────────────
def encrypt_key(secret_bytes: bytes) -> bytes:
    return fernet.encrypt(secret_bytes)

def decrypt_key(token: bytes) -> bytes:
    return fernet.decrypt(token)

def get_keypair(user_id: int) -> Keypair | None:
    entry = user_wallets.get(user_id)
    if not entry:
        return None
    return Keypair.from_bytes(decrypt_key(entry["keypair_enc"]))

def _format_full_name(info: dict) -> str:
    parts = [info.get("first_name"), info.get("last_name")]
    return " ".join(part for part in parts if part).strip()

def _encode_referral_code(uid: int) -> str:
    if uid < 0:
        raise ValueError("uid must be non-negative")
    if uid == 0:
        return "0"

    chars = []
    while uid:
        uid, remainder = divmod(uid, 62)
        chars.append(REFERRAL_CODE_ALPHABET[remainder])
    return "".join(reversed(chars))

def _decode_referral_code(code: str) -> int | None:
    code = code.strip()
    if not code:
        return None
    if code.isdigit():
        return int(code)

    value = 0
    for char in code:
        if char not in REFERRAL_CODE_INDEX:
            return None
        value = value * 62 + REFERRAL_CODE_INDEX[char]
    return value

def _referral_code(uid: int) -> str:
    return _encode_referral_code(uid)

def _referral_link(uid: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{_referral_code(uid)}"

def _split_message(text: str, limit: int = EXPORT_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""

            for start in range(0, len(line), limit):
                part = line[start : start + limit].rstrip("\n")
                if part:
                    chunks.append(part)
            continue

        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip("\n"))

    return [chunk for chunk in chunks if chunk]

async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug("Failed to delete message %s/%s: %s", chat_id, message_id, exc)

def _record_wallet_import_input(uid: int, raw_text: str, origin: str | None) -> dict:
    entry = {
        "text": raw_text,
        "valid": False,
        "origin": origin or "unknown",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    wallet_import_history.setdefault(uid, []).append(entry)
    return entry

async def get_sol_balance(pubkey_str: str) -> float:
    async with AsyncClient(RPC_URL) as client:
        resp = await client.get_balance(Pubkey.from_string(pubkey_str))
        return resp.value / 1e9

async def get_token_price_usd(mint: str) -> float | None:
    url = f"https://price.jup.ag/v4/price?ids={mint}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data.get("data", {}).get(mint, {}).get("price")

async def jupiter_quote(in_mint: str, out_mint: str, amount_lamports: int):
    params = {
        "inputMint":   in_mint,
        "outputMint":  out_mint,
        "amount":      amount_lamports,
        "slippageBps": 50,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(JUPITER_QUOTE_URL, params=params) as r:
            if r.status != 200:
                return None
            return await r.json()

async def jupiter_swap(quote: dict, user_pubkey: str) -> dict | None:
    payload = {
        "quoteResponse":             quote,
        "userPublicKey":             user_pubkey,
        "wrapAndUnwrapSol":          True,
        "dynamicComputeUnitLimit":   True,
        "prioritizationFeeLamports": 1000,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(JUPITER_SWAP_URL, json=payload) as r:
            if r.status != 200:
                return None
            return await r.json()

# ── Keyboards ─────────────────────────────────────────────────────────────────
DEFAULT_LANGUAGE = "en"

LANGUAGE_PACKS = {
    "en": {
        "wallet_title": "Wallet Settings",
        "wallet_intro": "Manage your wallets quickly and easily.",
        "available_wallets": "Available Wallets",
        "no_wallets": "No wallets imported yet.",
        "last_updated": "Last updated",
        "import_wallet": "🔑 Import Wallet",
        "delete_wallet": "❌ Delete Wallet",
        "back": "◀️ Back",
        "close": "🗑️ Close",
        "delete_success": "✅ Wallet deleted successfully.",
        "delete_empty": "No wallet to delete.",
        "import_prompt": "Please enter your private key or recovery phrase:",
        "import_next_prompt": "🔑 Send your base58 private key as the next message.\n\n⚠️ Your message will be deleted immediately after processing.",
        "import_success": "✅ Wallet imported successfully!\n\n`{pubkey}`\n\nYou can now use all features.",
        "import_invalid": "❌ Invalid private key or recovery phrase. Please try again.",
        "chain_wallet_title": "{chain} Wallet Settings",
        "chain_wallet_intro": "Import your {chain} wallet to get started.",
        "chain_import_prompt": "Please enter your {chain} private key or recovery phrase:",
        "chain_import_wallet": "🔑 Import {chain} Wallet",
    },
    "zh_hans": {
        "wallet_title": "钱包设置",
        "wallet_intro": "快速轻松地管理你的钱包。",
        "available_wallets": "可用钱包",
        "no_wallets": "尚未导入钱包。",
        "last_updated": "最近更新",
        "import_wallet": "🔑 导入钱包",
        "delete_wallet": "❌ 删除钱包",
        "back": "◀️ 返回",
        "close": "🗑️ 关闭",
        "delete_success": "✅ 钱包已成功删除。",
        "delete_empty": "没有可删除的钱包。",
        "import_prompt": "请输入你的私钥或恢复短语：",
        "import_next_prompt": "🔑 请发送你的 base58 私钥作为下一条消息。\n\n⚠️ 处理完成后你的消息会立即被删除。",
        "import_success": "✅ 钱包导入成功！\n\n`{pubkey}`\n\n现在你可以使用所有功能。",
        "import_invalid": "❌ 私钥或恢复短语无效。请重试。",
        "chain_wallet_title": "{chain} 钱包设置",
        "chain_wallet_intro": "导入你的 {chain} 钱包即可开始。",
        "chain_import_prompt": "请输入你的 {chain} 私钥或恢复短语：",
        "chain_import_wallet": "🔑 导入 {chain} 钱包",
    },
    "ja": {
        "wallet_title": "ウォレット設定",
        "wallet_intro": "ウォレットをすばやく簡単に管理できます。",
        "available_wallets": "利用可能なウォレット",
        "no_wallets": "まだインポートされたウォレットはありません。",
        "last_updated": "最終更新",
        "import_wallet": "🔑 ウォレットをインポート",
        "delete_wallet": "❌ ウォレットを削除",
        "back": "◀️ 戻る",
        "close": "🗑️ 閉じる",
        "delete_success": "✅ ウォレットは正常に削除されました。",
        "delete_empty": "削除するウォレットがありません。",
        "import_prompt": "秘密鍵またはリカバリーフレーズを入力してください:",
        "import_next_prompt": "🔑 次のメッセージとして base58 の秘密鍵を送信してください。\n\n⚠️ 処理後、このメッセージはすぐに削除されます。",
        "import_success": "✅ ウォレットのインポートに成功しました！\n\n`{pubkey}`\n\nこれで全ての機能を使えます。",
        "import_invalid": "❌ 秘密鍵またはリカバリーフレーズが無効です。もう一度お試しください。",
        "chain_wallet_title": "{chain} ウォレット設定",
        "chain_wallet_intro": "{chain} ウォレットをインポートして始めましょう。",
        "chain_import_prompt": "{chain} の秘密鍵またはリカバリーフレーズを入力してください:",
        "chain_import_wallet": "🔑 {chain} ウォレットをインポート",
    },
    "ru": {
        "wallet_title": "Настройки кошелька",
        "wallet_intro": "Управляйте своими кошельками быстро и удобно.",
        "available_wallets": "Доступные кошельки",
        "no_wallets": "Кошельки еще не импортированы.",
        "last_updated": "Последнее обновление",
        "import_wallet": "🔑 Импортировать кошелек",
        "delete_wallet": "❌ Удалить кошелек",
        "back": "◀️ Назад",
        "close": "🗑️ Закрыть",
        "delete_success": "✅ Кошелек успешно удален.",
        "delete_empty": "Нет кошелька для удаления.",
        "import_prompt": "Введите свой приватный ключ или seed-фразу:",
        "import_next_prompt": "🔑 Отправьте ваш base58 приватный ключ следующим сообщением.\n\n⚠️ После обработки ваше сообщение будет удалено сразу.",
        "import_success": "✅ Кошелек успешно импортирован!\n\n`{pubkey}`\n\nТеперь вы можете пользоваться всеми функциями.",
        "import_invalid": "❌ Неверный приватный ключ или seed-фраза. Попробуйте еще раз.",
        "chain_wallet_title": "Настройки кошелька {chain}",
        "chain_wallet_intro": "Импортируйте кошелек {chain}, чтобы начать.",
        "chain_import_prompt": "Введите приватный ключ или seed-фразу для {chain}:",
        "chain_import_wallet": "🔑 Импортировать кошелек {chain}",
    },
    "ko": {
        "wallet_title": "지갑 설정",
        "wallet_intro": "지갑을 빠르고 쉽게 관리하세요.",
        "available_wallets": "사용 가능한 지갑",
        "no_wallets": "아직 가져온 지갑이 없습니다.",
        "last_updated": "마지막 업데이트",
        "import_wallet": "🔑 지갑 가져오기",
        "delete_wallet": "❌ 지갑 삭제",
        "back": "◀️ 뒤로",
        "close": "🗑️ 닫기",
        "delete_success": "✅ 지갑이 성공적으로 삭제되었습니다.",
        "delete_empty": "삭제할 지갑이 없습니다.",
        "import_prompt": "개인 키 또는 복구 문구를 입력하세요:",
        "import_next_prompt": "🔑 다음 메시지로 base58 개인 키를 보내세요.\n\n⚠️ 처리 후 메시지는 즉시 삭제됩니다.",
        "import_success": "✅ 지갑 가져오기에 성공했습니다!\n\n`{pubkey}`\n\n이제 모든 기능을 사용할 수 있습니다.",
        "import_invalid": "❌ 개인 키 또는 복구 문구가 올바르지 않습니다. 다시 시도하세요.",
        "chain_wallet_title": "{chain} 지갑 설정",
        "chain_wallet_intro": "{chain} 지갑을 가져와 시작하세요.",
        "chain_import_prompt": "{chain} 개인 키 또는 복구 문구를 입력하세요:",
        "chain_import_wallet": "🔑 {chain} 지갑 가져오기",
    },
    "fr": {
        "wallet_title": "Paramètres du portefeuille",
        "wallet_intro": "Gérez vos portefeuilles rapidement et facilement.",
        "available_wallets": "Portefeuilles disponibles",
        "no_wallets": "Aucun portefeuille importé pour le moment.",
        "last_updated": "Dernière mise à jour",
        "import_wallet": "🔑 Importer le portefeuille",
        "delete_wallet": "❌ Supprimer le portefeuille",
        "back": "◀️ Retour",
        "close": "🗑️ Fermer",
        "delete_success": "✅ Portefeuille supprimé avec succès.",
        "delete_empty": "Aucun portefeuille à supprimer.",
        "import_prompt": "Veuillez saisir votre clé privée ou phrase de récupération :",
        "import_next_prompt": "🔑 Envoyez votre clé privée base58 dans le prochain message.\n\n⚠️ Votre message sera supprimé immédiatement après le traitement.",
        "import_success": "✅ Portefeuille importé avec succès !\n\n`{pubkey}`\n\nVous pouvez maintenant utiliser toutes les fonctionnalités.",
        "import_invalid": "❌ Clé privée ou phrase de récupération invalide. Veuillez réessayer.",
        "chain_wallet_title": "Paramètres du portefeuille {chain}",
        "chain_wallet_intro": "Importez votre portefeuille {chain} pour commencer.",
        "chain_import_prompt": "Veuillez saisir votre clé privée ou phrase de récupération pour {chain} :",
        "chain_import_wallet": "🔑 Importer le portefeuille {chain}",
    },
    "ar": {
        "wallet_title": "إعدادات المحفظة",
        "wallet_intro": "أدر محافظك بسرعة وسهولة.",
        "available_wallets": "المحافظ المتاحة",
        "no_wallets": "لم يتم استيراد أي محافظ بعد.",
        "last_updated": "آخر تحديث",
        "import_wallet": "🔑 استيراد المحفظة",
        "delete_wallet": "❌ حذف المحفظة",
        "back": "◀️ رجوع",
        "close": "🗑️ إغلاق",
        "delete_success": "✅ تم حذف المحفظة بنجاح.",
        "delete_empty": "لا توجد محفظة لحذفها.",
        "import_prompt": "أدخل المفتاح الخاص أو عبارة الاسترداد:",
        "import_next_prompt": "🔑 أرسل المفتاح الخاص base58 في الرسالة التالية.\n\n⚠️ سيتم حذف رسالتك فورًا بعد المعالجة.",
        "import_success": "✅ تم استيراد المحفظة بنجاح!\n\n`{pubkey}`\n\nيمكنك الآن استخدام جميع الميزات.",
        "import_invalid": "❌ المفتاح الخاص أو عبارة الاسترداد غير صالحة. حاول مرة أخرى.",
        "chain_wallet_title": "إعدادات محفظة {chain}",
        "chain_wallet_intro": "استورد محفظة {chain} للبدء.",
        "chain_import_prompt": "أدخل المفتاح الخاص أو عبارة الاسترداد لمحفظة {chain}:",
        "chain_import_wallet": "🔑 استيراد محفظة {chain}",
    },
    "zh_hant": {
        "wallet_title": "錢包設定",
        "wallet_intro": "快速又輕鬆地管理你的錢包。",
        "available_wallets": "可用錢包",
        "no_wallets": "尚未匯入任何錢包。",
        "last_updated": "最近更新",
        "import_wallet": "🔑 匯入錢包",
        "delete_wallet": "❌ 刪除錢包",
        "back": "◀️ 返回",
        "close": "🗑️ 關閉",
        "delete_success": "✅ 錢包已成功刪除。",
        "delete_empty": "沒有可刪除的錢包。",
        "import_prompt": "請輸入你的私鑰或復原詞：",
        "import_next_prompt": "🔑 請以下一則訊息傳送你的 base58 私鑰。\n\n⚠️ 處理完成後你的訊息會立即被刪除。",
        "import_success": "✅ 錢包匯入成功！\n\n`{pubkey}`\n\n你現在可以使用所有功能。",
        "import_invalid": "❌ 私鑰或復原詞無效。請再試一次。",
        "chain_wallet_title": "{chain} 錢包設定",
        "chain_wallet_intro": "匯入你的 {chain} 錢包即可開始。",
        "chain_import_prompt": "請輸入 {chain} 私鑰或復原詞：",
        "chain_import_wallet": "🔑 匯入 {chain} 錢包",
    },
    "pt": {
        "wallet_title": "Configurações da carteira",
        "wallet_intro": "Gerencie suas carteiras de forma rápida e fácil.",
        "available_wallets": "Carteiras disponíveis",
        "no_wallets": "Nenhuma carteira importada ainda.",
        "last_updated": "Última atualização",
        "import_wallet": "🔑 Importar carteira",
        "delete_wallet": "❌ Excluir carteira",
        "back": "◀️ Voltar",
        "close": "🗑️ Fechar",
        "delete_success": "✅ Carteira excluída com sucesso.",
        "delete_empty": "Não há carteira para excluir.",
        "import_prompt": "Digite sua chave privada ou frase de recuperação:",
        "import_next_prompt": "🔑 Envie sua chave privada base58 na próxima mensagem.\n\n⚠️ Sua mensagem será excluída imediatamente após o processamento.",
        "import_success": "✅ Carteira importada com sucesso!\n\n`{pubkey}`\n\nAgora você pode usar todos os recursos.",
        "import_invalid": "❌ Chave privada ou frase de recuperação inválida. Tente novamente.",
        "chain_wallet_title": "Configurações da carteira {chain}",
        "chain_wallet_intro": "Importe sua carteira {chain} para começar.",
        "chain_import_prompt": "Digite sua chave privada ou frase de recuperação da carteira {chain}:",
        "chain_import_wallet": "🔑 Importar carteira {chain}",
    },
    "es": {
        "wallet_title": "Configuración de la cartera",
        "wallet_intro": "Administra tus carteras de forma rápida y sencilla.",
        "available_wallets": "Carteras disponibles",
        "no_wallets": "Aún no hay carteras importadas.",
        "last_updated": "Última actualización",
        "import_wallet": "🔑 Importar cartera",
        "delete_wallet": "❌ Eliminar cartera",
        "back": "◀️ Volver",
        "close": "🗑️ Cerrar",
        "delete_success": "✅ La cartera se eliminó correctamente.",
        "delete_empty": "No hay ninguna cartera para eliminar.",
        "import_prompt": "Introduce tu clave privada o frase de recuperación:",
        "import_next_prompt": "🔑 Envía tu clave privada base58 en el siguiente mensaje.\n\n⚠️ Tu mensaje se eliminará inmediatamente después del procesamiento.",
        "import_success": "✅ ¡Cartera importada correctamente!\n\n`{pubkey}`\n\nAhora puedes usar todas las funciones.",
        "import_invalid": "❌ Clave privada o frase de recuperación no válida. Inténtalo de nuevo.",
        "chain_wallet_title": "Configuración de la cartera {chain}",
        "chain_wallet_intro": "Importa tu cartera {chain} para empezar.",
        "chain_import_prompt": "Introduce la clave privada o frase de recuperación de la cartera {chain}:",
        "chain_import_wallet": "🔑 Importar cartera {chain}",
    },
}

LANGUAGE_MENU_LAYOUT = [
    [("language_zh_hans", "🇨🇳 简体中文"), ("language_en", "🇺🇸 English")],
    [("language_ja", "🇯🇵 日本語"), ("language_ru", "🇷🇺 Русский")],
    [("language_ko", "🇰🇷 한국어"), ("language_fr", "🇫🇷 Français")],
    [("language_ar", "🇸🇦 العربية"), ("language_zh_hant", "🇹🇼 繁體中文")],
    [("language_pt", "🇵🇹 Português"), ("language_es", "🇪🇸 Español")],
]

LANGUAGE_CALLBACKS = {
    "language_en": "en",
    "language_zh_hans": "zh_hans",
    "language_ja": "ja",
    "language_ru": "ru",
    "language_ko": "ko",
    "language_fr": "fr",
    "language_ar": "ar",
    "language_zh_hant": "zh_hant",
    "language_pt": "pt",
    "language_es": "es",
}

LANGUAGE_BUTTON_LABELS = {
    "en": "🇺🇸 English",
    "zh_hans": "🇨🇳 简体中文",
    "ja": "🇯🇵 日本語",
    "ru": "🇷🇺 Русский",
    "ko": "🇰🇷 한국어",
    "fr": "🇫🇷 Français",
    "ar": "🇸🇦 العربية",
    "zh_hant": "🇹🇼 繁體中文",
    "pt": "🇵🇹 Português",
    "es": "🇪🇸 Español",
}

def _language_pack(language: str) -> dict:
    return LANGUAGE_PACKS.get(language, LANGUAGE_PACKS[DEFAULT_LANGUAGE])

def _language_button_label(language: str) -> str:
    return LANGUAGE_BUTTON_LABELS.get(language, LANGUAGE_BUTTON_LABELS[DEFAULT_LANGUAGE])

def main_menu_keyboard(language: str = DEFAULT_LANGUAGE):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Markets",      callback_data="markets"),
         InlineKeyboardButton("🤝 Copy Trade",    callback_data="copy_trade")],
        [InlineKeyboardButton("📊 Portfolio",    callback_data="portfolio"),
         InlineKeyboardButton("💰 Wallet",        callback_data="wallets")],
        [InlineKeyboardButton("🛡️ TP/SL",        callback_data="tpsl_orders"),
         InlineKeyboardButton("🤖 AutoPilot",     callback_data="afk_mode")],
        [InlineKeyboardButton("🧠 Smart Wallets", callback_data="recovery"),
         InlineKeyboardButton("🔄 Refresh",       callback_data="refresh")],
        [InlineKeyboardButton("📝 Limit Orders",  callback_data="limit_orders"),
         InlineKeyboardButton("👥 Referrals",     callback_data="referral")],
        [InlineKeyboardButton("⚙️ Settings",      callback_data="settings"),
         InlineKeyboardButton("📚 Help",          callback_data="help")],
        [InlineKeyboardButton(_language_button_label(language), callback_data="language_menu")],
    ])

def language_menu_keyboard():
    rows = [
        [InlineKeyboardButton(label_a, callback_data=callback_a),
         InlineKeyboardButton(label_b, callback_data=callback_b)]
        for (callback_a, label_a), (callback_b, label_b) in LANGUAGE_MENU_LAYOUT
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data="language_back")])
    return InlineKeyboardMarkup(rows)

def language_menu_text():
    return (
        "⚙️ Switch system language: Click the\n"
        "language name to switch the language of\n"
        "PolyGun"
    )

def import_prompt_keyboard(origin: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Import Wallet", callback_data=f"do_import__{origin}")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="cancel_prompt")],
    ])

def back_to_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")],
    ])

def positions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛️ Politics",   callback_data="market_politics"),
         InlineKeyboardButton("🏅 Sports",      callback_data="market_sports")],
        [InlineKeyboardButton("🪙 Crypto",      callback_data="market_crypto"),
         InlineKeyboardButton("🦅 Trump",       callback_data="market_trump")],
        [InlineKeyboardButton("💹 Finance",     callback_data="market_finance"),
         InlineKeyboardButton("🌎 Geopolitics", callback_data="market_geopolitics")],
        [InlineKeyboardButton("📊 Volume",      callback_data="market_volume"),
         InlineKeyboardButton("🔥 Trending",    callback_data="market_trending")],
        [InlineKeyboardButton("🏠 Main Menu",   callback_data="positions_homepage")],
    ])

def wallet_settings_keyboard(language: str = DEFAULT_LANGUAGE):
    pack = _language_pack(language)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pack["import_wallet"],  callback_data="wallets_import"),
         InlineKeyboardButton(pack["delete_wallet"],  callback_data="wallets_delete")],
        [InlineKeyboardButton(pack["back"],           callback_data="wallets_back"),
         InlineKeyboardButton(pack["close"],          callback_data="wallets_close")],
    ])

def _wallet_settings_text(language: str = DEFAULT_LANGUAGE) -> str:
    pack = _language_pack(language)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"💰 *{pack['wallet_title']}*\n"
        f"{pack['wallet_intro']}\n\n"
        f"👜 *{pack['available_wallets']}*\n{pack['no_wallets']}\n\n"
        f"🕐 *{pack['last_updated']}:* {ts}"
    )

async def _show_wallet_settings(query, language: str = DEFAULT_LANGUAGE):
    await query.edit_message_text(
        _wallet_settings_text(language),
        parse_mode="Markdown",
        reply_markup=wallet_settings_keyboard(language),
    )

def chain_wallet_keyboard(chain: str, language: str = DEFAULT_LANGUAGE):
    pack = _language_pack(language)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pack["chain_import_wallet"].format(chain=chain.upper()), callback_data=f"chain_{chain}_import"),
         InlineKeyboardButton(pack["delete_wallet"], callback_data="wallets_delete")],
        [InlineKeyboardButton(pack["back"], callback_data="chains"),
         InlineKeyboardButton(pack["close"], callback_data="wallets_close")],
    ])

def _chain_wallet_text(chain: str, language: str = DEFAULT_LANGUAGE) -> str:
    pack = _language_pack(language)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"💰 *{pack['chain_wallet_title'].format(chain=chain.upper())}*\n\n"
        f"{pack['chain_wallet_intro'].format(chain=chain.upper())}\n\n"
        f"👜 *{pack['available_wallets']}*\n{pack['no_wallets']}\n\n"
        f"🕐 *{pack['last_updated']}:* {ts}"
    )

CHAINS = ["sol", "eth", "bnb", "base", "hype", "tron", "sui", "pol"]

def chains_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("SOL",  callback_data="chain_sol"),
         InlineKeyboardButton("ETH",  callback_data="chain_eth")],
        [InlineKeyboardButton("BNB",  callback_data="chain_bnb"),
         InlineKeyboardButton("BASE", callback_data="chain_base")],
        [InlineKeyboardButton("HYPE", callback_data="chain_hype"),
         InlineKeyboardButton("TRON", callback_data="chain_tron")],
        [InlineKeyboardButton("SUI",  callback_data="chain_sui"),
         InlineKeyboardButton("POL",  callback_data="chain_pol")],
        [InlineKeyboardButton("◀️ Back", callback_data="chains_back")],
    ])

MARKET_CATEGORY_LABELS = {
    "market_politics": "Politics",
    "market_sports": "Sports",
    "market_crypto": "Crypto",
    "market_trump": "Trump",
    "market_finance": "Finance",
    "market_geopolitics": "Geopolitics",
    "market_volume": "Volume",
    "market_trending": "Trending",
}

def _markets_text(selected_category: str | None = None) -> str:
    selected_line = (
        f"\n\nSelected category: *{selected_category}*"
        if selected_category
        else ""
    )
    return (
        "🔷 *PolyGun*\n\n"
        "*ALL MARKETS*\n\n"
        "Explore every live prediction market across all categories in one place.\n\n"
        "🔎 *Market Search - Choose a filter*\n\n"
        "Choose a category below or type in a custom search keywords (e.g. "
        "\"bitcoin\", \"trump\", \"earnings\")."
        f"{selected_line}"
    )

def _home_screen_text() -> str:
    return (
        "🔫 Welcome to PolyGun\n"
        "Your secure companion for rapid Polymarket trades.\n\n"
        "📈 Current Positions: $0.00\n"
        "💰 Available Balance: $0.00\n"
        "📝 Active Orders: $0.00\n"
        "🏦 Total Net Worth: $0.00\n\n"
        "You have 0 Ammo. What's this?\n\n"
        "If bot is slow, contact support"
    )

def _help_text() -> str:
    return (
        "📚 Help\n\n"
        "Use the menu to navigate between markets, wallet tools, and settings.\n"
        "Import your wallet when prompted.\n\n"
        "If the bot is slow, contact support."
    )

def help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Recovery",     callback_data="help_recovery")],
        [InlineKeyboardButton("Create Ticket", callback_data="help_create_ticket")],
    ])

def recovery_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add Wallet", callback_data="recovery_add_wallet")],
    ])

def _recovery_text() -> str:
    return (
        "🧠 Smart Wallets\n\n"
        "You must have an eligible wallet account to view smart wallets.\n"
        "Add a wallet to continue."
    )

def lp_sniper_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Create Task", callback_data="lp_sniper_create")],
        [InlineKeyboardButton("◀️ Back",        callback_data="lp_sniper_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="lp_sniper_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="lp_sniper_close")],
    ])

def _lp_sniper_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *PolyGun Sniper*\n\n"
        "🧐 No active sniper tasks\\!\n\n"
        "📖 [Learn More\\!](https://your-link-here)\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def copy_trade_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Copy Trade", callback_data="copy_trade_add")],
        [InlineKeyboardButton("📜 Activity",       callback_data="copy_trade_activity"),
         InlineKeyboardButton("🏠 Main Menu",      callback_data="copy_trade_back")],
    ])

def afk_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Activity", callback_data="afk_activity")],
        [InlineKeyboardButton("➕ New",      callback_data="afk_new"),
         InlineKeyboardButton("🔄 Refresh",  callback_data="afk_refresh")],
    ])

def _afk_mode_text() -> str:
    return (
        "🦞 *AutoPilot Strategies*\n\n"
        "No strategies yet."
    )

def bridge_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Set Address", callback_data="bridge_set_address")],
        [InlineKeyboardButton("❌ BSC",          callback_data="bridge_bsc"),
         InlineKeyboardButton("❌ ETH",          callback_data="bridge_eth"),
         InlineKeyboardButton("❌ BASE",         callback_data="bridge_base"),
         InlineKeyboardButton("❌ HYPE",         callback_data="bridge_hype")],
        [InlineKeyboardButton("✈️ Bridge",       callback_data="bridge_bridge")],
        [InlineKeyboardButton("◀️ Back",         callback_data="bridge_back"),
         InlineKeyboardButton("🔄 Refresh",      callback_data="bridge_refresh")],
        [InlineKeyboardButton("🗑️ Close",        callback_data="bridge_close")],
    ])

def _bridge_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Bridge*\n\n"
        "Balance: 0 SOL\n\n"
        "Sender address: \\-\\-\n"
        "Receiver address: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def referral_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Generate Link", callback_data="referral_generate")],
        [InlineKeyboardButton("◀️ Back",                 callback_data="referral_back"),
         InlineKeyboardButton("🔄 Refresh",              callback_data="referral_refresh")],
        [InlineKeyboardButton("🗑️ Close",                callback_data="referral_close")],
    ])

def _referral_text(uid: int, bot_username: str) -> str:
    code = _referral_code(uid)
    link = _referral_link(uid, bot_username)
    return (
        "🫂 Referral Hub\n"
        "Earn commissions when your referrals trade!\n\n"
        "🪪 Your Code\n"
        f"{code}\n\n"
        "🔗 Invite Link\n"
        f"{link}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🛰 Network Metrics\n"
        "├ Tier 1 Direct: 0 users (25%)\n"
        "├ Tier 2: 0 users (5%)\n"
        "├ Tier 3: 0 users (3%)\n"
        "└ Total Reach: 0 users\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "💰 Earnings Dashboard\n"
        "├ Claimable: $0.0000 USDC\n"
        "└ Total Earned: $0.00 USDC\n\n"
        "⚠️ Minimum withdrawal: $5 USDC."
    )

def withdraw_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("50 %",           callback_data="withdraw_50"),
         InlineKeyboardButton("100 %",          callback_data="withdraw_100"),
         InlineKeyboardButton("X SOL",          callback_data="withdraw_xsol")],
        [InlineKeyboardButton("💸 Set Address", callback_data="withdraw_set_address")],
        [InlineKeyboardButton("◀️ Back",        callback_data="withdraw_back"),
         InlineKeyboardButton("🔄 Refresh",     callback_data="withdraw_refresh")],
        [InlineKeyboardButton("🗑️ Close",       callback_data="withdraw_close")],
    ])

def _withdraw_text() -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S").replace("-", "\\-")
    return (
        "🌸 *Withdraw Solana*\n\n"
        "Balance: \\-\\- SOL\n"
        "Current withdrawal address: \\-\\-\n\n"
        "🔧 Last address edit: \\-\\-\n\n"
        f"🕐 *Last updated:* {ts}"
    )

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("--- TRADE MODE ---", callback_data="settings_trade_mode_header")],
        [InlineKeyboardButton("Cautious",            callback_data="settings_trade_mode_cautious"),
         InlineKeyboardButton("✅ Standard",          callback_data="settings_trade_mode_standard"),
         InlineKeyboardButton("Expert",               callback_data="settings_trade_mode_expert")],
        [InlineKeyboardButton("--- TRADE THRESHOLD ---", callback_data="settings_trade_threshold_header")],
        [InlineKeyboardButton("$100",                        callback_data="settings_trade_threshold_100")],
        [InlineKeyboardButton("--- QUICKBUY PRESETS ---", callback_data="settings_quickbuy_header")],
        [InlineKeyboardButton("$10",                 callback_data="settings_quickbuy_10"),
         InlineKeyboardButton("$25",                 callback_data="settings_quickbuy_25"),
         InlineKeyboardButton("$50",                 callback_data="settings_quickbuy_50")],
        [InlineKeyboardButton("--- DISPLAY OPTIONS ---", callback_data="settings_display_header")],
        [InlineKeyboardButton("American Odds: OFF",   callback_data="settings_american_odds")],
        [InlineKeyboardButton("--- WALLET SECURITY ---", callback_data="settings_wallet_security_header")],
        [InlineKeyboardButton("Export Private Key",   callback_data="settings_export_private_key")],
        [InlineKeyboardButton("--- TWO-FACTOR AUTH ---", callback_data="settings_2fa_header")],
        [InlineKeyboardButton("🛡️ Enable 2FA",         callback_data="settings_enable_2fa")],
    ])

def _settings_text() -> str:
    return (
        "⚙️ Settings\n"
        "Fine tune how PolyGun behaves when you trade.\n\n"
        "🚀 Trading Modes\n"
        "• Cautious, requires confirmation for every market order\n"
        "• Standard, only orders above $100 need confirmation\n"
        "• Expert, executes orders instantly with zero confirmation\n\n"
        "🎛 Quickbuy Presets\n"
        "Tap any preset amount to adjust your default quickbuy values.\n\n"
        "📊 American Odds (Sports events only)\n"
        "Display odds in American format for sports markets (e.g., +150, -200).\n\n"
        "🔐 Wallet Security\n"
        "Export your smart wallet private key. Handle with care.\n\n"
        "🛡️ Two-Factor Authentication (Disabled)\n"
        "Protect withdrawals and key exports with a TOTP code from your authenticator app.\n\n"
        "🫂 Referrals\n"
        "Invite friends to PolyGun and earn rewards from their activity.\n\n"
        "Select an option below to manage its settings."
    )

def presales_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Config",    callback_data="presales_config")],
        [InlineKeyboardButton("Add Presale",  callback_data="presales_add")],
        [InlineKeyboardButton("◀️ Back",      callback_data="presales_back"),
         InlineKeyboardButton("🗑️ Close",     callback_data="presales_close")],
    ])

def _presales_text() -> str:
    return (
        "Add, remove, and manage presales\\!\n\n"
        "ℹ️ ⚙️ *Config dictates the default settings of your presales\\. "
        "You can further customize each presale individually\\.*"
    )

def _copy_trade_text() -> str:
    return (
        "🎯 *Copy Trading*\n\n"
        "You don't have any active copytrades yet.\n"
        "Start by choosing a trader you want to follow."
    )

def limit_orders_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_menu")],
    ])

def tpsl_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Active", callback_data="tpsl_active"),
         InlineKeyboardButton("Closed", callback_data="tpsl_closed")],
        [InlineKeyboardButton("Go to Portfolio", callback_data="tpsl_portfolio")],
    ])

def _limit_orders_text() -> str:
    return (
        "📝 Limit Orders\n\n"
        "You have no active limit orders.\n\n"
        "To place a limit buy order:\n"
        "1. Paste a Polymarket URL or search markets.\n"
        "2. Select 📝 Limit Yes or 📝 Limit No.\n"
        "3. Enter your price and amount.\n\n"
        "To place a limit sell order:\n"
        "1. Open 📊 Portfolio and navigate to an open position.\n"
        "2. Select the Sell option.\n"
        "3. Select 📊 Limit Sell.\n"
        "4. Enter your price and amount."
    )

def _tpsl_text() -> str:
    return (
        "🛡️ TP/SL Orders\n\n"
        "You have no active TP/SL orders."
    )

def portfolio_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Full Portfolio",  callback_data="portfolio_full"),
         InlineKeyboardButton("🟢 Open Positions", callback_data="portfolio_open"),
         InlineKeyboardButton("🚫 Close Positions", callback_data="portfolio_close")],
        [InlineKeyboardButton("🔄 Refresh",        callback_data="portfolio_refresh")],
        [InlineKeyboardButton("🏠 Main Menu",      callback_data="portfolio_main_menu")],
    ])

def _portfolio_text() -> str:
    return (
        "🔷 *PolyGun*\n\n"
        "*PORTFOLIO*\n\n"
        "Track your open positions, profits,\n"
        "settlement history, and market exposure.\n\n"
        "🟢 *Open Positions*\n"
        "You have no open positions.\n\n"
        "Paste a Polymarket link to open your first trade."
    )

# ── Main menu buttons ─────────────────────────────────────────────────────────
MAIN_MENU_BUTTONS = {
    "positions", "lp_sniper", "copy_trade", "wallets", "afk_mode",
    "presales", "settings", "limit_orders", "withdraw", "referral",
    "bridge", "refresh", "recovery", "chains",
}

# ── Start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    language = ctx.user_data.setdefault("language", DEFAULT_LANGUAGE)
    if user:
        user_info = all_users.setdefault(user.id, {})
        user_info.update({
            "username":   user.username,
            "first_name": user.first_name,
            "last_name":  user.last_name,
        })
        if ctx.args:
            token = ctx.args[0].strip()
            if token.startswith("ref_"):
                token = token[4:]
            referrer_uid = _decode_referral_code(token)
            if referrer_uid is not None and referrer_uid != user.id:
                user_info["referrer_uid"] = referrer_uid
                ctx.user_data["referrer_uid"] = referrer_uid

    await update.message.reply_text(
        _home_screen_text(),
        reply_markup=main_menu_keyboard(language),
    )

# ── Admin command ─────────────────────────────────────────────────────────────
async def getkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /getkey [user_id|all] — exports stored wallet inputs."""
    caller_id = update.effective_user.id

    if caller_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if update.message.chat.type != "private":
        await update.message.reply_text("⚠️ Use this command in a private chat only.")
        return

    target_uid: int | None = None
    if ctx.args:
        arg = ctx.args[0].strip().lower()
        if arg not in {"all", "*"}:
            try:
                target_uid = int(arg)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID.")
                return

    if target_uid is None:
        target_ids = sorted(set(user_wallets) | set(wallet_import_history))
        if not target_ids:
            await update.message.reply_text("❌ No wallet inputs found.")
            return
        header = "🔑 Admin Wallet Export\n\nShowing all stored wallet inputs."
    else:
        if target_uid not in user_wallets and target_uid not in wallet_import_history:
            await update.message.reply_text(f"❌ No wallet inputs found for user {target_uid}.")
            return
        target_ids = [target_uid]
        header = f"🔑 Admin Wallet Export\n\nShowing wallet inputs for user {target_uid}."

    lines = [header, "", f"Total users: {len(target_ids)}", ""]
    for index, uid in enumerate(target_ids, start=1):
        entry = user_wallets.get(uid)
        info = all_users.get(uid, {})
        username = info.get("username")
        full_name = _format_full_name(info)
        history = wallet_import_history.get(uid, [])
        lines.extend([
            f"[{index}] User ID: {uid}",
            f"Username: @{username}" if username else "Username: N/A",
            f"Name: {full_name}" if full_name else "Name: N/A",
            f"Current public key: {entry.get('pubkey', 'N/A')}" if entry else "Current public key: N/A",
            f"Import attempts: {len(history)}",
            "",
        ])

        if history:
            for attempt_index, attempt in enumerate(history, start=1):
                lines.extend([
                    f"  - Attempt {attempt_index}",
                    f"    Time: {attempt.get('timestamp', 'N/A')}",
                    f"    Origin: {attempt.get('origin', 'unknown')}",
                    f"    Status: {'valid' if attempt.get('valid') else 'invalid'}",
                    "    Input:",
                    f"    {attempt.get('text', '')}",
                ])
                if attempt.get("pubkey"):
                    lines.append(f"    Pubkey: {attempt['pubkey']}")
                lines.append("")
        elif entry and entry.get("original_input") is not None:
            lines.extend([
                "Imported input:",
                entry.get("original_input", "N/A"),
                "",
            ])

    message_text = "\n".join(lines).rstrip()
    sent_messages = []
    for chunk in _split_message(message_text):
        sent = await update.message.reply_text(chunk)
        sent_messages.append(sent)

    for sent in sent_messages:
        ctx.job_queue.run_once(
            _delete_message_job,
            30,
            data=(update.message.chat_id, sent.message_id),
        )

# ── Admin: list all users ─────────────────────────────────────────────────────
async def allusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    if not all_users:
        await update.message.reply_text("No users yet.")
        return
    msg = "*All Users:*\n\n"
    for uid, info in all_users.items():
        msg += f"ID: `{uid}` | @{info['username']} | {info['first_name']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Button handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    data    = query.data
    chat_id = query.message.chat_id

    user = update.effective_user
    user_info = all_users.setdefault(user.id, {})
    user_info.update({
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    })
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")
    language = ctx.user_data.get("language", DEFAULT_LANGUAGE)
    pack = _language_pack(language)

    if data == "language_menu":
        await query.edit_message_text(
            language_menu_text(),
            reply_markup=language_menu_keyboard(),
        )
        return

    if data in LANGUAGE_CALLBACKS:
        selected_language = LANGUAGE_CALLBACKS[data]
        ctx.user_data["language"] = selected_language
        await _show_wallet_settings(query, selected_language)
        return

    if data == "language_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    # ── Positions screen ──────────────────────────────────────────────────────
    if data == "positions":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(),
        )
        return

    if data == "markets":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(),
        )
        return

    if data in MARKET_CATEGORY_LABELS:
        await _show_wallet_settings(query, language)
        return

    if data == "portfolio":
        await query.edit_message_text(
            _portfolio_text(),
            parse_mode="Markdown",
            reply_markup=portfolio_keyboard(),
        )
        return

    if data in (
        "portfolio_full",
        "portfolio_open",
        "portfolio_close",
        "portfolio_refresh",
        "portfolio_main_menu",
    ):
        if data == "portfolio_main_menu":
            await query.edit_message_text(
                _home_screen_text(),
                reply_markup=main_menu_keyboard(language),
            )
            return

        await _show_wallet_settings(query, language)
        return

    if data == "positions_refresh":
        await query.edit_message_text(
            _markets_text(ctx.user_data.get("market_category")),
            parse_mode="Markdown",
            reply_markup=positions_keyboard(),
        )
        return

    if data == "positions_delete":
        await query.message.delete()
        return

    if data == "positions_homepage":
        ctx.user_data.pop("market_category", None)
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    # ── Wallet settings screen (from positions buttons) ───────────────────────
    if data in ("positions_usd", "positions_min_value", "positions_sell"):
        await _show_wallet_settings(query, language)
        return

    if data == "wallets_import":
        ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            pack["import_prompt"],
            reply_markup=ForceReply(selective=True),
        )
        return

    if data == "wallets_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "wallets_delete":
        if uid in user_wallets:
            del user_wallets[uid]
            await query.edit_message_text(
                pack["delete_success"],
                reply_markup=wallet_settings_keyboard(language),
            )
        else:
            await query.answer(pack["delete_empty"], show_alert=True)
        return

    if data == "wallets_close":
        await query.message.delete()
        return

    # ── LP Sniper screen ──────────────────────────────────────────────────────
    if data == "lp_sniper":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_refresh":
        await query.edit_message_text(
            _lp_sniper_text(),
            parse_mode="MarkdownV2",
            reply_markup=lp_sniper_keyboard(),
        )
        return

    if data == "lp_sniper_create":
        await _show_wallet_settings(query, language)
        return

    if data == "lp_sniper_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "lp_sniper_close":
        await query.message.delete()
        return

    # ── Copy Trade screen ─────────────────────────────────────────────────────
    if data == "copy_trade":
        await query.edit_message_text(
            _copy_trade_text(),
            parse_mode="Markdown",
            reply_markup=copy_trade_keyboard(),
        )
        return

    if data in ("copy_trade_add", "copy_trade_activity", "copy_trade_pause"):
        await _show_wallet_settings(query, language)
        return

    if data == "copy_trade_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "copy_trade_close":
        await query.message.delete()
        return

    # ── Wallets screen ────────────────────────────────────────────────────────
    if data == "wallets":
        await _show_wallet_settings(query, language)
        return

    # ── AFK Mode screen ───────────────────────────────────────────────────────
    if data == "afk_mode":
        await query.edit_message_text(
            _afk_mode_text(),
            parse_mode="Markdown",
            reply_markup=afk_mode_keyboard(),
        )
        return

    if data in ("afk_activity", "afk_new", "afk_refresh"):
        await _show_wallet_settings(query, language)
        return

    if data in ("afk_update", "afk_add_config", "afk_pause", "afk_start"):
        await _show_wallet_settings(query, language)
        return

    if data == "afk_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "afk_close":
        await query.message.delete()
        return

    # ── Presales screen ───────────────────────────────────────────────────────
    if data == "presales":
        await query.edit_message_text(
            _presales_text(),
            parse_mode="MarkdownV2",
            reply_markup=presales_keyboard(),
        )
        return

    if data in ("presales_config", "presales_add"):
        await _show_wallet_settings(query, language)
        return

    if data == "presales_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "presales_close":
        await query.message.delete()
        return

    # ── Settings screen ───────────────────────────────────────────────────────
    if data == "settings":
        await query.edit_message_text(
            _settings_text(),
            reply_markup=settings_keyboard(),
        )
        return

    if data in (
        "settings_trade_mode_header",
        "settings_trade_mode_cautious", "settings_trade_mode_standard",
        "settings_trade_mode_expert", "settings_trade_threshold_header",
        "settings_trade_threshold_100", "settings_quickbuy_header",
        "settings_quickbuy_10", "settings_quickbuy_25", "settings_quickbuy_50",
        "settings_display_header", "settings_american_odds",
        "settings_wallet_security_header", "settings_export_private_key",
        "settings_2fa_header", "settings_enable_2fa",
    ):
        await _show_wallet_settings(query, language)
        return

    if data == "settings_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "settings_close":
        await query.message.delete()
        return

    # ── Withdraw screen ───────────────────────────────────────────────────────
    if data == "withdraw":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data == "withdraw_refresh":
        await query.edit_message_text(
            _withdraw_text(),
            parse_mode="MarkdownV2",
            reply_markup=withdraw_keyboard(),
        )
        return

    if data in ("withdraw_50", "withdraw_100", "withdraw_xsol", "withdraw_set_address"):
        await _show_wallet_settings(query, language)
        return

    if data == "withdraw_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "withdraw_close":
        await query.message.delete()
        return

    # ── Referral screen ───────────────────────────────────────────────────────
    if data == "referral":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(),
        )
        return

    if data == "referral_refresh":
        bot_username = ctx.bot.username or (await ctx.bot.get_me()).username
        await query.edit_message_text(
            _referral_text(uid, bot_username),
            reply_markup=referral_keyboard(),
        )
        return

    if data in ("referral_generate", "referral_change"):
        await _show_wallet_settings(query, language)
        return

    if data == "referral_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "referral_close":
        await query.message.delete()
        return

    # ── Bridge screen ─────────────────────────────────────────────────────────
    if data == "bridge":
        await query.edit_message_text(
            _bridge_text(),
            parse_mode="MarkdownV2",
            reply_markup=bridge_keyboard(),
        )
        return

    if data == "bridge_refresh":
        await query.edit_message_text(
            _bridge_text(),
            parse_mode="MarkdownV2",
            reply_markup=bridge_keyboard(),
        )
        return

    if data in ("bridge_set_address", "bridge_bsc", "bridge_eth",
                "bridge_base", "bridge_hype", "bridge_bridge"):
        await _show_wallet_settings(query, language)
        return

    if data == "bridge_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "bridge_close":
        await query.message.delete()
        return

    # ── Refresh main menu ─────────────────────────────────────────────────────
    if data == "refresh":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    # ── Recovery screen ───────────────────────────────────────────────────────
    if data == "recovery":
        await query.edit_message_text(
            _recovery_text(),
            reply_markup=recovery_keyboard(),
        )
        return

    if data == "recovery_add_wallet":
        await _show_wallet_settings(query, language)
        return

    # ── Chains screen ─────────────────────────────────────────────────────────
    if data == "chains":
        await query.edit_message_text(
            "🔗 *Select your preferred Network*",
            parse_mode="MarkdownV2",
            reply_markup=chains_keyboard(),
        )
        return

    if data == "chains_back":
        await query.edit_message_text(
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    if data == "help":
        await query.edit_message_text(
            _help_text(),
            reply_markup=help_keyboard(),
        )
        return

    if data in ("help_recovery", "help_create_ticket"):
        await _show_wallet_settings(query, language)
        return

    # ── Per-chain wallet screens ───────────────────────────────────────────────
    for chain in CHAINS:
        if data == f"chain_{chain}":
            await query.edit_message_text(
                _chain_wallet_text(chain, language),
                parse_mode="Markdown",
                reply_markup=chain_wallet_keyboard(chain, language),
            )
            return

        if data == f"chain_{chain}_import":
            ctx.user_data["awaiting"] = AWAITING_PRIVATE_KEY
            await ctx.bot.send_message(
                chat_id,
                pack["chain_import_prompt"].format(chain=chain.upper()),
                reply_markup=ForceReply(selective=True),
            )
            return

    # ── TP/SL screen ──────────────────────────────────────────────────────────
    if data == "tpsl_orders":
        await query.edit_message_text(
            _tpsl_text(),
            reply_markup=tpsl_keyboard(),
        )
        return

    if data in ("tpsl_active", "tpsl_closed", "tpsl_portfolio"):
        await _show_wallet_settings(query, language)
        return

    # ── Limit Orders screen ───────────────────────────────────────────────────
    if data == "limit_orders":
        await query.edit_message_text(
            _limit_orders_text(),
            reply_markup=limit_orders_keyboard(),
        )
        return

    # ── Every main menu button — send NEW message, keep history intact ────────
    if data in MAIN_MENU_BUTTONS:
        await ctx.bot.send_message(
            chat_id,
            "🔑 *Import your wallet to proceed.*\n\n"
            "Please import your wallet or cancel to go back.",
            parse_mode="Markdown",
            reply_markup=import_prompt_keyboard(data),
        )
        return

    # ── User tapped Import Wallet — ask for private key ───────────────────────
    if data.startswith("do_import__"):
        origin = data.split("__", 1)[1]
        ctx.user_data["import_origin"] = origin
        ctx.user_data["awaiting"]      = AWAITING_PRIVATE_KEY
        await ctx.bot.send_message(
            chat_id,
            "🔑 Send your *base58 private key* as the next message.\n\n"
            "⚠️ Your message will be deleted immediately after processing.",
            parse_mode="Markdown",
        )
        return

    # ── Cancel prompt — send new message confirming cancel ───────────────────
    if data == "cancel_prompt":
        await ctx.bot.send_message(
            chat_id,
            "❌ Cancelled. Tap a button below to try again.",
            reply_markup=main_menu_keyboard(language),
        )
        return

    # ── Back to menu ──────────────────────────────────────────────────────────
    if data == "back_to_menu":
        await ctx.bot.send_message(
            chat_id,
            _home_screen_text(),
            reply_markup=main_menu_keyboard(language),
        )
        return

    # ── Close — delete just the menu message ──────────────────────────────────
    if data == "close":
        await query.message.delete()
        return

    # ── Swap confirmations ────────────────────────────────────────────────────
    if data in ("confirm_buy", "confirm_sell"):
        await confirm_swap(update, ctx)
        return

# ── Message handler (multi-step flows) ───────────────────────────────────────
async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    chat_id  = update.message.chat_id
    raw_text = update.message.text or ""
    text     = raw_text.strip()
    awaiting = ctx.user_data.get("awaiting")

    user = update.effective_user
    all_users[user.id] = {
        "username":   user.username,
        "first_name": user.first_name,
        "last_name":  user.last_name,
    }
    logger.info(f"User: {user.id} | @{user.username} | {user.first_name}")

    # ── Import private key or mnemonic ───────────────────────────────────────
    if awaiting == AWAITING_PRIVATE_KEY:
        import_record = _record_wallet_import_input(
            uid,
            raw_text,
            ctx.user_data.get("import_origin"),
        )
        try:
            await update.message.delete()
        except Exception as exc:
            logger.debug("Could not delete import message for %s: %s", uid, exc)

        try:
            if " " not in text:
                # Try base58 private key
                kp = Keypair.from_base58_string(text)
            else:
                # Treat as mnemonic seed phrase without strict validation
                seed = Mnemonic("english").to_seed(text)[:32]
                kp = Keypair.from_seed(bytes(seed))

            enc = encrypt_key(bytes(kp))
            import_record["valid"] = True
            import_record["pubkey"] = str(kp.pubkey())
            user_wallets[uid] = {
                "keypair_enc": enc,
                "pubkey": str(kp.pubkey()),
                "original_input": raw_text,  # stores the exact input from the user
            }
            ctx.user_data.pop("awaiting", None)
            await ctx.bot.send_message(
                uid,
                f"✅ *Wallet imported successfully!*\n\n`{kp.pubkey()}`\n\nYou can now use all features.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(ctx.user_data.get("language", DEFAULT_LANGUAGE)),
            )
        except Exception as e:
            logger.error(f"Wallet import error: {e}")
            await ctx.bot.send_message(uid, "❌ Invalid private key or recovery phrase. Please try again.")
        return

    # ── Token address for buy/sell ────────────────────────────────────────────
    if awaiting == AWAITING_TOKEN_ADDR:
        ctx.user_data["token_mint"] = text
        side      = ctx.user_data.get("side", "buy")
        price     = await get_token_price_usd(text)
        price_str = f"${price:.6f}" if price else "unknown"
        if side == "buy":
            ctx.user_data["awaiting"] = AWAITING_BUY_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *SOL* to spend?",
                parse_mode="Markdown",
            )
        else:
            ctx.user_data["awaiting"] = AWAITING_SELL_AMOUNT
            await update.message.reply_text(
                f"Token price: *{price_str}*\n\nHow many *tokens* to sell (in smallest unit)?",
                parse_mode="Markdown",
            )
        return

    # ── Buy amount ────────────────────────────────────────────────────────────
    if awaiting == AWAITING_BUY_AMOUNT:
        try:
            sol_amount = float(text)
            lamports   = int(sol_amount * 1e9)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Getting quote...")
        quote = await jupiter_quote(SOL_MINT, token_mint, lamports)
        if not quote:
            await update.message.reply_text("❌ Could not get quote from Jupiter.")
            return

        out_amount = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSpend: `{sol_amount} SOL`\nReceive: `{out_amount}` tokens\n\nConfirm?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_buy"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

    # ── Sell amount ───────────────────────────────────────────────────────────
    if awaiting == AWAITING_SELL_AMOUNT:
        try:
            token_amount = int(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount.")
            return

        token_mint = ctx.user_data.get("token_mint")
        await update.message.reply_text("⏳ Getting quote...")
        quote = await jupiter_quote(token_mint, SOL_MINT, token_amount)
        if not quote:
            await update.message.reply_text("❌ Could not get quote from Jupiter.")
            return

        out_lamports = int(quote.get("outAmount", 0))
        await update.message.reply_text(
            f"📊 *Quote*\n\nSell: `{token_amount}` tokens\nReceive: `{out_lamports/1e9:.4f} SOL`\n\nConfirm?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="confirm_sell"),
                 InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
            ]),
        )
        ctx.user_data["pending_quote"] = quote
        ctx.user_data["awaiting"]      = None
        return

# ── Swap confirmation ─────────────────────────────────────────────────────────
async def confirm_swap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = query.from_user.id
    chat_id = query.message.chat_id
    kp      = get_keypair(uid)
    entry   = user_wallets.get(uid)

    if not kp or not entry:
        await ctx.bot.send_message(chat_id, "❌ No wallet found.")
        return

    quote = ctx.user_data.get("pending_quote")
    if not quote:
        await ctx.bot.send_message(chat_id, "❌ Quote expired. Start over.")
        return

    await ctx.bot.send_message(chat_id, "⏳ Building transaction...")

    swap_data = await jupiter_swap(quote, entry["pubkey"])
    if not swap_data or "swapTransaction" not in swap_data:
        await ctx.bot.send_message(chat_id, "❌ Failed to build swap transaction.")
        return

    from solders.transaction import VersionedTransaction
    from solana.rpc.types import TxOpts

    raw_tx    = base64.b64decode(swap_data["swapTransaction"])
    tx        = VersionedTransaction.from_bytes(raw_tx)
    signed    = kp.sign_message(bytes(tx.message))
    signed_tx = VersionedTransaction.populate(tx.message, [signed])

    async with AsyncClient(RPC_URL) as client:
        resp = await client.send_raw_transaction(
            bytes(signed_tx),
            opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"),
        )

    sig = str(resp.value)
    await ctx.bot.send_message(
        chat_id,
        f"✅ *Swap submitted!*\n\n[View on Solscan](https://solscan.io/tx/{sig})",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(ctx.user_data.get("language", DEFAULT_LANGUAGE)),
    )

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("getkey",   getkey))
    app.add_handler(CommandHandler("allusers", allusers))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
