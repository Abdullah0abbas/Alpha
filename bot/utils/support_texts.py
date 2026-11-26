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


TERMS_AND_CONDITIONS_TEXT = """📜 **شروط استخدام Alpha Bot**

مرحبًا بك! باستخدامك Alpha Bot، فإنك توافق على الالتزام بالشروط والأحكام التالية. يرجى قراءتها بعناية.

**1. القبول بالشروط:**
باستخدامك للبوت، فإنك تقر بأنك قرأت وفهمت ووافقت على الالتزام بجميع الشروط والأحكام الواردة هنا. إذا لم توافق على أي جزء من هذه الشروط، فلا يجوز لك استخدام البوت.

**2. طبيعة الخدمة:**
يقدم البوت خدمة تداول مباشر (شخص لشخص) لعمليات بيع وشراء عملة USDT. نحن نقوم بالربط بين طرفي المعاملة لضمان حقوقهما. نُعلمك بأننا غير مسؤولين عن أي طرف ثالث آخر قد يتدخل في المعاملة خارج نطاق خدمتنا.
آلية عملنا: نستلم رصيد USDT من البائع، ثم ننتظر أن يقوم الطرف الآخر (المشتري) بإرسال القيمة المتفق عليها للطرف الأول (البائع) حسب طريقة الدفع المتفق عليها. بعد تأكيد وصول حق الطرف الأول كاملاً، نقوم بإرسال رصيد USDT للطرف الثاني. هنا تنتهي مهمتنا كوسيط.

**3. استخدام البوت:**
• **الاستخدام المشروع:** يجب استخدام البوت بطريقة قانونية ومسؤولة، وبما يتوافق مع جميع القوانين واللوائح المعمول بها.
• **المحتوى:** أنت مسؤول بالكامل عن أي محتوى أو معلومات تقوم بتقديمها أو إنشائها باستخدام البوت.
• **السلوك المحظور:** يحظر استخدام البوت لـ:
— نشر أو إرسال أي محتوى غير قانوني، ضار، تهديدي، مسيء، تشهيري، فاحش، بغيض، أو عنصري.
— التحرش، المطاردة، أو الإضرار بأي شخص آخر.
— انتحال شخصية أي كيان أو فرد.
— التدخل في عمل البوت أو الشبكات المتصلة به، أو الإضرار بها.
— جمع أو تخزين بيانات شخصية عن المستخدمين الآخرين دون موافقتهم.
— القيام بأي نشاط ينتهك حقوق الملكية الفكرية للآخرين.
— استغلال البوت لأغراض تجارية غير مصرح بها (ما لم يتم الاتفاق على ذلك صراحة).
— نشر عروض وهمية أو تقديم عروض كاذبة أو مضللة. أي ملاحظة أو دليل على ذلك سيعرضك للحظر الفوري من استخدام البوت.
• **التواصل مع الدعم:** يجب أن يقتصر التواصل مع الدعم الفني فقط على المسائل المتعلقة بعمل البوت أو العروض الخاصة بك.

**4. الخصوصية:**
نحن نلتزم بحماية خصوصيتك. يرجى مراجعة سياسة الخصوصية الخاصة بنا لفهم كيفية جمعنا واستخدامنا وتخزيننا للمعلومات.

**5. إخلاء المسؤولية:**
يتم تقديم البوت "كما هو" و"حسب توفره"، دون أي ضمانات من أي نوع، سواء كانت صريحة أو ضمنية. نحن لا نضمن أن البوت سيكون خاليًا من الأخطاء، أو أن الوصول إليه سيكون غير منقطع، أو أن أي عيوب سيتم تصحيحها.

**6. حدود المسؤولية:**
لن نكون مسؤولين عن أي أضرار مباشرة أو غير مباشرة أو عرضية أو خاصة أو تبعية تنشأ عن استخدامك أو عدم قدرتك على استخدام البوت، حتى لو تم إبلاغنا بإمكانية حدوث مثل هذه الأضرار.

**7. التعديلات على الشروط:**
نحتفظ بالحق في تعديل شروط الاستخدام هذه في أي وقت. سيتم نشر أي تغييرات على هذه الصفحة. استمرارك في استخدام البوت بعد نشر التغييرات يعني موافقتك على الشروط المعدلة. يرجى مراجعة هذه الشروط بانتظام.

**9. إخلاء مسؤولية وتنويه هام:**
جميع عمليات البيع والشراء عبر خدمة شام كاش وغيرها تتم بين الأطراف بشكل مباشر.
نحن نُخلِي مسؤوليتنا بالكامل عن أي تعاملات أو نزاعات تحدث بين الأطراف.
أنت وحدك تتحمّل المسؤولية الكاملة عن أي ضرر أو خسارة قد تلحق بك نتيجة هذه العمليات.
لا نضمن أي طرف أو أي عملية تداول تتم عبر الخدمة. بمجرد الضغط على زر "تأكيد التحقق"، ينتهي دورنا كوسيط بشكل نهائي في العملية.
دورنا يقتصر فقط على الوساطة لضمان أموال الطرفين أثناء إتمام العملية، ولسنا مسؤولين عن أي أمر آخر بعد ذلك.

**10. الإنهاء:**
يجوز لنا تعليق أو إنهاء وصولك إلى البوت في أي وقت، دون إشعار مسبق، لأي سبب، بما في ذلك انتهاك هذه الشروط.

فريق Alpha Bot 🔥"""


import re

def split_terms_sections(raw_text: str = TERMS_AND_CONDITIONS_TEXT) -> tuple[str, list[tuple[str, str, str]]]:
    """
    Splits TERMS_AND_CONDITIONS_TEXT at numbered headers starting with '**N. '.
    Returns:
        (preamble, [(section_key, section_title, section_full_text), ...])
    Reassembly contract:
        preamble + "".join(section_full_text for _, _, section_full_text in sections) == raw_text
    """
    parts = re.split(r"(?=^\*\*\d+\.\s)", raw_text, flags=re.MULTILINE)
    preamble = parts[0]
    sections = []
    for idx, part in enumerate(parts[1:], 1):
        first_line = part.split("\n", 1)[0]
        # Title is header text without '**'
        title = first_line.replace("**", "").strip()
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


