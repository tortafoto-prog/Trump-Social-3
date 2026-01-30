#!/usr/bin/env python3
"""
Trump Social Media Scraper (Roll Call Factbase) with Hungarian LLM Translation
Scrapes Donald Trump's posts from Roll Call Factbase, translates to Hungarian, and posts to Discord.
"""

import os
import sys
import time
import re
from pathlib import Path
from typing import List, Dict, Any

from anthropic import Anthropic
from discord_webhook import DiscordWebhook, DiscordEmbed
from playwright.sync_api import sync_playwright


def log(message: str):
    """Print with flush for immediate output in Docker"""
    print(message, flush=True)


# Configuration from environment variables
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
DATA_DIR = os.getenv("DATA_DIR", "/data")
FORCE_REPROCESS = os.getenv("FORCE_REPROCESS", "false").lower() == "true"

# Target configuration
ROLLCALL_URL = "https://rollcall.com/factbase/trump/topic/social/?platform=all&sort=date&sort_order=desc&page=1"

# Translation system prompt
TRANSLATION_SYSTEM_PROMPT = """Te egy professzionális fordító vagy, aki gyönyörű, természetes magyarsággal dolgozik.

Feladatod: Fordítsd le ezt a közösségi média bejegyzést angolról magyarra!

FORDÍTÁSI ELVEK:
- Használj természetes, gördülékeny magyar nyelvezetet.
- Tartsd meg az eredeti hangnemet.
- **Speciális Bemeneti Címkék Kezelése:**
    - "[ReTruthed from @XYZ]": Kezdd így: "Donald Trump megosztotta @XYZ bejegyzését:"
    - "[SHARED_CONTENT]": Ez a megosztott bejegyzés szövege. Fordítsd le és illeszd be a fenti bevezető után.
    - "[LINK_PREVIEW]": Ez egy külső link/cikk/X-poszt tartalma. Kezdd így: "Donald Trump megosztott egy X/TRUTH bejegyzést, ami a következőt tartalmazza:", majd fordítsd le a tartalmat.
- NE fordítsd le: URL-eket, hashtag-eket (#), említéseket (@)
- VÁLASZ: Csak a kész, formázott magyar szöveget add vissza."""


