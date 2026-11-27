"""
Support, FAQ, and Terms configuration for Alpha Bot and Alpha P2P.
"""
from config.settings import settings
from bot.utils.branding import BOT_NAME, CHANNEL_NAME

SUPPORT_FAQ_TEXT = (
    "**مركز المساعدة والأسئلة الشائعة 💡**\n\n"
    "• **من نحن وما هي منصة Alpha P2P؟⚡️**\n"
    "نحن منصة Alpha .. وجهتك الشاملة والآمنة لتسهيل الخدمات الرقمية والتجارية\n"
    "نقدم لك حلولاً موثوقة تشمل :\n"
    "• بيع وشراء الـ USDT\n\n"
    "• **كيف تعمل خدمة الضمان وحماية التداول؟**\n"
    "يقوم البوت باحتجاز رصيد USDT من البائع كوديعة ضمان مؤمنة قبل أن يقوم المشتري بالتحويل. "
    "ولا يتم تحرير USDT للمشتري إلا بعد تأكيد البائع استلام كامل المبلغ بالليرة السورية وموافقة الإدارة.\n\n"
    "• **ما هي المدد الزمنية للصفقات؟**\n"
    "— مهلة موافقة صاحب العرض: 30 دقيقة.\n"
    "— مهلة إيداع الضمان (USDT): 15 دقيقة.\n"
    "— مهلة تحويل الليرة السورية وإرفاق الإشعار: 30 دقيقة.\n"
    "— مهلة تأكيد استلام البائع بعد إرسال الإشعار: 30 دقيقة (مع تنبيه آلي للإدارة عند التأخر).\n\n"
    "• **كيف أتأكد من سلامة المعاملة؟**\n"
    "لا تقم أبداً بإرسال الليرة قبل إشعارك من البوت بأن الضمان قد تم حجزه وتأمينه بالفعل. "
    "والتزم بالتحويل حصراً لبيانات الحساب المحددة داخل جلسة الصفقة في البوت.\n\n"
    "• **ما هي عمولات التداول؟**\n"
    "مجانية تماماً للصفقات حتى 25 USDT، وتبلغ 0.1% على كل طرف للصفقات التي تتجاوز 25 USDT."
)


def _format_tg_handle(handle: str) -> str:
    clean = handle.strip().lstrip("@")
    escaped = clean.replace("_", r"\_")
    return f"@{escaped}"


def get_support_contact_text() -> str:
    support_user = settings.SUPPORT_USERNAME.strip() if settings.SUPPORT_USERNAME else ""
    channel_user = settings.CHANNEL_USERNAME.strip() if settings.CHANNEL_USERNAME else ""

    support_line = (
        f"👤 **معرف الدعم الفني :** {_format_tg_handle(support_user)}"
        if support_user
        else "👤 **الدعم الفني :** متاح عبر خيار إرسال رسالة للإدارة أدناه."
    )
    channel_line = (
        f"📢 **القناة الرسمية :** {_format_tg_handle(channel_user)} ({CHANNEL_NAME})"
        if channel_user
        else f"📢 **القناة الرسمية :** {CHANNEL_NAME}"
    )

    return (
        f"\u200f💬 **الدعم الفني — {BOT_NAME}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "للتواصل مع فريق الدعم وشرح مشكلتك أو الاستفسار عن صفقة، راسلنا مباشرة عبر:\n\n"
        f"{support_line}\n"
        f"{channel_line}"
    )


