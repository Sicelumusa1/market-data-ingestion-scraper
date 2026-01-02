from bs4 import BeautifulSoup

def scrape_date(driver):
    """Scrapes the date from the iframe."""
    soup = BeautifulSoup(driver.page_source, "html.parser")
    right_div = soup.find("div", id="right2")

    if not right_div:
        return None

    date_element = right_div.find("b")
    return date_element.get_text(strip=True) if date_element else None