# KSeF Notifier

English documentation is first in this file. Polish documentation follows below.

## English

### What this project does

`ksefnotifier` downloads purchase or sales invoices from KSeF for your company, keeps track of what has already been downloaded, and can also render each XML invoice into a readable PDF.

Default behavior when you run the downloader without parameters:

- it downloads invoices for the current month
- it uses the KSeF storage date (`PermanentStorage`) as the date filter
- it downloads purchase invoices by default (`Subject2`)
- sales invoices can be downloaded with `--invoice-type sales` (`Subject1`)
- it uses `seller-id` as the default filename mode, using seller names for purchase invoices and buyer names for sales invoices
- it renders PDFs by default
- it reads active KSeF API limits from `/rate-limits` and slows down only when needed
- if KSeF returns `429 Too Many Requests`, it waits according to the server retry hint and retries the request
- if a long wait would outlive the access token, the script refreshes the access token and continues
- if `dir_prefix.txt` does not exist or is empty, files are saved to `downloads/<YYYY-MM>/ksef_purchase` next to the script or executable
- if `dir_prefix.txt` exists and contains a path, purchase files are saved only to `<dir_prefix>/<YYYY_MM>/ksef_purchase`
- sales files are saved to the matching `ksef_sales` folder

This is not a "download everything every time" script.

Typical usage looks like this:

1. You run the downloader.
2. New invoices are downloaded.
3. You review them, move them elsewhere, approve them, or import them into your accounting system.
4. The script keeps `downloaded.txt` in the active target folder, so on the next run it downloads only invoices that were not tracked before.

This means the script does not rely only on the presence of XML files in the directory. Even if some downloaded invoices are later moved out manually, `downloaded.txt` still tells the script which invoice IDs were already processed.

Important limitations:

- The downloader currently authenticates by `NIP` extracted from the KSeF token.
- This means it is intended for companies and other entities that use KSeF in a `NIP` context.
- It does not currently support authentication for individual users in a `PESEL`-style context.
- Purchase invoices use KSeF `Subject2`; sales invoices use KSeF `Subject1`.

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

- If `dir_prefix.txt` exists and is not empty, purchase files are saved only to `<dir_prefix>/<YYYY_MM>/ksef_purchase`
- If `dir_prefix.txt` exists and is not empty, sales files are saved only to `<dir_prefix>/<YYYY_MM>/ksef_sales`
- If `dir_prefix.txt` does not exist or is empty, purchase files are saved only to `downloads/<YYYY-MM>/ksef_purchase` next to the script or executable
- If `dir_prefix.txt` does not exist or is empty, sales files are saved only to `downloads/<YYYY-MM>/ksef_sales` next to the script or executable

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

Sales invoices:

