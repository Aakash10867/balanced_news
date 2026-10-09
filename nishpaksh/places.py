"""Where a story happens, by state or union territory (owner, Oct 9 2026: readers follow places and are notified).

The section question at publication (categories.py) also names the states the news happens in, from a fixed list;
code keeps a state only if the article itself names it, or one of its well-known cities or districts, in English or
Hindi. A state the model guessed from nowhere is dropped. A place can colour nothing and merge nothing: it only
decides who is told about an article and the site's place filter.

Stored as `payload.places` = ["haryana", "delhi"] (keys below, first = main), the same on the Hindi page.
"""
from __future__ import annotations

import re

# key: (English, Hindi, other names and cities found in articles)
STATES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "andhra-pradesh": ("Andhra Pradesh", "आंध्र प्रदेश", ("Andhra", "Amaravati", "Visakhapatnam", "Vizag", "Vijayawada", "Tirupati", "Guntur", "Nellore", "Kurnool", "विशाखापत्तनम", "तिरुपति")),
    "arunachal-pradesh": ("Arunachal Pradesh", "अरुणाचल प्रदेश", ("Arunachal", "Itanagar", "Tawang", "ईटानगर", "तवांग")),
    "assam": ("Assam", "असम", ("Guwahati", "Dibrugarh", "Silchar", "Jorhat", "Tezpur", "गुवाहाटी")),
    "bihar": ("Bihar", "बिहार", ("Patna", "Gaya", "Muzaffarpur", "Bhagalpur", "Darbhanga", "Purnia", "Saran", "Chhapra", "Begusarai", "पटना", "गया", "मुजफ्फरपुर", "भागलपुर", "दरभंगा")),
    "chhattisgarh": ("Chhattisgarh", "छत्तीसगढ़", ("Raipur", "Bilaspur", "Bastar", "Dantewada", "Bijapur", "Sukma", "Durg", "Bhilai", "रायपुर", "बस्तर", "दंतेवाड़ा", "सुकमा")),
    "goa": ("Goa", "गोवा", ("Panaji", "Margao", "Vasco", "पणजी")),
    "gujarat": ("Gujarat", "गुजरात", ("Ahmedabad", "Surat", "Vadodara", "Rajkot", "Gandhinagar", "Bhavnagar", "Jamnagar", "Kutch", "अहमदाबाद", "सूरत", "वडोदरा", "राजकोट", "गांधीनगर")),
    "haryana": ("Haryana", "हरियाणा", ("Gurugram", "Gurgaon", "Faridabad", "Rohtak", "Hisar", "Panipat", "Karnal", "Ambala", "Sonipat", "Panchkula", "Kurukshetra", "Nuh", "गुरुग्राम", "गुड़गांव", "फरीदाबाद", "रोहतक", "हिसार", "पानीपत", "करनाल", "अंबाला")),
    "himachal-pradesh": ("Himachal Pradesh", "हिमाचल प्रदेश", ("Himachal", "Shimla", "Manali", "Dharamshala", "Kullu", "Mandi", "शिमला", "मनाली", "कुल्लू")),
    "jharkhand": ("Jharkhand", "झारखंड", ("Ranchi", "Jamshedpur", "Dhanbad", "Bokaro", "Hazaribagh", "Deoghar", "रांची", "जमशेदपुर", "धनबाद", "बोकारो")),
    "karnataka": ("Karnataka", "कर्नाटक", ("Bengaluru", "Bangalore", "Mysuru", "Mysore", "Mangaluru", "Mangalore", "Hubballi", "Belagavi", "Belgaum", "बेंगलुरु", "मैसूर")),
    "kerala": ("Kerala", "केरल", ("Thiruvananthapuram", "Kochi", "Kozhikode", "Thrissur", "Kannur", "Kollam", "Wayanad", "Alappuzha", "Palakkad", "तिरुवनंतपुरम", "कोच्चि")),
    "madhya-pradesh": ("Madhya Pradesh", "मध्य प्रदेश", ("Bhopal", "Indore", "Gwalior", "Jabalpur", "Ujjain", "Sagar", "Rewa", "भोपाल", "इंदौर", "ग्वालियर", "जबलपुर", "उज्जैन")),
    "maharashtra": ("Maharashtra", "महाराष्ट्र", ("Mumbai", "Pune", "Nagpur", "Nashik", "Thane", "Aurangabad", "Chhatrapati Sambhajinagar", "Kolhapur", "Solapur", "Navi Mumbai", "मुंबई", "पुणे", "नागपुर", "नासिक", "ठाणे")),
    "manipur": ("Manipur", "मणिपुर", ("Imphal", "Churachandpur", "इंफाल")),
    "meghalaya": ("Meghalaya", "मेघालय", ("Shillong", "शिलांग")),
    "mizoram": ("Mizoram", "मिज़ोरम", ("Aizawl", "आइजोल")),
    "nagaland": ("Nagaland", "नगालैंड", ("Kohima", "Dimapur", "कोहिमा")),
    "odisha": ("Odisha", "ओडिशा", ("Orissa", "Bhubaneswar", "Cuttack", "Puri", "Rourkela", "Sambalpur", "भुवनेश्वर", "कटक", "पुरी")),
    "punjab": ("Punjab", "पंजाब", ("Ludhiana", "Amritsar", "Jalandhar", "Patiala", "Bathinda", "Mohali", "Tarn Taran", "Ferozepur", "Gurdaspur", "लुधियाना", "अमृतसर", "जालंधर", "पटियाला")),
    "rajasthan": ("Rajasthan", "राजस्थान", ("Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer", "Bikaner", "Alwar", "Jaisalmer", "जयपुर", "जोधपुर", "उदयपुर", "कोटा", "अजमेर", "बीकानेर")),
    "sikkim": ("Sikkim", "सिक्किम", ("Gangtok", "गंगटोक")),
    "tamil-nadu": ("Tamil Nadu", "तमिलनाडु", ("Chennai", "Coimbatore", "Madurai", "Tiruchirappalli", "Trichy", "Salem", "Tirunelveli", "Karur", "Vellore", "चेन्नई", "कोयंबटूर", "मदुरै")),
    "telangana": ("Telangana", "तेलंगाना", ("Hyderabad", "Warangal", "Secunderabad", "Karimnagar", "हैदराबाद")),
    "tripura": ("Tripura", "त्रिपुरा", ("Agartala", "अगरतला")),
    "uttar-pradesh": ("Uttar Pradesh", "उत्तर प्रदेश", ("Lucknow", "Kanpur", "Varanasi", "Prayagraj", "Allahabad", "Agra", "Meerut", "Ghaziabad", "Noida", "Gorakhpur", "Aligarh", "Bareilly", "Ayodhya", "Mathura", "Vrindavan", "Moradabad", "Sambhal", "लखनऊ", "कानपुर", "वाराणसी", "प्रयागराज", "आगरा", "मेरठ", "गाजियाबाद", "नोएडा", "गोरखपुर", "अयोध्या", "मथुरा")),
    "uttarakhand": ("Uttarakhand", "उत्तराखंड", ("Dehradun", "Haridwar", "Rishikesh", "Nainital", "Haldwani", "Kedarnath", "Badrinath", "देहरादून", "हरिद्वार", "ऋषिकेश", "नैनीताल")),
    "west-bengal": ("West Bengal", "पश्चिम बंगाल", ("Bengal", "Kolkata", "Calcutta", "Howrah", "Siliguri", "Darjeeling", "Durgapur", "Asansol", "Murshidabad", "कोलकाता", "हावड़ा", "सिलीगुड़ी", "बंगाल")),
    "andaman-nicobar": ("Andaman and Nicobar Islands", "अंडमान और निकोबार", ("Andaman", "Nicobar", "Port Blair", "Sri Vijaya Puram", "पोर्ट ब्लेयर")),
    "chandigarh": ("Chandigarh", "चंडीगढ़", ()),
    "dadra-daman-diu": ("Dadra and Nagar Haveli and Daman and Diu", "दादरा और नगर हवेली और दमन और दीव", ("Daman", "Diu", "Silvassa", "दमन", "दीव")),
    "delhi": ("Delhi", "दिल्ली", ("New Delhi", "नई दिल्ली")),
    "jammu-kashmir": ("Jammu and Kashmir", "जम्मू-कश्मीर", ("Jammu", "Kashmir", "Srinagar", "Anantnag", "Baramulla", "Pahalgam", "Kathua", "Kupwara", "Poonch", "Rajouri", "जम्मू", "कश्मीर", "श्रीनगर")),
    "ladakh": ("Ladakh", "लद्दाख", ("Leh", "Kargil", "लेह", "कारगिल")),
    "lakshadweep": ("Lakshadweep", "लक्षद्वीप", ("Kavaratti",)),
    "puducherry": ("Puducherry", "पुडुचेरी", ("Pondicherry", "Karaikal")),
}
MAX_PLACES = 3


