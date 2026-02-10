import os
import logging
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    load_dotenv()

    target_url = os.getenv("TARGET_URL")
    if not target_url:
        raise RuntimeError("TARGET_URL is not set")
    
    driver = None
    try:
        # Import date checker functions
        from github_date_checker import (
            check_date_with_driver, 
            get_driver_for_scraping,
            setup_chrome_driver
        )
        
        # Step 1: Setup Chrome driver (using the same setup as date checker)
        logger.info("Setting up Chrome driver...")
        driver = setup_chrome_driver()
        
        # Check date with the existing driver
        logger.info("Checking date...")
        exit_code, current_date, should_scrape = check_date_with_driver(driver, target_url)
        
        if exit_code == 2:
            # Error in date checking
            raise RuntimeError("Date check failed")
        
        if not should_scrape:
            logger.info("Date unchanged - exiting without scraping")
            return  # Exit early if date hasn't changed
        
        # If we should scrape, reuse the same driver
        logger.info("Date is new - continuing with scraping using the same Chrome session...")
        
        # Import scraping modules
        from scraper.form_handler import top_five
        from scraper.div_link_handler import handle_div_links_in_iframe
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
        
        # The driver is already on the page from date checking
        # But ensure we're at the base URL (not in an iframe)
        driver.switch_to.default_content()
        
        # Switch to the iframe
        wait = WebDriverWait(driver, 10)
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
        driver.switch_to.frame(iframe)
        
        # Handle the form submission and top five commodities
        top_five(driver, wait)
        
        # Handle clicking div > a links and scraping tables
        handle_div_links_in_iframe(driver, wait)
        
        logger.info("Scraping completed successfully")
    
    except Exception as e:
        logger.error(f"Error during scraping: {e}")
        raise
    
    finally:
        if driver:
            driver.quit()
            logger.info("Chrome driver closed")

if __name__ == '__main__':
    main()