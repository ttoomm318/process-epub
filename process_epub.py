import os
import re
import sys
import zipfile
import shutil
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

def convert_epub_to_cbz(epub_path, output_dir):
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
        
        cbz_path = os.path.join(output_dir, metadata['Series'], f"{metadata['Title']}.cbz")
        if os.path.exists(cbz_path):
            print(f"[-] Skipping: {metadata['Title']} (CBZ already exists)")
            return
        os.makedirs(os.path.dirname(cbz_path), exist_ok=True)

        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as cbz:
            # 3. Save metadata into ComicInfo.xml in the CBZ
            ci_root = etree.Element('ComicInfo', nsmap=ci_ns)

            for key in metadata_keys:
                if key in metadata and metadata[key]:
                    etree.SubElement(ci_root, key).text = str(metadata[key])

            etree.SubElement(ci_root, 'Manga').text = 'YesAndRightToLeft'
            comic_info_xml = etree.tostring(ci_root, encoding='utf-8', xml_declaration=True, pretty_print=True).decode('utf-8')
            # print(f"[*] ComicInfo.xml: \n{comic_info_xml}")
            cbz.writestr('ComicInfo.xml', comic_info_xml)

            # 4. Extract images based on the Spine order and add to CBZ
            page_idx = 1
            for itemref in spine_items:
                href = manifest.get(itemref.get('idref'))
                if not href:
                    continue

                full_href_path = os.path.join(opf_dir, href).replace('\\', '/')
                
                # If spine points to HTML, find the image inside it
                if href.endswith(('.html', '.xhtml')):
                    h_root = etree.fromstring(z.read(full_href_path))
                    img_srcs = h_root.xpath('//xhtml:img/@src | //svg:image/@xlink:href | //svg:image/@href', namespaces=svg_ns)
                    for src in img_srcs:
                        img_path = os.path.normpath(os.path.join(os.path.dirname(full_href_path), src)).replace('\\', '/')
                        if store_image(z, img_path, cbz, page_idx):
                            page_idx += 1
                # If spine points directly to an image
                elif href.lower().endswith(('.jpg', '.jpeg', '.png')):
                    if store_image(z, full_href_path, cbz, page_idx):
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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process-epub.py <path_to_epub_folder> [output_directory]")
        sys.exit(1)
    epub_dir = sys.argv[1]
    if (not os.path.isdir(epub_dir)):
        print(f"Error: {epub_dir} is not a valid directory.")
        sys.exit(1)
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'output'
    os.makedirs(output_dir, exist_ok=True)
    for file in list(Path(epub_dir).rglob('*.epub')):
        if file.name.endswith('.epub'):
            convert_epub_to_cbz(file.absolute(), output_dir)
    
    # 5. Run Mokuro to process the CBZ files
    for item in Path(output_dir).iterdir():
        if item.is_dir():
            subprocess.run(["mokuro", "-l=False", "--disable-confirmation=True", f"--parent_dir={item.absolute()}"])