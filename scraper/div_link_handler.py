import pandas as pd
import re
import logging
from pathlib import Path
from datetime import date as dt_date
from typing import Tuple, Optional, List, Dict, Any
import time
import json
import os
from dotenv import load_dotenv
import shutil

from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, NoSuchElementException
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
        logging.FileHandler('scraper.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
INGESTION_RUN_ID = dt_date.today().isoformat()
CONTAINER_LINK_TEXT = "View All Container Statistics"
VARIETY_LINK_TEXT = "View Statistics per Container and Variety"
CHECKPOINT_FILE = Path("scraper_checkpoint.json")
COMPLETED_COMMODITIES_FILE = Path("completed_commodities.json")

# GCP Configuration
GCP_BUCKET_NAME = os.getenv("GCP_BUCKET_NAME")  
GCP_UPLOAD_ENABLED = os.getenv("GCP_UPLOAD_ENABLED", "true").lower() == "true"
GCP_BUCKET_PATH = os.getenv("GCP_BUCKET_PATH")

# Define base data directory and subdirectories
BASE_DATA_DIR = Path("data")
SUMMARY_DIR = BASE_DATA_DIR / "summary"
CONTAINER_DIR = BASE_DATA_DIR / "container"
VARIETY_DIR = BASE_DATA_DIR / "variety"

# Define archive directory for keeping historical copies
ARCHIVE_DIR = Path("archive")
ARCHIVE_SUMMARY_DIR = ARCHIVE_DIR / "summary"
ARCHIVE_CONTAINER_DIR = ARCHIVE_DIR / "container"
ARCHIVE_VARIETY_DIR = ARCHIVE_DIR / "variety"

# Ensure all directories exist
for directory in [BASE_DATA_DIR, SUMMARY_DIR, CONTAINER_DIR, VARIETY_DIR, 
                  ARCHIVE_DIR, ARCHIVE_SUMMARY_DIR, ARCHIVE_CONTAINER_DIR, ARCHIVE_VARIETY_DIR]:
    directory.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Ensured directory exists: {directory}")

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
    """
    Upload a file to Google Cloud Storage.
    Creates a copy in GCP while keeping the original in the repo.
    
    Args:
        local_file_path: Path to local file
        link_type: Type of data ('summary', 'container', 'variety')
        scraped_date: Date when data was scraped
        commodity_name: Name of the commodity
        
    Returns:
        True if upload successful, False otherwise
    """
    if not GCP_UPLOAD_ENABLED:
        logger.debug("GCP upload disabled, skipping upload")
        return False
    
    if not GCP_AVAILABLE:
        logger.warning("GCP libraries not available, skipping upload")
        return False
    
    client = get_gcp_client()
    if not client:
        logger.warning("GCP client not available, skipping upload")
        return False
    
    try:
        # Create blob path in bucket - keep the same filename
        filename = local_file_path.name
        
        # Build blob path - organize by link_type
        if GCP_BUCKET_PATH:
            blob_path = f"{GCP_BUCKET_PATH}/{link_type}/{filename}"
        else:
            blob_path = f"{link_type}/{filename}"
        
        # Get bucket
        bucket = client.bucket(GCP_BUCKET_NAME)
        
        # Upload file (creates copy in GCP)
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(str(local_file_path))
        
        logger.info(f"  ✓ Created GCP copy: gs://{GCP_BUCKET_NAME}/{blob_path}")
        logger.info(f"  ✓ Local copy remains: {local_file_path}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to upload {local_file_path} to GCP: {e}")
        return False

def sync_directory_to_gcp(local_dir: Path, link_type: str) -> Tuple[int, int, List[str]]:
    """
    Sync all files in a directory to GCP.
    Only uploads files that don't exist in GCP or have changed.
    
    Args:
        local_dir: Local directory path
        link_type: Type of data ('summary', 'container', 'variety')
    
    Returns:
        Tuple of (uploaded_count, total_count, uploaded_files)
    """
    if not local_dir.exists():
        logger.warning(f"Directory does not exist: {local_dir}")
        return 0, 0, []
    
    csv_files = list(local_dir.glob("*.csv"))
    if not csv_files:
        logger.info(f"No CSV files found in {local_dir}")
        return 0, 0, []
    
    uploaded_files = []
    uploaded_count = 0
    
    logger.info(f"Syncing {len(csv_files)} files from {local_dir} to GCP...")
    
    for csv_file in csv_files:
        try:
            # Extract info from filename
            filename = csv_file.name
            parts = filename.replace('.csv', '').split('_')
            
            # Try to extract commodity and date
            commodity_name = "unknown"
            scraped_date = "unknown"
            
            if len(parts) >= 5:
                # Find link_type position
                for i, part in enumerate(parts):
                    if part in ["summary", "container", "variety"]:
                        commodity_parts = parts[2:i]  # Between 'market' and link_type
                        commodity_name = ' '.join(commodity_parts)
                        if i + 1 < len(parts):
                            scraped_date = ' '.join(parts[i+1:]).replace('_', ' ')
                        break
            
            # Upload to GCP
            if upload_to_gcp(csv_file, link_type, scraped_date, commodity_name):
                uploaded_count += 1
                uploaded_files.append(filename)
                
        except Exception as e:
            logger.error(f"Error uploading {csv_file.name}: {e}")
    
    logger.info(f"Synced {uploaded_count}/{len(csv_files)} files from {link_type} directory to GCP")
    return uploaded_count, len(csv_files), uploaded_files

def archive_file(file_path: Path, link_type: str):
    """
    Create an archived copy of a file for historical reference.
    
    Args:
        file_path: Path to the file to archive
        link_type: Type of data ('summary', 'container', 'variety')
    """
    try:
        # Determine archive directory
        if link_type == "summary":
            archive_dir = ARCHIVE_SUMMARY_DIR
        elif link_type == "container":
            archive_dir = ARCHIVE_CONTAINER_DIR
        elif link_type == "variety":
            archive_dir = ARCHIVE_VARIETY_DIR
        else:
            archive_dir = ARCHIVE_DIR
        
        # Ensure archive directory exists
        archive_dir.mkdir(parents=True, exist_ok=True)
        
        # Create archive path with timestamp
        timestamp = dt_date.today().isoformat()
        archive_filename = f"{file_path.stem}_{timestamp}{file_path.suffix}"
        archive_path = archive_dir / archive_filename
        
        # Copy file to archive
        shutil.copy2(file_path, archive_path)
        logger.debug(f"Archived copy created: {archive_path}")
        
    except Exception as e:
        logger.error(f"Failed to archive {file_path}: {e}")

# Checkpoint Utilities
def load_checkpoint() -> Dict[str, Any]:
    """Load scraping checkpoint if exists."""
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, 'r') as f:
                data = json.load(f)
                logger.info(f"Loaded checkpoint: {data.get('current_commodity', 'None')}")
                return data
        except Exception as e:
            logger.error(f"Error loading checkpoint: {e}")
    return {"current_index": 0, "current_commodity": None, "completed": []}

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
        with open(CHECKPOINT_FILE, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)
        logger.debug(f"Checkpoint saved: index={index}, commodity={commodity_name}")
    except Exception as e:
        logger.error(f"Error saving checkpoint: {e}")

