import pandas as pd
import re
import logging
from pathlib import Path
from datetime import date as dt_date
from datetime import datetime, timedelta
from typing import Tuple, Optional, List, Dict, Any, Set
import time
import json
import os
from dotenv import load_dotenv
import shutil
from collections import defaultdict

from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, NoSuchElementException, WebDriverException
from selenium.webdriver.remote.webelement import WebElement

from scraper.table_scraper import table_scraper
from scraper.date_scraper import scrape_date


# Try to import GCP libraries
try:
    from google.cloud import storage
    GCP_AVAILABLE = True
except ImportError:
    GCP_AVAILABLE = False
    logging.warning("google-cloud-storage not installed. GCP uploads will be disabled.")

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
INGESTION_RUN_ID = dt_date.today().isoformat()
CONTAINER_LINK_TEXT = "View All Container Statistics"
VARIETY_LINK_TEXT = "View Statistics per Container and Variety"

# File paths - use Path.cwd() for local development
BASE_DIR = Path.cwd()
DATA_DIR = Path(os.getenv("SCRAPER_DATA_DIR", BASE_DIR / "data"))
ARCHIVE_DIR = Path(os.getenv("SCRAPER_ARCHIVE_DIR", BASE_DIR / "archive"))
LOGS_DIR = Path(os.getenv("SCRAPER_LOGS_DIR", BASE_DIR / "logs"))
CHECKPOINT_DIR = Path(os.getenv("SCRAPER_CHECKPOINT_DIR", BASE_DIR / "checkpoints"))

# Ensure all directories exist
for directory in [DATA_DIR, ARCHIVE_DIR, LOGS_DIR, CHECKPOINT_DIR]:
    directory.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Ensured directory exists: {directory}")

# Define subdirectories
SUMMARY_DIR = DATA_DIR / "summary"
CONTAINER_DIR = DATA_DIR / "container"
VARIETY_DIR = DATA_DIR / "variety"
ARCHIVE_SUMMARY_DIR = ARCHIVE_DIR / "summary"
ARCHIVE_CONTAINER_DIR = ARCHIVE_DIR / "container"
ARCHIVE_VARIETY_DIR = ARCHIVE_DIR / "variety"

# Create subdirectories
for directory in [SUMMARY_DIR, CONTAINER_DIR, VARIETY_DIR, 
                  ARCHIVE_SUMMARY_DIR, ARCHIVE_CONTAINER_DIR, ARCHIVE_VARIETY_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Status files in checkpoint directory
CHECKPOINT_FILE = CHECKPOINT_DIR / "scraper_checkpoint.json"
FAILED_COMMODITIES_FILE = CHECKPOINT_DIR / "failed_commodities.json"
COMPLETED_COMMODITIES_FILE = CHECKPOINT_DIR / "completed_commodities.json"
RETRY_STATUS_FILE = CHECKPOINT_DIR / "retry_status.json"

# GCP Configuration
GCP_BUCKET_NAME = os.getenv("GCP_BUCKET_NAME")  
GCP_UPLOAD_ENABLED = os.getenv("GCP_UPLOAD_ENABLED", "true").lower() == "true"
GCP_BUCKET_PATH = os.getenv("GCP_BUCKET_PATH")

# Retry Configuration
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "5"))
MAX_SCRAPING_PASSES = int(os.getenv("MAX_SCRAPING_PASSES", "3"))
STATUS_MAX_AGE_HOURS = int(os.getenv("STATUS_MAX_AGE_HOURS", "24"))

# GCP Storage client
_gcp_client = None

def get_gcp_client():
    """Get or create GCP storage client."""
    global _gcp_client
    if _gcp_client is None and GCP_AVAILABLE and GCP_UPLOAD_ENABLED:
        try:
            _gcp_client = storage.Client()
            logger.info("GCP Storage client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize GCP client: {e}")
            _gcp_client = None
    return _gcp_client

