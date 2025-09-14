import requests
import os
import json
from bs4 import BeautifulSoup
from tqdm import tqdm
import re
import time
import html
import html2text
import sys

# Ensure proper UTF-8 encoding for Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

BASE_URL = "https://thomasmore.instructure.com"
COOKIES_FILE = "canvas_cookies.json"
DOWNLOAD_DIR = "CanvasDownloads"
WAIT_BETWEEN_REQUESTS = 0.5

def safe_print(text):
    """Print text safely, handling Unicode characters that might cause encoding issues."""
    try:
        print(text)
    except UnicodeEncodeError:
        # Fallback: encode to ASCII and ignore problematic characters
        safe_text = text.encode('ascii', errors='ignore').decode('ascii')
        print(f"{safe_text} [Unicode characters removed]")

def load_cookies():
    with open(COOKIES_FILE, "r") as f:
        cookies_json = json.load(f)
    if isinstance(cookies_json, dict):
        return cookies_json
    return {cookie["name"]: cookie["value"] for cookie in cookies_json}

def get_dashboard_html(session):
    url = f"{BASE_URL}/courses"
    resp = session.get(url)
    resp.raise_for_status()
    return resp.text

def parse_courses(html):
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    for row in soup.select('tr.course-list-table-row'):
        name_el = row.select_one('.course-list-course-title-column .name')
        id_el = row.select_one('.course-list-star-column [data-course-id]')
        if name_el and id_el:
            course_name = name_el.text.strip()
            course_id = id_el['data-course-id']
            courses.append({'name': course_name, 'id': course_id})
    return courses

def get_modules_html(session, course_id):
    url = f"{BASE_URL}/courses/{course_id}/modules"
    resp = session.get(url)
    resp.raise_for_status()
    return resp.text

def parse_modules_and_items(html, course_id):
    soup = BeautifulSoup(html, "html.parser")
    modules = []
    for module_div in soup.select("div.item-group-condensed.context_module"):
        module_name_el = module_div.select_one("span.name")
        module_name = module_name_el.text.strip() if module_name_el else "UnknownModule"
        module_name = re.sub(r'[\\/*?:"<>|]', "", module_name)
        items = []
        for li in module_div.select("li.context_module_item"):
            link = li.select_one("a.item_link")
            if not link: continue
            item_title = link.text.strip()
            item_href = link.get("href")
            item_url = BASE_URL + item_href
            items.append({'title': item_title, 'url': item_url})
        modules.append({'name': module_name, 'items': items})
    return modules

def get_module_item_page(session, item_url):
    resp = session.get(item_url, allow_redirects=True)
    resp.raise_for_status()
    return resp.url, resp.text

def parse_file_download_link(file_page_html):
    soup = BeautifulSoup(file_page_html, "html.parser")
    
    # First, try to find the traditional download link
    a = soup.find('a', attrs={'download': 'true'})
    if a and '/download?download_frd=1' in a['href']:
        file_url = a['href']
        file_name = a.text.strip()
        if file_url.startswith("/"):
            file_url = BASE_URL + file_url
        if file_name.lower().startswith("download "):
            file_name = file_name[8:]
        file_name = file_name.strip()
        return file_name, file_url
    
    # Also look for other Canvas file patterns
    # Look for file links in the page content
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.text.strip()
        
        # Skip empty or navigation links
        if not href or href.startswith('#') or href.startswith('mailto:'):
            continue
            
        # Make URL absolute if relative
        if href.startswith("/"):
            full_url = BASE_URL + href
        else:
            full_url = href
        
        # Check for Canvas file patterns or downloadable extensions
        if is_downloadable_file(full_url, text):
            filename = get_filename_from_url_or_text(full_url, text)
            return filename, full_url
    
    return None, None

def resolve_canvas_file_url(session, url):
    """Resolve Canvas file URLs to get the actual download URL with proper authentication."""
    try:
        # For Canvas file links, we need to follow redirects and look for the actual download link
        response = session.get(url, allow_redirects=True)
        response.raise_for_status()
        
        # Check if we got redirected to a file download URL
        final_url = response.url
        if '/download?download_frd=1' in final_url:
            return final_url
        
        # If we're on a Canvas file page, look for the download link in the HTML
        if 'instructure.com' in final_url and ('/files/' in final_url or '/courses/' in final_url):
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Look for download link
            download_link = soup.find('a', attrs={'download': 'true'})
            if download_link and download_link.get('href'):
                download_url = download_link['href']
                if download_url.startswith('/'):
                    download_url = BASE_URL + download_url
                return download_url
            
            # Look for any link with download_frd=1
            for a in soup.find_all('a', href=True):
                if '/download?download_frd=1' in a['href']:
                    download_url = a['href']
                    if download_url.startswith('/'):
                        download_url = BASE_URL + download_url
                    return download_url
        
        # If it's not a Canvas file or we can't find a download link, return original URL
        return url
        
    except Exception as e:
        print(f"          Warning: Could not resolve Canvas file URL {url}: {e}")
        return url

