"""
Standalone date checker for GitHub Actions.
Checks if website date is new and triggers main.py if needed.
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime
import logging
import re

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
DATES_FILE = Path("dates.txt")
CHECK_RESULT_FILE = Path("date_check_result.json")

def ensure_dates_file():
    """Ensure dates.txt exists."""
    if not DATES_FILE.exists():
        DATES_FILE.touch()
        logger.info(f"Created {DATES_FILE}")
    return DATES_FILE.exists()

def read_last_date():
    """Read the last date from dates.txt."""
    try:
        if not DATES_FILE.exists():
            return None
        
        with open(DATES_FILE, 'r') as f:
            lines = [line.strip() for line in f if line.strip()]
        
        return lines[-1] if lines else None
    except Exception as e:
        logger.error(f"Error reading dates.txt: {e}")
        return None

def save_new_date(date_str):
    """Save a new date to dates.txt."""
    try:
        with open(DATES_FILE, 'a') as f:
            f.write(f"{date_str}\n")
        logger.info(f"Saved new date to {DATES_FILE}: {date_str}")
        return True
    except Exception as e:
        logger.error(f"Error saving date: {e}")
        return False

def normalize_date(date_str):
    """
    Normalize date string for comparison.
    Handles format like "2 February 2026".
    """
    try:
        # Clean the string
        date_str = date_str.strip()
        
        # Remove ordinal suffixes (st, nd, rd, th)
        date_str = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', date_str)
        
        # Parse the date
        return datetime.strptime(date_str, "%d %B %Y")
    except ValueError:
        # If basic parsing fails, try with month abbreviations
        month_map = {
            'Jan': 'January', 'Feb': 'February', 'Mar': 'March',
            'Apr': 'April', 'May': 'May', 'Jun': 'June',
            'Jul': 'July', 'Aug': 'August', 'Sep': 'September',
            'Oct': 'October', 'Nov': 'November', 'Dec': 'December',
            'Sept': 'September'  # Handle both Sep and Sept
        }
        
        for abbr, full in month_map.items():
            if abbr in date_str:
                normalized = date_str.replace(abbr, full)
                try:
                    return datetime.strptime(normalized, "%d %B %Y")
                except ValueError:
                    continue
        
        logger.error(f"Could not parse date: {date_str}")
        return None

def compare_dates(old_date_str, new_date_str):
    """
    Compare two date strings.
    Returns True if different, False if same.
    """
    if not old_date_str or not new_date_str:
        return True  # If either is missing, consider different
    
    # First, simple string comparison
    if old_date_str == new_date_str:
        return False
    
    # Try normalized comparison
    old_dt = normalize_date(old_date_str)
    new_dt = normalize_date(new_date_str)
    
    if old_dt and new_dt:
        return old_dt.date() != new_dt.date()
    
    # Fallback to string comparison
    return old_date_str != new_date_str

def setup_chrome_driver():
    """Setup Chrome driver with fallback options."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        
        chrome_options = Options()
        
        # Add basic options
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Try different approaches to get ChromeDriver
        driver = None
        
        try:
            # Try with webdriver_manager
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info("Using webdriver-manager for ChromeDriver")
            
        except ImportError:
            logger.warning("webdriver-manager not available, trying system ChromeDriver")
            # Fallback to system ChromeDriver
            service = Service()
            driver = webdriver.Chrome(service=service, options=chrome_options)
            
        except Exception as e:
            logger.warning(f"webdriver-manager failed: {e}, trying without service")
            # Final fallback
            driver = webdriver.Chrome(options=chrome_options)
        
        # Set page load timeout
        if driver:
            driver.set_page_load_timeout(30)
        
        return driver
        
    except Exception as e:
        logger.error(f"Failed to setup Chrome driver: {e}")
        raise

