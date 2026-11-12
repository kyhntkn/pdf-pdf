# PDF Studio (Python)

Çevrimdışı PDF dönüştürücü ve düzenleyici.

## Kendi bilgisayarında çalıştırma
    pip install -r requirements.txt
    python app/main.py
OCR için Tesseract kurulu olmalı (https://github.com/UB-Mannheim/tesseract/wiki), İngilizce/Türkçe dil dosyalarıyla.

## Exe
GitHub'a yükleyin; Actions > Build exe. Tesseract exe'nin içine otomatik eklenir.
Çıktı: `PDF-Studio.zip` (içinde `PDF-Studio/PDF-Studio.exe`).

## Hata ayıklama
Word/Excel çevirisinde OCR kullanılırsa, çıktının yanına `...-ocr-log.txt` yazılır.