def load_completed_commodities() -> Dict[str, List[str]]:
    """Load previously completed commodities with their link types."""
    if COMPLETED_COMMODITIES_FILE.exists():
        try:
            with open(COMPLETED_COMMODITIES_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading completed commodities: {e}")
    return {}

def save_completed_commodity(commodity_name: str, link_types: List[str]):
    """Save completed commodity with its scraped link types."""
    completed = load_completed_commodities()
    completed[commodity_name] = link_types
    
    try:
        with open(COMPLETED_COMMODITIES_FILE, 'w') as f:
            json.dump(completed, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving completed commodities: {e}")

def is_commodity_complete(commodity_name: str, expected_link_types: List[str]) -> bool:
    """Check if a commodity has been fully scraped."""
    completed = load_completed_commodities()
    if commodity_name not in completed:
        return False
    
    scraped_types = completed[commodity_name]
    # Check if all expected link types are present
    return all(link_type in scraped_types for link_type in expected_link_types)

def cleanup_checkpoint():
    """Remove checkpoint file after successful completion."""
    try:
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
            logger.info("Checkpoint file removed - scraping completed")
    except Exception as e:
        logger.error(f"Error removing checkpoint: {e}")

# Utilities
def sanitize_sheet_name(name: str) -> str:
    """
    Sanitizes commodity names for filesystem safety (CSV filenames).
    """
    name = name.strip().lower()
    name = re.sub(r"[^\w\s-]", "", name)   # remove special chars
    name = re.sub(r"\s+", "_", name)       # spaces → underscores
    return name[:100]

def safe_click(driver, element: WebElement):
    """
    Clicks an element using JavaScript after scrolling it into view.
    Prevents ElementClickInterceptedException.
    """
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", element
    )
    driver.execute_script("arguments[0].click();", element)

def switch_to_iframe(driver, wait: WebDriverWait):
    """
    Safely reset context and re-enter iframe.
    """
    driver.switch_to.default_content()
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
    iframe = driver.find_element(By.TAG_NAME, "iframe")
    driver.switch_to.frame(iframe)
    logger.debug("Switched to iframe")

def wait_for_table_change(driver, wait: WebDriverWait, previous_row_count: int, timeout: int = 10):
    """
    Wait for table content to actually change, not just be present.
    Uses row count as the change indicator.
    """
    def table_has_changed(driver):
        try:
            current_rows = len(driver.find_elements(
                By.CSS_SELECTOR, "table.alltable tbody tr"
            ))
            logger.debug(f"Previous rows: {previous_row_count}, Current rows: {current_rows}")
            return current_rows != previous_row_count and current_rows > 0
        except Exception as e:
            logger.debug(f"Error checking table change: {e}")
            return False
    
    logger.debug(f"Waiting for table to change from {previous_row_count} rows")
    wait.until(table_has_changed)
    # Small buffer for JavaScript finalization
    time.sleep(0.3)

def get_table_row_count(driver) -> int:
    """Get current number of rows in the main table."""
    try:
        return len(driver.find_elements(
            By.CSS_SELECTOR, "table.alltable tbody tr"
        ))
    except:
        return 0

def get_available_links(driver, wait: WebDriverWait) -> Tuple[List[WebElement], Optional[WebElement], Optional[WebElement]]:
    """
    Get all available div > a links on the page and identify container/variety links.
    ALWAYS fetches fresh links - never stores WebElements for reuse.
    """
    try:
        links = wait.until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div > a"))
        )
        
        container_link = None
        variety_link = None
        
        for link in links:
            link_text = link.text.strip()
            
            if CONTAINER_LINK_TEXT in link_text:
                container_link = link
            elif VARIETY_LINK_TEXT in link_text:
                variety_link = link
        
        logger.debug(f"Found links - container: {bool(container_link)}, variety: {bool(variety_link)}")
        return links, container_link, variety_link
        
    except TimeoutException:
        logger.warning("No div > a links found")
        return [], None, None