class HybridScraper:
    """Handles hybrid scraping: Detection via Roll Call, Details via Truth Social"""

    def __init__(self, headless: bool = True):
        self.headless = headless

    def monitor_feed(self) -> List[Dict[str, Any]]:
        """Stage 1: Detect new posts via Roll Call (Safe, Low-Blocking)"""
        posts = []
        
        # Set a hard timeout for the scraping operation (Linux/Railway only)
        # This prevents the script from hanging indefinitely if the browser stucks
        import signal
        if hasattr(signal, "alarm"):
            def handler(signum, frame):
                raise TimeoutError("Scraping timed out (Hard Limit)")
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(180) # 3 minutes hard limit (generous for start)

        try:
            with sync_playwright() as p:
                log("⏳ Opening headless browser to scrape Roll Call...")
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                log("✓ Browser launched successfully")

                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                log("✓ Page created, navigating to Roll Call...")

                try:
                    # Add cache buster to URL
                    cache_buster = int(time.time())
                    final_url = f"{ROLLCALL_URL}&t={cache_buster}"
                    
                    # Use domcontentloaded instead of networkidle (faster, more reliable)
                    page.goto(final_url, wait_until="domcontentloaded", timeout=90000)
                    log("✓ DOM loaded, waiting for posts to render...")

                    # Wait for the actual post content to appear
                    page.wait_for_selector("div.rounded-xl.border", timeout=60000)
                    log("✓ Post cards found, waiting for content to fully load...")

                    # Wait a bit more for Alpine.js to render content
                    time.sleep(5)
                    
                    log("⏳ Extracting data from page...")
                    extracted_data = page.evaluate("""() => {
                        const posts = [];
                        const cards = document.querySelectorAll('div.rounded-xl.border');

                        cards.forEach(card => {
                            // Only process cards that have a Truth Social link
                            const truthLinkEl = Array.from(card.querySelectorAll('a')).find(a =>
                                a.innerText.includes('View on Truth Social') && a.href.includes('truthsocial.com')
                            );

                            if (!truthLinkEl) return; // Skip non-post cards

                            const url = truthLinkEl.href;
                            const contentEl = card.querySelector('div.text-sm.font-medium.whitespace-pre-wrap');
                            const content = contentEl ? contentEl.innerText.trim() : "";

                            const timeEl = Array.from(card.querySelectorAll('div')).find(div =>
                                div.innerText.includes('@') && div.innerText.includes('ET')
                            );
                            const timestamp_str = timeEl ? timeEl.innerText.trim() : "";

                            // Extract ID from URL
                            const matches = url.match(/posts\\/(\\d+)/);
                            const id = matches ? matches[1] : "";

                            // Extract Media (Images for ReTruths/Posts)
                            const imgs = Array.from(card.querySelectorAll('img'));
                            const mediaUrls = imgs
                                .filter(img => {
                                    // Filter out usually small avatars or icons.
                                    // Assuming content images are larger.
                                    return img.naturalWidth > 150 || img.naturalHeight > 150;
                                })
                                .map(img => img.src);

                            if (id && (content || url)) {
                                posts.push({
                                    id: id,
                                    url: url,
                                    content: content,
                                    timestamp_str: timestamp_str,
                                    media_urls: mediaUrls,
                                    created_at: new Date().toISOString()
                                });
                            }
                        });
                        return posts;
                    }""")

                    posts = extracted_data
                    log(f"✓ Found {len(posts)} posts on Roll Call")

                except Exception as e:
                    log(f"✗ Error during scraping: {e}")
                finally:
                    try:
                        browser.close()
                        log("✓ Browser closed")
                    except Exception as e:
                        log(f"⚠ Warning: Could not close browser cleanly: {e}")

        except Exception as e:
            log(f"✗ Playwright/Timeout error: {e}")

        # Cancel alarm
        if hasattr(signal, "alarm"):
            signal.alarm(0)
            
        return posts

    def scrape_details(self, url: str) -> Dict[str, Any]:
        """Stage 2: Deep Scrape from Truth Social Direct Link (Public Access)"""
        details = {
            "is_retruth": False,
            "retruth_header": "", # "ReTruthed from @target"
            "full_text": "",
            "media_urls": [],
            "video_url": None,
            "external_link": None
        }
        
        # Set a shorter timeout for Stage 2
        import signal
        if hasattr(signal, "alarm"):
            signal.alarm(90) # 90s for single detail scrape

        try:
            with sync_playwright() as p:
                log(f"⏳ [Stage 2] Deep scraping: {url}")
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                
                # Standard User Agent to look like a real browser
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                try:
                    # Navigate to Truth Social
                    # Note: Without cookies, we rely on the page being public.
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    
                    # Wait for main content
                    try:
                        page.wait_for_selector("div.status__content", timeout=15000)
                        log("✓ [Stage 2] Truth Social page loaded")
                        
                        # Extract Data
                        evaluated = page.evaluate("""() => {
                            const res = {
                                is_retruth: false,
                                retruth_header: "",
                                full_text: "",
                                media_urls: [],
                                video_url: null
                            };
                            
                            // 1. Check ReTruth Header ("ReTruthed by...")
                            const headerEl = document.querySelector('.status__header');
                            if (headerEl && headerEl.innerText.includes('ReTruthed')) {
                                res.is_retruth = true;
                                res.retruth_header = headerEl.innerText.trim();
                            }

                            // 2. Get Full Text
                            const contentEl = document.querySelector('.status__content');
                            if (contentEl) {
                                res.full_text = contentEl.innerText.trim();
                            }

                            // 3. Link Previews / Cards (CRITICAL for X posts and Articles)
                            // Truth Social wrappers external links in a .status-card info block
                            const cardEl = document.querySelector('a.status-card');
                            if (cardEl) {
                                const title = cardEl.querySelector('strong.status-card__title')?.innerText.trim();
                                const desc = cardEl.querySelector('.status-card__description')?.innerText.trim();
                                if (title || desc) {
                                    res.card_content = [title, desc].filter(Boolean).join("\\n");
                                }
                            }

                            // 4. Media Extraction (High Res)
                            // Images
                            const mediaDiv = document.querySelector('.status__media');
                            if (mediaDiv) {
                                const imgs = Array.from(mediaDiv.querySelectorAll('img'));
                                res.media_urls = imgs.map(img => img.src);
                                
                                // Videos (status__video usually contains video or iframe)
                                const videoEl = mediaDiv.querySelector('video');
                                if (videoEl) {
                                    res.video_url = videoEl.src;
                                }
                            }
                            
                            return res;
                        }""")
                        
                        details.update(evaluated)
                        log(f"  -> Extracted: ReTruth={details['is_retruth']}, Card={bool(details.get('card_content'))}, Media={len(details['media_urls'])}")

                    except Exception as e:
                        log(f"⚠ [Stage 2] Could not find post content (possibly blocked or layout changed): {e}")

                except Exception as e:
                    log(f"✗ [Stage 2] Navigation error: {e}")
                
        except Exception as e:
             log(f"✗ [Stage 2] Browser resource error: {e}")

        # Cancel alarm
        if hasattr(signal, "alarm"):
            signal.alarm(0)

        # Ensure browser is closed
        if 'browser' in locals():
            try:
                browser.close()
            except Exception as e:
                log(f"⚠ [Stage 2] Warning: Could not close browser cleanly: {e}")

        return details


