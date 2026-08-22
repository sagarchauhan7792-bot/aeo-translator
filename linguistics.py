"""Per-language linguistic resources for the quality scorer.

These encode specific, checkable ways Indic machine translation gives itself
away. Every list here is a hypothesis, and calibrate.py tests them against real
text. Two of the original hypotheses failed that test and are documented below
as failures rather than quietly deleted, because the reasoning that produced
them is exactly the reasoning that will produce the next wrong rule.

WHAT SURVIVED (measured, native vs raw MT, per 1000 words):
  * Prepositional calques -- "के रूप में", "के द्वारा", "के माध्यम से" are
    literal renderings of "as", "by", "through". Native 0.4, MT 10.7.
  * English-shaped relative clauses -- जो / जिसे / जिसमें mirror "which/that".
    Native writing splits the sentence instead. Native 0.4, MT 3.7.
  * Comma density -- English punctuation rhythm carried across. Native 22, MT 72.
  * Space before punctuation -- a detokenisation artefact. Native 0.5, MT 29.8.
  * Honorific consistency -- mixed aap/tum in one document. Untested by the
    current fixtures (news and encyclopedia text does not address a reader), so
    it is kept but weighted low until there is evidence.

WHAT FAILED, AND IS THEREFORE NOT SCORED:
  * VERB_ENDINGS / verb-finality. The claim was that translated prose keeps
    English SVO order so fewer sentences end on a verb. Measured: native 0.921,
    human-translated 0.956, raw MT 0.906. Indic NMT gets word order right; this
    separates nothing. The table is kept because the measurement is still
    reported for diagnosis, but it carries zero weight.
  * DANDA. The claim was that Devanagari prose ends sentences with a danda and a
    full stop reads as machine output. Measured: BBC Hindi, written by Hindi
    journalists, uses a danda in 0% of paragraphs; Hindi Wikipedia and raw MT use
    one in ~98%. The rule was not merely useless, it was backwards, and it was
    penalising the most native text in the sample. Sentence-final punctuation is
    a house-style choice, not a humanness signal.

Change any list here, re-run calibrate.py, and look at the separation before
trusting the result.
"""
from __future__ import annotations

# --------------------------------------------------------------- verb endings
# Suffixes that mark a sentence-final verb. Checked against the last token.
VERB_ENDINGS: dict[str, tuple[str, ...]] = {
    "hi": ("है", "हैं", "था", "थी", "थे", "हो", "हूँ", "हूं", "गा", "गी", "गे",
           "ता", "ती", "ते", "या", "ये", "ना", "नी", "ने", "कर", "चाहिए",
           "सकता", "सकती", "सकते", "रहा", "रही", "रहे", "दें", "करें", "जाए",
           "जाता", "जाती", "होता", "होती", "होते", "लें", "पड़ता", "पड़ती"),
    "mr": ("आहे", "आहेत", "होता", "होती", "होते", "नाही", "करा", "करावे",
           "शकते", "शकतो", "जाते", "जातो", "असते", "असतो", "लागते", "येते",
           "पाहिजे", "हवे", "देते", "घ्या"),
    "gu": ("છે", "હતું", "હતા", "હતી", "થાય", "કરો", "શકે", "શકાય", "જોઈએ",
           "આવે", "રહે", "હોય", "થશે", "કરવું", "લેવું"),
    "bn": ("করে", "হয়", "ছিল", "আছে", "যায়", "করুন", "হবে", "থাকে", "পারে",
           "দিন", "নিন", "করছে", "হয়েছে", "উচিত", "যেতে"),
    "ta": ("கிறது", "ஆகும்", "உள்ளது", "வேண்டும்", "இருக்கும்", "படும்",
           "செய்யுங்கள்", "முடியும்", "இல்லை", "ஆகிறது", "வரும்", "தரும்"),
    "te": ("ఉంది", "అవుతుంది", "చేయండి", "ఉన్నాయి", "కావచ్చు", "చేస్తుంది",
           "ఇస్తుంది", "రావచ్చు", "లేదు", "ఉంటుంది", "కలదు", "చేయాలి"),
    "kn": ("ಇದೆ", "ಆಗುತ್ತದೆ", "ಮಾಡಿ", "ಬಹುದು", "ಇವೆ", "ಆಗಿದೆ", "ನೀಡುತ್ತದೆ",
           "ಇಲ್ಲ", "ಇರುತ್ತದೆ", "ಮಾಡಬೇಕು", "ಬರುತ್ತದೆ"),
    "pa": ("ਹੈ", "ਹਨ", "ਸੀ", "ਕਰੋ", "ਸਕਦਾ", "ਸਕਦੀ", "ਸਕਦੇ", "ਚਾਹੀਦਾ",
           "ਹੁੰਦਾ", "ਹੁੰਦੀ", "ਜਾਂਦਾ", "ਰਿਹਾ", "ਦਿਓ", "ਲਵੋ"),
}
VERB_ENDINGS["hinglish"] = ("hai", "hain", "tha", "thi", "the", "hoga", "hogi",
                            "karein", "sakta", "sakti", "sakte", "raha", "rahi",
                            "rahe", "chahiye", "jata", "jati", "hota", "hoti")

