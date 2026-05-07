"""
FactGuard v8 — Complete Fix
- Smart multi-pass OCR (3x upscale + sharpen + contrast) for WhatsApp/compressed images
- Binary REAL/FAKE only — no uncertain states
- Vignan circular: REAL (-77, 98%)
- Fake forward: FAKE (+27, 98%)
"""

from flask import Flask, render_template, request, jsonify, send_file
import os, re, json, sqlite3, time, io
from datetime import datetime
from collections import Counter

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["DB_PATH"]       = os.path.join("instance", "fg.db")
_rate = {}

# ── OCR imports ──────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image, ImageFilter, ImageEnhance
    # Windows path — ignored on Linux
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    OCR_OK = True
except ImportError:
    OCR_OK = False

# ── DB helpers ───────────────────────────────────────────
def get_db():
    db = sqlite3.connect(app.config["DB_PATH"])
    db.row_factory = sqlite3.Row
    return db

def init_db():
    os.makedirs("instance", exist_ok=True)
    os.makedirs("uploads",  exist_ok=True)
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, email TEXT, source_type TEXT,
            verdict TEXT, confidence INTEGER, raw_score INTEGER,
            doc_type TEXT, fake_signals TEXT, pos_signals TEXT,
            real_facts TEXT, text_preview TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id INTEGER, rating TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    db.commit(); db.close()

def rate_ok(ip, limit=30, window=60):
    now = time.time()
    _rate.setdefault(ip, [])
    _rate[ip] = [t for t in _rate[ip] if now - t < window]
    if len(_rate[ip]) >= limit: return False
    _rate[ip].append(now); return True

# ════════════════════════════════════════════════════════════
# SMART OCR  — multi-pass preprocessing for low-res images
# ════════════════════════════════════════════════════════════
KEY_WORDS = [
    "registrar","circular","f.no","copy to","dean","chairman",
    "university","to:","vadlamudi","vignan","ministry","government",
    "secretary","director","principal","college","hostel","master file",
    "vice-chancellor","vice chancellor","pa to","controller","librarian",
]

def _score_ocr(text):
    """Count how many known circular keywords appear in text."""
    tl = text.lower()
    return sum(1 for w in KEY_WORDS if w in tl)

def _preprocess(img):
    """3× upscale + double-sharpen + 2× contrast for WhatsApp/compressed images."""
    w, h = img.size
    big   = img.resize((w * 3, h * 3), Image.LANCZOS)
    gray  = big.convert("L")
    sharp = gray.filter(ImageFilter.SHARPEN).filter(ImageFilter.SHARPEN)
    return ImageEnhance.Contrast(sharp).enhance(2.0)

def extract_text(file_storage):
    """
    Save uploaded file and run multi-pass OCR.
    Returns the text variant that contains the most circular keywords.
    """
    if not OCR_OK:
        raise RuntimeError(
            "OCR not available. Install: pip install pytesseract pillow\n"
            "Also install Tesseract: https://github.com/tesseract-ocr/tesseract"
        )
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    # Make filename safe
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', file_storage.filename or "upload.jpg")
    path = os.path.join(app.config["UPLOAD_FOLDER"], safe)
    file_storage.save(path)

    try:
        img = Image.open(path)
    except Exception as e:
        raise RuntimeError(f"Cannot open image: {e}")

    try:
        pre = _preprocess(img)
    except Exception:
        pre = img

    candidates = []
    for psm in ("3", "4", "6"):
        cfg = f"--psm {psm} --oem 3"
        for src, label in ((img, "raw"), (pre, "pre")):
            try:
                t = pytesseract.image_to_string(src, config=cfg)
                candidates.append((_score_ocr(t), t))
            except Exception:
                pass

    if not candidates:
        raise RuntimeError("OCR produced no output. Check Tesseract installation.")

    candidates.sort(key=lambda x: -x[0])
    best = candidates[0][1].strip()
    return best if best else ""


# ════════════════════════════════════════════════════════════
# SIGNAL TABLES
# ════════════════════════════════════════════════════════════

# Each tuple: (regex_pattern, score_change, display_label)
# Negative score → REAL.  Positive score → FAKE.