```bash
python download_current_month_invoices.py --invoice-type sales
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

- If you run the program without parameters, it downloads the current month.
- If you run the program without parameters, it uses `PermanentStorage` as the date type.
- Default invoice type is `purchase`
- Default filename mode is `seller-id`; it uses the counterparty name prefix, meaning seller for purchase invoices and buyer for sales invoices
- Default render mode is `yes`
- The script prints the token status, target directory, date range, and downloaded/rendered invoice list in the console

### 10. Render XML invoices to PDF separately

If you already have XML files and want to render them separately:

Single file:

```bash
python render_ksef_invoice_pdf.py invoice.xml
```

Single file with an explicit KSeF ID added to the PDF:

```bash
python render_ksef_invoice_pdf.py invoice.xml --ksef-id 20260311-ABCD-1234
```

Whole directory:

```bash
python render_ksef_invoice_pdf.py downloads/2026-03
```

Note:

- `--ksef-id` is optional, because this ID is not present in the XML itself
- when PDFs are generated by `download_current_month_invoices.py`, the downloader passes the KSeF ID automatically
- the generated PDF is a best-effort visualization of the XML, not an official KSeF rendering
- the appendix lists XML fields; fields highlighted in red are present in XML but are not represented on the first PDF page
- if any red fields are present, treat the PDF with caution because the visual rendering may be incomplete or incorrect; verify the XML or compare with the official KSeF visualization before accounting or approval decisions

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

`ksefnotifier` pobiera z KSeF faktury zakupowe albo sprzedazowe dla Twojej firmy, pilnuje ktore faktury zostaly juz pobrane, i moze tez automatycznie wygenerowac czytelny PDF z kazdego pliku XML.

Domyslne zachowanie po uruchomieniu downloadera bez parametrow:

- pobierany jest biezacy miesiac
- jako filtr daty uzywana jest data przechowywania w KSeF (`PermanentStorage`)
- domyslnie pobierane sa faktury zakupowe (`Subject2`)
- faktury sprzedazowe mozna pobrac przez `--invoice-type sales` (`Subject1`)
- domyslny tryb nazewnictwa to `seller-id`, z nazwa sprzedawcy dla faktur zakupowych i nazwa nabywcy dla faktur sprzedazowych
- generowanie PDF jest domyslnie wlaczone
- skrypt odczytuje aktualne limity KSeF API z `/rate-limits` i zwalnia tylko wtedy, kiedy jest to potrzebne
- jesli KSeF zwroci `429 Too Many Requests`, skrypt czeka zgodnie z podpowiedzia serwera i ponawia zadanie
- jesli dlugie oczekiwanie mogloby przekroczyc waznosc access tokena, skrypt odswieza access token i kontynuuje prace
- jesli `dir_prefix.txt` nie istnieje albo jest pusty, faktury zakupowe trafiaja do `downloads/<YYYY-MM>/ksef_purchase` obok skryptu lub pliku wykonywalnego
- jesli `dir_prefix.txt` istnieje i zawiera sciezke, faktury zakupowe trafiaja tylko do `<dir_prefix>/<YYYY_MM>/ksef_purchase`
- faktury sprzedazowe trafiaja do analogicznego katalogu `ksef_sales`

To nie jest skrypt typu "za kazdym razem pobierz wszystko od nowa".

Typowy scenariusz wyglada tak:

1. Uruchamiasz downloader.
2. Nowe faktury sa pobierane.
3. Przegladasz je, przenosisz w inne miejsce, zatwierdzasz albo importujesz do systemu ksiegowego.
4. Skrypt przechowuje `downloaded.txt` w aktywnym katalogu docelowym, dzieki czemu przy nastepnym uruchomieniu pobierze tylko faktury, ktore wczesniej nie byly zapisane na liscie.

Oznacza to, ze skrypt nie opiera sie tylko na obecnosci plikow XML w katalogu. Nawet jesli czesc pobranych faktur zostanie pozniej recznie przeniesiona w inne miejsce, `downloaded.txt` nadal informuje skrypt, ktore identyfikatory faktur byly juz obsluzone.

Wazne ograniczenia:

- Downloader uwierzytelnia sie przez `NIP` wyciagniety z tokena KSeF.
- Oznacza to, ze projekt jest przeznaczony glownie dla firm i innych podmiotow dzialajacych w kontekscie `NIP`.
- Projekt nie obsluguje obecnie logowania dla osob fizycznych w kontekscie `PESEL`.
- Faktury zakupowe uzywaja w KSeF `Subject2`; faktury sprzedazowe uzywaja `Subject1`.

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

- Jesli `dir_prefix.txt` istnieje i nie jest pusty, faktury zakupowe sa zapisywane tylko do `<dir_prefix>/<YYYY_MM>/ksef_purchase`
- Jesli `dir_prefix.txt` istnieje i nie jest pusty, faktury sprzedazowe sa zapisywane tylko do `<dir_prefix>/<YYYY_MM>/ksef_sales`
- Jesli `dir_prefix.txt` nie istnieje albo jest pusty, faktury zakupowe sa zapisywane tylko do `downloads/<YYYY-MM>/ksef_purchase` obok skryptu lub pliku wykonywalnego
- Jesli `dir_prefix.txt` nie istnieje albo jest pusty, faktury sprzedazowe sa zapisywane tylko do `downloads/<YYYY-MM>/ksef_sales` obok skryptu lub pliku wykonywalnego

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

Faktury sprzedazowe:

```bash
python download_current_month_invoices.py --invoice-type sales
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

- Jesli uruchomisz program bez parametrow, pobierany jest biezacy miesiac.
- Jesli uruchomisz program bez parametrow, jako typ daty uzywany jest `PermanentStorage`.
- Domyslny typ faktur to `purchase`
- Domyslny tryb nazewnictwa to `seller-id`; uzywa prefiksu nazwy kontrahenta, czyli sprzedawcy dla faktur zakupowych i nabywcy dla faktur sprzedazowych
- Domyslne renderowanie PDF jest wlaczone (`yes`)
- Skrypt wypisuje w konsoli status tokena, katalog docelowy, zakres dat oraz liste pobranych i wyrenderowanych faktur

### 10. Osobne renderowanie XML do PDF

Jesli masz juz pliki XML i chcesz wygenerowac PDF osobno:

Jeden plik:

```bash
python render_ksef_invoice_pdf.py invoice.xml
```

Jeden plik z jawnym identyfikatorem KSeF dodanym do PDF:

```bash
python render_ksef_invoice_pdf.py invoice.xml --ksef-id 20260311-ABCD-1234
```

Caly katalog:

```bash
python render_ksef_invoice_pdf.py downloads/2026-03
```

Uwagi:

- `--ksef-id` jest opcjonalny, bo tego identyfikatora nie ma w samym XML
- gdy PDF-y sa tworzone przez `download_current_month_invoices.py`, downloader przekazuje identyfikator KSeF automatycznie
- wygenerowany PDF jest pomocnicza wizualizacja XML, a nie oficjalnym renderem KSeF
- zalacznik w PDF pokazuje pola XML; pola oznaczone na czerwono sa obecne w XML, ale nie maja reprezentacji na pierwszej stronie PDF
- jesli widzisz czerwone pola, traktuj PDF ostroznie, bo wizualizacja moze byc niepelna albo niepoprawna; przed ksiegowaniem lub akceptacja sprawdz XML albo porownaj z oficjalna wizualizacja KSeF

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