# ------------------------------------------------------- overused connectives
# Direct calques of the English discourse markers that generated prose leans on.
AI_CONNECTIVES: dict[str, tuple[str, ...]] = {
    "hi": ("इसके अलावा", "इसके अतिरिक्त", "हालांकि", "इसलिए", "साथ ही",
           "दूसरी ओर", "निष्कर्ष के तौर पर", "यह ध्यान देने योग्य है",
           "महत्वपूर्ण है कि", "इस प्रकार", "अंततः", "सबसे पहले", "इसके फलस्वरूप",
           "उपरोक्त", "निम्नलिखित"),
    "mr": ("याशिवाय", "तथापि", "म्हणून", "त्याचप्रमाणे", "दुसरीकडे",
           "निष्कर्षानुसार", "हे लक्षात घेणे महत्त्वाचे", "अशा प्रकारे", "शेवटी"),
    "gu": ("વધુમાં", "જોકે", "તેથી", "તે ઉપરાંત", "બીજી બાજુ", "નિષ્કર્ષમાં",
           "એ નોંધવું મહત્વપૂર્ણ છે", "આ રીતે", "અંતે"),
    "bn": ("এছাড়াও", "তবে", "অতএব", "উপরন্তু", "অন্যদিকে", "উপসংহারে",
           "এটি লক্ষ্য করা গুরুত্বপূর্ণ", "এইভাবে", "পরিশেষে"),
    "ta": ("மேலும்", "இருப்பினும்", "எனவே", "கூடுதலாக", "மறுபுறம்",
           "முடிவாக", "இது குறிப்பிடத்தக்கது", "இவ்வாறு", "இறுதியாக"),
    "te": ("అదనంగా", "అయితే", "కాబట్టి", "ఇంకా", "మరోవైపు", "ముగింపులో",
           "ఇది గమనించడం ముఖ్యం", "ఈ విధంగా", "చివరగా"),
    "kn": ("ಇದಲ್ಲದೆ", "ಆದಾಗ್ಯೂ", "ಆದ್ದರಿಂದ", "ಜೊತೆಗೆ", "ಮತ್ತೊಂದೆಡೆ",
           "ಕೊನೆಯಲ್ಲಿ", "ಇದನ್ನು ಗಮನಿಸುವುದು ಮುಖ್ಯ", "ಈ ರೀತಿಯಾಗಿ"),
    "pa": ("ਇਸ ਤੋਂ ਇਲਾਵਾ", "ਹਾਲਾਂਕਿ", "ਇਸ ਲਈ", "ਨਾਲ ਹੀ", "ਦੂਜੇ ਪਾਸੇ",
           "ਸਿੱਟੇ ਵਜੋਂ", "ਇਹ ਧਿਆਨ ਦੇਣ ਯੋਗ ਹੈ", "ਇਸ ਤਰ੍ਹਾਂ"),
}
AI_CONNECTIVES["hinglish"] = ("iske alawa", "halanki", "isliye", "saath hi",
                              "doosri or", "nishkarsh", "is prakar", "antatah")

