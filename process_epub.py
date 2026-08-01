import os
import re
import sys
import zipfile
import shutil
import argparse
import subprocess
from lxml import etree
from pathlib import Path
from datetime import datetime

metadata_keys = ['Title', 'Series', 'Number', 'Writer', 'LanguageISO', 'Year', 'Month', 'Day', 'Summary', 'Publisher', 'Tags']
opf_ns = {'opf': 'http://www.idpf.org/2007/opf'}
dc_ns = {'dc': 'http://purl.org/dc/elements/1.1/'}
ci_ns = {
    'xsd': 'http://www.w3.org/2001/XMLSchema',
    'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
}
svg_ns = {
    'xhtml': 'http://www.w3.org/1999/xhtml',
    'svg': 'http://www.w3.org/2000/svg',
    'xlink': 'http://www.w3.org/1999/xlink'
}

# ---------------------------------------------------------------------------
# Classification tuning
# ---------------------------------------------------------------------------
# A spine page counts as "text-heavy" once it has at least this many
# non-whitespace characters of body text. Manga image pages have ~none;
# light-novel prose pages have hundreds+.
TEXT_PAGE_MIN_CHARS = 200

# Language codes treated as Japanese. Mokuro OCR only helps Japanese text, so
# anything else is auto-marked no-OCR.
JAPANESE_LANG_PREFIX = 'ja'

# --- Manual overrides: add these as tags in Calibre before exporting ---
# Force-treat as a light novel -> skip entirely (no CBZ, no OCR).
FORCE_SKIP_TAGS = {'light-novel', 'light novel', 'ln', 'skip'}
# Force-treat as manga even if the content heuristic guesses light novel.
FORCE_MANGA_TAGS = {'manga', 'force-manga'}
# Convert to CBZ but skip Mokuro OCR (the automated _no_ocr replacement).
FORCE_NO_OCR_TAGS = {'no-ocr', 'no_ocr', 'noocr'}

# Automatic light-novel hints from Kobo/Rakuten metadata (substring match).
LIGHT_NOVEL_KEYWORDS = {'ライトノベル', 'ラノベ', 'light novel'}

