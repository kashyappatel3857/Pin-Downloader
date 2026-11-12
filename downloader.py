import os
import re
import json
import urllib.parse
from typing import Optional, Dict, Any, Tuple, List
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,video/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
}

class PinterestDownloader:
    def __init__(self, output_dir: str = "downloads", max_retries: int = 3):
        self.output_dir = output_dir
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        
        # Configure automatic retries with exponential backoff for network resilience
        retries = Retry(
            total=max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        os.makedirs(self.output_dir, exist_ok=True)

    def resolve_url(self, raw_url: str) -> str:
        """Resolve redirects (such as pin.it short links) to the canonical Pinterest URL."""
        url = raw_url.strip()
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'https://' + url
            
        try:
            resp = self.session.head(url, allow_redirects=True, timeout=10)
            return resp.url
        except Exception:
            try:
                resp = self.session.get(url, allow_redirects=True, timeout=10)
                return resp.url
            except Exception:
                return url

    def is_board_url(self, url: str) -> bool:
        """Detect if the provided URL is a Pinterest user board rather than a single pin."""
        clean = url.strip().split('?')[0].rstrip('/')
        parts = clean.replace('https://', '').replace('http://', '').split('/')
        if len(parts) >= 2 and parts[0].endswith('pinterest.com'):
            # e.g., pinterest.com/username/boardname
            if len(parts) == 3 and parts[1] not in ['pin', 'search', 'ideas', 'today', 'settings', '_']:
                return True
        return False

    def extract_board_pins(self, board_url: str) -> List[str]:
        """Extract individual pin URLs from a Pinterest board page."""
        resolved = self.resolve_url(board_url)
        resp = self.session.get(resolved, timeout=15)
        if resp.status_code != 200:
            return []
        
        pin_ids = re.findall(r'/pin/(\d+)/?', resp.text)
        seen = set()
        pin_urls = []
        for pid in pin_ids:
            if pid not in seen:
                seen.add(pid)
                pin_urls.append(f"https://www.pinterest.com/pin/{pid}/")
        return pin_urls

    def sanitize_filename(self, name: str, max_length: int = 60) -> str:
        """Remove characters not allowed in Windows/Linux filenames."""
        clean = re.sub(r'[\\/*?:"<>|]', '', name)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if len(clean) > max_length:
            clean = clean[:max_length].strip()
        return clean or "pinterest_media"

    def _clean_unescape(self, text: str) -> str:
        """Unescape JSON/HTML escaped forward slashes and unicode characters."""
        return text.replace(r'\/', '/').replace(r'\u002f', '/').replace(r'\u002F', '/')

    def _extract_from_relay(self, soup: BeautifulSoup, target_pin_id: Optional[str]) -> List[Dict[str, Any]]:
        """
        Extract media items from Pinterest's modern Relay GraphQL payloads
        (window.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__).
        """
        media_items = []
        for s in soup.find_all('script'):
            if not s.string or '__PWS_RELAY_REGISTER_COMPLETED_REQUEST__' not in s.string:
                continue
            if target_pin_id and target_pin_id not in s.string:
                continue

            match = re.search(
                r'window\.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__\s*\(\s*".*?"\s*,\s*(\{.*?\})\s*\)\s*;?',
                s.string,
                re.DOTALL
            )
            if not match:
                continue

            try:
                payload = json.loads(match.group(1))
                pin_data = payload.get('data', {}).get('v3GetPinQueryv2', {}).get('data', {})
                if not pin_data:
                    continue

                # 1. Check for Videos
                videos = pin_data.get('videos')
                if videos and isinstance(videos, dict):
                    v_list = videos.get('video_list', {})
                    best_v_url = None
                    best_w = 0
                    for vk, vi in v_list.items():
                        if isinstance(vi, dict) and 'url' in vi:
                            w = vi.get('width', 0)
                            if w >= best_w and vi['url'].endswith('.mp4'):
                                best_w = w
                                best_v_url = vi['url']
                    if best_v_url:
                        media_items.append({
                            'url': best_v_url,
                            'type': 'video',
                            'ext': 'mp4',
                            'quality': f"{best_w}p HD Video" if best_w else "HD MP4"
                        })

                # 2. Check for top-level Original Master Image
                img_orig = pin_data.get('images_orig')
                if img_orig and isinstance(img_orig, dict) and img_orig.get('url'):
                    orig_url = img_orig['url']
                    ext = orig_url.split('?')[0].rsplit('.', 1)[-1].lower()
                    media_items.append({
                        'url': orig_url,
                        'type': 'image',
                        'ext': ext if ext in ['jpg', 'png', 'gif', 'webp', 'jpeg'] else 'jpg',
                        'quality': f'Original Quality ({ext.upper()})'
                    })

                # 3. Check for multi-page Story / Idea Pin / Carousel (when pages > 1)
                spd = pin_data.get('storyPinData')
                if spd and isinstance(spd, dict):
                    pages = spd.get('pages', [])
                    if len(pages) > 1:
                        for p_i, page in enumerate(pages):
                            for blk in page.get('blocks', []):
                                # Block original image
                                img_block = blk.get('images_orig')
                                if img_block and isinstance(img_block, dict) and img_block.get('url'):
                                    orig_u = img_block['url']
                                    ext = orig_u.split('?')[0].rsplit('.', 1)[-1].lower()
                                    media_items.append({
                                        'url': orig_u,
                                        'type': 'image',
                                        'ext': ext if ext in ['jpg', 'png', 'gif', 'webp', 'jpeg'] else 'jpg',
                                        'quality': f'Slide {p_i+1} Original'
                                    })
                                elif blk.get('images_750x') and blk['images_750x'].get('url'):
                                    u = blk['images_750x']['url']
                                    promoted = re.sub(r'/(?:\d+x)/', '/originals/', u)
                                    ext = promoted.split('?')[0].rsplit('.', 1)[-1].lower()
                                    media_items.append({
                                        'url': promoted,
                                        'type': 'image',
                                        'ext': ext if ext in ['jpg', 'png', 'gif', 'webp', 'jpeg'] else 'jpg',
                                        'quality': f'Slide {p_i+1} Original'
                                    })
                                if blk.get('video') and isinstance(blk['video'], dict):
                                    bv_list = blk['video'].get('video_list', {})
                                    for q_key in ['V_1080P', 'V_720P', 'V_EXP7', 'V_EXP6']:
                                        if q_key in bv_list and 'url' in bv_list[q_key]:
                                            media_items.append({
                                                'url': bv_list[q_key]['url'],
                                                'type': 'video',
                                                'ext': 'mp4',
                                                'quality': f'Slide {p_i+1} Video'
                                            })
                                            break

            except Exception:
                continue

        # Deduplicate found media items by image/video hash and URL while preserving order
        deduped = []
        seen_keys = set()
        for m in media_items:
            m_url = m['url']
            # Extract file hash (e.g. 'a2b9bc70822090347bb97e3c8333c22a')
            base_file = m_url.split('?')[0].rsplit('/', 1)[-1]
            key = base_file.rsplit('.', 1)[0] or m_url
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(m)

        return deduped

    def extract_pin_media(self, url: str) -> List[Dict[str, Any]]:
        """
        Extract all highest quality media items (images or videos) for the given pin.
        Returns a list of media item dicts ready for downloading.
        """
        resolved_url = self.resolve_url(url)
        
        # Extract pin id from URL if present
        pin_id_match = re.search(r'/pin/(\d+)', resolved_url)
        pin_id = pin_id_match.group(1) if pin_id_match else None
        
        # Prefer canonical pin URL without tracking query parameters
        canonical_url = f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else resolved_url
        
        resp = self.session.get(canonical_url, timeout=15)
        if resp.status_code == 404:
            raise ValueError("Pin not found (HTTP 404)")
        elif resp.status_code != 200:
            raise ValueError(f"Failed to fetch page, HTTP status: {resp.status_code}")
            
        html_text = resp.text
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # Metadata parsing (Title & Description)
        pin_title = None
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            pin_title = og_title['content'].strip()
            
        pin_desc = None
        og_desc = soup.find('meta', property='og:description')
        if og_desc and og_desc.get('content'):
            pin_desc = og_desc['content'].strip()

        # Strategy 1: Targeted Relay GraphQL data extraction
        raw_media_items = self._extract_from_relay(soup, pin_id)

        # Strategy 2: Target Hash matching with og:image
        clean_text = self._clean_unescape(html_text)
        if not raw_media_items:
            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                og_img_url = og_img['content']
                # Extract image path hash (e.g., 'a2b9bc70822090347bb97e3c8333c22a')
                base_name = og_img_url.split('?')[0].rsplit('/', 1)[-1]
                img_hash = base_name.rsplit('.', 1)[0]
                
                # Look for an /originals/ image in clean HTML containing this exact hash
                pattern = rf'https://i\.pinimg\.com/originals/[^\s"\'<>]*{re.escape(img_hash)}[^\s"\'<>]*(?:\.jpg|\.png|\.gif|\.webp|\.jpeg)'
                matched_orig = re.findall(pattern, clean_text, re.IGNORECASE)
                if matched_orig:
                    best_url = matched_orig[0]
                    ext = best_url.split('?')[0].rsplit('.', 1)[-1].lower()
                    raw_media_items.append({
                        'url': best_url,
                        'type': 'image',
                        'ext': ext if ext in ['jpg', 'png', 'gif', 'webp', 'jpeg'] else 'jpg',
                        'quality': f'Original Quality ({ext.upper()})'
                    })
                else:
                    # Upgrade og:image URL to /originals/
                    upgraded = re.sub(r'/(?:\d+x)/', '/originals/', og_img_url)
                    ext = upgraded.split('?')[0].rsplit('.', 1)[-1].lower()
                    raw_media_items.append({
                        'url': upgraded,
                        'type': 'image',
                        'ext': ext if ext in ['jpg', 'png', 'gif', 'webp', 'jpeg'] else 'jpg',
                        'quality': 'Original Master Image'
                    })

        # Strategy 3: Video Fallback
        if not raw_media_items:
            og_video = soup.find('meta', property='og:video') or soup.find('meta', property='og:video:secure_url')
            if og_video and og_video.get('content') and og_video['content'].endswith('.mp4'):
                raw_media_items.append({
                    'url': og_video['content'],
                    'type': 'video',
                    'ext': 'mp4',
                    'quality': 'MP4 Video'
                })
            else:
                video_urls = re.findall(r'https://v\.pinimg\.com/videos/[^\s"\'<>]+\.mp4', clean_text)
                if video_urls:
                    def video_sort_key(u: str) -> int:
                        if '1080p' in u: return 1080
                        if '720p' in u: return 720
                        if '540p' in u: return 540
                        if '480p' in u: return 480
                        return 0
                    sorted_v = sorted(set(video_urls), key=video_sort_key, reverse=True)
                    raw_media_items.append({
                        'url': sorted_v[0],
                        'type': 'video',
                        'ext': 'mp4',
                        'quality': 'High Quality MP4'
                    })

        if not raw_media_items:
            raise ValueError("Could not extract any high-quality image or video media from this pin.")

        # Build finalized info objects for all media items
        total_items = len(raw_media_items)
        clean_title = self.sanitize_filename(pin_title or pin_desc or "", max_length=50)
        display_id = pin_id or "pin"
        
        finalized_list = []
        for idx, item in enumerate(raw_media_items, 1):
            ext = item['ext']
            if total_items > 1:
                # Multi-item / Carousel numbering
                filename = f"{display_id}_{idx}_{clean_title}.{ext}" if clean_title else f"{display_id}_{idx}.{ext}"
            else:
                filename = f"{display_id}_{clean_title}.{ext}" if clean_title else f"{display_id}.{ext}"
                
            filepath = os.path.join(self.output_dir, filename)
            
            info = {
                'pin_id': display_id,
                'source_url': resolved_url,
                'title': pin_title,
                'description': pin_desc,
                'media_type': item['type'],
                'download_url': item['url'],
                'extension': ext,
                'quality_info': item['quality'],
                'filename': filename,
                'filepath': filepath,
                'item_index': idx,
                'total_items': total_items
            }
            finalized_list.append(info)

        return finalized_list

    def extract_pin_info(self, url: str) -> Dict[str, Any]:
        """
        Extract primary media info for a pin.
        Provides backward compatibility with single-media callers while attaching 'all_media'.
        """
        all_media = self.extract_pin_media(url)
        primary = all_media[0].copy()
        primary['all_media'] = all_media
        return primary

    def download_media(self, info: Dict[str, Any], progress_callback=None) -> Tuple[bool, str]:
        """
        Download media with chunked streaming and atomic write.
        Checks if file already exists with same size to avoid re-downloading.
        progress_callback(downloaded_bytes, total_bytes) can be provided.
        Returns: (success: bool, message: str)
        """
        url = info.get('download_url')
        filepath = info.get('filepath')
        
        if not url or not filepath:
            return False, "Missing download URL or destination filepath"

        headers = {
            'User-Agent': DEFAULT_HEADERS['User-Agent'],
            'Referer': 'https://www.pinterest.com/',
        }

        try:
            with self.session.get(url, headers=headers, stream=True, timeout=30) as resp:
                if resp.status_code != 200:
                    return False, f"Failed to download media, HTTP {resp.status_code}"

                total_size = int(resp.headers.get('content-length', 0))

                # Check if file already exists with same size
                if os.path.exists(filepath) and total_size > 0:
                    if os.path.getsize(filepath) == total_size:
                        return True, f"Already downloaded: {os.path.basename(filepath)}"

                downloaded = 0
                temp_filepath = filepath + ".part"
                with open(temp_filepath, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(downloaded, total_size)

                # Move temporary file to final path once fully downloaded
                if os.path.exists(filepath):
                    os.remove(filepath)
                os.rename(temp_filepath, filepath)

            return True, f"Saved to {os.path.basename(filepath)}"
        except Exception as e:
            if os.path.exists(filepath + ".part"):
                try:
                    os.remove(filepath + ".part")
                except Exception:
                    pass
            return False, str(e)