def find_link_by_text(driver, search_text: str) -> Optional[WebElement]:
    """
    Find a link by text without storing the element.
    """
    try:
        links = driver.find_elements(By.CSS_SELECTOR, "div > a")
        for link in links:
            if search_text in link.text:
                return link
    except:
        pass
    return None

def analyze_summary_table(driver) -> dict:
    """
    Analyze the summary table to determine commodity characteristics.
    """
    analysis = {
        "data_rows": 0,
        "is_single_container": False,
        "table_structure": "unknown"
    }
    
    try:
        table = driver.find_element(By.CSS_SELECTOR, "table.alltable")
        rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        
        # Count rows that look like data (not totals/summaries)
        data_rows = 0
        for row in rows:
            row_text = row.text.lower()
            # Skip rows that are totals or summaries
            if "total" not in row_text and "summary" not in row_text:
                data_rows += 1
        
        analysis["data_rows"] = data_rows
        
        # Determine if single-container
        if data_rows <= 1:
            analysis["is_single_container"] = True
            analysis["table_structure"] = "single_container"
        else:
            analysis["is_single_container"] = False
            analysis["table_structure"] = "multi_container"
            
        logger.info(f"Table analysis: {data_rows} data rows, structure: {analysis['table_structure']}")
        
    except Exception as e:
        logger.error(f"Error analyzing table: {e}")
    
    return analysis