# ----------------------------------------------------------------- calques
# Literal renderings of English constructions. Grammatical, but not how the
# language is written by people.
CALQUES: dict[str, tuple[str, ...]] = {
    "hi": ("यह ध्यान देने योग्य है कि", "के संदर्भ में", "के मामले में",
           "एक बार जब", "के रूप में जाना जाता है", "यह कहा जा सकता है कि",
           "के संबंध में", "इस तथ्य के कारण", "यह सुनिश्चित करें कि",
           "की एक विस्तृत श्रृंखला", "में एक महत्वपूर्ण भूमिका निभाता है",
           "जब बात आती है", "अपने आप में", "दिन के अंत में"),
    "mr": ("हे लक्षात घेणे महत्त्वाचे आहे की", "च्या संदर्भात", "च्या बाबतीत",
           "म्हणून ओळखले जाते", "एक महत्त्वाची भूमिका बजावते",
           "ची विस्तृत श्रेणी"),
    "gu": ("એ નોંધવું જોઈએ કે", "ના સંદર્ભમાં", "ના કિસ્સામાં",
           "તરીકે ઓળખાય છે", "મહત્વપૂર્ણ ભૂમિકા ભજવે છે"),
    "bn": ("এটি লক্ষণীয় যে", "এর প্রসঙ্গে", "এর ক্ষেত্রে",
           "হিসাবে পরিচিত", "গুরুত্বপূর্ণ ভূমিকা পালন করে"),
    "ta": ("இது குறிப்பிடத்தக்கது என்னவென்றால்", "சூழலில்", "விஷயத்தில்",
           "என அழைக்கப்படுகிறது", "முக்கிய பங்கு வகிக்கிறது"),
    "te": ("ఇది గమనించదగినది", "సందర్భంలో", "విషయంలో",
           "అని పిలుస్తారు", "ముఖ్యమైన పాత్ర పోషిస్తుంది"),
    "kn": ("ಇದನ್ನು ಗಮನಿಸಬೇಕು", "ಸಂದರ್ಭದಲ್ಲಿ", "ವಿಷಯದಲ್ಲಿ",
           "ಎಂದು ಕರೆಯಲಾಗುತ್ತದೆ", "ಪ್ರಮುಖ ಪಾತ್ರ ವಹಿಸುತ್ತದೆ"),
    "pa": ("ਇਹ ਧਿਆਨ ਦੇਣ ਯੋਗ ਹੈ ਕਿ", "ਦੇ ਸੰਦਰਭ ਵਿੱਚ", "ਦੇ ਮਾਮਲੇ ਵਿੱਚ",
           "ਵਜੋਂ ਜਾਣਿਆ ਜਾਂਦਾ ਹੈ", "ਮਹੱਤਵਪੂਰਨ ਭੂਮਿਕਾ ਨਿਭਾਉਂਦਾ ਹੈ"),
}
CALQUES["hinglish"] = ("yeh dhyan dene yogya hai", "ke sandarbh mein",
                       "ke maamle mein", "ke roop mein jana jata hai")