def upload_to_gcp(local_file_path: Path, link_type: str, scraped_date: str, commodity_name: str) -> bool:
    """Upload a file to Google Cloud Storage."""
    if not GCP_UPLOAD_ENABLED:
        return False
    
    if not GCP_AVAILABLE:
        return False
    
    client = get_gcp_client()
    if not client:
        return False
    
    try:
        filename = local_file_path.name
        
        if GCP_BUCKET_PATH:
            blob_path = f"{GCP_BUCKET_PATH}/{link_type}/{filename}"
        else:
            blob_path = f"{link_type}/{filename}"
        
        bucket = client.bucket(GCP_BUCKET_NAME)
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(str(local_file_path))
        
        logger.info(f"  Created GCP copy: gs://{GCP_BUCKET_NAME}/{blob_path}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to upload {local_file_path} to GCP: {e}")
        return False

def sync_directory_to_gcp(local_dir: Path, link_type: str) -> Tuple[int, int, List[str]]:
    """Sync all files in a directory to GCP."""
    if not local_dir.exists():
        return 0, 0, []
    
    csv_files = list(local_dir.glob("*.csv"))
    if not csv_files:
        return 0, 0, []
    
    uploaded_files = []
    uploaded_count = 0
    
    for csv_file in csv_files:
        try:
            filename = csv_file.name
            parts = filename.replace('.csv', '').split('_')
            
            commodity_name = "unknown"
            scraped_date = "unknown"
            
            if len(parts) >= 5:
                for i, part in enumerate(parts):
                    if part in ["summary", "container", "variety"]:
                        commodity_parts = parts[2:i]
                        commodity_name = ' '.join(commodity_parts)
                        if i + 1 < len(parts):
                            scraped_date = ' '.join(parts[i+1:]).replace('_', ' ')
                        break
            
            if upload_to_gcp(csv_file, link_type, scraped_date, commodity_name):
                uploaded_count += 1
                uploaded_files.append(filename)
                
        except Exception as e:
            logger.error(f"Error uploading {csv_file.name}: {e}")
    
    return uploaded_count, len(csv_files), uploaded_files

def archive_file(file_path: Path, link_type: str):
    """Create an archived copy of a file for historical reference."""
    try:
        if link_type == "summary":
            archive_dir = ARCHIVE_SUMMARY_DIR
        elif link_type == "container":
            archive_dir = ARCHIVE_CONTAINER_DIR
        elif link_type == "variety":
            archive_dir = ARCHIVE_VARIETY_DIR
        else:
            archive_dir = ARCHIVE_DIR
        
        archive_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = dt_date.today().isoformat()
        archive_filename = f"{file_path.stem}_{timestamp}{file_path.suffix}"
        archive_path = archive_dir / archive_filename
        
        shutil.copy2(file_path, archive_path)
        logger.debug(f"Archived copy created: {archive_path}")
        
    except Exception as e:
        logger.error(f"Failed to archive {file_path}: {e}")


# ============= STATE MANAGEMENT FUNCTIONS =============

def ensure_correct_state(driver, wait: WebDriverWait) -> bool:
    """Ensure driver is in correct state before processing next commodity."""
    try:
        # Always start from default content
        driver.switch_to.default_content()
        
        # Check if we're on the right page
        target_url = os.getenv("TARGET_URL")
        if target_url and target_url not in driver.current_url:
            logger.warning("Not on correct page, navigating to URL...")
            driver.get(target_url)
            time.sleep(2)
        
        # Find and switch to iframe
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        driver.switch_to.frame(iframe)
        
        # Verify we can see the select element
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "select")))
        
        logger.debug("Driver state verified - ready for next commodity")
        return True
        
    except Exception as e:
        logger.error(f"Failed to ensure correct state: {e}")
        return False

def refresh_page_if_needed(driver, wait: WebDriverWait, commodity_count: int):
    """Refresh the page periodically to prevent memory/issues."""
    # Refresh every 20 commodities
    if commodity_count > 0 and commodity_count % 20 == 0:
        logger.info(f"Processed {commodity_count} commodities, refreshing page...")
        try:
            driver.switch_to.default_content()
            driver.refresh()
            time.sleep(3)
            switch_to_iframe(driver, wait)
            logger.info("Page refreshed successfully")
        except Exception as e:
            logger.error(f"Error refreshing page: {e}")