def get_output_directory(link_type: str) -> Path:
    """
    Get the appropriate output directory based on link type.
    
    Args:
        link_type: Type of data ('summary', 'container', 'variety')
    
    Returns:
        Path object for the directory
    """
    if link_type == "summary":
        return SUMMARY_DIR
    elif link_type == "container":
        return CONTAINER_DIR
    elif link_type == "variety":
        return VARIETY_DIR
    else:
        # Default to data directory if unknown type
        logger.warning(f"Unknown link type: {link_type}, using base data directory")
        return BASE_DATA_DIR

def create_filename(commodity_name: str, link_type: str, scraped_date: str) -> str:
    """
    Create a standardized filename with link_type included.
    
    Args:
        commodity_name: Name of the commodity
        link_type: Type of data ('summary', 'container', 'variety')
        scraped_date: Date when data was scraped
        
    Returns:
        Filename string
    """
    safe_name = sanitize_sheet_name(commodity_name)
    
    # Format the date for filename (replace spaces with underscores)
    date_for_filename = re.sub(r'[^\w]', '_', scraped_date)
    
    # Create filename with link_type included
    filename = f"joburg_market_{safe_name}_{link_type}_{date_for_filename}.csv"
    
    return filename

def scrape_and_save_table(driver, scraped_date: str, commodity_name: str, link_type: str) -> Optional[int]:
    """
    Scrape table and save with metadata and ingestion tracking.
    Saves files locally AND uploads to GCP (creates two copies).
    """
    # Get current row count
    previous_rows = get_table_row_count(driver)
    
    # Scrape the table
    df = table_scraper(driver)
    
    if df is None or df.empty:
        logger.warning(f"Empty table for {commodity_name} - {link_type}")
        return previous_rows
    
    # Log how many rows were scraped
    actual_rows = len(df)
    logger.info(f"  Scraped {actual_rows} rows for {link_type}")
    
    # Add metadata
    df["scrape_date"] = scraped_date
    df["commodity"] = commodity_name
    df["link_type"] = link_type
    df["ingestion_run_id"] = INGESTION_RUN_ID
    
    # Get the appropriate output directory
    output_dir = get_output_directory(link_type)
    
    # Ensure directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create filename with link_type included
    filename = create_filename(commodity_name, link_type, scraped_date)
    file_path = output_dir / filename
    
    # Save to local file (FIRST COPY - in repo)
    df.to_csv(file_path, index=False)
    logger.info(f"Saved local copy: {file_path}")
    
    # Create archived copy (optional historical reference)
    archive_file(file_path, link_type)
    
    # Upload to GCP (SECOND COPY - in cloud)
    if upload_to_gcp(file_path, link_type, scraped_date, commodity_name):
        logger.info(f"Created GCP copy for {link_type} data")
    else:
        logger.warning(f"Failed to create GCP copy for {link_type} data")
    
    return previous_rows

def reselect_commodity(driver, wait: WebDriverWait, commodity_name: str):
    """
    Re-select a commodity after navigation.
    """
    switch_to_iframe(driver, wait)
    
    select_el = wait.until(EC.element_to_be_clickable((By.TAG_NAME, "select")))
    select = Select(select_el)
    
    # Find the option that matches our commodity
    options = select_el.find_elements(By.TAG_NAME, "option")
    for idx, option in enumerate(options):
        if option.text.strip() == commodity_name:
            select.select_by_index(idx)
            time.sleep(1)  # Allow page to update
            break