def convert_epub_to_cbz(epub_path, output_dir, dry_run=False):
    # Unzip the EPUB file
    with zipfile.ZipFile(epub_path, 'r') as z:
        # 1. Find the OPF file
        container = z.read('META-INF/container.xml')
        c_root = etree.fromstring(container)
        opf_rel_path = c_root.xpath('//ns:rootfile/@full-path', namespaces={'ns': 'urn:oasis:names:tc:opendocument:xmlns:container'})[0]
        opf_dir = os.path.dirname(opf_rel_path)
        
        # 2. Parse OPF to get metadata and the Spine (reading order)
        opf_data = z.read(opf_rel_path)
        o_root = etree.fromstring(opf_data)
        metadata_tree = o_root.xpath('//opf:metadata', namespaces=opf_ns)[0]
        metadata = {}
        titles = metadata_tree.xpath('dc:title/text()', namespaces=dc_ns)
        metadata['Title'] = titles[0] if titles else 'Unknown Title'
        seriesList = metadata_tree.xpath('opf:meta[@property="belongs-to-collection"]/text()', namespaces=opf_ns)
        metadata['Series'] = seriesList[0] if seriesList else 'Unknown Series'
        numbers = metadata_tree.xpath('opf:meta[@property="group-position"]/text()', namespaces=opf_ns)
        metadata['Number'] = numbers[0] if numbers else ''
        if not dry_run:
            print(f"[*] Processing: {metadata['Series']} - {metadata['Number']}")
        metadata['Writer'] = ",".join(metadata_tree.xpath('dc:creator/text()', namespaces=dc_ns))
        languages = metadata_tree.xpath('dc:language/text()', namespaces=dc_ns)
        metadata['LanguageISO'] = languages[0] if languages else ''
        timestamps = metadata_tree.xpath('dc:date/text()', namespaces=dc_ns)
        metadata['publish_date'] = datetime.fromisoformat(timestamps[0]) if timestamps else None
        if metadata['publish_date']:
            metadata['Year'] = metadata['publish_date'].year
            metadata['Month'] = metadata['publish_date'].month
            metadata['Day'] = metadata['publish_date'].day
        descriptions = metadata_tree.xpath('dc:description/text()', namespaces=dc_ns)
        metadata['Summary'] = descriptions[0] if descriptions else ''
        tags = re.compile('<.*?>')
        metadata['Summary'] = re.sub(tags, '', metadata['Summary']).strip()
        publishers = metadata_tree.xpath('dc:publisher/text()', namespaces=dc_ns)
        metadata['Publisher'] = publishers[0] if publishers else ''
        metadata['Tags'] = ",".join(metadata_tree.xpath('dc:subject/text()', namespaces=dc_ns))
        # print(f"[*] Author(s): {metadata['Writer']}, Language: {metadata['LanguageISO']}, Publisher: {metadata['Publisher']}, tags: {metadata['Tags']}")
        # if metadata['publish_date']:
        #     print(f"[*] Publish Date: {metadata['Year']}-{metadata['Month']}-{metadata['Day']}")
        # print(f"[*] Description: {metadata['Summary']}...")
        manifest = {item.get('id'): item.get('href') for item in o_root.xpath('//opf:manifest/opf:item', namespaces=opf_ns)}
        spine_items = o_root.xpath('//opf:spine/opf:itemref', namespaces=opf_ns)

        # 3. Single pass over the spine: resolve each page's images and collect
        #    the stats used to tell manga apart from light novels.
        pages = []            # ordered list of in-zip image paths per spine entry
        image_page_count = 0  # spine pages holding at least one image
        text_page_count = 0   # spine pages that are text-heavy (prose)
        for itemref in spine_items:
            href = manifest.get(itemref.get('idref'))
            if not href:
                continue

            full_href_path = os.path.join(opf_dir, href).replace('\\', '/')

            # If spine points to HTML, find the image(s) inside it
            if href.endswith(('.html', '.xhtml')):
                h_root = etree.fromstring(z.read(full_href_path))
                img_srcs = h_root.xpath('//xhtml:img/@src | //svg:image/@xlink:href | //svg:image/@href', namespaces=svg_ns)
                img_paths = [os.path.normpath(os.path.join(os.path.dirname(full_href_path), src)).replace('\\', '/') for src in img_srcs]
                if img_paths:
                    image_page_count += 1
                if visible_text_length(h_root) >= TEXT_PAGE_MIN_CHARS:
                    text_page_count += 1
                pages.append(img_paths)
            # If spine points directly to an image
            elif href.lower().endswith(('.jpg', '.jpeg', '.png')):
                image_page_count += 1
                pages.append([full_href_path])

        # 4. Classify: light novel -> skip entirely; manga -> convert (+ maybe OCR).
        is_ln, ln_reason = is_light_novel(metadata, image_page_count, text_page_count)
        skip_ocr, ocr_reason = should_skip_ocr(metadata)

        if dry_run:
            stats = f"imgs={image_page_count} text={text_page_count}"
            if is_ln:
                print(f"[DRY] LIGHT NOVEL (skip) | {stats:<20} | {ln_reason:<28} | {metadata['Series']} - {metadata['Title']}")
            else:
                verdict = 'MANGA no-OCR' if skip_ocr else 'MANGA + OCR '
                reason = ocr_reason if skip_ocr else f"{ln_reason}; {ocr_reason}"
                print(f"[DRY] {verdict}     | {stats:<20} | {reason:<28} | {metadata['Series']} - {metadata['Title']}")
            return

        if is_ln:
            print(f"[-] Skipping light novel: {metadata['Title']} ({ln_reason})")
            return

        cbz_path = os.path.join(output_dir, metadata['Series'], f"{metadata['Title']}.cbz")
        series_dir = os.path.dirname(cbz_path)
        os.makedirs(series_dir, exist_ok=True)

        # 5. Auto-mark series that should skip Mokuro OCR (replaces the manual
        #    _no_ocr file). The marker is honored by the mokuro loop below.
        if skip_ocr:
            mark_no_ocr(series_dir)
            print(f"[*] Marking no-OCR: {metadata['Series']} ({ocr_reason})")

        if os.path.exists(cbz_path):
            print(f"[-] Skipping: {metadata['Title']} (CBZ already exists)")
            return

        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as cbz:
            # 6. Save metadata into ComicInfo.xml in the CBZ
            ci_root = etree.Element('ComicInfo', nsmap=ci_ns)

            for key in metadata_keys:
                if key in metadata and metadata[key]:
                    etree.SubElement(ci_root, key).text = str(metadata[key])

            etree.SubElement(ci_root, 'Manga').text = 'YesAndRightToLeft'
            comic_info_xml = etree.tostring(ci_root, encoding='utf-8', xml_declaration=True, pretty_print=True).decode('utf-8')
            # print(f"[*] ComicInfo.xml: \n{comic_info_xml}")
            cbz.writestr('ComicInfo.xml', comic_info_xml)

            # 7. Write page images in spine order
            page_idx = 1
            for img_paths in pages:
                for img_path in img_paths:
                    if store_image(z, img_path, cbz, page_idx):
                        page_idx += 1

        print(f"[*] Conversion complete: {cbz_path}")