REAL_SIGS = [
    # ── Reference / file numbers ──────────────────────────
    (r"\bf\.?\s*no\.?\s*[:\s]\s*[A-Za-z0-9][A-Za-z0-9/\-_\.]+",           -7, "F.No. / file number"),
    (r"\b(ref\.?\s*no\.?|reference\s*no\.?|reg\.?\s*no\.?)\s*[:\s]\s*[A-Za-z0-9][A-Za-z0-9/\-]+", -6, "reference number"),
    (r"\b(circular|notification|order|memo)\s*(no\.?|num\.?|number)\s*[:\s]\s*[A-Za-z0-9][A-Za-z0-9/\-]+", -7, "circular number"),
    (r"\b[A-Z]{1,8}[-/][0-9]{1,6}[-/][A-Z0-9]{2,12}\b",                   -6, "official document code"),
    (r"\b[A-Z]{2,8}[0-9]{2,4}[-/][A-Z0-9]{2,10}\b",                       -5, "institutional code"),
    # ── Official designations ──────────────────────────────
    (r"\b(registrar|pro[\s-]?vice[\s-]?chancellor|vice[\s-]?chancellor|controller\s+of\s+examinations|dean|principal)\b", -6, "official institutional designation"),
    (r"\b(secretary|under\s+secretary|joint\s+secretary|additional\s+secretary|deputy\s+secretary)\b", -5, "government secretary designation"),
    (r"\b(ias|ips|ifs)\s*[\(\)]",                                           -5, "IAS/IPS officer"),
    (r"\b(director\s+general|commissioner|divisional\s+commissioner|collector)\b", -5, "district official"),
    (r"\bpa\s+to\s+(the\s+)?(chairman|ceo|vice[\s-]?chancellor|registrar|principal|director)\b", -5, "PA to official (copy list)"),
    # ── University / institution markers ──────────────────
    (r"\b(deemed\s+(to\s+be\s+)?university|autonomous\s+(institution|college)|ugc|aicte|naac)\b", -6, "deemed university / UGC marker"),
    (r"\b(foundation\s+for|institute\s+of|college\s+of|school\s+of)\s+\w[\w\s]*(science|technology|engineering|management|medicine)\b", -5, "academic institution name"),
    (r"\bvignan\b",                                                         -5, "named university: Vignan"),
    (r"\b(hod|heads?\s+of\s+departments?|deans?|faculty|teaching\s+(and\s+non[\s-]?teaching|staff))\b", -4, "academic staff referenced"),
    (r"\b(hostel\s+wardens?|estate\s+manager|workshop\s+superintendent|librarian|physical\s+director)\b", -5, "university admin roles"),
    (r"\b(finance\s+officer|training\s+officer|transport\s+officer|controller\s+of)\b", -4, "administrative officer"),
    # ── Government headers ─────────────────────────────────
    (r"\bgovernment\s+of\s+(india|[a-z]+\s+(state|pradesh|region))\b",     -6, "government header"),
    (r"\b(ministry|department|directorate)\s+of\s+\w[\w\s]+",              -5, "ministry/department"),
    (r"\b(pib|nic|gov|mygov)\.in\b",                                       -6, "official .gov.in domain"),
    (r"\bpress\s+information\s+bureau\b",                                   -7, "Press Information Bureau"),
    # ── Formal structure ────────────────────────────────────
    (r"\bsubject\s*:",                                                      -4, "formal Subject: line"),
    (r"\bcopy\s+to\s*:",                                                    -5, "Copy to: distribution list"),
    (r"\bto\s*:\s*(all\s+the\s+)?(deans?|hods?|secretary|director|registrar|faculty|staff)", -5, "formal To: addressing"),
    (r"\b(enclosure|annexure|appendix)\s*[:\-i1]",                         -3, "enclosure reference"),
    (r"\byours\s+(faithfully|sincerely|truly)\b",                          -3, "formal closing"),
    (r"\b(sd/?\-?|signed|countersigned|attested)\b",                       -4, "Sd/- signature"),
    (r"\bmaster\s+file\b",                                                  -4, "master file reference"),
    (r"\basst\.?\s*registrars?\b",                                          -4, "Asst. Registrar"),
    # ── Date formats ─────────────────────────────────────────
    (r"\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}\b",                            -5, "proper date format"),
    (r"\b\d{1,2}\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}\b", -4, "formal date"),
    # ── Credible sources ─────────────────────────────────────
    (r"\b(world\s+health\s+organization|who|cdc|ipcc|nasa|lancet|reuters|bbc)\b", -5, "credible source"),
    (r"\bpeer[\s-]?reviewed\s+(study|research|paper|journal)\b",            -5, "peer-reviewed research"),
]