def handle_single_container_flow(driver, wait: WebDriverWait, scraped_date: str, 
                                commodity_name: str, safe_commodity_name: str) -> List[str]:
    """
    Handle single-container commodities.
    Returns list of successfully scraped link types.
    """
    logger.info(f"  Handling single-container commodity")
    scraped_types = ["summary"]
    
    # Try container link if it exists
    container_link = find_link_by_text(driver, CONTAINER_LINK_TEXT)
    if container_link:
        logger.info(f"  Attempting container link...")
        try:
            safe_click(driver, container_link)
            
            # Wait for navigation
            time.sleep(1)
            
            # Scrape container table
            try:
                scrape_and_save_table(driver, scraped_date, safe_commodity_name, "container")
                scraped_types.append("container")
            except Exception as e:
                logger.warning(f"  Could not scrape container table: {e}")
            
            # Go back and reset state
            driver.back()
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            switch_to_iframe(driver, wait)
            
            # Re-select commodity after navigation
            reselect_commodity(driver, wait, commodity_name)
            
            # Wait for page to load
            time.sleep(1)
            
        except Exception as e:
            logger.warning(f"  Container link failed: {e}")
            # Try to recover
            try:
                driver.back()
                switch_to_iframe(driver, wait)
                reselect_commodity(driver, wait, commodity_name)
            except:
                pass
    
    # Try variety link
    variety_link = find_link_by_text(driver, VARIETY_LINK_TEXT)
    if variety_link:
        logger.info(f"  Attempting variety link...")
        try:
            safe_click(driver, variety_link)
            
            # Wait for table to load
            wait.until(lambda d: get_table_row_count(d) > 0)
            scrape_and_save_table(driver, scraped_date, safe_commodity_name, "variety")
            scraped_types.append("variety")
            
            # Go back
            driver.back()
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
        except Exception as e:
            logger.warning(f"  Variety link failed: {e}")
            try:
                driver.back()
            except:
                pass
    else:
        logger.info(f"  No variety link available")
    
    return scraped_types

def handle_multi_container_flow(driver, wait: WebDriverWait, scraped_date: str,
                               commodity_name: str, safe_commodity_name: str, 
                               previous_rows: int) -> List[str]:
    """
    Handle multi-container commodities.
    Returns list of successfully scraped link types.
    """
    logger.info(f"  Handling multi-container commodity")
    scraped_types = ["summary"]
    
    # Container link is essential
    container_link = find_link_by_text(driver, CONTAINER_LINK_TEXT)
    if not container_link:
        logger.warning(f"  No container link found")
        return scraped_types
    
    logger.info(f"  Clicking container link...")
    try:
        safe_click(driver, container_link)
        
        # Wait for table to change
        wait_for_table_change(driver, wait, previous_rows)
        scrape_and_save_table(driver, scraped_date, safe_commodity_name, "container")
        scraped_types.append("container")
        
        # Go back and reset
        driver.back()
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        switch_to_iframe(driver, wait)
        
        # Re-select commodity
        reselect_commodity(driver, wait, commodity_name)
        
        # Wait for page to load
        time.sleep(1)
        
    except Exception as e:
        logger.error(f"  Container link failed: {e}")
        return scraped_types
    
    # Try variety link
    variety_link = find_link_by_text(driver, VARIETY_LINK_TEXT)
    if variety_link:
        logger.info(f"  Clicking variety link...")
        try:
            safe_click(driver, variety_link)
            
            # Wait for table to load
            wait.until(lambda d: get_table_row_count(d) > 0)
            scrape_and_save_table(driver, scraped_date, safe_commodity_name, "variety")
            scraped_types.append("variety")
            
            # Go back
            driver.back()
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
        except Exception as e:
            logger.warning(f"  Variety link failed: {e}")
            try:
                driver.back()
            except:
                pass
    else:
        logger.info(f"  No variety link available")
    
    return scraped_types

