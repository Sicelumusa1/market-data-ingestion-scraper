import pandas as pd
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from scraper.table_scraper import table_scraper


def top_five(driver, wait):
    """Locates the form, submits it, and scrapes the top five commodities."""
    pass