def recover_driver_state(driver, wait: WebDriverWait) -> bool:
    """Attempt to recover driver state after fatal error."""
    try:
        logger.info("Attempting to recover driver state...")
        driver.switch_to.default_content()
        
        target_url = os.getenv("TARGET_URL")
        if target_url:
            driver.get(target_url)
            time.sleep(3)
        
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
        driver.switch_to.frame(iframe)
        logger.info("Driver state recovered successfully")
        return True
    except Exception as recovery_error:
        logger.error(f"Recovery failed: {recovery_error}")
        return False


# ============= FAILURE HANDLING =============

class CommodityStatus:
    """Track status of each commodity during scraping."""
    
    def __init__(self):
        self.completed = set()
        self.partial = {}
        self.failed = {}
        self.pending = set()
        self.current_pass = 1
        self.max_passes = MAX_SCRAPING_PASSES
        
    def to_dict(self) -> Dict:
        return {
            "completed": list(self.completed),
            "partial": self.partial,
            "failed": self.failed,
            "current_pass": self.current_pass,
            "max_passes": self.max_passes,
            "timestamp": time.time()
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'CommodityStatus':
        status = cls()
        status.completed = set(data.get("completed", []))
        status.partial = data.get("partial", {})
        status.failed = data.get("failed", {})
        status.current_pass = data.get("current_pass", 1)
        status.max_passes = data.get("max_passes", MAX_SCRAPING_PASSES)
        return status
    
    def mark_completed(self, commodity_name: str, link_types: List[str]):
        self.completed.add(commodity_name)
        if commodity_name in self.partial:
            del self.partial[commodity_name]
        if commodity_name in self.failed:
            del self.failed[commodity_name]
        logger.info(f" Commodity marked as COMPLETED: {commodity_name}")
    
    def mark_partial(self, commodity_name: str, completed_types: List[str], failed_type: Optional[str] = None):
        self.partial[commodity_name] = {
            "completed_types": completed_types,
            "failed_type": failed_type,
            "attempts": self.partial.get(commodity_name, {}).get("attempts", 0) + 1,
            "last_attempt": time.time()
        }
        logger.info(f" Commodity marked as PARTIAL: {commodity_name}")
    
    def mark_failed(self, commodity_name: str, error_reason: str):
        failure_info = {
            "reason": error_reason,
            "attempts": self.failed.get(commodity_name, {}).get("attempts", 0) + 1,
            "last_attempt": time.time(),
            "pass": self.current_pass
        }
        self.failed[commodity_name] = failure_info
        logger.error(f" Commodity marked as FAILED: {commodity_name}")
    
    def should_retry(self, commodity_name: str) -> bool:
        if commodity_name in self.completed:
            return False
            
        if commodity_name in self.failed:
            attempts = self.failed[commodity_name].get("attempts", 0)
            if attempts >= MAX_RETRIES:
                return False
        
        if self.current_pass >= self.max_passes:
            return False
            
        return True
    
    def get_pending_for_pass(self, all_commodities: List[str]) -> List[str]:
        pending = []
        for commodity in all_commodities:
            if commodity in self.completed:
                continue
            if self.should_retry(commodity):
                pending.append(commodity)
        
        self.pending = set(pending)
        return pending
    
    def increment_pass(self):
        self.current_pass += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"STARTING SCRAPING PASS {self.current_pass}/{self.max_passes}")
        logger.info(f"{'='*60}")
    
    def get_summary(self) -> Dict:
        return {
            "total_commodities": len(self.completed) + len(self.partial) + len(self.failed),
            "completed": len(self.completed),
            "partial": len(self.partial),
            "failed": len(self.failed),
            "current_pass": self.current_pass,
            "max_passes": self.max_passes
        }


# ============= STATUS MANAGEMENT =============

def cleanup_status_files(force: bool = False) -> bool:
    """Remove all status files."""
    try:
        files_to_remove = [
            FAILED_COMMODITIES_FILE, 
            RETRY_STATUS_FILE, 
            COMPLETED_COMMODITIES_FILE, 
            CHECKPOINT_FILE
        ]
        
        removed_count = 0
        for file in files_to_remove:
            if file.exists():
                file.unlink()
                removed_count += 1
                logger.debug(f"Removed {file}")
        
        if removed_count > 0:
            logger.info(f"Cleaned up {removed_count} status files")
        return True
    except Exception as e:
        logger.error(f"Error cleaning up status files: {e}")
        return False