def validate_file_content(file_path, expected_extension):
    """Check if downloaded file content matches expected file type."""
    try:
        with open(file_path, 'rb') as f:
            # Read first few bytes to check file signature
            header = f.read(1024)
            
        # Check for common file signatures
        if expected_extension.lower() == '.pdf':
            if not header.startswith(b'%PDF-'):
                # Check if it's HTML (indicating an error page)
                if b'<html' in header.lower() or b'<!doctype' in header.lower():
                    return False, "Downloaded HTML instead of PDF"
                return False, "Not a valid PDF file"
        elif expected_extension.lower() in ['.zip', '.rar', '.7z']:
            zip_signatures = [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']
            if not any(header.startswith(sig) for sig in zip_signatures):
                if b'<html' in header.lower():
                    return False, "Downloaded HTML instead of archive"
                return False, "Not a valid archive file"
        elif expected_extension.lower() in ['.jpg', '.jpeg']:
            if not header.startswith(b'\xff\xd8\xff'):
                if b'<html' in header.lower():
                    return False, "Downloaded HTML instead of image"
                return False, "Not a valid JPEG file"
        elif expected_extension.lower() == '.png':
            if not header.startswith(b'\x89PNG\r\n\x1a\n'):
                if b'<html' in header.lower():
                    return False, "Downloaded HTML instead of image"
                return False, "Not a valid PNG file"
        
        # For other file types, just check if it's not HTML
        if b'<html' in header.lower() or b'<!doctype' in header.lower():
            return False, "Downloaded HTML instead of expected file type"
            
        return True, "File appears valid"
        
    except Exception as e:
        return False, f"Could not validate file: {e}"

def download_file(session, url, save_path):
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    if os.path.exists(save_path):
        print(f"Already downloaded: {os.path.basename(save_path)}")
        return
    
    # Resolve Canvas file URLs to get proper download links
    print(f"          Resolving URL: {url}")
    resolved_url = resolve_canvas_file_url(session, url)
    print(f"          Resolved to: {resolved_url}")
    
    try:
        with session.get(resolved_url, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            with open(save_path, 'wb') as f, tqdm(
                desc=os.path.basename(save_path), total=total, unit='B', unit_scale=True
            ) as bar:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    bar.update(len(chunk))
        
        # Validate the downloaded file
        file_extension = os.path.splitext(save_path)[1]
        is_valid, message = validate_file_content(save_path, file_extension)
        
        if is_valid:
            print(f"Downloaded: {os.path.basename(save_path)}")
        else:
            print(f"Downloaded: {os.path.basename(save_path)} - Warning: {message}")
            # Optionally remove the invalid file
            # os.remove(save_path)
            
    except Exception as e:
        print(f"Error downloading {os.path.basename(save_path)}: {e}")

def is_downloadable_file(url, text):
    """Check if a URL likely points to a downloadable file based on patterns and extensions."""
    # Common file extensions that should be downloaded
    downloadable_extensions = {
        '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
        '.zip', '.rar', '.7z', '.tar', '.gz',
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg',
        '.mp3', '.mp4', '.avi', '.mov', '.wav',
        '.txt', '.csv', '.json', '.xml', '.html', '.css', '.js',
        '.py', '.java', '.cpp', '.c', '.h',
        '.sql', '.db', '.sqlite', '.rtf', '.odt', '.ods', '.odp'
    }
    
    # Check if URL has a downloadable file extension
    url_lower = url.lower()
    for ext in downloadable_extensions:
        if url_lower.endswith(ext) or f'{ext}?' in url_lower or f'{ext}#' in url_lower:
            return True
    
    # Check for Canvas file patterns
    canvas_file_patterns = [
        r'/courses/\d+/files/\d+',
        r'/files/\d+',
        r'/download\?download_frd=1',
        r'/courses/\d+/file_contents/',
        r'/users/\d+/files/\d+',
        r'instructure\.com.*files',
    ]
    
    for pattern in canvas_file_patterns:
        if re.search(pattern, url):
            return True
    
    # Check if link text suggests it's a file
    file_indicators = ['download', 'attachment', 'file', '.pdf', '.doc', '.ppt', '.xls', 
                      'handout', 'worksheet', 'assignment', 'syllabus', 'slides']
    text_lower = text.lower()
    for indicator in file_indicators:
        if indicator in text_lower:
            return True
    
    # Skip obvious non-file links
    non_file_indicators = ['http://www.', 'https://www.', 'wiki', 'page', 'module', 
                          'discussion', 'assignment submission', 'grade', 'course']
    for indicator in non_file_indicators:
        if indicator in text_lower and not any(ext in url_lower for ext in downloadable_extensions):
            return False
    
    return False

def get_filename_from_url_or_text(url, text):
    """Extract filename from URL or fallback to link text."""
    # Try to get filename from URL
    if '?' in url:
        url_path = url.split('?')[0]
    else:
        url_path = url
    
    filename = os.path.basename(url_path)
    
    # If no filename in URL or it's just numbers, use text
    if not filename or filename.isdigit() or not '.' in filename:
        filename = text.strip()
        # Clean up common prefixes
        if filename.lower().startswith("download "):
            filename = filename[9:]
        # If text doesn't have extension, try to guess from URL
        if not '.' in filename and '.' in url_path:
            url_parts = url_path.split('.')
            if len(url_parts) > 1:
                ext = '.' + url_parts[-1]
                if len(ext) <= 5:  # reasonable extension length
                    filename += ext
    
    # Clean filename for filesystem
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    return filename if filename else "downloaded_file"

def parse_canvas_page_content_and_downloads(page_html, download_dir, session):
    # Look for WIKI_PAGE body in JS
    body_match = re.search(r'"body":"((?:[^"\\]|\\.)*)"', page_html)
    if not body_match:
        print("          Warning: Could not find WIKI_PAGE body in page HTML.")
        return None

    body_html = body_match.group(1)
    body_html = body_html.encode('utf-8').decode('unicode_escape')
    body_html = html.unescape(body_html)
    soup = BeautifulSoup(body_html, "html.parser")

    # Find all potentially downloadable links in this content
    download_links = []
    all_links_count = 0
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.text.strip()
        all_links_count += 1
        
        # Skip empty links, mailto, and fragment links
        if not href or href.startswith('mailto:') or href.startswith('#'):
            continue
        
        # Make URL absolute if it's relative
        if href.startswith("/"):
            full_url = BASE_URL + href
        else:
            full_url = href
        
        # Check if this looks like a downloadable file
        if is_downloadable_file(full_url, text):
            filename = get_filename_from_url_or_text(full_url, text)
            print(f"          Found downloadable link: {text[:50]}... -> {full_url}")
            download_links.append({
                'name': filename, 
                'url': full_url, 
                'a_tag': a,
                'original_href': href
            })
    
    print(f"          Found {len(download_links)} downloadable links out of {all_links_count} total links")

    # Download each file and update the link in the HTML to local file
    for link in download_links:
        local_path = os.path.join(download_dir, link['name'])
        try:
            print(f"          Downloading linked file: {link['name']}")
            download_file(session, link['url'], local_path)
            # Update the link to point to the local file
            link['a_tag']['href'] = link['name']
        except Exception as e:
            print(f"          Error downloading linked file {link['name']}: {e}")
            # Keep original link if download fails
            continue

    # Convert HTML to markdown
    html_content = str(soup)
    markdown_content = html2text.html2text(html_content)
    return markdown_content

def main():
    print("Loading cookies...")
    cookies = load_cookies()
    session = requests.Session()
    session.cookies.update(cookies)

    print("Fetching dashboard HTML...")
    html = get_dashboard_html(session)
    courses = parse_courses(html)
    print(f"Found {len(courses)} courses.")

    for course in courses:
        course_name = re.sub(r'[\\/*?:"<>|]', "", course['name'])
        print(f"\nProcessing course: {course_name}")
        try:
            modules_html = get_modules_html(session, course['id'])
        except requests.HTTPError as e:
            print(f"  Failed to fetch modules page: {e}")
            continue
        modules = parse_modules_and_items(modules_html, course['id'])
        print(f"  Found {len(modules)} modules.")
        for module in modules:
            safe_print(f"    Processing module: {module['name']}")
            for item in module['items']:
                safe_print(f"      Processing module item: {item['title']}")
                try:
                    redirected_url, page_html = get_module_item_page(session, item['url'])
                except requests.HTTPError as e:
                    print(f"        Failed to fetch module item page: {e}")
                    continue
                # First: download if it's a file item
                file_name, download_url = parse_file_download_link(page_html)
                if file_name and download_url:
                    fname = re.sub(r'[\\/*?:"<>|]', "", file_name)
                    if not fname:
                        fname = "downloaded_file"
                    save_path = os.path.join(DOWNLOAD_DIR, course_name, module['name'], fname)
                    try:
                        download_file(session, download_url, save_path)
                    except Exception as e:
                        print(f"          Error downloading {fname}: {e}")
                    time.sleep(WAIT_BETWEEN_REQUESTS)
                # If it's a Canvas page, save as markdown and download linked files
                elif "/pages/" in redirected_url:
                    page_dir = os.path.join(DOWNLOAD_DIR, course_name, module['name'])
                    markdown = parse_canvas_page_content_and_downloads(page_html, page_dir, session)
                    if markdown:
                        fname = re.sub(r'[\\/*?:"<>|]', "", item['title']) + ".md"
                        save_path = os.path.join(page_dir, fname)
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)  # Ensure directory exists
                        with open(save_path, "w", encoding="utf-8") as f:
                            f.write(markdown)
                        safe_print(f"        Canvas page saved as markdown: {fname}")
                    else:
                        print(f"        Canvas page not found or no content: {redirected_url}")
                else:
                    print(f"        Skipped (not a file or page): {redirected_url}")

if __name__ == "__main__":
    main()