def handle_div_links_in_iframe(driver, wait: WebDriverWait):
    """
    Main scraping function with checkpointing.
    Creates TWO copies of each file: one in repo, one in GCP.
    """
    logger.info(f"Starting ingestion run: {INGESTION_RUN_ID}")
    logger.info("=" * 60)
    logger.info("DUAL COPY STRATEGY: Files will be saved in BOTH repo and GCP")
    logger.info("=" * 60)
    
    # Log GCP status
    if GCP_UPLOAD_ENABLED:
        if GCP_AVAILABLE:
            logger.info(f"GCP upload enabled. Bucket: {GCP_BUCKET_NAME}")
            # Test GCP connection
            client = get_gcp_client()
            if client:
                try:
                    # Try to access bucket
                    bucket = client.bucket(GCP_BUCKET_NAME)
                    bucket.exists()  # This will test connectivity
                    logger.info(" GCP connection test successful")
                except Exception as e:
                    logger.warning(f" GCP connection test failed: {e}. Uploads may fail.")
        else:
            logger.warning("GCP libraries not available. Install with: pip install google-cloud-storage")
    else:
        logger.info("GCP upload disabled - only local copies will be saved")
    
    # Load checkpoint state
    checkpoint = load_checkpoint()
    start_index = checkpoint.get("current_index", 0)
    completed_commodities = checkpoint.get("completed", [])
    
    logger.info(f"Resuming from index {start_index}")
    
    # Initial iframe entry
    switch_to_iframe(driver, wait)

    # Find commodity selector
    select_el = wait.until(EC.element_to_be_clickable((By.TAG_NAME, "select")))
    options = select_el.find_elements(By.TAG_NAME, "option")
    option_count = len(options)
    
    logger.info(f"Found {option_count - 1} commodities to process")
    
    # Process each commodity starting from checkpoint
    for i in range(start_index + 1, option_count):
        commodity_name = None
        
        try:
            # Fresh state for each commodity
            switch_to_iframe(driver, wait)
            
            # Get commodity info
            select_el = wait.until(EC.element_to_be_clickable((By.TAG_NAME, "select")))
            select = Select(select_el)
            
            # Extract commodity name
            options = select_el.find_elements(By.TAG_NAME, "option")
            commodity_name = options[i].text.strip()
            safe_commodity_name = sanitize_sheet_name(commodity_name)
            
            # Skip if already completed
            if commodity_name in completed_commodities:
                logger.info(f"\n=== Skipping {i}/{option_count-1}: {commodity_name} (already completed) ===")
                continue
            
            logger.info(f"\n=== Processing {i}/{option_count-1}: {commodity_name} ===")
            
            # Save checkpoint BEFORE processing
            save_checkpoint(i - 1, commodity_name, completed_commodities)
            
            # Select the commodity
            select.select_by_index(i)
            
            # Wait for page to update
            time.sleep(1.5)
            
            # Scrape date
            scraped_date = scrape_date(driver)
            logger.info(f"  Date: {scraped_date}")
            
            # Step 1: Analyze the summary table
            table_analysis = analyze_summary_table(driver)
            
            # Step 2: Always scrape the summary table first
            previous_rows = scrape_and_save_table(driver, scraped_date, safe_commodity_name, "summary")
            
            # Step 3: Handle based on table structure
            if table_analysis["is_single_container"]:
                scraped_types = handle_single_container_flow(driver, wait, scraped_date, 
                                                            commodity_name, safe_commodity_name)
            else:
                scraped_types = handle_multi_container_flow(driver, wait, scraped_date,
                                                           commodity_name, safe_commodity_name, previous_rows)
            
            # Mark as completed
            completed_commodities.append(commodity_name)
            save_completed_commodity(commodity_name, scraped_types)
            
            logger.info(f"✓ Completed: {commodity_name}")
            
        except TimeoutException as e:
            logger.error(f"Timeout processing {commodity_name or 'unknown'}: {e}")
            try:
                driver.back()
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except:
                pass
                
        except Exception as e:
            logger.error(f"Error processing {commodity_name or 'unknown'}: {e}")
            logger.exception(e)  # Log full traceback for debugging
            
            # Save checkpoint at failure point for recovery
            if commodity_name:
                save_checkpoint(i - 1, commodity_name, completed_commodities)
            
            try:
                driver.back()
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except:
                pass
            
            # Re-raise to stop execution (user can restart from checkpoint)
            raise
    
    # Cleanup after successful completion
    cleanup_checkpoint()
    
    # Final sync of all files to GCP (safety net)
    if GCP_UPLOAD_ENABLED and GCP_AVAILABLE:
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
    
    logger.info(f"\n Completed ingestion run: {INGESTION_RUN_ID}")
    
    # Print dual copy summary
    print_dual_copy_summary()

