import os
import re
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB limit

ALLOWED_EXTENSIONS = {'epub', 'txt', 'pdf'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_txt(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def extract_text_from_pdf(filepath):
    import pdfplumber
    text_parts = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return '\n'.join(text_parts)


def _epub_resolve(href, item_offsets):
    """Map a TOC href to a word-offset, tolerating path prefix differences."""
    base = href.split('#')[0].split('?')[0].lstrip('/')
    if base in item_offsets:
        return item_offsets[base]
    # Match by the trailing portion of the path
    for name in item_offsets:
        if name.endswith('/' + base) or name == base:
            return item_offsets[name]
    # Last resort: match by bare filename
    leaf = base.rsplit('/', 1)[-1]
    for name in item_offsets:
        if name.rsplit('/', 1)[-1] == leaf:
            return item_offsets[name]
    return None


def _epub_toc_chapters(toc, item_offsets):
    chapters = []

    def walk(items):
        for item in items:
            if isinstance(item, tuple):
                section, children = item
                _add(section)
                walk(children)
            else:
                _add(item)

    def _add(entry):
        href = getattr(entry, 'href', None)
        title = getattr(entry, 'title', None)
        if not href or not title or not title.strip():
            return
        word_idx = _epub_resolve(href, item_offsets)
        if word_idx is not None:
            chapters.append({'title': title.strip(), 'word_index': word_idx})

    walk(toc)
    return sorted(chapters, key=lambda x: x['word_index'])


def extract_text_from_epub(filepath):
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(filepath, options={'ignore_ncx': True})
    text_parts = []
    item_offsets = {}   # item name -> cumulative word start index
    item_soups = {}     # item name -> soup (kept for heading fallback)
    word_offset = 0

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        name = item.get_name()
        item_offsets[name] = word_offset
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        item_soups[name] = soup
        text = soup.get_text(separator=' ')
        cleaned = re.sub(r'\s+', ' ', text).strip()
        word_offset += len(tokenize(cleaned))
        text_parts.append(text)

    # Primary: use the declared EPUB table of contents
    chapters = _epub_toc_chapters(book.toc, item_offsets)

    # Fallback: scan h1/h2/h3 tags per document item
    if not chapters:
        for name in sorted(item_offsets, key=item_offsets.get):
            heading = item_soups[name].find(['h1', 'h2', 'h3'])
            if heading:
                title = heading.get_text(strip=True)
                if title and len(title) < 120:
                    chapters.append({'title': title, 'word_index': item_offsets[name]})

    return '\n'.join(text_parts), chapters


def detect_chapters(words):
    """Heuristic chapter detection for plain text and PDF."""
    TRIGGERS = {
        'chapter', 'part', 'section', 'book', 'prologue',
        'epilogue', 'introduction', 'preface', 'afterword', 'appendix',
        'interlude', 'coda',
    }
    chapters = []
    i = 0
    while i < len(words):
        w = words[i].lower().rstrip('.,!?;:')
        if w in TRIGGERS:
            # Include the trigger word plus up to 4 following tokens as the title
            end = min(i + 5, len(words))
            title = ' '.join(words[i:end])
            chapters.append({'title': title, 'word_index': i})
            i = end
        else:
            i += 1
    return chapters


def clean_text(text):
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    return text


def tokenize(text):
    # Attach trailing punctuation to the preceding word so bare marks never flash alone
    words = re.findall(r"[A-Za-z0-9''’-]+[.,!?;:…]*", text)
    return [w for w in words if w.strip()]


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Unsupported file type'}), 400

    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[1].lower()
    tmp_path = f'/tmp/spritz_upload.{ext}'
    file.save(tmp_path)

    try:
        if ext == 'txt':
            raw = extract_text_from_txt(tmp_path)
            chapters = []
        elif ext == 'pdf':
            raw = extract_text_from_pdf(tmp_path)
            chapters = []
        elif ext == 'epub':
            raw, chapters = extract_text_from_epub(tmp_path)
        else:
            return jsonify({'error': 'Unsupported format'}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to extract text: {str(e)}'}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    text = clean_text(raw)
    words = tokenize(text)

    if not words:
        return jsonify({'error': 'No readable text found in file'}), 400

    if ext in ('txt', 'pdf'):
        chapters = detect_chapters(words)

    return jsonify({'words': words, 'total': len(words), 'chapters': chapters})


if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    app.run(debug=True, port=5000)