def scrape_date_from_website(driver, url):
    """Scrape date from website using the existing scrape_date function or fallback."""
    try:
        # Navigate to URL
        logger.info(f"Navigating to: {url}")
        driver.get(url)
        
        # Wait for page to load
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
        
        wait = WebDriverWait(driver, 20)
        
        # Try to switch to iframe
        try:
            iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
            driver.switch_to.frame(iframe)
            logger.info("Switched to iframe")
        except:
            logger.info("No iframe found, continuing with main page")
        
        # Try to import and use existing scrape_date function
        date_str = None
        try:
            # Try to import from your scraper module
            from scraper.date_scraper import scrape_date
            date_str = scrape_date(driver)
            if date_str:
                logger.info(f"Used scraper.date_scraper: {date_str}")
        except ImportError:
            logger.warning("scraper.date_scraper not found, using fallback method")
        
        # If scrape_date didn't work or returned None, use fallback
        if not date_str:
            try:
                # Fallback: Try to find date manually
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(driver.page_source, "html.parser")
                right_div = soup.find("div", id="right2")
                
                if right_div:
                    date_element = right_div.find("b")
                    if date_element:
                        date_str = date_element.get_text(strip=True)
                        logger.info(f"Used fallback method: {date_str}")
            except Exception as e:
                logger.error(f"Fallback method failed: {e}")
        
        return date_str
    
    except Exception as e:
        logger.error(f"Error scraping date: {e}")
        return None
    finally:
        # Always switch back to default content
        try:
            driver.switch_to.default_content()
        except:
            pass

def save_check_result(result_data):
    """Save check result to JSON file."""
    try:
        with open(CHECK_RESULT_FILE, 'w') as f:
            json.dump(result_data, f, indent=2)
        logger.info(f"Saved check result to {CHECK_RESULT_FILE}")
    except Exception as e:
        logger.error(f"Error saving check result: {e}")

# INTEGRATION WITH MAIN.PY


def check_date_with_driver(driver, target_url):
    """
    Check if date has changed using an existing driver instance.
    
    Args:
        driver: Existing Selenium WebDriver instance
        target_url: URL to check
        
    Returns:
        tuple: (exit_code, current_date, should_run_scraper)
            exit_code: 0=unchanged, 1=new date, 2=error
            current_date: The date scraped from website
            should_run_scraper: True if main scraper should run
    """
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
        
        wait = WebDriverWait(driver, 20)
        
        # Navigate to URL
        logger.info(f"Navigating to: {target_url}")
        driver.get(target_url)
        
        # Wait for page to load
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        # Try to switch to iframe
        try:
            iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
            driver.switch_to.frame(iframe)
            logger.info("Switched to iframe")
        except:
            logger.info("No iframe found, continuing with main page")
        
        # Try to import and use existing scrape_date function
        current_date = None
        try:
            # Try to import from your scraper module
            from scraper.date_scraper import scrape_date
            current_date = scrape_date(driver)
            if current_date:
                logger.info(f"Used scraper.date_scraper: {current_date}")
        except ImportError:
            logger.warning("scraper.date_scraper not found, using fallback method")
        
        # If scrape_date didn't work or returned None, use fallback
        if not current_date:
            try:
                # Fallback: Try to find date manually
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(driver.page_source, "html.parser")
                right_div = soup.find("div", id="right2")
                
                if right_div:
                    date_element = right_div.find("b")
                    if date_element:
                        current_date = date_element.get_text(strip=True)
                        logger.info(f"Used fallback method: {current_date}")
            except Exception as e:
                logger.error(f"Fallback method failed: {e}")
        
        if not current_date:
            logger.error("Failed to scrape date from website")
            return 2, None, False
        
        logger.info(f"Current website date: {current_date}")
        
        # Switch back to default content for consistency
        try:
            driver.switch_to.default_content()
        except:
            pass
        
        # Get last stored date
        last_date = read_last_date()
        logger.info(f"Last stored date: {last_date or 'None (first run)'}")
        
        # Compare dates
        is_new_date = compare_dates(last_date, current_date)
        
        # Prepare result
        result = {
            "timestamp": datetime.now().isoformat(),
            "last_stored_date": last_date,
            "current_website_date": current_date,
            "is_new_date": is_new_date,
            "action_required": is_new_date,
            "message": ""
        }
        
        if not last_date:
            # First run
            save_new_date(current_date)
            result["message"] = f"First run - saved date: {current_date}"
            result["action_required"] = True
            logger.info(result["message"])
            should_run_scraper = True
            
        elif is_new_date:
            # New date found
            save_new_date(current_date)
            result["message"] = f"New date found: {current_date} (was: {last_date})"
            logger.info(result["message"])
            should_run_scraper = True
            
        else:
            # Date unchanged
            result["message"] = f"Date unchanged: {current_date}"
            logger.info(result["message"])
            should_run_scraper = False
        
        # Save result
        save_check_result(result)
        
        # Show what will happen
        print("\n" + "=" * 60)
        if should_run_scraper:
            print("RESULT: NEW DATE DETECTED")
            print(f"   Last date:    {last_date or 'None'}")
            print(f"   Current date: {current_date}")
            print("   Action:       Continue with scraping")
            print("=" * 60)
            return 1, current_date, True
        else:
            print("RESULT: DATE UNCHANGED")
            print(f"   Last date:    {last_date}")
            print(f"   Current date: {current_date}")
            print("   Action:       Exiting without scraping")
            print("=" * 60)
            return 0, current_date, False
            
    except Exception as e:
        logger.error(f"Error in date check: {e}")
        import traceback
        traceback.print_exc()
        return 2, None, False

