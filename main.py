import os
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from scraper.date_scraper import scrape_date
from scraper.form_handler import top_five
from scraper.div_link_handler import handle_div_links_in_iframe


def main():

    load_dotenv()

    target_url = os.getenv("TARGET_URL")
    if not target_url:
        raise RuntimeError("TARGET_URL is not set")

    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 10)

    try:
        driver.get(target_url)
        
        # Switch to the iframe
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'iframe')))
        driver.switch_to.frame(iframe)
        
        # Scrape the date
        scrape_date(driver)
        
        # Handle the form submission and top five commodities
        top_five(driver, wait)
        
        # Handle clicking div > a links and scraping tables
        handle_div_links_in_iframe(driver, wait)
    
    finally:
        driver.quit()

if __name__ == '__main__':
    main()
