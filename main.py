import os
import sys
import logging
import argparse
from dotenv import load_dotenv
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from scraper.date_scraper import scrape_date
from scraper.form_handler import top_five
from scraper.div_link_handler import run_scraping_with_retries, cleanup_status_files

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

def setup_driver(headless: bool = False) -> webdriver.Chrome:
    """Setup Chrome driver with appropriate options."""
    options = Options()
    
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
    
    options.add_argument("--start-maximized")
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    return driver

def check_environment():
    """Check if all required environment variables are set."""
    required_vars = ["TARGET_URL"]
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        return False
    
    if os.getenv("GCP_UPLOAD_ENABLED", "false").lower() == "true":
        if not os.getenv("GCP_BUCKET_NAME"):
            logger.warning("GCP_UPLOAD_ENABLED is true but GCP_BUCKET_NAME is not set")
    
    return True

def create_required_directories():
    """Create all required directories for the scraper."""
    
    
    directories = [
        Path("data/summary"),
        Path("data/container"),
        Path("data/variety"),
        Path("archive/summary"),
        Path("archive/container"),
        Path("archive/variety"),
        Path("logs"),
        Path("checkpoints"),
    ]
    
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured directory exists: {directory}")

def print_startup_banner(args):
    """Print a nice startup banner with configuration."""
    logger.info("=" * 60)
    logger.info("JOBURG MARKET SCRAPER - STARTING UP")
    logger.info("=" * 60)
    
    mode = "HEADLESS" if os.getenv("HEADLESS", "false").lower() == "true" else "VISIBLE"
    logger.info(f"Mode: {mode}")
    
    gcp_enabled = os.getenv("GCP_UPLOAD_ENABLED", "false").lower() == "true"
    logger.info(f"GCP Upload: {'ENABLED' if gcp_enabled else 'DISABLED'}")
    if gcp_enabled and os.getenv("GCP_BUCKET_NAME"):
        logger.info(f"GCP Bucket: {os.getenv('GCP_BUCKET_NAME')}")
    
    logger.info(f"Max Retries per commodity: {os.getenv('MAX_RETRIES', '3')}")
    logger.info(f"Max Scraping Passes: {os.getenv('MAX_SCRAPING_PASSES', '3')}")
    logger.info(f"Status Max Age: {os.getenv('STATUS_MAX_AGE_HOURS', '24')} hours")
    
    if args.fresh:
        logger.info("FLAG: --fresh - Starting completely fresh (ignoring all status files)")
    if args.reset:
        logger.info("FLAG: --reset - Resetting all status files")
    
    logger.info("=" * 60)

def main():
    """Main entry point for the scraper."""
    parser = argparse.ArgumentParser(description='Joburg Market Scraper')
    parser.add_argument('--fresh', action='store_true',
                       help='Start fresh, ignore all existing status files')
    parser.add_argument('--reset', action='store_true',
                       help='Delete all status files and exit')
    parser.add_argument('--headless', action='store_true',
                       help='Run in headless mode (overrides env var)')
    args = parser.parse_args()
    
    # Load environment variables
    load_dotenv()
    
    # Handle reset flag (deletes everything and exits)
    if args.reset:
        logger.info("Reset flag detected - cleaning up all status files")
        if cleanup_status_files(force=True):
            logger.info("All status files deleted successfully")
        else:
            logger.error("Failed to delete some status files")
        sys.exit(0)
    
    # Check environment
    if not check_environment():
        sys.exit(1)
    
    # Create required directories
    create_required_directories()
    
    # Print startup banner
    print_startup_banner(args)
    
    # Get target URL
    target_url = os.getenv("TARGET_URL")
    
    # Setup driver (headless mode for production/CI)
    headless = args.headless or os.getenv("HEADLESS", "false").lower() == "true"
    driver = setup_driver(headless)
    wait = WebDriverWait(driver, int(os.getenv("DEFAULT_TIMEOUT", "20")))
    
    try:
        logger.info(f"Navigating to {target_url}")
        driver.get(target_url)
        
        logger.info("Switching to iframe...")
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
        driver.switch_to.frame(iframe)
        
        logger.info("Scraping current date...")
        current_date = scrape_date(driver)
        logger.info(f"Current date: {current_date}")
        
        logger.info("Processing top five commodities form...")
        top_five(driver, wait)
        
        logger.info("Starting main scraping with retry logic...")
        success = run_scraping_with_retries(driver, wait, fresh_start=args.fresh)
        
        if success:
            logger.info("=" * 60)
            logger.info("SCRAPING COMPLETED")
            logger.info("=" * 60)
        else:
            logger.error("Scraping failed with unrecoverable errors")
            sys.exit(1)
    
    except KeyboardInterrupt:
        logger.info("Scraping interrupted by user")
        sys.exit(130)
    
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        logger.exception(e)
        sys.exit(1)
    
    finally:
        logger.info("Closing browser...")
        driver.quit()
        logger.info("Scraper finished")

if __name__ == '__main__':
    main()