class Translator:
    """Handles translation using Anthropic Claude API"""

    def __init__(self, api_key: str, model: str):
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def clean_text(self, text: str) -> str:
        """Basic text cleanup"""
        if not text:
            return ""
        return text.strip()

    def extract_urls(self, text: str) -> List[str]:
        """Extract URLs from text to preserve them"""
        url_pattern = r'https?://[^\s]+'
        return re.findall(url_pattern, text)

    def has_translatable_content(self, text: str) -> bool:
        """Check if text has content worth translating (not just URLs/links)"""
        if not text:
            return False
        # Remove URLs from text
        text_without_urls = re.sub(r'https?://[^\s]+', '', text).strip()
        # Check if there's meaningful text left (at least 10 chars)
        return len(text_without_urls) >= 10

    def translate_to_hungarian(self, text: str) -> str:
        """Translate text to Hungarian while preserving URLs, hashtags, and mentions"""
        text = self.clean_text(text)

        if not text or not text.strip():
            return ""

        # Skip translation if text is just URLs/links
        if not self.has_translatable_content(text):
            log("⏭ Skipping translation: text is only URLs/links")
            return ""

        try:
            original_urls = self.extract_urls(text)

            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=TRANSLATION_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": text}
                ],
                temperature=0.3
            )

            translated = response.content[0].text.strip()

            translated_urls = self.extract_urls(translated)
            if set(original_urls) != set(translated_urls):
                log("⚠ Warning: URL mismatch in translation.")

            log(f"✓ Translated text ({len(text)} -> {len(translated)} chars)")
            return translated

        except Exception as e:
            log(f"✗ Translation error: {e}")
            return text


