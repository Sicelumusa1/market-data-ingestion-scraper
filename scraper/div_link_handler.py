import pandas as pd
import re
import logging
from pathlib import Path
from datetime import date as dt_date
from typing import Tuple, Optional, List, Dict, Any
import time
import json

from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, NoSuchElementException
from selenium.webdriver.remote.webelement import WebElement

from scraper.table_scraper import table_scraper
from scraper.date_scraper import scrape_date


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


def scrape_and_save_table(driver, scraped_date: str, commodity_name: str, link_type: str) -> Optional[int]:
    """
    Scrape table and save with metadata and ingestion tracking.
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
    
    # Save to file
    output_dir = Path("data/raw")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    safe_name = sanitize_sheet_name(commodity_name)
    filename = output_dir / f"joburg_market_{safe_name}_{link_type}_{scraped_date}.csv"
    df.to_csv(filename, index=False)
    
    logger.info(f"  Saved: {filename}")
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
    """
    logger.info(f"Starting ingestion run: {INGESTION_RUN_ID}")
    
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
    logger.info(f"Completed ingestion run: {INGESTION_RUN_ID}")