TERMS_AND_CONDITIONS_TEXT = """⛔️ وُضِعت هذه القواعد لضمان وضوح التعاملات وسلامتها .. وحفظ حقوق جميع الأطراف في القناة :


1_ نعمل كوسطاء بين البائع والمشتري (P2P) .. ولا نتحمل أي مسؤولية تجاه أي طرف ثالث ضمن المعاملة / على سبيل المثال : في حال قمتَ بوضع حساب شخص آخر لاستلام المبلغ بدلاً من حسابك الشخصي .. وتأخر هذا الشخص في تأكيد الاستلام أو بدر منه أي تقصير .. فسيتم إتمام المعاملة بعد مرور 30 دقيقة .. ولن نتحمل مسؤولية أي خطأ في التحويل .


2_ في حال قمتَ بنشر عرض في القناة وتقدّم أحد العملاء بطلب عليه .. ومضت أكثر من 30 دقيقة دون قبولك أو رفضك للطلب .. سيقوم البوت تلقائياً بحذف عرضك من القناة وتوجيه إنذار لك (بطاقة صفراء 🟨) .


3_ تبلغ مدة أي معاملة 30 د داخل Alpha Bot .. أي أنه في حال كنتَ بائعاً لعملة USDT وقمتَ بتحويل العملات إلينا .. وتأخر المشتري في تحويل المبلغ إليك .. فلا يحق لك إلغاء المعاملة واسترجاع العملات إلا بعد مرور 30 دقيقة .. وبالمقابل .. إذا كنتَ مشترياً لعملة USDT وقمتَ بتحويل المبلغ للبائع وتأخر في تأكيد الاستلام .. فلن يتم تسليمك العملات يدوياً من قِبل الإدارة إلا بعد مرور 30 دقيقة من وقت التحويل .


4_ في حال كنتَ بائعاً لعملة USDT ومضت أكثر من 15 دقيقة دون قيامك بتحويل العملات إلى حساب الوسيط .. سيقوم البوت بإلغاء المعاملة تلقائياً وتوجيه بطاقة صفراء لك 🟨 . 


5_ في حال كنتَ مشترياً لعملة USDT وتم إرسال بيانات الدفع إليك عبر البوت ولم تَقُم بتحويل المبلغ .. سيقوم البوت ب إلغاء المعاملة بعد مرور 30 د و بتوجيه بطاقة صفراء لك 🟨 . 


6_ في حال كنتَ بائعاً لعملة USDT ووصلك إشعار الدفع .. ومضت أكثر من 30 دقيقة دون تأكيدك أو رفضك لاستلام المبلغ .. سيقوم البوت بتوجيه بطاقة صفراء لك 🟨 / وفي حال عدم ردك على حساب الدعم لتأكيد استلام المبلغ .. ستقوم الإدارة بتحويل العملات إلى المشتري دون أدنى مسؤولية علينا .


7_ أي محاولة للتواصل المباشر مع عملاء القناة ستؤدي فوراً إلى الحظر النهائي من استخدام القناة والبوت 🚫 .


8_ في حال كنتَ بائعاً لعملة USDT ووصلك إشعار التحويل (قيمة العملات) عبر البوت، وقمتَ بتأكيد الاستلام دون التحقق الدقيق من تفاصيل الإشعار .. فسيتم إرسال العملات إلى المشتري بشكل فوري خلال 3 ثوانٍ .. ولا نتحمل أي مسؤولية عن أي خطأ في الاستلام ناتج عن التسرع في التأكيد وعدم التدقيق . 

9_ عند حصولك على 10 بطاقات صفر 🟨 للأسف سوف يتم حظرك بشكل نهائي من القناة 🚷 .


10_ يمنعك النظام تلقائياً من نشر عرض آخر مماثل لعرضك الأول إلا بعد مرور 15 دقيقة من نشر الأول . 


نشكركم على حسن تعاونكم والتزامكم، ونعتز بثقتكم في Alpha Bot 🙏"""


import re

def split_terms_sections(raw_text: str = TERMS_AND_CONDITIONS_TEXT) -> tuple[str, list[tuple[str, str, str]]]:
    """
    Splits TERMS_AND_CONDITIONS_TEXT at numbered headers starting with 'N_ ' or '**N. '.
    Returns:
        (preamble, [(section_key, section_title, section_full_text), ...])
    Reassembly contract:
        preamble + "".join(section_full_text for _, _, section_full_text in sections) == raw_text
    """
    parts = re.split(r"(?=^(?:\*\*\d+\.\s|\d+_\s))", raw_text, flags=re.MULTILINE)
    preamble = parts[0]
    sections = []
    for idx, part in enumerate(parts[1:], 1):
        first_line = part.split("\n", 1)[0]
        title = first_line.replace("**", "").strip()
        if len(title) > 45:
            title = f"البند {idx}"
        sections.append((str(idx), title, part))
    return preamble, sections


def reassemble_terms(preamble: str, sections: list[tuple[str, str, str]]) -> str:
    """Reassembles terms sections back to the original text."""
    return preamble + "".join(text for _, _, text in sections)


def split_faq_items(raw_text: str = SUPPORT_FAQ_TEXT) -> tuple[str, list[tuple[str, str, str]]]:
    """
    Splits SUPPORT_FAQ_TEXT at questions starting with '• **'.
    Returns:
        (preamble, [(item_key, question_title, item_full_text), ...])
    Reassembly contract:
        preamble + "".join(item_full_text for _, _, item_full_text in items) == raw_text
    """
    parts = re.split(r"(?=^•\s+\*\*)", raw_text, flags=re.MULTILINE)
    preamble = parts[0]
    items = []
    for idx, part in enumerate(parts[1:], 1):
        first_line = part.split("\n", 1)[0]
        # Title is question text without '•' and '**'
        q_title = first_line.replace("•", "").replace("**", "").strip()
        items.append((str(idx), q_title, part))
    return preamble, items


def reassemble_faq(preamble: str, items: list[tuple[str, str, str]]) -> str:
    """Reassembles FAQ items back to the original text."""
    return preamble + "".join(text for _, _, text in items)