class DiscordPoster:
    """Handles posting to Discord via webhook"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def post_to_discord(self, post_data: Dict[str, Any], translated_text: str, original_text: str = ""):
        """Post translated content to Discord with both original and translated text"""
        try:
            webhook = DiscordWebhook(url=self.webhook_url)

            embed = DiscordEmbed()
            # ReTruth Context (Header)
            retruth_header = post_data.get("retruth_header")
            if retruth_header:
                 embed.set_author(name=f"🔁 {retruth_header}", icon_url="https://truthsocial.com/favicon.ico")

            # Description (Body)
            description_parts = []
            
            # Truncate if too long
            if original_text and len(original_text) > 1800:
                original_text = original_text[:1800] + "... [tovább az eredeti linken]"
            
            # Formatting for ReTruths
            # If we have a translation, the Narrative Prompt handles the introduction ("Trump shared...").
            # We only add the explicit header if we are falling back to original English text.
            if post_data.get("is_retruth") and not translated_text:
                 description_parts.append(f"**{retruth_header}**")
                 description_parts.append("---")

            if translated_text:
                # If we have a translation, show ONLY the translation (User Request)
                 description_parts.append(translated_text)
            elif original_text:
                # Fallback
                 description_parts.append(original_text)

            full_description = "\n".join(description_parts)
            if len(full_description) > 4096:
                 full_description = full_description[:4093] + "..."

            if description_parts:
                embed.set_description(full_description)

            # Spacer (Visual separation)
            embed.add_embed_field(name="\u200b", value="\u200b", inline=False)

            # Add Image if available
            media_urls = post_data.get("media_urls", [])
            if media_urls:
                embed.set_image(url=media_urls[0])

            # Add Video Link if available (Discord plays mp4/m3u8 often if linked directly, or just as a link)
            video_url = post_data.get("video_url")
            if video_url:
                 embed.add_embed_field(
                    name="🎬 Videó",
                    value=f"[Lejátszás/Megtekintés]({video_url})",
                    inline=False
                )

            # Add extra space before the link

            # Add extra space before the link
            post_url = post_data.get("url", "")
            if post_url:
                 embed.add_embed_field(
                    name="🔗 Eredeti bejegyzés",
                    value=f"[Link a Truth Social-hoz]({post_url})",
                    inline=False
                )

            # Spacer before footer
            embed.add_embed_field(name="\u200b", value="\u200b", inline=False)

            # Footer with original timestamp
            timestamp_str = post_data.get("timestamp_str", "")
            
            # Clean up timestamp: Extract date/time pattern "Month DD, YYYY @ HH:MM AM/PM ET"
            clean_time = timestamp_str
            if timestamp_str:
                import re
                match = re.search(r"([A-Za-z]+ \d{1,2}, \d{4} @ \d{1,2}:\d{2} [AP]M ET)", timestamp_str)
                if match:
                    clean_time = match.group(1)
            
            if clean_time:
                # Discord footer supports newlines
                embed.set_footer(text=f"🤖 Generated by TotM AI\nposted on Truth: {clean_time}")
            else:
                # Fallback to current time
                from datetime import datetime
                import pytz
                budapest_tz = pytz.timezone('Europe/Budapest')
                budapest_time = datetime.now(budapest_tz).strftime("%Y.%m.%d. %H:%M")
                embed.set_footer(text=f"🤖 Generated by TotM AI\nposted on Truth: {budapest_time} (Gen)")

            embed.set_color(color=0x1DA1F2)

            webhook.add_embed(embed)
            response = webhook.execute()

            if response.status_code in [200, 204]:
                log("✓ Posted to Discord successfully")
            else:
                log(f"✗ Discord post failed with status {response.status_code}")

        except Exception as e:
            log(f"✗ Error posting to Discord: {e}")


class StateManager:
    """Manages persistent state for tracking processed posts"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "last_id.txt"

    def load_last_id(self) -> str:
        """Load the last processed post ID"""
        try:
            if self.state_file.exists():
                last_id = self.state_file.read_text().strip()
                log(f"✓ Loaded last processed ID: {last_id}")
                return last_id
        except Exception as e:
            log(f"⚠ Could not load state: {e}")
        return None

    def save_last_id(self, last_id: str):
        """Save the last processed post ID"""
        try:
            self.state_file.write_text(str(last_id))
            # log(f"✓ Saved last processed ID: {last_id}") # Too verbose for every post
        except Exception as e:
            log(f"⚠ Could not save state: {e}")


def validate_environment():
    """Validate required environment variables"""
    missing = []

    if not DISCORD_WEBHOOK_URL:
        missing.append("DISCORD_WEBHOOK_URL")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")

    if missing:
        log(f"✗ Missing required environment variables: {', '.join(missing)}")
        return False

    log("✓ Environment variables validated")
    return True