def print_dual_copy_summary():
    """Print a summary of files saved in both locations."""
    logger.info("\n" + "=" * 60)
    logger.info("DUAL COPY SUMMARY")
    logger.info("=" * 60)
    
    # Local files summary
    logger.info("\nLOCAL COPIES (in repository):")
    local_total = 0
    
    for dir_name, directory in [("Summary", SUMMARY_DIR), 
                                ("Container", CONTAINER_DIR), 
                                ("Variety", VARIETY_DIR)]:
        if directory.exists():
            csv_files = list(directory.glob("*.csv"))
            if csv_files:
                logger.info(f"  {dir_name}: {len(csv_files)} files")
                local_total += len(csv_files)
            else:
                logger.info(f"  {dir_name}: No files")
        else:
            logger.info(f"  {dir_name}: Directory does not exist")
    
    logger.info(f"  Total local files: {local_total}")
    
    # GCP status
    if GCP_UPLOAD_ENABLED and GCP_AVAILABLE:
        logger.info("\nGCP COPIES (in cloud storage):")
        logger.info(f"  Bucket: {GCP_BUCKET_NAME}")
        if GCP_BUCKET_PATH:
            logger.info(f"  Path: {GCP_BUCKET_PATH}/")
        logger.info("  Files organized by: summary/, container/, variety/")
        
        # Note: We can't count GCP files without listing them
        client = get_gcp_client()
        if client:
            try:
                bucket = client.bucket(GCP_BUCKET_NAME)
                gcp_count = 0
                for prefix in ["summary", "container", "variety"]:
                    if GCP_BUCKET_PATH:
                        prefix_path = f"{GCP_BUCKET_PATH}/{prefix}/"
                    else:
                        prefix_path = f"{prefix}/"
                    
                    blobs = list(bucket.list_blobs(prefix=prefix_path))
                    if blobs:
                        # Count only CSV files
                        csv_blobs = [b for b in blobs if b.name.endswith('.csv')]
                        gcp_count += len(csv_blobs)
                        logger.info(f"  {prefix.capitalize()}: {len(csv_blobs)} files")
                
                logger.info(f"  Total GCP files: {gcp_count}")
                
                # Compare counts
                if local_total == gcp_count:
                    logger.info("\n SUCCESS: All local files have GCP copies!")
                elif gcp_count < local_total:
                    logger.warning(f"\n WARNING: {local_total - gcp_count} local files missing GCP copies")
                else:
                    logger.info(f"\n GCP has {gcp_count - local_total} additional files")
                    
            except Exception as e:
                logger.warning(f"  Could not list GCP files: {e}")
    else:
        logger.info("\nGCP COPIES: Not enabled or available")
    
    logger.info("=" * 60)
    logger.info("DUAL COPY STRATEGY COMPLETE")
    logger.info("=" * 60)

# GitHub Actions compatible function
def run_scraping_with_dual_copies(driver, wait: WebDriverWait):
    """
    Run scraping with dual copies for GitHub Actions.
    """
    try:
        handle_div_links_in_iframe(driver, wait)
        return True
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        return False

# Sync existing files to GCP
def sync_existing_files_to_gcp():
    """
    Sync all existing local files to GCP.
    Useful for initial setup or recovery.
    """
    if not GCP_UPLOAD_ENABLED or not GCP_AVAILABLE:
        logger.error("GCP upload not enabled or available")
        return False
    
    logger.info("=" * 60)
    logger.info("SYNCING EXISTING FILES TO GCP")
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
            
            if files:
                logger.info(f"\n{dir_name} uploaded files:")
                for file in files[:5]:  # Show first 5
                    logger.info(f"  • {file}")
                if len(files) > 5:
                    logger.info(f"  ... and {len(files) - 5} more")
    
    logger.info(f"\n Sync complete: {total_uploaded}/{total_files} files synced to GCP")
    logger.info("=" * 60)
    
    return total_uploaded == total_files

if __name__ == "__main__":
    # For testing
    import argparse
    
    parser = argparse.ArgumentParser(description="Manage dual copy scraping strategy")
    parser.add_argument("--sync-existing", action="store_true", help="Sync existing local files to GCP")
    parser.add_argument("--summary", action="store_true", help="Print summary of local and GCP files")
    
    args = parser.parse_args()
    
    if args.sync_existing:
        sync_existing_files_to_gcp()
        
    elif args.summary:
        print_dual_copy_summary()