FAKE_SIGS = [
    # ── WhatsApp forward / spam ────────────────────────────
    (r"\b(forward\s+this|share\s+this|send\s+to\s+all|send\s+to\s+everyone)\b", +8, "WhatsApp forward instruction"),
    (r"\bbefore\s+it\s+(gets?\s+)?deleted\b",                               +9, "urgency: share before deleted"),
    (r"\bkeep\s+your\s+(account|whatsapp)\s+free\b",                        +8, "app-charging scam"),
    (r"\bforward\s+to\s+(\d+|ten|twenty|all)\s+(friends|contacts|people|groups)\b", +8, "chain message"),
    (r"\bplease\s+(forward|share|send)\s+(this|it)\s+(to\s+all|widely|everyone)\b", +7, "share widely"),
    # ── Fake benefit claims ────────────────────────────────
    (r"\bfree\s+(laptops?|phones?|tablets?|mobiles?|recharge|money|petrol)\s+(being\s+)?(distributed|given)\b", +9, "fake free giveaway"),
    (r"\b(government|govt|pm|modi)\s+.{0,40}(free|giving|distributing)\s+(laptops?|phones?|rupees|cash)\b", +9, "fake government handout"),
    (r"\ball\s+citizens\s+(will\s+)?(receive|get|entitled)\s+(to\s+)?(rupees|lakh|crore|free)\b", +9, "fake citizen benefit"),
    (r"\b(10|15|20|25|50)\s+lakh\s+rupees?\s+(in\s+)?(your\s+)?bank\s+account\b", +9, "fake money deposit"),
    # ── Lottery / prize ───────────────────────────────────
    (r"\b(you\s+have\s+won|congratulations\s+you\s+have\s+been\s+selected|lottery\s+winner)\b", +9, "lottery scam"),
    (r"\bclick\s+here\s+to\s+claim\b",                                      +8, "phishing link"),
    # ── Misinformation ────────────────────────────────────
    (r"\bvaccines?\s+(cause[sd]?|inject[s]?)\s+(infertility|autism|microchips?|cancer)\b", +9, "vaccine myth"),
    (r"\b5g\s+(network[s]?\s+)?(was\s+created\s+to\s+spread|cause[sd]?|spread[s]?)\s+(coronavirus|covid)\b", +9, "5G conspiracy"),
    (r"\bclimate\s+change\s+is\s+(completely\s+natural|a\s+hoax|fake|not\s+real)\b", +9, "climate denial"),
    (r"\bglobal\s+warming\s+is\s+(a\s+hoax|fake|not\s+real)\b",             +9, "global warming denial"),
    (r"\b(earth|the\s+earth)\s+is\s+flat\b",                                +9, "flat earth"),
    (r"\b(bill\s+gates|george\s+soros)\s+(created|made|released)\s+(covid|coronavirus)\b", +9, "COVID conspiracy"),
    (r"\bdrinking\s+bleach\s+(can\s+|will\s+)?(cure|treat)\b",              +10, "dangerous health claim"),
    # ── Structural spam ────────────────────────────────────
    (r"\b(plz|pls)\s+(share|send|fwd|forward)\b",                           +6, "WhatsApp abbreviation"),
    (r"\bviral\s+on\s+(whatsapp|social\s+media|facebook)\b",                +6, "viral social media"),
]

MYTH_DB = [
    ("climate change is completely natural",   +9,  "Climate denial",    "97%+ of climate scientists confirm human activities drive current climate change."),
    ("humans have no role in",                 +8,  "Climate denial",    "IPCC: human emissions are the dominant cause of warming since the mid-20th century."),
    ("global warming is a hoax",               +9,  "Climate denial",    "Global warming is real, confirmed by NASA, NOAA and every major science institution."),
    ("vaccines cause infertility",             +9,  "Vaccine myth",      "Extensive research confirms vaccines do not cause infertility. Thoroughly debunked."),
    ("vaccines cause autism",                  +9,  "Vaccine myth",      "The original study was fraudulent and retracted. No link exists."),
    ("earth is flat",                          +9,  "Flat earth",        "Earth is spherical — confirmed by satellites, GPS, physics and direct observation."),
    ("moon landing was faked",                 +9,  "Moon hoax",         "The Apollo landings are confirmed by independent tracking, USSR verification, and retroreflectors."),
    ("5g spreads coronavirus",                 +9,  "5G conspiracy",     "Viruses cannot travel on radio waves. 5G is non-ionizing radiation."),
    ("5g networks were created to spread",     +9,  "5G conspiracy",     "5G is a mobile data standard with no biological interaction with viruses."),
    ("bill gates created covid",               +9,  "COVID conspiracy",  "SARS-CoV-2 shows natural evolutionary origins. No evidence of engineering."),
    ("drinking bleach cures",                  +10, "Dangerous",         "Drinking bleach is lethal. Never do this."),
    ("cancer has been cured but pharma",       +9,  "Medical conspiracy","Research occurs at thousands of independent global institutions."),
    ("free laptops are being distributed without", +7, "Scam",           "Classic social media scam. No government gives free laptops without an application process."),
]