def get_driver_for_scraping():
    """
    Get a Chrome driver configured for scraping.
    This uses the same setup as the date checker.
    """
    return setup_chrome_driver()

# ORIGINAL MAIN FUNCTION (for standalone use)

def main():
    """Main function for standalone date checker."""
    logger.info("=" * 60)
    logger.info("GitHub Actions Date Checker")
    logger.info("=" * 60)
    
    # Get URL from environment variable or .env file
    from dotenv import load_dotenv
    load_dotenv()
    
    target_url = os.getenv("TARGET_URL")
    if not target_url:
        logger.error("TARGET_URL environment variable is not set")
        logger.info("Please set TARGET_URL in .env file or environment variable")
        return 2  # Error code
    
    logger.info(f"Target URL: {target_url}")
    
    # Ensure dates.txt exists
    ensure_dates_file()
    
    # Get last stored date
    last_date = read_last_date()
    logger.info(f"Last stored date: {last_date or 'None (first run)'}")
    
    driver = None
    try:
        # Setup driver
        driver = setup_chrome_driver()
        logger.info("Chrome driver setup successful")
        
        # Scrape current date
        current_date = scrape_date_from_website(driver, target_url)
        
        if not current_date:
            logger.error("Failed to scrape date from website")
            return 2
        
        logger.info(f"Current website date: {current_date}")
        
        # Compare dates
        is_new_date = compare_dates(last_date, current_date)
        
        # Prepare result
        result = {
            "timestamp": datetime.now().isoformat(),
            "last_stored_date": last_date,
            "current_website_date": current_date,
            "is_new_date": is_new_date,
            "action_required": is_new_date,
            "message": ""
        }
        
        if not last_date:
            # First run
            save_new_date(current_date)
            result["message"] = f"First run - saved date: {current_date}"
            result["action_required"] = True  # Always run on first execution
            logger.info(result["message"])
            
        elif is_new_date:
            # New date found
            save_new_date(current_date)
            result["message"] = f"New date found: {current_date} (was: {last_date})"
            logger.info(result["message"])
            
        else:
            # Date unchanged
            result["message"] = f"Date unchanged: {current_date}"
            logger.info(result["message"])
        
        # Save result
        save_check_result(result)
        
        # Show what will happen
        print("\n" + "=" * 60)
        if result["action_required"]:
            print("RESULT: NEW DATE DETECTED")
            print(f"   Last date:    {last_date or 'None'}")
            print(f"   Current date: {current_date}")
            print(f"   Action:       Run main.py scraper")
            print("=" * 60)
            return 1
        else:
            print("RESULT: DATE UNCHANGED")
            print(f"   Last date:    {last_date}")
            print(f"   Current date: {current_date}")
            print(f"   Action:       Skip scraping")
            print("=" * 60)
            return 0
            
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 2
        
    finally:
        if driver:
            try:
                driver.quit()
                logger.info("Chrome driver closed")
            except:
                pass

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)