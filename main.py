import os
import csv
import re
import html
import requests
import xml.etree.ElementTree as ET
import boto3
from botocore.exceptions import ClientError

# Konfiguracja ze zmiennych środowiskowych
XML_URL = os.environ.get('IDOSELL_XML_URL')
R2_ENDPOINT_URL = os.environ.get('R2_ENDPOINT_URL')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'openai-product-feed')
CSV_FILENAME = 'feed.csv'

# Ścieżki do plików tymczasowych na dysku
TEMP_XML_FILE = 'temp_feed.xml'
TEMP_CSV_FILE = 'temp_feed.csv'

def clean_html(raw_html):
    """Błyskawiczne czyszczenie HTML z minimalnym zużyciem pamięci RAM"""
    if not raw_html:
        return ""
    text = re.sub(r'</?(?:p|div|h\d|li|br|ul|ol|tr|td)[^>]*>', ' ', raw_html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return " ".join(text.split())

def download_xml_stream(url, target_path):
    """Pobiera plik XML strumieniowo na dysk"""
    print("Pobieranie pliku XML IOF z IdoSell...")
    with requests.get(url, stream=True) as response:
        response.raise_for_status()
        with open(target_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    print("Pobrano plik XML na dysk.")

def convert_xml_to_csv_stream(xml_path, csv_path):
    """Strumieniowe parsowanie formatu IOF IdoSell"""
    print("Rozpoczynanie konwersji IOF XML -> CSV...")
    
    # Skrócona lista nagłówków (usunięto gender, material, age_group)
    fieldnames = [
        'item_id', 'title', 'description', 'url', 'brand', 'image_url', 
        'additional_image_urls', 'price', 'sale_price', 'availability', 
        'condition', 'product_category', 'color', 
        'seller_name', 'seller_url', 'return_policy', 
        'target_countries', 'store_country', 
        'is_eligible_search', 'is_eligible_checkout', 'is_ads_eligible'
    ]
    
    count = 0
    with open(csv_path, 'w', newline='', encoding='utf-8') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        
        context = ET.iterparse(xml_path, events=('start', 'end'))
        _, root = next(context)
        
        for event, elem in context:
            # W formacie IOF produkty znajdują się w tagu <product>
            if event == 'end' and elem.tag == 'product':
                
                # Podstawowe identyfikatory i waluta
                item_id = elem.attrib.get('id', '')
                currency = elem.attrib.get('currency', 'PLN')
                
                # Wyciąganie tytułu i opisu
                desc_node = elem.find('description')
                title = ""
                description = ""
                if desc_node is not None:
                    name_node = desc_node.find('name')
                    if name_node is not None and name_node.text:
                        title = name_node.text.strip()
                        
                    long_desc_node = desc_node.find('long_desc')
                    if long_desc_node is not None and long_desc_node.text:
                        description = clean_html(long_desc_node.text)

                # Link do produktu
                card_node = elem.find('card')
                url = card_node.attrib.get('url', '') if card_node is not None else ""

                # Marka (Producent)
                prod_node = elem.find('producer')
                brand = prod_node.attrib.get('name', '') if prod_node is not None else ""
                
                # Kategoria
                cat_node = elem.find('category')
                product_category = cat_node.attrib.get('name', '') if cat_node is not None else ""

                # Zdjęcia (główne i dodatkowe)
                images_node = elem.find('./images/large')
                all_imgs = [img.attrib.get('url') for img in images_node.findall('image') if img.attrib.get('url')] if images_node is not None else []
                image_url = all_imgs[0] if all_imgs else ""
                additional_image_urls = ",".join(all_imgs[1:]) if len(all_imgs) > 1 else ""

                # ZARZĄDZANIE CENĄ (Rozpoznawanie promocji)
                price_node = elem.find('price')
                strike_node = elem.find('strikethrough_retail_price')
                
                current_price = price_node.attrib.get('gross', '') if price_node is not None else ""
                strike_price = strike_node.attrib.get('gross', '') if strike_node is not None else ""
                
                if strike_price:
                    # Produkt jest w promocji
                    final_price = f"{strike_price} {currency}"
                    final_sale_price = f"{current_price} {currency}"
                else:
                    # Produkt w cenie regularnej
                    final_price = f"{current_price} {currency}"
                    final_sale_price = ""

                # Dostępność
                sizes_node = elem.find('./sizes/size')
                availability = sizes_node.attrib.get('available', 'in_stock') if sizes_node is not None else 'in_stock'
                # Jeśli IdoSell zwróci 'yes' zamiast 'in_stock', poprawiamy na standard OpenAI
                if availability in ['yes', 'in_stock']:
                    availability = 'in_stock'
                else:
                    availability = 'out_of_stock'
                    
                # Wyciąganie parametru Kolor
                color = ""
                params_node = elem.find('parameters')
                if params_node is not None:
                    for param in params_node.findall('parameter'):
                        if param.attrib.get('name') == 'Kolor':
                            val_node = param.find('value')
                            if val_node is not None:
                                color = val_node.attrib.get('name', '')
                                break

                # Konstrukcja wiersza CSV
                row = {
                    'item_id': item_id,
                    'title': title,
                    'description': description,
                    'url': url,
                    'brand': brand,
                    'image_url': image_url,
                    'additional_image_urls': additional_image_urls,
                    'price': final_price,
                    'sale_price': final_sale_price,
                    'availability': availability,
                    'condition': 'new',  # Zawsze nowy
                    'product_category': product_category,
                    'color': color,
                    
                    # Wartości stałe dla sklepu 3kiwi
                    'seller_name': '3kiwi',
                    'seller_url': 'https://3kiwi.pl',
                    'return_policy': 'https://www.3kiwi.pl/pol-returns-and_replacements.html',
                    'target_countries': 'PL',
                    'store_country': 'PL',
                    'is_eligible_search': 'true',
                    'is_eligible_checkout': 'false',
                    'is_ads_eligible': 'true'
                }
                
                writer.writerow(row)
                count += 1
                
                # Zwalnianie pamięci po zapisie wiersza
                elem.clear()
                root.clear()

    print(f"Pomyślnie przetworzono i zapisano {count} produktów do pliku CSV.")

def upload_csv_to_r2(csv_path):
    """Wysyła gotowy plik CSV na chmurę Cloudflare R2"""
    print("Łączenie z Cloudflare R2...")
    s3_client = boto3.client(
        's3',
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name='auto'
    )
    
    print(f"Wysyłanie {CSV_FILENAME} do Cloudflare R2...")
    try:
        s3_client.upload_file(
            Filename=csv_path,
            Bucket=R2_BUCKET_NAME,
            Key=CSV_FILENAME,
            ExtraArgs={'ContentType': 'text/csv; charset=utf-8'}
        )
        print("✅ Sukces! Plik CSV został zaktualizowany na Cloudflare.")
    except ClientError as e:
        print(f"❌ Błąd podczas wysyłania do R2: {e}")
        raise

def cleanup():
    """Usuwa pliki tymczasowe"""
    for temp_file in [TEMP_XML_FILE, TEMP_CSV_FILE]:
        if os.path.exists(temp_file):
            os.remove(temp_file)
    print("Wyczyszczono pliki tymczasowe.")

if __name__ == "__main__":
    try:
        download_xml_stream(XML_URL, TEMP_XML_FILE)
        convert_xml_to_csv_stream(TEMP_XML_FILE, TEMP_CSV_FILE)
        upload_csv_to_r2(TEMP_CSV_FILE)
    except Exception as e:
        print(f"Wystąpił błąd krytyczny: {e}")
    finally:
        cleanup()