# --------------------------------------------------------------- honorifics
# (pronoun forms, expected verb agreement) per politeness level.
HONORIFICS: dict[str, dict[str, tuple[str, ...]]] = {
    "hi": {"formal": ("आप", "आपको", "आपका", "आपकी", "आपके"),
           "informal": ("तुम", "तुम्हें", "तुम्हारा", "तुम्हारी"),
           "intimate": ("तू", "तुझे", "तेरा", "तेरी")},
    "mr": {"formal": ("तुम्ही", "तुम्हाला", "तुमचा", "तुमची"),
           "informal": ("तू", "तुला", "तुझा", "तुझी"), "intimate": ()},
    "gu": {"formal": ("તમે", "તમને", "તમારું", "તમારી"),
           "informal": ("તું", "તને", "તારું"), "intimate": ()},
    "bn": {"formal": ("আপনি", "আপনার", "আপনাকে"),
           "informal": ("তুমি", "তোমার", "তোমাকে"),
           "intimate": ("তুই", "তোর")},
    "ta": {"formal": ("நீங்கள்", "உங்கள்", "உங்களுக்கு"),
           "informal": ("நீ", "உன்", "உனக்கு"), "intimate": ()},
    "te": {"formal": ("మీరు", "మీ", "మీకు"),
           "informal": ("నువ్వు", "నీ", "నీకు"), "intimate": ()},
    "kn": {"formal": ("ನೀವು", "ನಿಮ್ಮ", "ನಿಮಗೆ"),
           "informal": ("ನೀನು", "ನಿನ್ನ", "ನಿನಗೆ"), "intimate": ()},
    "pa": {"formal": ("ਤੁਸੀਂ", "ਤੁਹਾਡਾ", "ਤੁਹਾਨੂੰ"),
           "informal": ("ਤੂੰ", "ਤੇਰਾ", "ਤੈਨੂੰ"), "intimate": ()},
}
HONORIFICS["hinglish"] = {"formal": ("aap", "aapko", "aapka", "aapki"),
                          "informal": ("tum", "tumhe", "tumhara"),
                          "intimate": ("tu", "tujhe", "tera")}

# ------------------------------------------------- over-formal (tatsama) words
# Heavily Sanskritised vocabulary. Correct, but wrong register for a brand that
# speaks plainly -- and a reliable marker of dictionary-driven translation.
FORMAL_MARKERS: dict[str, tuple[str, ...]] = {
    "hi": ("अत्यंत", "तथापि", "किंचित", "यथोचित", "एवं", "तथा", "हेतु",
           "उपरांत", "परिलक्षित", "प्रयुक्त", "समुचित", "तत्पश्चात",
           "विद्यमान", "आवश्यकतानुसार", "उल्लेखनीय", "प्रतीत", "स्वास्थ्यवर्धक"),
    "mr": ("अत्यंत", "तथापि", "एवं", "तथा", "उपरांत", "प्रयुक्त", "समुचित"),
    "gu": ("અત્યંત", "તથાપિ", "એવં", "તથા", "ઉપરાંત"),
    "bn": ("অত্যন্ত", "তথাপি", "এবং", "তথা", "উপরন্তু", "প্রযুক্ত"),
    "ta": ("மிகவும்", "ஆயினும்", "மேலும்", "எனவே"),
    "te": ("అత్యంత", "అయినప్పటికీ", "మరియు", "తథా"),
    "kn": ("ಅತ್ಯಂತ", "ಆದಾಗ್ಯೂ", "ಮತ್ತು", "ತಥಾ"),
    "pa": ("ਅਤਿਅੰਤ", "ਤਥਾਪਿ", "ਏਵੰ", "ਤਥਾ"),
}
FORMAL_MARKERS["hinglish"] = ("atyant", "tathapi", "evam", "tatha", "hetu")

# Languages whose prose ends sentences with a danda rather than a full stop.
DANDA_LANGS = {"hi", "mr", "pa", "bn"}
DANDA = "।"

# Scripts, as (start, end) codepoint pairs, for the purity check.
SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "Devanagari": (0x0900, 0x097F),
    "Bengali": (0x0980, 0x09FF),
    "Gurmukhi": (0x0A00, 0x0A7F),
    "Gujarati": (0x0A80, 0x0AFF),
    "Oriya": (0x0B00, 0x0B7F),
    "Tamil": (0x0B80, 0x0BFF),
    "Telugu": (0x0C00, 0x0C7F),
    "Kannada": (0x0C80, 0x0CFF),
    "Malayalam": (0x0D00, 0x0D7F),
    "Latin": (0x0041, 0x007A),
}


def get(table: dict, lang: str, default=()):
    """Look up a language, falling back to the script parent (mr -> hi shapes)."""
    if lang in table:
        return table[lang]
    return default