def store_image(zip_ref, zip_path, cbz_ref, idx):
    if zip_path not in zip_ref.namelist():
        print(f"[-] Warning: Image not found in EPUB: {zip_path}")
        return False
    ext = os.path.splitext(zip_path)[1]
    new_name = f"{idx:04d}{ext}"
    with zip_ref.open(zip_path) as src:
        cbz_ref.writestr(new_name, src.read())
    return True

def visible_text_length(h_root):
    # Count non-whitespace body text, ignoring <script>/<style> so that inline
    # CSS on manga image pages isn't mistaken for prose.
    for el in h_root.xpath('//*[local-name()="script" or local-name()="style"]'):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    bodies = h_root.xpath('//*[local-name()="body"]')
    node = bodies[0] if bodies else h_root
    return len(re.sub(r'\s+', '', ''.join(node.itertext())))

def epub_tag_set(metadata):
    return {t.strip().lower() for t in metadata.get('Tags', '').split(',') if t.strip()}

def is_light_novel(metadata, image_pages, text_pages):
    """Return (is_light_novel, reason)."""
    tag_set = epub_tag_set(metadata)
    # Manual overrides win over any automatic guess.
    if tag_set & FORCE_MANGA_TAGS:
        return False, 'force-manga tag'
    if tag_set & FORCE_SKIP_TAGS:
        return True, 'skip tag'
    # Kobo/Rakuten genre hint.
    tags_joined = metadata.get('Tags', '').lower()
    hit = next((k for k in LIGHT_NOVEL_KEYWORDS if k.lower() in tags_joined), None)
    if hit:
        return True, f'genre tag "{hit}"'
    # Content heuristic: a manga is (almost) all full-page images, while a light
    # novel is mostly prose with a handful of illustration inserts.
    if image_pages == 0:
        return True, 'no image pages'
    if text_pages > image_pages:
        return True, f'text pages {text_pages} > image pages {image_pages}'
    return False, f'image pages {image_pages} >= text pages {text_pages}'

def should_skip_ocr(metadata):
    """Return (skip_ocr, reason)."""
    if epub_tag_set(metadata) & FORCE_NO_OCR_TAGS:
        return True, 'no-ocr tag'
    lang = metadata.get('LanguageISO', '').lower()
    # Only auto-skip when the language is known and clearly not Japanese; treat
    # a missing language as Japanese since the library is Japanese manga.
    if lang and not lang.startswith(JAPANESE_LANG_PREFIX):
        return True, f'language "{lang}"'
    return False, f'language "{lang or "unknown"}"'

def mark_no_ocr(series_dir):
    os.makedirs(series_dir, exist_ok=True)
    marker = os.path.join(series_dir, '_no_ocr')
    if not os.path.exists(marker):
        open(marker, 'a').close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert manga EPUBs to CBZ (skipping light novels) and OCR them with Mokuro.")
    parser.add_argument("epub_dir", help="Folder searched recursively for .epub files")
    parser.add_argument("output_dir", nargs="?", default="output",
                        help="Output directory (default: output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only print how each EPUB would be classified; write nothing and skip Mokuro.")
    args = parser.parse_args()

    if not os.path.isdir(args.epub_dir):
        print(f"Error: {args.epub_dir} is not a valid directory.")
        sys.exit(1)

    if not args.dry_run:
        os.makedirs(args.output_dir, exist_ok=True)

    for file in list(Path(args.epub_dir).rglob('*.epub')):
        if file.name.endswith('.epub'):
            convert_epub_to_cbz(file.absolute(), args.output_dir, dry_run=args.dry_run)

    if args.dry_run:
        print("[DRY] Dry run complete — no files written, Mokuro not run.")
        sys.exit(0)

    # Run Mokuro to process the CBZ files
    for item in Path(args.output_dir).iterdir():
        if item.is_dir():
            if (item / "_no_ocr").exists():
                print(f"[-] Skipping: {item.name} marked as no OCR")
                continue
            subprocess.run(["mokuro", "-l=False", "--disable-confirmation=True", f"--parent_dir={item.absolute()}"])