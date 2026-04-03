# KSeF Notifier

## English

### What this project does

`ksefnotifier` downloads purchase invoices from KSeF for your company and can also render each XML invoice into a readable PDF.

Important limitations:

- The downloader currently authenticates by `NIP` extracted from the KSeF token.
- This means it is intended for companies and other entities that use KSeF in a `NIP` context.
- It does not currently support authentication for individual users in a `PESEL`-style context.
- It downloads purchase invoices only (`Subject2`), not invoices issued by your own company.

### 1. Install Python

You need Python installed before running the scripts.

Recommended version:

- Python `3.11` or newer

Check if Python is already installed:

Windows:

```powershell
py --version
```

macOS:

```bash
python3 --version
```

If Python is not installed:

Windows:

1. Go to [python.org/downloads](https://www.python.org/downloads/).
2. Download the latest Python 3 installer for Windows.
3. Run the installer.
4. Make sure you enable the checkbox `Add Python to PATH`.
5. Finish the installation.

macOS:

1. Go to [python.org/downloads](https://www.python.org/downloads/).
2. Download the latest Python 3 installer for macOS.
3. Run the installer and finish the setup.
4. Open Terminal and check again with `python3 --version`.

### 2. Download this project

You need the project files on your computer.

If you use Git:

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

If you do not use Git:

1. Download the repository as ZIP from GitHub.
2. Extract it.
3. Open Terminal or PowerShell in the extracted project folder.

### 3. Create a virtual environment

This keeps Python libraries local to this project.

Windows:

```powershell
py -3 -m venv .venv
```

macOS:

```bash
python3 -m venv .venv
```

### 4. Activate the virtual environment

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows Command Prompt:

```cmd
.venv\Scripts\activate.bat
```

macOS:

```bash
source .venv/bin/activate
```

After activation, your terminal usually shows `(.venv)` at the beginning of the line.

### 5. Install the required libraries

Run this inside the project folder after activating the virtual environment.

Windows:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 6. Create `token.txt`

The downloader needs a KSeF token.

Create a file named `token.txt` in the same folder as:

- `download_current_month_invoices.py` when running the Python script
- `ksef_downloader.exe` or `ksef_downloader` when running the standalone build

The file should contain the token value on one line.

Examples:

```text
your-token-value-here
```

or:

```text
Bearer your-token-value-here
```

Both formats work.

### 7. Where to get the token

The token must be created in KSeF by a user/entity that has the required permissions.

In practice, generate the token in official KSeF tools such as:

- Aplikacja Podatnika KSeF 2.0
- another KSeF-integrated system that can create API tokens

Important:

- This project expects the token to contain the company `NIP`.
- The script extracts the `NIP` from the token automatically.
- The token should have rights to access the purchase invoices you want to download.
- In the official KSeF documentation, token management is described in the KSeF 2.0 getting-started handbook, and API token authentication is described in the KSeF API v2 documentation.

Useful official sources:

- [KSeF API v2 documentation](https://api.ksef.mf.gov.pl/docs/v2/index.html)
- [KSeF 2.0 handbook - getting started](https://ksef.podatki.gov.pl/media/iqfjmrws/podrecznik-ksef-20-czesc-i-rozpoczecie-korzystania-z-ksef-19022026.pdf)

### 8. Optional: create `dir_prefix.txt`

This file is optional.

If you create `dir_prefix.txt` in the same folder as the script or executable, put one base path inside it.

Example on Windows:

```text
C:\Users\YourName\Documents\Accounting
```

Example on macOS:

```text
/Users/yourname/Documents/Accounting
```

Behavior:

- If `dir_prefix.txt` exists and is not empty, files are saved only to `<dir_prefix>/<YYYY_MM>/ksef`
- If `dir_prefix.txt` does not exist or is empty, files are saved only to `downloads/<YYYY-MM>` next to the script or executable

The script also stores `downloaded.txt` in the active target folder, so already downloaded invoice IDs are not downloaded again.

### 9. Run the downloader

Current month:

Windows:

```powershell
python download_current_month_invoices.py
```

macOS:

```bash
python download_current_month_invoices.py
```

Specific month:

```bash
python download_current_month_invoices.py --year 2026 --month 3
```

Dry run only:

```bash
python download_current_month_invoices.py --dry-run
```

Download without PDF rendering:

```bash
python download_current_month_invoices.py --render no
```

Use plain KSeF number as filename:

```bash
python download_current_month_invoices.py --filename-mode id
```

Notes:

- Default filename mode is `seller-id`
- Default render mode is `yes`
- The script prints the token status, target directory, date range, and downloaded/rendered invoice list in the console

### 10. Render XML invoices to PDF separately

If you already have XML files and want to render them separately:

Single file:

```bash
python render_ksef_invoice_pdf.py invoice.xml
```

Whole directory:

```bash
python render_ksef_invoice_pdf.py downloads/2026-03
```

### 11. Build a standalone executable

This creates a standalone app with Python and libraries bundled inside.

Important:

- Build on Windows if you want a Windows `.exe`
- Build on macOS if you want a macOS executable
- PyInstaller does not cross-compile between Windows and macOS

First, activate the virtual environment and make sure requirements are installed.

Build command on Windows:

```powershell
python -m PyInstaller --noconfirm --clean --onefile --name ksef_downloader --hidden-import render_ksef_invoice_pdf --hidden-import qrcode --hidden-import PIL --collect-submodules qrcode download_current_month_invoices.py
```

Build command on macOS:

```bash
python -m PyInstaller --noconfirm --clean --onefile --name ksef_downloader --hidden-import render_ksef_invoice_pdf --hidden-import qrcode --hidden-import PIL --collect-submodules qrcode download_current_month_invoices.py
```

Build result:

- Windows: `dist/ksef_downloader.exe`
- macOS: `dist/ksef_downloader`

To run the standalone build:

Windows:

```powershell
.\dist\ksef_downloader.exe
```

macOS:

```bash
./dist/ksef_downloader
```

Place these files next to the executable if you want the default setup:

- `token.txt`
- optional `dir_prefix.txt`

## Polski

### Do czego sluzy ten projekt

`ksefnotifier` pobiera z KSeF faktury zakupowe dla Twojej firmy i moze tez automatycznie wygenerowac czytelny PDF z kazdego pliku XML.

Wazne ograniczenia:

- Downloader uwierzytelnia sie przez `NIP` wyciagniety z tokena KSeF.
- Oznacza to, ze projekt jest przeznaczony glownie dla firm i innych podmiotow dzialajacych w kontekscie `NIP`.
- Projekt nie obsluguje obecnie logowania dla osob fizycznych w kontekscie `PESEL`.
- Pobierane sa tylko faktury zakupowe (`Subject2`), a nie faktury wystawione przez Twoja firme.

### 1. Zainstaluj Python

Przed uruchomieniem skryptow musisz miec zainstalowanego Pythona.

Zalecana wersja:

- Python `3.11` lub nowszy

Sprawdz, czy Python jest juz zainstalowany:

Windows:

```powershell
py --version
```

macOS:

```bash
python3 --version
```

Jesli Python nie jest zainstalowany:

Windows:

1. Wejdz na [python.org/downloads](https://www.python.org/downloads/).
2. Pobierz najnowszy instalator Python 3 dla Windows.
3. Uruchom instalator.
4. Koniecznie zaznacz opcje `Add Python to PATH`.
5. Dokoncz instalacje.

macOS:

1. Wejdz na [python.org/downloads](https://www.python.org/downloads/).
2. Pobierz najnowszy instalator Python 3 dla macOS.
3. Uruchom instalator i zakoncz konfiguracje.
4. Otworz Terminal i ponownie sprawdz `python3 --version`.

### 2. Pobierz projekt

Musisz miec pliki projektu na swoim komputerze.

Jesli korzystasz z Git:

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

Jesli nie korzystasz z Git:

1. Pobierz repozytorium jako ZIP z GitHub.
2. Rozpakuj archiwum.
3. Otworz Terminal albo PowerShell w katalogu projektu.

### 3. Utworz wirtualne srodowisko

To pozwala trzymac biblioteki Pythona tylko w tym projekcie.

Windows:

```powershell
py -3 -m venv .venv
```

macOS:

```bash
python3 -m venv .venv
```

### 4. Aktywuj wirtualne srodowisko

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows Command Prompt:

```cmd
.venv\Scripts\activate.bat
```

macOS:

```bash
source .venv/bin/activate
```

Po aktywacji terminal zwykle pokazuje `(.venv)` na poczatku linii.

### 5. Zainstaluj wymagane biblioteki

Uruchom te komendy w katalogu projektu po aktywacji wirtualnego srodowiska.

Windows:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 6. Utworz plik `token.txt`

Downloader potrzebuje tokena KSeF.

Utworz plik `token.txt` w tym samym katalogu co:

- `download_current_month_invoices.py`, jesli uruchamiasz skrypt Pythona
- `ksef_downloader.exe` albo `ksef_downloader`, jesli uruchamiasz wersje standalone

Plik powinien zawierac wartosc tokena w jednym wierszu.

Przyklad:

```text
twoj-token-tutaj
```

albo:

```text
Bearer twoj-token-tutaj
```

Obie formy sa akceptowane.

### 7. Skad wziac token

Token musi zostac wygenerowany w KSeF przez uzytkownika albo podmiot, ktory ma odpowiednie uprawnienia.

W praktyce token mozna wygenerowac w oficjalnych narzedziach KSeF, takich jak:

- Aplikacja Podatnika KSeF 2.0
- inne oprogramowanie zintegrowane z KSeF, ktore potrafi tworzyc tokeny API

Wazne:

- Projekt zaklada, ze token zawiera firmowy `NIP`.
- Skrypt automatycznie odczytuje `NIP` z tokena.
- Token powinien miec uprawnienia do pobierania faktur zakupowych, ktore chcesz sciagnac.
- W oficjalnej dokumentacji KSeF opis zarzadzania tokenami znajduje sie w podreczniku startowym KSeF 2.0, a opis uwierzytelniania tokenem w dokumentacji API KSeF v2.

Przydatne oficjalne zrodla:

- [Dokumentacja KSeF API v2](https://api.ksef.mf.gov.pl/docs/v2/index.html)
- [Podrecznik KSeF 2.0 - rozpoczecie korzystania](https://ksef.podatki.gov.pl/media/iqfjmrws/podrecznik-ksef-20-czesc-i-rozpoczecie-korzystania-z-ksef-19022026.pdf)

### 8. Opcjonalnie: utworz `dir_prefix.txt`

Ten plik jest opcjonalny.

Jesli utworzysz `dir_prefix.txt` w tym samym katalogu co skrypt lub plik wykonywalny, wpisz do niego jedna sciezke bazowa.

Przyklad dla Windows:

```text
C:\Users\YourName\Documents\Accounting
```

Przyklad dla macOS:

```text
/Users/yourname/Documents/Accounting
```

Dzialanie:

- Jesli `dir_prefix.txt` istnieje i nie jest pusty, pliki sa zapisywane tylko do `<dir_prefix>/<YYYY_MM>/ksef`
- Jesli `dir_prefix.txt` nie istnieje albo jest pusty, pliki sa zapisywane tylko do `downloads/<YYYY-MM>` obok skryptu lub pliku wykonywalnego

Skrypt zapisuje tez `downloaded.txt` w aktywnym katalogu docelowym, aby nie pobierac ponownie tych samych identyfikatorow faktur.

### 9. Uruchom downloader

Biezacy miesiac:

Windows:

```powershell
python download_current_month_invoices.py
```

macOS:

```bash
python download_current_month_invoices.py
```

Konkretny miesiac:

```bash
python download_current_month_invoices.py --year 2026 --month 3
```

Tylko podglad bez pobierania:

```bash
python download_current_month_invoices.py --dry-run
```

Pobieranie bez generowania PDF:

```bash
python download_current_month_invoices.py --render no
```

Nazwy plikow tylko po numerze KSeF:

```bash
python download_current_month_invoices.py --filename-mode id
```

Uwagi:

- Domyslny tryb nazewnictwa to `seller-id`
- Domyslne renderowanie PDF jest wlaczone (`yes`)
- Skrypt wypisuje w konsoli status tokena, katalog docelowy, zakres dat oraz liste pobranych i wyrenderowanych faktur

### 10. Osobne renderowanie XML do PDF

Jesli masz juz pliki XML i chcesz wygenerowac PDF osobno:

Jeden plik:

```bash
python render_ksef_invoice_pdf.py invoice.xml
```

Caly katalog:

```bash
python render_ksef_invoice_pdf.py downloads/2026-03
```

### 11. Budowa wersji standalone

Mozesz zbudowac samodzielny program z dolaczonym Pythonem i bibliotekami.

Wazne:

- Jesli chcesz plik `.exe`, buduj na Windows
- Jesli chcesz plik wykonywalny dla macOS, buduj na macOS
- PyInstaller nie robi cross-compile miedzy Windows i macOS

Najpierw aktywuj wirtualne srodowisko i upewnij sie, ze biblioteki z `requirements.txt` sa zainstalowane.

Komenda build na Windows:

```powershell
python -m PyInstaller --noconfirm --clean --onefile --name ksef_downloader --hidden-import render_ksef_invoice_pdf --hidden-import qrcode --hidden-import PIL --collect-submodules qrcode download_current_month_invoices.py
```

Komenda build na macOS:

```bash
python -m PyInstaller --noconfirm --clean --onefile --name ksef_downloader --hidden-import render_ksef_invoice_pdf --hidden-import qrcode --hidden-import PIL --collect-submodules qrcode download_current_month_invoices.py
```

Wynik builda:

- Windows: `dist/ksef_downloader.exe`
- macOS: `dist/ksef_downloader`

Aby uruchomic wersje standalone:

Windows:

```powershell
.\dist\ksef_downloader.exe
```

macOS:

```bash
./dist/ksef_downloader
```

Jesli chcesz korzystac z ustawien domyslnych, umiesc obok pliku wykonywalnego:

- `token.txt`
- opcjonalnie `dir_prefix.txt`