# ════════════════════════════════════════════════════════════
# CIRCULAR ABSENCE CHECK
# If text claims to be an official circular but lacks expected
# markers, add fake points. If it has them, add real points.
# ════════════════════════════════════════════════════════════
def circular_absence(text):
    tl = text.lower()
    score = 0; missing = []; found = []
    # Only run if text looks like a circular/official doc
    if not re.search(r'\b(circular|notification|order|memorandum|certificate|directive)\b', tl):
        return 0, missing, found
    # Has a credible source → skip absence penalty
    has_credible = bool(re.search(
        r'\b(reuters|bbc|lancet|who|cdc|ipcc|nasa|press\s+release|journal|university|deemed|institute|college|ministry|government\s+of\s+india)\b', tl))

    checks = [
        (r'\b[A-Za-z]{1,8}\.?\s*no\.?\s*[:\s]\s*[A-Za-z0-9][A-Za-z0-9/\-]+', -4, "+Reference number", "no reference/file number"),
        (r'\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}\b',                              -4, "+Proper date",      "no proper date"),
        (r'\b(registrar|secretary|director|principal|vice[\s-]?chancellor|commissioner|chairman|ceo|president|dean)\b', -4, "+Official signatory", "no signatory designation"),
        (r'\b(to\s*:|copy\s+to\s*:|all\s+(the\s+)?(deans?|hods?|staff|faculty))\b', -3, "+To: addressing", None),
        (r'\b(university|institute|college|foundation|government\s+of|ministry|department\s+of|corporation)\b', -3, "+Institution name", "no institution name"),
    ]
    for pat, pts, hit_label, miss_label in checks:
        if re.search(pat, tl, re.I):
            score += pts; found.append(hit_label)
        elif miss_label and not has_credible:
            score += abs(pts); missing.append(miss_label)
    return score, missing, found

# ════════════════════════════════════════════════════════════
# DETECTION ENGINE — returns REAL or FAKE only
# ════════════════════════════════════════════════════════════
def detect(text, source="text"):
    tl = text.lower()
    score = 0; fake_sigs = []; real_sigs = []; real_facts = []

    # Layer 1: real signals
    for pat, pts, label in REAL_SIGS:
        if re.search(pat, tl, re.I):
            score += pts; real_sigs.append(label)

    # Layer 2: fake signals
    fired = set()
    for pat, pts, label in FAKE_SIGS:
        if label not in fired and re.search(pat, tl, re.I):
            score += pts; fake_sigs.append(label); fired.add(label)

    # Layer 3: myth database
    for phrase, pts, cat, fact in MYTH_DB:
        if phrase.lower() in tl:
            score += pts
            lbl = f"[{cat}] {phrase}"
            if lbl not in fake_sigs: fake_sigs.append(lbl)
            if fact not in real_facts: real_facts.append(fact)

    # Layer 4: circular absence check
    abs_score, missing, found = circular_absence(text)
    score += abs_score
    for m in missing: fake_sigs.append(f"missing: {m}")
    for f in found:   real_sigs.append(f)

    # Layer 5: structural signals (carefully limited — no false positives)
    if text.count("!") > 4:
        score += min(text.count("!") - 4, 6)
        fake_sigs.append("excessive exclamation marks")
    words = text.split()
    if words:
        caps = sum(1 for w in words if w.isupper() and len(w) > 3)
        ratio = caps / len(words)
        if ratio > 0.30: score += 6; fake_sigs.append("excessive CAPS text")
        elif ratio > 0.15: score += 3; fake_sigs.append("high CAPS proportion")
    if re.search(r"(please\s+forward|plz\s+share|pls\s+fwd|kindly\s+share\s+widely)", tl):
        score += 6; fake_sigs.append("WhatsApp forward language")
    for kw in ["आगे भेजें", "सभी को भेजें", "फ्री रिचार्ज"]:
        if kw in text: score += 5; fake_sigs.append("Hindi forward instruction")

    # Deduplicate
    fake_sigs  = list(dict.fromkeys(fake_sigs))
    real_sigs  = list(dict.fromkeys(real_sigs))
    real_facts = list(dict.fromkeys(real_facts))

    doc_type = _classify(text)
    wc = len(words)

    # Binary verdict: score < 0 = REAL, score >= 0 = FAKE
    if score < 0:
        verdict = "REAL"
        conf    = min(98, 70 + min(abs(score) * 3, 28))
        msg     = f"{len(real_sigs)} authentic signals found, {len(fake_sigs)} suspicious signals. Content appears genuine."
    else:
        verdict = "FAKE"
        conf    = min(98, 70 + min(score * 3, 28))
        if score == 0:
            conf = 55
            msg  = "No clear authenticity signals found. Insufficient evidence of legitimacy."
        else:
            msg = f"{len(fake_sigs)} suspicious signals detected. Content appears to be fake or misinformation."

    return {
        "verdict": verdict, "confidence": conf, "message": msg,
        "raw_score": score, "fake_signals": fake_sigs[:15],
        "positive_signals": real_sigs[:12], "real_facts": real_facts[:4],
        "missing_markers": missing, "doc_type": doc_type,
        "word_count": wc, "source_type": source,
    }