def main():
    """Main execution loop"""
    log("=" * 60)
    log("Trump Scraper (Roll Call Aggregator Mode) - v2")
    log("=" * 60)

    if not validate_environment():
        return

    # Check if data directory exists and is writable
    data_path = Path(DATA_DIR)
    log(f"Data directory: {DATA_DIR}")
    try:
        data_path.mkdir(parents=True, exist_ok=True)
        test_file = data_path / "test_write.tmp"
        test_file.write_text("test")
        test_file.unlink()
        log(f"✓ Data directory is writable")
    except Exception as e:
        log(f"⚠ WARNING: Data directory not writable: {e}")
        log(f"⚠ State persistence will NOT work - duplicates may occur!")

    scraper = HybridScraper(headless=True)
    translator = Translator(ANTHROPIC_API_KEY, ANTHROPIC_MODEL)
    discord_poster = DiscordPoster(DISCORD_WEBHOOK_URL)
    state_manager = StateManager(DATA_DIR)

    last_id = state_manager.load_last_id()
    
    if FORCE_REPROCESS:
        log("⚠ FORCE_REPROCESS enabled: Ignoring saved state for this run!")
        check_last_id = None
    else:
        check_last_id = last_id

    log(f"\n✓ Starting monitoring loop (interval: {CHECK_INTERVAL}s)")
    log("-" * 60)

    try:
        while True:
            log(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking Roll Call feed (Stage 1)...")

            posts = scraper.monitor_feed()

            # Roll Call returns posts in DESC order (newest first)
            # We want to process them in ASC order (oldest first)
            posts.reverse()

            # Find new posts (posts with ID > last_id)
            new_posts = []
            
            # Helper to convert to int safely
            def to_int(val):
                try:
                    return int(val)
                except:
                    return None

            last_id_int = to_int(check_last_id) if check_last_id else None

            if not check_last_id:
                # First run / No state logic
                if posts:
                    if FORCE_REPROCESS:
                        # If manually forced, process the last 5 posts for testing/debugging
                        log("⚠ FORCE_REPROCESS: Processing the last 5 posts...")
                        new_posts = posts[-5:] 
                    else:
                        # Normal First Run: Process ONLY the newest post to initialize state
                        newest_post = posts[-1]
                        new_posts = [newest_post]
                        log(f"First run (no state): Processing only the latest post ({newest_post['id']}) to initialize.")
            else:
                # Normal operation: Filter posts newer than last_id
                for post in posts:
                    post_id_int = to_int(post['id'])
                    
                    if last_id_int and post_id_int:
                        if post_id_int > last_id_int:
                            new_posts.append(post)
                    elif check_last_id:
                         if str(post['id']) > str(check_last_id):
                              new_posts.append(post)

            if new_posts:
                log(f"Found {len(new_posts)} new posts. Starting Stage 2 (Deep Scrape)...")
                
                for post in new_posts:
                    log(f"Processing post {post['id']}...")
                    
                    # STAGE 2: Deep Scrape
                    # We have the URL, now let's get the details
                    details = scraper.scrape_details(post['url'])
                    
                    # Merge Logic (Smart Fallback):
                    # 1. Capture what we want to keep from details
                    deep_media = details.get('media_urls', [])
                    video_url = details.get('video_url')
                    full_text = details.get('full_text')
                    is_retruth = details.get('is_retruth')
                    retruth_header = details.get('retruth_header')

                    # 2. Update Post Text & Metadata
                    post['is_retruth'] = is_retruth
                    if is_retruth:
                         post['retruth_header'] = retruth_header
                    
                    if video_url:
                         post['video_url'] = video_url
                    
                    if full_text and len(full_text) > len(post.get('content', '')):
                         post['content'] = full_text

                    # 3. Image Priority
                    # If Deep Scrape found images, use them (High Res).
                    # If NOT, keep the Roll Call images (Stage 1), because they might be snapshots of X/Links.
                    if deep_media:
                        post['media_urls'] = deep_media
                        log(f"  -> Using Deep Scrape media ({len(deep_media)} images)")
                    else:
                        log(f"  -> Deep Scrape found no media. Keeping Roll Call fallback ({len(post.get('media_urls', []))} images)")

                    # NOTE: We do NOT call post.update(details) blindy anymore, to protect media_urls

                    # Prepare text for translation
                    original_text = translator.clean_text(post.get('content', ""))
                    card_content = details.get('card_content', "")
                    translated = ""
                    
                    # Construct Composite Prompt Logic
                    translation_parts = []
                    
                    # 1. Trump's own text (or the ReTruth header context)
                    if post.get('is_retruth'):
                         header = post.get('retruth_header', 'ReTruthed from ???')
                         translation_parts.append(f"[{header}]")
                         
                         # If it's a simple ReTruth (Trump wrote nothing), original_text is likely the shared content
                         # We mark it as [SHARED_CONTENT] so the AI knows to say "Trump shared a post containing..."
                         if original_text:
                             translation_parts.append("[SHARED_CONTENT]")
                             translation_parts.append(original_text)
                    else:
                        # Normal post by Trump
                        if original_text:
                            translation_parts.append(original_text)

                    # 2. Variable: Link Previews (X posts, Articles)
                    # If we found card content (Title/Desc from X or Article), add it as context
                    if card_content:
                        translation_parts.append("\n[LINK_PREVIEW]")
                        translation_parts.append(card_content)

                    # 3. Execute Translation if we have ANY content
                    if translation_parts:
                        full_input = "\n".join(translation_parts)
                        translated = translator.translate_to_hungarian(full_input)

                    discord_poster.post_to_discord(post, translated, original_text)
                    
                    # Update state immediately
                    check_last_id = post['id']
                    state_manager.save_last_id(check_last_id)
                    
                    time.sleep(2)
            else:
                log("✓ No new posts found (since last check)")

            log(f"\n⏳ Waiting {CHECK_INTERVAL} seconds until next check...")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        log("\n\n✓ Shutting down gracefully...")
    except Exception as e:
        log(f"\n✗ Unexpected error: {e}")
        raise


if __name__ == "__main__":
    main()