def are_status_files_stale() -> bool:
    """Check if status files are stale."""
    current_time = time.time()
    
    for file in [COMPLETED_COMMODITIES_FILE, FAILED_COMMODITIES_FILE, RETRY_STATUS_FILE]:
        if file.exists():
            file_age_hours = (current_time - file.stat().st_mtime) / 3600
            if file_age_hours > STATUS_MAX_AGE_HOURS:
                logger.warning(f"{file.name} is {file_age_hours:.1f} hours old (> {STATUS_MAX_AGE_HOURS}h)")
                return True
    
    return False

def load_scraping_status(fresh_start: bool = False) -> CommodityStatus:
    """Load scraping status from files with staleness check."""
    if fresh_start:
        logger.info("Fresh start requested - cleaning up all status files")
        cleanup_status_files(force=True)
        return CommodityStatus()
    
    if are_status_files_stale():
        logger.warning("Status files are stale. Starting fresh...")
        cleanup_status_files(force=True)
        return CommodityStatus()
    
    status = CommodityStatus()
    
    if COMPLETED_COMMODITIES_FILE.exists():
        try:
            with open(COMPLETED_COMMODITIES_FILE, 'r') as f:
                completed_data = json.load(f)
                for commodity, types in completed_data.items():
                    status.completed.add(commodity)
                    status.partial[commodity] = {
                        "completed_types": types,
                        "failed_type": None,
                        "attempts": 1
                    }
        except Exception as e:
            logger.error(f"Error loading completed commodities: {e}")
    
    if FAILED_COMMODITIES_FILE.exists():
        try:
            with open(FAILED_COMMODITIES_FILE, 'r') as f:
                failed_data = json.load(f)
                status.failed = failed_data
        except Exception as e:
            logger.error(f"Error loading failed commodities: {e}")
    
    if RETRY_STATUS_FILE.exists():
        try:
            with open(RETRY_STATUS_FILE, 'r') as f:
                retry_data = json.load(f)
                status.current_pass = retry_data.get("current_pass", 1)
        except Exception as e:
            logger.error(f"Error loading retry status: {e}")
    
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                checkpoint_data = json.load(f)
                logger.info(f"Found checkpoint at commodity: {checkpoint_data.get('current_commodity', 'Unknown')}")
        except Exception as e:
            logger.error(f"Error loading checkpoint: {e}")
    
    return status

def save_scraping_status(status: CommodityStatus):
    """Save current scraping status."""
    try:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        
        completed_dict = {}
        for commodity in status.completed:
            completed_dict[commodity] = status.partial.get(commodity, {}).get("completed_types", ["summary"])
        
        with open(COMPLETED_COMMODITIES_FILE, 'w') as f:
            json.dump(completed_dict, f, indent=2)
        
        with open(FAILED_COMMODITIES_FILE, 'w') as f:
            json.dump(status.failed, f, indent=2)
        
        retry_data = {
            "current_pass": status.current_pass,
            "max_passes": status.max_passes,
            "timestamp": time.time()
        }
        with open(RETRY_STATUS_FILE, 'w') as f:
            json.dump(retry_data, f, indent=2)
            
    except Exception as e:
        logger.error(f"Error saving scraping status: {e}")

def save_checkpoint(index: int, commodity_name: str, completed: List[str]):
    """Save current scraping state."""
    checkpoint_data = {
        "current_index": index,
        "current_commodity": commodity_name,
        "completed": completed,
        "timestamp": time.time(),
        "run_id": INGESTION_RUN_ID
    }
    try:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)
        logger.debug(f"Checkpoint saved: index={index}, commodity={commodity_name}")
    except Exception as e:
        logger.error(f"Error saving checkpoint: {e}")


# ============= RETRY DECORATOR =============