def _key(x) -> str:
    return re.sub(r"[\s_]+", "-", str(x or "").strip().lower().strip("\"'")).replace("&", "and")


BY_NAME = {}
for _k, (_en, _hi, _alt) in STATES.items():
    for _n in (_k, _en, _hi, *_alt):
        BY_NAME[_key(_n)] = _k
BY_NAME.update({"jammu-and-kashmir": "jammu-kashmir", "j&k": "jammu-kashmir", "andaman-and-nicobar": "andaman-nicobar",
                "nct-of-delhi": "delhi", "up": "uttar-pradesh", "mp": "madhya-pradesh"})


def menu() -> str:
    """The list for the prompt: keys with their names."""
    return ", ".join(f"{k} ({v[0]})" for k, v in STATES.items())


def _named(text: str, name: str) -> bool:
    if not name:
        return False
    if re.search(r"[ऀ-ॿ]", name):          # Devanagari: no word boundaries to rely on
        return name in text
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, flags=re.I) is not None


def mentioned(key: str, text: str) -> bool:
    """The article names the state, or one of its cities or districts. "New Delhi" alone (the seat of the Centre)
    is not Delhi unless the news is about the city: a line of the government speaking "in New Delhi" is not."""
    en, hi, alt = STATES[key]
    names = [en, hi, *alt]
    if key == "delhi":
        bare = re.sub(r"\bNew Delhi\b|नई दिल्ली", " ", text)
        return _named(bare, "Delhi") or _named(bare, "दिल्ली")
    return any(_named(text, n) for n in names)


def confirm(picks, text: str) -> list[str]:
    """The model's states, kept only when the article names them (code), in its order, at most MAX_PLACES."""
    out: list[str] = []
    for p in picks if isinstance(picks, list) else [picks] if picks else []:
        k = BY_NAME.get(_key(p))
        if k and k not in out and mentioned(k, text or ""):
            out.append(k)
        if len(out) >= MAX_PLACES:
            break
    return out


def labels(lang: str = "en") -> dict[str, str]:
    i = 1 if lang == "hi" else 0
    return {k: v[i] for k, v in STATES.items()}


def article_text(payload: dict) -> str:
    """Headline and every sentence of the written article (what a reader sees)."""
    nar = (payload or {}).get("narrative") or {}
    parts = [payload.get("headline") or ""]
    for para in nar.get("paragraphs") or []:
        parts += [s.get("text") or "" for s in para or []]
    return "\n".join(parts)