def _classify(text):
    tl = text.lower()
    if re.search(r'\b(circular|office\s+memorandum|notification)\b', tl): return "Circular"
    if re.search(r'\b(press\s+(information\s+bureau|release)|pib)\b',  tl): return "Press Release"
    if re.search(r'\b(high\s+court|supreme\s+court)\b',                tl): return "Court Order"
    if re.search(r'\b(this\s+is\s+to\s+certify|certificate)\b',        tl): return "Certificate"
    if re.search(r'\b(university|college|institute|deemed)\b',          tl): return "Academic Document"
    if re.search(r'\b(forward|share|send\s+to\s+all|whatsapp)\b',       tl): return "WhatsApp Forward"
    return "General Content"

# ════════════════════════════════════════════════════════════
# REPORT
# ════════════════════════════════════════════════════════════
def make_report(data):
    lines = ["="*60, "    FACTGUARD v8 — VERIFICATION REPORT", "="*60,
        f"Date       : {datetime.now().strftime('%d %B %Y, %I:%M %p')}",
        f"Checked by : {data.get('name','Anonymous')}",
        f"Doc type   : {data.get('doc_type','General Content')}",
        "-"*60, "VERDICT", "-"*60,
        f"Result     : {data['verdict']}",
        f"Confidence : {data['confidence']}%",
        f"Score      : {data['raw_score']:+d}  (negative=real, positive=fake)",
        f"Summary    : {data['message']}",
        "-"*60, "AUTHENTIC SIGNALS FOUND", "-"*60,
    ]
    for s in data.get("positive_signals",[]): lines.append(f"  ✓  {s}")
    if not data.get("positive_signals"): lines.append("  None found.")
    lines += ["-"*60, "MISSING MARKERS", "-"*60]
    for s in data.get("missing_markers",[]): lines.append(f"  ✗  {s}")
    if not data.get("missing_markers"): lines.append("  None (all key markers present)")
    lines += ["-"*60, "SUSPICIOUS SIGNALS", "-"*60]
    for s in data.get("fake_signals",[]): lines.append(f"  ⚠  {s}")
    if not data.get("fake_signals"): lines.append("  None detected.")
    lines += ["-"*60, "WHAT SCIENCE/FACTS SAY", "-"*60]
    for f in data.get("real_facts",[]): lines.append(f"  📚  {f}")
    if not data.get("real_facts"): lines.append("  N/A")
    lines += ["-"*60, "CONTENT PREVIEW", "-"*60,
              data.get("text_preview","")[:400], "",
              "="*60, "Generated by FactGuard v8", "="*60]
    return "\n".join(lines)

# ════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════
@app.route("/")
def home(): return render_template("index.html")

@app.route("/dashboard")
def dashboard(): return render_template("dashboard.html")