def retry_on_failure(max_retries: int = 3, delay: int = 5, backoff: float = 2.0):
    """Decorator to retry a function on failure with exponential backoff."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (TimeoutException, StaleElementReferenceException, 
                        NoSuchElementException, WebDriverException) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"Attempt {attempt + 1}/{max_retries} failed for {func.__name__}: {e}")
                        logger.warning(f"Retrying in {current_delay} seconds...")
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(f"All {max_retries} attempts failed for {func.__name__}")
                except Exception as e:
                    logger.error(f"Unexpected error in {func.__name__}: {e}")
                    raise
            
            raise last_exception
        return wrapper
    return decorator


# ============= SCRAPING FUNCTIONS =============

@retry_on_failure(max_retries=3, delay=2)
def safe_select_commodity(driver, wait: WebDriverWait, index: int) -> str:
    """Safely select a commodity by index with retries."""
    try:
        # Ensure we're in the right state first
        if not ensure_correct_state(driver, wait):
            raise Exception("Failed to establish correct driver state")
        
        # Now find and interact with select
        select_el = wait.until(EC.element_to_be_clickable((By.TAG_NAME, "select")))
        
        # Get fresh options each time
        options = select_el.find_elements(By.TAG_NAME, "option")
        if index >= len(options):
            raise IndexError(f"Index {index} out of range (max: {len(options)-1})")
        
        commodity_name = options[index].text.strip()
        
        # Use Select class for more reliable interaction
        select = Select(select_el)
        select.select_by_index(index)
        
        # Wait for page to update - look for table to appear
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "table.alltable tbody tr")) > 0)
        time.sleep(0.5)  # Small buffer for JavaScript
        
        return commodity_name
        
    except StaleElementReferenceException:
        logger.warning("Stale element in safe_select_commodity, retrying...")
        # Force a refresh of the state
        driver.switch_to.default_content()
        switch_to_iframe(driver, wait)
        raise  # Let retry decorator handle it

@retry_on_failure(max_retries=2, delay=3)
def safe_scrape_date(driver) -> str:
    return scrape_date(driver)

@retry_on_failure(max_retries=2, delay=3)
def safe_analyze_table(driver) -> dict:
    return analyze_summary_table(driver)

@retry_on_failure(max_retries=2, delay=3)
def safe_scrape_table(driver, scraped_date: str, commodity_name: str, link_type: str) -> Optional[int]:
    return scrape_and_save_table(driver, scraped_date, commodity_name, link_type)

@retry_on_failure(max_retries=2, delay=3)
def safe_handle_link(driver, wait: WebDriverWait, link_text: str, 
                     scraped_date: str, commodity_name: str, safe_commodity_name: str,
                     previous_rows: Optional[int] = None) -> bool:
    """Safely handle a link (container or variety) with retries."""
    # Ensure we're in iframe before looking for link
    try:
        switch_to_iframe(driver, wait)
    except:
        pass
    
    link = find_link_by_text(driver, link_text)
    if not link:
        logger.info(f"  No {link_text} link found")
        return False
    
    link_type = "container" if CONTAINER_LINK_TEXT in link_text else "variety"
    logger.info(f"  Attempting {link_type} link...")
    
    try:
        safe_click(driver, link)
        
        if previous_rows is not None:
            wait_for_table_change(driver, wait, previous_rows)
        else:
            wait.until(lambda d: get_table_row_count(d) > 0)
        
        safe_scrape_table(driver, scraped_date, safe_commodity_name, link_type)
        
        # Navigate back
        driver.back()
        
        # Wait for page to load and re-enter iframe
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        switch_to_iframe(driver, wait)
        
        # Re-select commodity if needed
        if link_type == "container":
            reselect_commodity(driver, wait, commodity_name)
        
        return True
        
    except Exception as e:
        logger.warning(f"  {link_type} link failed: {e}")
        # Try to recover
        try:
            driver.back()
            switch_to_iframe(driver, wait)
            if link_type == "container":
                reselect_commodity(driver, wait, commodity_name)
        except:
            pass
        return False


# ============= MAIN SCRAPING FUNCTIONS =============

def process_single_commodity(driver, wait: WebDriverWait, index: int, 
                           commodity_name: str, status: CommodityStatus,
                           commodity_number: int) -> Tuple[bool, List[str]]:
    """Process a single commodity with proper error handling."""
    safe_commodity_name = sanitize_sheet_name(commodity_name)
    completed_types = []
    
    # Refresh page periodically
    refresh_page_if_needed(driver, wait, commodity_number)
    
    try:
        logger.info(f"\n  Processing: {commodity_name}")
        
        # Ensure we're in correct state before starting
        if not ensure_correct_state(driver, wait):
            raise Exception("Cannot establish correct driver state")
        
        # Scrape date
        scraped_date = safe_scrape_date(driver)
        logger.info(f"  Date: {scraped_date}")
        
        #  Analyze summary table
        table_analysis = safe_analyze_table(driver)
        
        #  Always scrape summary table first
        previous_rows = safe_scrape_table(driver, scraped_date, safe_commodity_name, "summary")
        if previous_rows is not None:
            completed_types.append("summary")
        
        #  Handle container link
        if safe_handle_link(driver, wait, CONTAINER_LINK_TEXT, 
                           scraped_date, commodity_name, safe_commodity_name,
                           previous_rows if not table_analysis["is_single_container"] else None):
            completed_types.append("container")
        
        # Handle variety link
        if safe_handle_link(driver, wait, VARIETY_LINK_TEXT,
                           scraped_date, commodity_name, safe_commodity_name,
                           None):
            completed_types.append("variety")
        
        # Determine if fully successful
        expected_types = ["summary"]
        if not table_analysis["is_single_container"]:
            expected_types.append("container")
        
        all_expected = all(t in completed_types for t in expected_types)
        
        if all_expected:
            status.mark_completed(commodity_name, completed_types)
            return True, completed_types
        else:
            failed_type = None
            if "container" not in completed_types and not table_analysis["is_single_container"]:
                failed_type = "container"
            status.mark_partial(commodity_name, completed_types, failed_type)
            return False, completed_types
            
    except Exception as e:
        logger.error(f"  Error processing {commodity_name}: {e}")
        status.mark_failed(commodity_name, str(e))
        return False, completed_types

def run_scraping_passes(driver, wait: WebDriverWait, fresh_start: bool = False):
    """Run multiple scraping passes to handle failures and retries."""
    logger.info(f"Starting ingestion run: {INGESTION_RUN_ID}")
    logger.info("=" * 60)
    logger.info(f"MAX RETRIES PER COMMODITY: {MAX_RETRIES}")
    logger.info(f"MAX SCRAPING PASSES: {MAX_SCRAPING_PASSES}")
    if fresh_start:
        logger.info("MODE: FRESH START - Ignoring existing status files")
    logger.info("=" * 60)
    
    # Load existing status or create new one
    status = load_scraping_status(fresh_start)
    
    # Initial setup - get all commodities
    if not ensure_correct_state(driver, wait):
        logger.error("Failed to establish initial driver state")
        return
    
    select_el = wait.until(EC.element_to_be_clickable((By.TAG_NAME, "select")))
    options = select_el.find_elements(By.TAG_NAME, "option")
    all_commodities = [opt.text.strip() for opt in options[1:]]
    
    logger.info(f"Total commodities to process: {len(all_commodities)}")
    logger.info(f"Initial status: {status.get_summary()}")
    
    # Main scraping loop - multiple passes
    while status.current_pass <= status.max_passes:
        pending = status.get_pending_for_pass(all_commodities)
        
        if not pending:
            logger.info(f"No pending commodities in pass {status.current_pass}")
            if status.current_pass < status.max_passes:
                status.increment_pass()
                continue
            else:
                break
        
        logger.info(f"\n{'='*60}")
        logger.info(f"PASS {status.current_pass}/{status.max_passes}: Processing {len(pending)} commodities")
        logger.info(f"{'='*60}")
        
        for idx, commodity_name in enumerate(pending):
            commodity_number = all_commodities.index(commodity_name) + 1
            logger.info(f"\n--- [{idx+1}/{len(pending)}] Processing: {commodity_name} (index {commodity_number}) ---")
            
            try:
                # Add a small delay between commodities to avoid rate limiting
                if idx > 0:
                    time.sleep(2)
                
                # Select the commodity
                selected_name = safe_select_commodity(driver, wait, commodity_number)
                
                # Process the commodity
                success, completed_types = process_single_commodity(
                    driver, wait, 
                    commodity_number,
                    commodity_name, 
                    status,
                    commodity_number
                )
                
                save_scraping_status(status)
                
            except Exception as e:
                logger.error(f"Fatal error processing {commodity_name}: {e}")
                status.mark_failed(commodity_name, f"Fatal: {str(e)}")
                save_scraping_status(status)
                
                # Try to recover driver state
                if not recover_driver_state(driver, wait):
                    logger.error("Failed to recover driver state, aborting pass")
                    break
        
        # Pass complete - show summary
        logger.info(f"\n{'='*60}")
        logger.info(f"PASS {status.current_pass} COMPLETE")
        logger.info(f"Status: {status.get_summary()}")
        logger.info(f"{'='*60}")
        
        if not status.failed and not status.partial:
            logger.info("All commodities successfully processed!")
            break
        
        if status.current_pass < status.max_passes:
            status.increment_pass()
            save_scraping_status(status)
            logger.info(f"Waiting {RETRY_DELAY_SECONDS} seconds before next pass...")
            time.sleep(RETRY_DELAY_SECONDS)
    
    # Final summary
    print_final_summary(status)
    
    # Cleanup based on results
    if not status.failed and not status.partial:
        cleanup_status_files(force=True)
        logger.info("All commodities successfully processed - status files cleaned up")
    else:
        logger.warning(f"\nSome commodities were not fully processed.")
        logger.warning(f"Failed: {len(status.failed)}, Partial: {len(status.partial)}")
        logger.warning(f"Status files preserved in {CHECKPOINT_DIR}")
        logger.warning(f"Use --fresh flag next time to start completely fresh")
    
    # Final GCP sync
    if GCP_UPLOAD_ENABLED and GCP_AVAILABLE:
        final_gcp_sync()

def print_final_summary(status: CommodityStatus):
    """Print final summary of scraping results."""
    logger.info("\n" + "=" * 60)
    logger.info("FINAL SCRAPING SUMMARY")
    logger.info("=" * 60)
    
    logger.info(f"\nCOMPLETED: {len(status.completed)} commodities")
    for commodity in sorted(list(status.completed))[:10]:
        logger.info(f"   {commodity}")
    if len(status.completed) > 10:
        logger.info(f"  ... and {len(status.completed) - 10} more")
    
    if status.partial:
        logger.info(f"\nPARTIAL ({len(status.partial)} commodities):")
        for commodity, info in list(status.partial.items())[:10]:
            logger.info(f"   {commodity} - completed: {info['completed_types']}")
    
    if status.failed:
        logger.info(f"\nFAILED ({len(status.failed)} commodities):")
        for commodity, info in list(status.failed.items())[:10]:
            logger.info(f"   {commodity} - attempts: {info['attempts']}")
    
    logger.info("\n" + "=" * 60)

def final_gcp_sync():
    """Final sync of all files to GCP."""
    logger.info("\n" + "=" * 60)
    logger.info("FINAL GCP SYNC - Ensuring all files have GCP copies")
    logger.info("=" * 60)
    
    total_uploaded = 0
    total_files = 0
    
    for dir_name, directory, link_type in [
        ("Summary", SUMMARY_DIR, "summary"),
        ("Container", CONTAINER_DIR, "container"),
        ("Variety", VARIETY_DIR, "variety")
    ]:
        if directory.exists():
            uploaded, total, files = sync_directory_to_gcp(directory, link_type)
            total_uploaded += uploaded
            total_files += total
            if total > 0:
                logger.info(f"{dir_name}: {uploaded}/{total} files synced to GCP")
    
    logger.info(f"\nTotal: {total_uploaded}/{total_files} files have GCP copies")


# ============= UTILITY FUNCTIONS =============

def sanitize_sheet_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "_", name)
    return name[:100]

def safe_click(driver, element: WebElement):
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", element
    )
    driver.execute_script("arguments[0].click();", element)

def switch_to_iframe(driver, wait: WebDriverWait):
    driver.switch_to.default_content()
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
    iframe = driver.find_element(By.TAG_NAME, "iframe")
    driver.switch_to.frame(iframe)

def wait_for_table_change(driver, wait: WebDriverWait, previous_row_count: int, timeout: int = 10):
    def table_has_changed(driver):
        try:
            current_rows = len(driver.find_elements(
                By.CSS_SELECTOR, "table.alltable tbody tr"
            ))
            return current_rows != previous_row_count and current_rows > 0
        except:
            return False
    
    wait.until(table_has_changed)
    time.sleep(0.3)

def get_table_row_count(driver) -> int:
    try:
        return len(driver.find_elements(
            By.CSS_SELECTOR, "table.alltable tbody tr"
        ))
    except:
        return 0

def find_link_by_text(driver, search_text: str) -> Optional[WebElement]:
    try:
        links = driver.find_elements(By.CSS_SELECTOR, "div > a")
        for link in links:
            if search_text in link.text:
                return link
    except:
        pass
    return None

def analyze_summary_table(driver) -> dict:
    analysis = {
        "data_rows": 0,
        "is_single_container": False,
        "table_structure": "unknown"
    }
    
    try:
        table = driver.find_element(By.CSS_SELECTOR, "table.alltable")
        rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        
        data_rows = 0
        for row in rows:
            row_text = row.text.lower()
            if "total" not in row_text and "summary" not in row_text:
                data_rows += 1
        
        analysis["data_rows"] = data_rows
        analysis["is_single_container"] = data_rows <= 1
        analysis["table_structure"] = "single_container" if data_rows <= 1 else "multi_container"
        
    except Exception as e:
        logger.error(f"Error analyzing table: {e}")
    
    return analysis

def get_output_directory(link_type: str) -> Path:
    if link_type == "summary":
        return SUMMARY_DIR
    elif link_type == "container":
        return CONTAINER_DIR
    elif link_type == "variety":
        return VARIETY_DIR
    else:
        return DATA_DIR

def create_filename(commodity_name: str, link_type: str, scraped_date: str) -> str:
    safe_name = sanitize_sheet_name(commodity_name)
    date_for_filename = re.sub(r'[^\w]', '_', scraped_date)
    return f"joburg_market_{safe_name}_{link_type}_{date_for_filename}.csv"

def scrape_and_save_table(driver, scraped_date: str, commodity_name: str, link_type: str) -> Optional[int]:
    previous_rows = get_table_row_count(driver)
    
    df = table_scraper(driver)
    
    if df is None or df.empty:
        logger.warning(f"Empty table for {commodity_name} - {link_type}")
        return previous_rows
    
    actual_rows = len(df)
    logger.info(f"  Scraped {actual_rows} rows for {link_type}")
    
    df["scrape_date"] = scraped_date
    df["commodity"] = commodity_name
    df["link_type"] = link_type
    df["ingestion_run_id"] = INGESTION_RUN_ID
    
    output_dir = get_output_directory(link_type)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    filename = create_filename(commodity_name, link_type, scraped_date)
    file_path = output_dir / filename
    
    df.to_csv(file_path, index=False)
    logger.info(f"  Saved local copy: {file_path}")
    
    archive_file(file_path, link_type)
    
    if upload_to_gcp(file_path, link_type, scraped_date, commodity_name):
        logger.info(f"  Created GCP copy for {link_type} data")
    
    return previous_rows

def reselect_commodity(driver, wait: WebDriverWait, commodity_name: str):
    switch_to_iframe(driver, wait)
    
    select_el = wait.until(EC.element_to_be_clickable((By.TAG_NAME, "select")))
    select = Select(select_el)
    
    options = select_el.find_elements(By.TAG_NAME, "option")
    for idx, option in enumerate(options):
        if option.text.strip() == commodity_name:
            select.select_by_index(idx)
            time.sleep(1)
            break


# ============= PUBLIC API =============

def run_scraping_with_retries(driver, wait: WebDriverWait, fresh_start: bool = False):
    """
    Run scraping with retry logic.
    
    Args:
        driver: Selenium webdriver
        wait: WebDriverWait instance
        fresh_start: If True, ignore existing status files and start fresh
    
    Returns:
        True if scraping completed (even with some failures), False on fatal error
    """
    try:
        run_scraping_passes(driver, wait, fresh_start)
        return True
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        logger.exception(e)
        return False