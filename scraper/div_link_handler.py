import pandas as pd
import re
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from scraper.table_scraper import table_scraper
from scraper.date_scraper import scrape_date

def scrape_and_save_table(driver, date, sheet_name, option_name=None):
    """Scrapes the table and saves it to a new sheet, with optional inclusion of the option name."""
    pass


def sanitize_sheet_name(name):
    """
    Sanitizes the commodity name to be used as a valid Excel sheet name.
    Replaces or removes problematic characters and ensures the length is valid.
    """
    pass


def handle_div_links_in_iframe(driver, wait):
    """Handles clicking div > a elements within the iframe and scraping tables."""
    pass