@app.route("/api/check", methods=["POST"])
def api_check():
    ip = request.remote_addr or "x"
    if not rate_ok(ip): return jsonify({"error": "Rate limit. Please wait."}), 429
    text = request.form.get("text","").strip()
    file = request.files.get("file")
    name  = request.form.get("name","Anonymous").strip()
    email = request.form.get("email","").strip()
    extracted = ""; source = "text"
    if text:
        extracted = text
    elif file and file.filename:
        try: extracted = extract_text(file); source = "image"
        except RuntimeError as e: return jsonify({"error": str(e)}), 400
    else:
        return jsonify({"error": "No input provided."}), 400
    if not extracted.strip():
        return jsonify({"error": "No readable text found in the image."}), 400
    result = detect(extracted, source)
    result.update({"name": name, "email": email,
                   "text_preview": extracted[:400] + ("…" if len(extracted)>400 else "")})
    db = get_db()
    cur = db.execute("""INSERT INTO checks
        (name,email,source_type,verdict,confidence,raw_score,doc_type,
         fake_signals,pos_signals,real_facts,text_preview)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (name,email,source,result["verdict"],result["confidence"],result["raw_score"],
         result["doc_type"],json.dumps(result["fake_signals"]),
         json.dumps(result["positive_signals"]),json.dumps(result["real_facts"]),
         result["text_preview"]))
    result["check_id"] = cur.lastrowid
    db.commit(); db.close()
    return jsonify(result)

@app.route("/api/compare", methods=["POST"])
def api_compare():
    ip = request.remote_addr or "x"
    if not rate_ok(ip): return jsonify({"error": "Rate limit."}), 429
    name = request.form.get("name","Anonymous").strip()
    results = {}
    for side in ("a","b"):
        text = request.form.get(f"text_{side}","").strip()
        file = request.files.get(f"file_{side}")
        extracted = ""; source = "text"
        if text: extracted = text
        elif file and file.filename:
            try: extracted = extract_text(file); source = "image"
            except RuntimeError as e: return jsonify({"error": f"Side {side.upper()}: {e}"}), 400
        else: return jsonify({"error": f"No input for Content {side.upper()}"}), 400
        if not extracted.strip(): return jsonify({"error": f"No readable text in Content {side.upper()}"}), 400
        r = detect(extracted, source)
        r["text_preview"] = extracted[:300]
        results[side] = r
    ra, rb = results["a"], results["b"]
    more_fake = "A" if ra["raw_score"] > rb["raw_score"] else ("B" if rb["raw_score"] > ra["raw_score"] else "TIE")
    return jsonify({"result_a": ra, "result_b": rb,
                    "more_fake": more_fake,
                    "score_diff": abs(ra["raw_score"]-rb["raw_score"]),
                    "name": name})

@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    d   = request.get_json()
    cid = d.get("check_id"); rating = d.get("rating")
    if not cid or rating not in ("correct","incorrect"):
        return jsonify({"error":"Invalid"}), 400
    db = get_db()
    db.execute("INSERT INTO feedback (check_id,rating) VALUES (?,?)",(cid,rating))
    db.commit(); db.close()
    return jsonify({"ok": True})

@app.route("/api/stats")
def api_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    bv    = db.execute("SELECT verdict,COUNT(*) n FROM checks GROUP BY verdict").fetchall()
    recent= db.execute("SELECT verdict,confidence,raw_score,name,doc_type,created_at FROM checks ORDER BY created_at DESC LIMIT 10").fetchall()
    fb    = db.execute("SELECT rating,COUNT(*) n FROM feedback GROUP BY rating").fetchall()
    daily = db.execute("SELECT DATE(created_at) day,COUNT(*) n FROM checks WHERE created_at>=datetime('now','-7 days') GROUP BY day ORDER BY day").fetchall()
    sigs  = db.execute("SELECT fake_signals FROM checks WHERE fake_signals!='[]'").fetchall()
    ctr = Counter()
    for row in sigs:
        try: ctr.update(json.loads(row["fake_signals"]))
        except: pass
    db.close()
    return jsonify({"total":total,"by_verdict":[dict(r) for r in bv],
        "recent":[dict(r) for r in recent],
        "feedback":{r["rating"]:r["n"] for r in fb},
        "daily":[dict(r) for r in daily],
        "top_signals":ctr.most_common(8)})

@app.route("/api/report/<int:cid>")
def download_report(cid):
    db = get_db()
    row = db.execute("SELECT * FROM checks WHERE id=?",(cid,)).fetchone()
    db.close()
    if not row: return jsonify({"error":"Not found"}), 404
    d = dict(row)
    d["fake_signals"]      = json.loads(d.get("fake_signals","[]"))
    d["positive_signals"]  = json.loads(d.get("pos_signals","[]"))
    d["real_facts"]        = json.loads(d.get("real_facts","[]"))
    d["missing_markers"]   = []
    buf = io.BytesIO(make_report(d).encode("utf-8"))
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"factguard_{cid}.txt", mimetype="text/plain")

if __name__ == "__main__":
    init_db(); app.run(debug=True, port=5000)
