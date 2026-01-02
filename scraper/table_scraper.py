from bs4 import BeautifulSoup
import pandas as pd

def table_scraper(driver):
    """Function to scrape the table data."""
    html_content = driver.page_source
    soup = BeautifulSoup(html_content, 'html.parser')
    
    table = soup.find('table', class_='alltable')

    if not table or not table.find("thead") or not table.find("tbody"):
        return None
    
    if table:
        # Extract table headers
        headers = [th.get_text(strip=True) for th in table.find('thead').find_all('th', class_='header')]
        
        # Initialize a list to hold the table rows
        rows_data = []
        
        # Iterate over rows in the table body
        rows = table.find('tbody').find_all('tr')
        for row in rows:
            # Extract the first column (td with class 'tleft2')
            row_data = []
            first_column = row.find('td', class_='tleft2')
            if first_column:
                row_data.append(first_column.get_text(strip=True))
            
            # Extract the remaining columns (td with class 'tleft')
            other_columns = row.find_all('td', class_='tleft')
            for col in other_columns:
                row_data.append(col.get_text(strip=True))
            
            rows_data.append(row_data)
        
        # Create a pandas DataFrame with the extracted data
        df = pd.DataFrame(rows_data, columns=headers)
        
        return df
    else:
        print("Table with class 'alltable' not found.")
        return None