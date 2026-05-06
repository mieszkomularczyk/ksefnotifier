#!/usr/bin/env python3
"""Render KSeF XML invoice(s) into a readable PDF visualization.

The output contains:
1) Business-friendly invoice view (seller/buyer, items, totals, payment)
2) Full XML field appendix (every XML leaf + attributes path/value)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from pathlib import Path
from typing import Optional

from fpdf import FPDF
from fpdf.fonts import FontFace


PAYMENT_METHOD_BY_CODE = {
    "1": "Gotowka",
    "2": "Karta",
    "3": "Bon",
    "4": "Czek",
    "5": "Weksel",
    "6": "Przelew",
    "7": "Inna",
}

MONEY_QUANT = Decimal("0.01")


@dataclass
class Party:
    name: str
    nip: str
    address_lines: list[str]
    extra_rows: list[tuple[str, str]]


@dataclass
class InvoiceItem:
    line_no: str
    description: str
    unit: str
    quantity: Decimal
    unit_price: Decimal
    unit_price_kind: str
    net_amount: Decimal
    vat_rate: str
    vat_amount: Decimal
    gross_amount: Decimal
    before_correction: str
    extra_rows: list[tuple[str, str]]


@dataclass
class InvoiceData:
    invoice_number: str
    issue_date: str
    sale_date: str
    issue_place: str
    currency: str
    invoice_type: str
    period_from: str
    period_to: str
    seller: Party
    buyer: Party
    items: list[InvoiceItem]
    payment_due_date: str
    payment_method: str
    bank_account: str
    bank_name: str
    net_total: Decimal
    vat_total: Decimal
    gross_total: Decimal
    summary_extra_rows: list[tuple[str, str]]
    qr_url: str
    all_fields: list[tuple[str, str]]
    visualized_field_counts: dict[str, int]
    schema_note_rows: list[tuple[str, str]]
    correction_rows: list[tuple[str, str]]
    transaction_rows: list[tuple[str, str]]
    additional_party_rows: list[tuple[str, str]]
    additional_description_rows: list[tuple[str, str, str]]
    payment_rows: list[tuple[str, str]]
    footer_rows: list[tuple[str, str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create PDF visualization of KSeF XML invoice(s)."
    )
    parser.add_argument("input", type=Path, help="Input XML file or directory with XML files")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output PDF path (single file input) or output directory (directory input).",
    )
    parser.add_argument(
        "--font-regular",
        type=Path,
        default=None,
        help="Optional path to TTF regular font file (for full Unicode support).",
    )
    parser.add_argument(
        "--font-bold",
        type=Path,
        default=None,
        help="Optional path to TTF bold font file (used with --font-regular).",
    )
    parser.add_argument(
        "--hide-empty-fields",
        action="store_true",
        help="In XML appendix skip rows with empty values.",
    )
    parser.add_argument(
        "--ksef-id",
        default=None,
        help="Optional KSeF ID to display in the PDF header (single XML input only).",
    )
    return parser.parse_args()


def strip_namespace(tag_name: str) -> str:
    if "}" in tag_name:
        return tag_name.split("}", 1)[1]
    return tag_name


def parse_decimal(raw: Optional[str]) -> Decimal:
    value = parse_optional_decimal(raw)
    return value if value is not None else Decimal("0")


def parse_optional_decimal(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def child_decimal(parent: Optional[ET.Element], ns_uri: str, tag_name: str) -> Optional[Decimal]:
    return parse_optional_decimal(child_text(parent, ns_uri, tag_name))


def parse_vat_rate(vat_rate: str) -> Optional[Decimal]:
    if not vat_rate:
        return None
    first_token = vat_rate.strip().replace(",", ".").split()[0]
    return parse_optional_decimal(first_token)


def round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def derive_net_from_gross(gross_amount: Decimal, vat_rate: str) -> Decimal:
    rate = parse_vat_rate(vat_rate)
    if rate is None or rate == Decimal("0"):
        return round_money(gross_amount)
    return round_money(gross_amount * Decimal("100") / (Decimal("100") + rate))


def derive_vat_from_net(net_amount: Decimal, vat_rate: str) -> Decimal:
    rate = parse_vat_rate(vat_rate)
    if rate is None or rate == Decimal("0"):
        return Decimal("0")
    return round_money(net_amount * rate / Decimal("100"))


def derive_vat_from_gross(gross_amount: Decimal, vat_rate: str) -> Decimal:
    return round_money(gross_amount - derive_net_from_gross(gross_amount, vat_rate))


def tag_with_ns(ns_uri: str, tag_name: str) -> str:
    return f"{{{ns_uri}}}{tag_name}" if ns_uri else tag_name


def child_text(parent: Optional[ET.Element], ns_uri: str, tag_name: str, default: str = "") -> str:
    if parent is None:
        return default
    node = parent.find(tag_with_ns(ns_uri, tag_name))
    if node is None or node.text is None:
        return default
    return node.text.strip()


def child_node(parent: Optional[ET.Element], ns_uri: str, tag_name: str) -> Optional[ET.Element]:
    if parent is None:
        return None
    return parent.find(tag_with_ns(ns_uri, tag_name))


def child_nodes(parent: Optional[ET.Element], ns_uri: str, tag_name: str) -> list[ET.Element]:
    if parent is None:
        return []
    return list(parent.findall(tag_with_ns(ns_uri, tag_name)))


def append_row(rows: list[tuple[str, str]], label: str, value: str) -> None:
    if value:
        rows.append((label, value))


def root_attribute(root: ET.Element, attribute_name: str) -> str:
    for key, value in root.attrib.items():
        if strip_namespace(key) == attribute_name:
            return str(value)
    return ""


def sum_numbered_fields(parent: ET.Element, ns_uri: str, prefix: str, max_idx: int = 12) -> Decimal:
    total = Decimal("0")
    for idx in range(1, max_idx + 1):
        total += parse_decimal(child_text(parent, ns_uri, f"{prefix}_{idx}"))
    return total


def parse_party(root: ET.Element, ns_uri: str, node_name: str) -> Party:
    node = root.find(tag_with_ns(ns_uri, node_name))
    identity = node.find(tag_with_ns(ns_uri, "DaneIdentyfikacyjne")) if node is not None else None
    address = node.find(tag_with_ns(ns_uri, "Adres")) if node is not None else None

    country = child_text(address, ns_uri, "KodKraju")
    line1 = child_text(address, ns_uri, "AdresL1")
    line2 = child_text(address, ns_uri, "AdresL2")
    address_gln = child_text(address, ns_uri, "GLN")
    address_lines = [x for x in [line1, line2, country] if x]

    extra_rows: list[tuple[str, str]] = []
    append_row(extra_rows, "Prefiks podatnika", child_text(node, ns_uri, "PrefiksPodatnika"))
    append_row(extra_rows, "Numer klienta", child_text(node, ns_uri, "NrKlienta"))
    append_row(extra_rows, "ID nabywcy", child_text(node, ns_uri, "IDNabywcy"))
    append_row(extra_rows, "Nr EORI", child_text(node, ns_uri, "NrEORI"))
    append_row(extra_rows, "GLN adresu", address_gln)

    no_tax_id = child_text(identity, ns_uri, "BrakID")
    if no_tax_id:
        append_row(extra_rows, "Brak identyfikatora podatkowego", flag_yes_no(no_tax_id))

    eu_country = child_text(identity, ns_uri, "KodUE")
    eu_vat = child_text(identity, ns_uri, "NrVatUE")
    if eu_country or eu_vat:
        append_row(extra_rows, "VAT UE", " ".join(part for part in [eu_country, eu_vat] if part))

    correspondence = child_node(node, ns_uri, "AdresKoresp")
    correspondence_lines = [
        child_text(correspondence, ns_uri, "AdresL1"),
        child_text(correspondence, ns_uri, "AdresL2"),
        child_text(correspondence, ns_uri, "KodKraju"),
    ]
    correspondence_text = " | ".join(line for line in correspondence_lines if line)
    append_row(extra_rows, "Adres korespondencyjny", correspondence_text)
    append_row(extra_rows, "GLN adresu korespondencyjnego", child_text(correspondence, ns_uri, "GLN"))

    for idx, contact in enumerate(child_nodes(node, ns_uri, "DaneKontaktowe"), start=1):
        suffix = f" {idx}" if idx > 1 else ""
        append_row(extra_rows, f"Email{suffix}", child_text(contact, ns_uri, "Email"))
        append_row(extra_rows, f"Telefon{suffix}", child_text(contact, ns_uri, "Telefon"))

    return Party(
        name=child_text(identity, ns_uri, "Nazwa"),
        nip=child_text(identity, ns_uri, "NIP"),
        address_lines=address_lines,
        extra_rows=extra_rows,
    )


def build_qr_verification_url(seller_nip: str, issue_date: str, xml_bytes: bytes) -> str:
    if not seller_nip or not issue_date:
        return ""

    try:
        date_for_qr = datetime.strptime(issue_date, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return ""

    digest = hashlib.sha256(xml_bytes).digest()
    hash_b64url = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"https://qr.ksef.mf.gov.pl/invoice/{seller_nip}/{date_for_qr}/{hash_b64url}"


def flatten_xml_fields(node: ET.Element, path: str, out_rows: list[tuple[str, str]]) -> None:
    current_tag = strip_namespace(node.tag)
    current_path = f"{path}/{current_tag}" if path else current_tag

    for attr_name, attr_value in node.attrib.items():
        attr_key = f"{current_path}@{strip_namespace(attr_name)}"
        out_rows.append((attr_key, str(attr_value)))

    text = (node.text or "").strip()
    children = list(node)
    if text or not children:
        out_rows.append((current_path, text))

    for child in children:
        flatten_xml_fields(child, current_path, out_rows)


def invoice_path(*parts: str) -> str:
    return "/".join(("Faktura", *parts))


def flag_yes_no(value: str, *, yes_value: str = "1") -> str:
    if value == yes_value:
        return "TAK"
    if value:
        return "NIE"
    return "-"


def flag_no_yes(value: str, *, no_value: str = "1") -> str:
    if value == no_value:
        return "NIE"
    if value:
        return "TAK"
    return "-"


def before_correction_label(value: str) -> str:
    if value == "1":
        return "Tak"
    if value:
        return "Nie"
    return ""


def correction_invoice_type_label(invoice_type: str) -> str:
    if invoice_type == "KOR":
        return "Faktura korygujaca"
    return "Faktura VAT"


def build_correction_rows(fa: ET.Element, ns_uri: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    append_row(rows, "Przyczyna korekty", child_text(fa, ns_uri, "PrzyczynaKorekty"))

    for corrected in fa.findall(tag_with_ns(ns_uri, "DaneFaKorygowanej")):
        date = child_text(corrected, ns_uri, "DataWystFaKorygowanej")
        number = child_text(corrected, ns_uri, "NrFaKorygowanej")
        has_ksef_number = child_text(corrected, ns_uri, "NrKSeF")
        ksef_number = child_text(corrected, ns_uri, "NrKSeFFaKorygowanej")
        outside_ksef = child_text(corrected, ns_uri, "NrKSeFN")

        if date:
            rows.append(("Data wystawienia faktury korygowanej", date))
        if number:
            rows.append(("Numer faktury korygowanej", number))
        if has_ksef_number:
            rows.append(("Faktura korygowana wystawiona w KSeF", flag_yes_no(has_ksef_number)))
        if ksef_number:
            rows.append(("Numer KSeF faktury korygowanej", ksef_number))
        if outside_ksef:
            rows.append(("Faktura korygowana wystawiona poza KSeF", flag_yes_no(outside_ksef)))

    return rows


def build_summary_extra_rows(fa: ET.Element, ns_uri: str, currency: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    settlement = child_node(fa, ns_uri, "Rozliczenie")
    amount_due_raw = child_text(settlement, ns_uri, "DoZaplaty")
    amount_due = parse_optional_decimal(amount_due_raw)
    if amount_due_raw:
        rows.append(
            (
                "Do zaplaty",
                format_amount(amount_due, currency) if amount_due is not None else amount_due_raw,
            )
        )

    advances = child_nodes(fa, ns_uri, "ZaliczkaCzesciowa")
    for idx, advance in enumerate(advances, start=1):
        suffix = f" {idx}" if len(advances) > 1 else ""
        append_row(rows, f"Data zaliczki czesciowej{suffix}", child_text(advance, ns_uri, "P_6Z"))
        advance_amount_raw = child_text(advance, ns_uri, "P_15Z")
        advance_amount = parse_optional_decimal(advance_amount_raw)
        if advance_amount_raw:
            rows.append(
                (
                    f"Kwota zaliczki czesciowej{suffix}",
                    format_amount(advance_amount, currency)
                    if advance_amount is not None
                    else advance_amount_raw,
                )
            )
    return rows


def build_transaction_rows(fa: ET.Element, ns_uri: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    wz_documents = child_nodes(fa, ns_uri, "WZ")
    for idx, wz in enumerate(wz_documents, start=1):
        value = (wz.text or "").strip()
        if value:
            suffix = f" {idx}" if len(wz_documents) > 1 else ""
            rows.append((f"Dokument WZ{suffix}", value))

    orders: list[ET.Element] = []
    for terms in child_nodes(fa, ns_uri, "WarunkiTransakcji"):
        orders.extend(child_nodes(terms, ns_uri, "Zamowienia"))

    for idx, order in enumerate(orders, start=1):
        suffix = f" {idx}" if len(orders) > 1 else ""
        append_row(rows, f"Numer zamowienia{suffix}", child_text(order, ns_uri, "NrZamowienia"))
        append_row(rows, f"Data zamowienia{suffix}", child_text(order, ns_uri, "DataZamowienia"))
    return rows


def build_additional_description_rows(fa: ET.Element, ns_uri: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for description in child_nodes(fa, ns_uri, "DodatkowyOpis"):
        line_no = child_text(description, ns_uri, "NrWiersza")
        key = child_text(description, ns_uri, "Klucz")
        value = child_text(description, ns_uri, "Wartosc")
        if line_no or key or value:
            rows.append((line_no, key, value))
    return rows


def build_additional_party_rows(root: ET.Element, ns_uri: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    parties = child_nodes(root, ns_uri, "Podmiot3")
    for idx, party in enumerate(parties, start=1):
        prefix = f"Podmiot dodatkowy {idx}" if len(parties) > 1 else "Podmiot dodatkowy"
        identity = child_node(party, ns_uri, "DaneIdentyfikacyjne")
        append_row(rows, f"{prefix} - rola", child_text(party, ns_uri, "Rola"))
        append_row(rows, f"{prefix} - nazwa", child_text(identity, ns_uri, "Nazwa"))
        append_row(rows, f"{prefix} - NIP", child_text(identity, ns_uri, "NIP"))
        append_row(rows, f"{prefix} - VAT UE", " ".join(
            part
            for part in [
                child_text(identity, ns_uri, "KodUE"),
                child_text(identity, ns_uri, "NrVatUE"),
            ]
            if part
        ))
        no_tax_id = child_text(identity, ns_uri, "BrakID")
        if no_tax_id:
            append_row(rows, f"{prefix} - brak identyfikatora", flag_yes_no(no_tax_id))
    return rows


def build_payment_rows(payment: Optional[ET.Element], ns_uri: str, method_display: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    term = child_node(payment, ns_uri, "TerminPlatnosci")
    term_description = child_node(term, ns_uri, "TerminOpis")
    term_parts = [
        child_text(term_description, ns_uri, "Ilosc"),
        child_text(term_description, ns_uri, "Jednostka"),
        child_text(term_description, ns_uri, "ZdarzeniePoczatkowe"),
    ]
    term_description_text = " ".join(part for part in term_parts if part)

    append_row(rows, "Termin platnosci", child_text(term, ns_uri, "Termin"))
    append_row(rows, "Opis terminu", term_description_text)

    paid = child_text(payment, ns_uri, "Zaplacono")
    if paid:
        append_row(rows, "Zaplacono", flag_yes_no(paid))
    append_row(rows, "Data zaplaty", child_text(payment, ns_uri, "DataZaplaty"))
    append_row(rows, "Forma", method_display)

    other_payment = child_text(payment, ns_uri, "PlatnoscInna")
    if other_payment:
        append_row(rows, "Platnosc inna", flag_yes_no(other_payment))
    append_row(rows, "Opis platnosci", child_text(payment, ns_uri, "OpisPlatnosci"))

    bank = child_node(payment, ns_uri, "RachunekBankowy")
    append_row(rows, "Rachunek", child_text(bank, ns_uri, "NrRB"))
    append_row(rows, "Opis rachunku", child_text(bank, ns_uri, "OpisRachunku"))
    own_bank_account = child_text(bank, ns_uri, "RachunekWlasnyBanku")
    if own_bank_account:
        append_row(rows, "Rachunek wlasny banku", flag_yes_no(own_bank_account))
    append_row(rows, "SWIFT", child_text(bank, ns_uri, "SWIFT"))
    append_row(rows, "Bank", child_text(bank, ns_uri, "NazwaBanku"))
    return rows


def build_footer_rows(root: ET.Element, ns_uri: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    footer = child_node(root, ns_uri, "Stopka")
    registers = child_node(footer, ns_uri, "Rejestry")
    append_row(rows, "Pelna nazwa", child_text(registers, ns_uri, "PelnaNazwa"))
    append_row(rows, "KRS", child_text(registers, ns_uri, "KRS"))
    append_row(rows, "REGON", child_text(registers, ns_uri, "REGON"))
    append_row(rows, "BDO", child_text(registers, ns_uri, "BDO"))

    footnotes: list[ET.Element] = []
    for information in child_nodes(footer, ns_uri, "Informacje"):
        footnotes.extend(child_nodes(information, ns_uri, "StopkaFaktury"))
    for idx, footnote in enumerate(footnotes, start=1):
        value = (footnote.text or "").strip()
        if value:
            label = f"Informacja stopki {idx}" if len(footnotes) > 1 else "Informacja stopki"
            rows.append((label, value))
    return rows


def build_schema_note_rows(root: ET.Element, fa: ET.Element, ns_uri: str) -> list[tuple[str, str]]:
    header = child_node(root, ns_uri, "Naglowek")
    form_code = child_node(header, ns_uri, "KodFormularza")
    form_code_text = (form_code.text or "").strip() if form_code is not None and form_code.text else ""
    system_code = form_code.attrib.get("kodSystemowy", "") if form_code is not None else ""
    schema_version = form_code.attrib.get("wersjaSchemy", "") if form_code is not None else ""
    schema_location = root_attribute(root, "schemaLocation")

    rows: list[tuple[str, str]] = []
    if form_code_text or system_code or schema_version:
        value_parts = [part for part in [form_code_text, system_code, f"wersja schemy {schema_version}" if schema_version else ""] if part]
        rows.append(("Kod formularza", " / ".join(value_parts)))
    append_row(rows, "Lokalizacja schemy", schema_location)

    for label, value in [
        ("Wariant formularza", child_text(header, ns_uri, "WariantFormularza")),
        ("Data wytworzenia XML", child_text(header, ns_uri, "DataWytworzeniaFa")),
        ("System wystawcy", child_text(header, ns_uri, "SystemInfo")),
    ]:
        if value:
            rows.append((label, value))

    buyer = child_node(root, ns_uri, "Podmiot2")
    rows.extend(
        [
            ("Faktura dotyczy jednostki podrzednej JST", flag_yes_no(child_text(buyer, ns_uri, "JST"))),
            ("Faktura dotyczy czlonka grupy VAT (GV)", flag_yes_no(child_text(buyer, ns_uri, "GV"))),
        ]
    )

    notes = child_node(fa, ns_uri, "Adnotacje")
    exemption = child_node(notes, ns_uri, "Zwolnienie")
    new_transport = child_node(notes, ns_uri, "NoweSrodkiTransportu")
    margin = child_node(notes, ns_uri, "PMarzy")
    rows.extend(
        [
            ("Metoda kasowa", flag_yes_no(child_text(notes, ns_uri, "P_16"))),
            ("Samofakturowanie", flag_yes_no(child_text(notes, ns_uri, "P_17"))),
            ("Odwrotne obciazenie", flag_yes_no(child_text(notes, ns_uri, "P_18"))),
            ("Mechanizm podzielonej platnosci", flag_yes_no(child_text(notes, ns_uri, "P_18A"))),
            ("Sprzedaz zwolniona", flag_no_yes(child_text(exemption, ns_uri, "P_19N"))),
            ("Zwolnienie - podstawa P_19", child_text(exemption, ns_uri, "P_19")),
            ("Zwolnienie - opis P_19A", child_text(exemption, ns_uri, "P_19A")),
            ("Zwolnienie - opis P_19B", child_text(exemption, ns_uri, "P_19B")),
            ("Zwolnienie - opis P_19C", child_text(exemption, ns_uri, "P_19C")),
            ("WDT nowych srodkow transportu", flag_no_yes(child_text(new_transport, ns_uri, "P_22N"))),
            ("Procedura uproszczona transakcji trojstronnej UE", flag_yes_no(child_text(notes, ns_uri, "P_23"))),
            ("Procedura marzy", flag_no_yes(child_text(margin, ns_uri, "P_PMarzyN"))),
        ]
    )

    return [(label, value) for label, value in rows if value and value != "-"]


def count_field_paths(all_fields: list[tuple[str, str]]) -> Counter[str]:
    return Counter(key for key, _ in all_fields)


def mark_one(counts: dict[str, int], totals: Counter[str], path: str) -> None:
    if totals[path]:
        counts[path] = max(counts.get(path, 0), 1)


def mark_all(counts: dict[str, int], totals: Counter[str], path: str) -> None:
    if totals[path]:
        counts[path] = totals[path]


def build_visualized_field_counts(all_fields: list[tuple[str, str]]) -> dict[str, int]:
    totals = count_field_paths(all_fields)
    counts: dict[str, int] = {}

    single_occurrence_paths = {
        "Faktura@schemaLocation",
        invoice_path("Podmiot1", "DaneIdentyfikacyjne", "Nazwa"),
        invoice_path("Podmiot1", "DaneIdentyfikacyjne", "NIP"),
        invoice_path("Podmiot1", "DaneIdentyfikacyjne", "BrakID"),
        invoice_path("Podmiot1", "DaneIdentyfikacyjne", "KodUE"),
        invoice_path("Podmiot1", "DaneIdentyfikacyjne", "NrVatUE"),
        invoice_path("Podmiot1", "Adres", "KodKraju"),
        invoice_path("Podmiot1", "Adres", "AdresL1"),
        invoice_path("Podmiot1", "Adres", "AdresL2"),
        invoice_path("Podmiot1", "Adres", "GLN"),
        invoice_path("Podmiot1", "PrefiksPodatnika"),
        invoice_path("Podmiot1", "NrKlienta"),
        invoice_path("Podmiot1", "NrEORI"),
        invoice_path("Podmiot1", "IDNabywcy"),
        invoice_path("Podmiot1", "AdresKoresp", "KodKraju"),
        invoice_path("Podmiot1", "AdresKoresp", "AdresL1"),
        invoice_path("Podmiot1", "AdresKoresp", "AdresL2"),
        invoice_path("Podmiot1", "AdresKoresp", "GLN"),
        invoice_path("Podmiot2", "DaneIdentyfikacyjne", "Nazwa"),
        invoice_path("Podmiot2", "DaneIdentyfikacyjne", "NIP"),
        invoice_path("Podmiot2", "DaneIdentyfikacyjne", "BrakID"),
        invoice_path("Podmiot2", "DaneIdentyfikacyjne", "KodUE"),
        invoice_path("Podmiot2", "DaneIdentyfikacyjne", "NrVatUE"),
        invoice_path("Podmiot2", "Adres", "KodKraju"),
        invoice_path("Podmiot2", "Adres", "AdresL1"),
        invoice_path("Podmiot2", "Adres", "AdresL2"),
        invoice_path("Podmiot2", "Adres", "GLN"),
        invoice_path("Podmiot2", "PrefiksPodatnika"),
        invoice_path("Podmiot2", "NrKlienta"),
        invoice_path("Podmiot2", "NrEORI"),
        invoice_path("Podmiot2", "IDNabywcy"),
        invoice_path("Podmiot2", "AdresKoresp", "KodKraju"),
        invoice_path("Podmiot2", "AdresKoresp", "AdresL1"),
        invoice_path("Podmiot2", "AdresKoresp", "AdresL2"),
        invoice_path("Podmiot2", "AdresKoresp", "GLN"),
        invoice_path("Fa", "KodWaluty"),
        invoice_path("Fa", "P_1"),
        invoice_path("Fa", "P_1M"),
        invoice_path("Fa", "P_2"),
        invoice_path("Fa", "P_6"),
        invoice_path("Fa", "OkresFa", "P_6_Od"),
        invoice_path("Fa", "OkresFa", "P_6_Do"),
        invoice_path("Fa", "RodzajFaktury"),
        invoice_path("Fa", "P_15"),
        invoice_path("Fa", "Platnosc", "TerminPlatnosci", "Termin"),
        invoice_path("Fa", "Platnosc", "TerminPlatnosci", "TerminOpis", "Ilosc"),
        invoice_path("Fa", "Platnosc", "TerminPlatnosci", "TerminOpis", "Jednostka"),
        invoice_path("Fa", "Platnosc", "TerminPlatnosci", "TerminOpis", "ZdarzeniePoczatkowe"),
        invoice_path("Fa", "Platnosc", "FormaPlatnosci"),
        invoice_path("Fa", "Platnosc", "PlatnoscInna"),
        invoice_path("Fa", "Platnosc", "OpisPlatnosci"),
        invoice_path("Fa", "Platnosc", "Zaplacono"),
        invoice_path("Fa", "Platnosc", "DataZaplaty"),
        invoice_path("Fa", "Platnosc", "RachunekBankowy", "NrRB"),
        invoice_path("Fa", "Platnosc", "RachunekBankowy", "NazwaBanku"),
        invoice_path("Fa", "Platnosc", "RachunekBankowy", "OpisRachunku"),
        invoice_path("Fa", "Platnosc", "RachunekBankowy", "RachunekWlasnyBanku"),
        invoice_path("Fa", "Platnosc", "RachunekBankowy", "SWIFT"),
        invoice_path("Fa", "PrzyczynaKorekty"),
        invoice_path("Fa", "Rozliczenie", "DoZaplaty"),
        invoice_path("Naglowek", "KodFormularza@kodSystemowy"),
        invoice_path("Naglowek", "KodFormularza@wersjaSchemy"),
        invoice_path("Naglowek", "KodFormularza"),
        invoice_path("Naglowek", "WariantFormularza"),
        invoice_path("Naglowek", "DataWytworzeniaFa"),
        invoice_path("Naglowek", "SystemInfo"),
        invoice_path("Podmiot2", "JST"),
        invoice_path("Podmiot2", "GV"),
        invoice_path("Fa", "Adnotacje", "P_16"),
        invoice_path("Fa", "Adnotacje", "P_17"),
        invoice_path("Fa", "Adnotacje", "P_18"),
        invoice_path("Fa", "Adnotacje", "P_18A"),
        invoice_path("Fa", "Adnotacje", "Zwolnienie", "P_19N"),
        invoice_path("Fa", "Adnotacje", "Zwolnienie", "P_19"),
        invoice_path("Fa", "Adnotacje", "Zwolnienie", "P_19A"),
        invoice_path("Fa", "Adnotacje", "Zwolnienie", "P_19B"),
        invoice_path("Fa", "Adnotacje", "Zwolnienie", "P_19C"),
        invoice_path("Fa", "Adnotacje", "NoweSrodkiTransportu", "P_22N"),
        invoice_path("Fa", "Adnotacje", "P_23"),
        invoice_path("Fa", "Adnotacje", "PMarzy", "P_PMarzyN"),
        invoice_path("Stopka", "Rejestry", "PelnaNazwa"),
        invoice_path("Stopka", "Rejestry", "KRS"),
        invoice_path("Stopka", "Rejestry", "REGON"),
        invoice_path("Stopka", "Rejestry", "BDO"),
    }

    all_occurrence_paths = {
        invoice_path("Podmiot1", "DaneKontaktowe", "Email"),
        invoice_path("Podmiot1", "DaneKontaktowe", "Telefon"),
        invoice_path("Podmiot2", "DaneKontaktowe", "Email"),
        invoice_path("Podmiot2", "DaneKontaktowe", "Telefon"),
        invoice_path("Podmiot1", "DaneKontaktowe"),
        invoice_path("Podmiot2", "DaneKontaktowe"),
        invoice_path("Podmiot3", "Rola"),
        invoice_path("Podmiot3", "DaneIdentyfikacyjne", "Nazwa"),
        invoice_path("Podmiot3", "DaneIdentyfikacyjne", "NIP"),
        invoice_path("Podmiot3", "DaneIdentyfikacyjne", "BrakID"),
        invoice_path("Podmiot3", "DaneIdentyfikacyjne", "KodUE"),
        invoice_path("Podmiot3", "DaneIdentyfikacyjne", "NrVatUE"),
        invoice_path("Fa", "FaWiersz", "NrWierszaFa"),
        invoice_path("Fa", "FaWiersz", "P_7"),
        invoice_path("Fa", "FaWiersz", "P_6A"),
        invoice_path("Fa", "FaWiersz", "P_8A"),
        invoice_path("Fa", "FaWiersz", "P_8B"),
        invoice_path("Fa", "FaWiersz", "P_9A"),
        invoice_path("Fa", "FaWiersz", "P_9B"),
        invoice_path("Fa", "FaWiersz", "P_10"),
        invoice_path("Fa", "FaWiersz", "P_11"),
        invoice_path("Fa", "FaWiersz", "P_11A"),
        invoice_path("Fa", "FaWiersz", "P_11Vat"),
        invoice_path("Fa", "FaWiersz", "P_12"),
        invoice_path("Fa", "FaWiersz", "Indeks"),
        invoice_path("Fa", "FaWiersz", "GTIN"),
        invoice_path("Fa", "FaWiersz", "UU_ID"),
        invoice_path("Fa", "FaWiersz", "KursWaluty"),
        invoice_path("Fa", "FaWiersz", "KwotaAkcyzy"),
        invoice_path("Fa", "FaWiersz", "StanPrzed"),
        invoice_path("Fa", "DodatkowyOpis", "NrWiersza"),
        invoice_path("Fa", "DodatkowyOpis", "Klucz"),
        invoice_path("Fa", "DodatkowyOpis", "Wartosc"),
        invoice_path("Fa", "DaneFaKorygowanej", "DataWystFaKorygowanej"),
        invoice_path("Fa", "DaneFaKorygowanej", "NrFaKorygowanej"),
        invoice_path("Fa", "DaneFaKorygowanej", "NrKSeF"),
        invoice_path("Fa", "DaneFaKorygowanej", "NrKSeFFaKorygowanej"),
        invoice_path("Fa", "DaneFaKorygowanej", "NrKSeFN"),
        invoice_path("Fa", "WZ"),
        invoice_path("Fa", "ZaliczkaCzesciowa", "P_6Z"),
        invoice_path("Fa", "ZaliczkaCzesciowa", "P_15Z"),
        invoice_path("Fa", "WarunkiTransakcji", "Zamowienia", "NrZamowienia"),
        invoice_path("Fa", "WarunkiTransakcji", "Zamowienia", "DataZamowienia"),
        invoice_path("Stopka", "Informacje", "StopkaFaktury"),
        invoice_path("Stopka", "Informacje"),
    }

    for path in single_occurrence_paths:
        mark_one(counts, totals, path)
    for path in all_occurrence_paths:
        mark_all(counts, totals, path)

    for key, _ in all_fields:
        leaf_name = key.rsplit("/", 1)[-1]
        if leaf_name.startswith("P_13_") or leaf_name.startswith("P_14_"):
            mark_one(counts, totals, key)

    return counts


def parse_invoice(xml_path: Path) -> InvoiceData:
    xml_bytes = xml_path.read_bytes()
    root = ET.fromstring(xml_bytes)
    ns_uri = ""
    if root.tag.startswith("{") and "}" in root.tag:
        ns_uri = root.tag[1 : root.tag.index("}")]

    all_fields: list[tuple[str, str]] = []
    flatten_xml_fields(root, "", all_fields)

    fa = root.find(tag_with_ns(ns_uri, "Fa"))
    if fa is None:
        raise ValueError(f"Missing <Fa> node in {xml_path}")

    items: list[InvoiceItem] = []
    for row in fa.findall(tag_with_ns(ns_uri, "FaWiersz")):
        vat_rate = child_text(row, ns_uri, "P_12")
        unit_net = child_decimal(row, ns_uri, "P_9A")
        unit_gross = child_decimal(row, ns_uri, "P_9B")
        net_amount_raw = child_decimal(row, ns_uri, "P_11")
        gross_amount_raw = child_decimal(row, ns_uri, "P_11A")
        vat_amount_raw = child_decimal(row, ns_uri, "P_11Vat")

        # FA(3) line values can be expressed either net (P_9A/P_11) or gross (P_9B/P_11A).
        if gross_amount_raw is not None:
            gross_amount = gross_amount_raw
            net_amount = net_amount_raw if net_amount_raw is not None else derive_net_from_gross(gross_amount, vat_rate)
        else:
            net_amount = net_amount_raw if net_amount_raw is not None else Decimal("0")
            gross_amount = net_amount

        if vat_amount_raw is not None:
            vat_amount = vat_amount_raw
        elif gross_amount_raw is not None:
            vat_amount = derive_vat_from_gross(gross_amount, vat_rate)
        else:
            vat_amount = derive_vat_from_net(net_amount, vat_rate)

        if gross_amount_raw is None:
            gross_amount = net_amount + vat_amount

        if unit_net is not None:
            unit_price = unit_net
            unit_price_kind = "net"
        elif unit_gross is not None:
            unit_price = unit_gross
            unit_price_kind = "gross"
        else:
            unit_price = Decimal("0")
            unit_price_kind = ""

        item_extra_rows: list[tuple[str, str]] = []
        for label, tag_name in [
            ("Data dostawy/uslugi pozycji", "P_6A"),
            ("Indeks", "Indeks"),
            ("GTIN", "GTIN"),
            ("UU_ID", "UU_ID"),
            ("Kwota opustu", "P_10"),
            ("Kurs waluty", "KursWaluty"),
            ("Kwota akcyzy", "KwotaAkcyzy"),
        ]:
            append_row(item_extra_rows, label, child_text(row, ns_uri, tag_name))

        items.append(
            InvoiceItem(
                line_no=child_text(row, ns_uri, "NrWierszaFa"),
                description=child_text(row, ns_uri, "P_7"),
                unit=child_text(row, ns_uri, "P_8A"),
                quantity=parse_decimal(child_text(row, ns_uri, "P_8B")),
                unit_price=unit_price,
                unit_price_kind=unit_price_kind,
                net_amount=net_amount,
                vat_rate=vat_rate,
                vat_amount=vat_amount,
                gross_amount=gross_amount,
                before_correction=before_correction_label(child_text(row, ns_uri, "StanPrzed")),
                extra_rows=item_extra_rows,
            )
        )

    net_total = sum_numbered_fields(fa, ns_uri, "P_13")
    if net_total == Decimal("0"):
        net_total = sum((i.net_amount for i in items), Decimal("0"))

    vat_total = sum_numbered_fields(fa, ns_uri, "P_14")
    if vat_total == Decimal("0"):
        vat_total = sum((i.vat_amount for i in items), Decimal("0"))

    gross_total = parse_decimal(child_text(fa, ns_uri, "P_15"))
    if gross_total == Decimal("0"):
        gross_total = net_total + vat_total

    payment = fa.find(tag_with_ns(ns_uri, "Platnosc"))
    due = child_text(
        payment.find(tag_with_ns(ns_uri, "TerminPlatnosci")) if payment is not None else None,
        ns_uri,
        "Termin",
    )
    method_code = child_text(payment, ns_uri, "FormaPlatnosci")
    method_display = PAYMENT_METHOD_BY_CODE.get(method_code, method_code)
    bank = payment.find(tag_with_ns(ns_uri, "RachunekBankowy")) if payment is not None else None

    period = fa.find(tag_with_ns(ns_uri, "OkresFa"))

    seller = parse_party(root, ns_uri, "Podmiot1")
    qr_url = build_qr_verification_url(
        seller_nip=seller.nip,
        issue_date=child_text(fa, ns_uri, "P_1"),
        xml_bytes=xml_bytes,
    )

    currency = child_text(fa, ns_uri, "KodWaluty", "PLN")

    return InvoiceData(
        invoice_number=child_text(fa, ns_uri, "P_2"),
        issue_date=child_text(fa, ns_uri, "P_1"),
        sale_date=child_text(fa, ns_uri, "P_6"),
        issue_place=child_text(fa, ns_uri, "P_1M"),
        currency=currency,
        invoice_type=child_text(fa, ns_uri, "RodzajFaktury", "VAT"),
        period_from=child_text(period, ns_uri, "P_6_Od"),
        period_to=child_text(period, ns_uri, "P_6_Do"),
        seller=seller,
        buyer=parse_party(root, ns_uri, "Podmiot2"),
        items=items,
        payment_due_date=due,
        payment_method=method_display,
        bank_account=child_text(bank, ns_uri, "NrRB"),
        bank_name=child_text(bank, ns_uri, "NazwaBanku"),
        net_total=net_total,
        vat_total=vat_total,
        gross_total=gross_total,
        summary_extra_rows=build_summary_extra_rows(fa, ns_uri, currency),
        qr_url=qr_url,
        all_fields=all_fields,
        visualized_field_counts=build_visualized_field_counts(all_fields),
        schema_note_rows=build_schema_note_rows(root, fa, ns_uri),
        correction_rows=build_correction_rows(fa, ns_uri),
        transaction_rows=build_transaction_rows(fa, ns_uri),
        additional_party_rows=build_additional_party_rows(root, ns_uri),
        additional_description_rows=build_additional_description_rows(fa, ns_uri),
        payment_rows=build_payment_rows(payment, ns_uri, method_display),
        footer_rows=build_footer_rows(root, ns_uri),
    )


def format_amount(value: Decimal, currency: str) -> str:
    fixed = round_money(value)
    text = f"{fixed:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} {currency}"


def format_qty(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return f"{normalized:f}"


def unit_price_heading(items: list[InvoiceItem]) -> str:
    kinds = {item.unit_price_kind for item in items if item.unit_price_kind}
    if kinds == {"gross"}:
        return "Cena brutto"
    if kinds == {"net"}:
        return "Cena netto"
    return "Cena jedn."


def discover_system_font_pair() -> Optional[tuple[Path, Path]]:
    candidates = [
        (Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")),
        (Path(r"C:\Windows\Fonts\segoeui.ttf"), Path(r"C:\Windows\Fonts\segoeuib.ttf")),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            return regular, bold
    return None


def configure_font(pdf: FPDF, regular_font: Optional[Path], bold_font: Optional[Path]) -> tuple[str, bool]:
    if regular_font and bold_font:
        if not regular_font.exists() or not bold_font.exists():
            raise FileNotFoundError("Provided font path does not exist")
        pdf.add_font("InvoiceFont", "", str(regular_font))
        pdf.add_font("InvoiceFont", "B", str(bold_font))
        return "InvoiceFont", True

    discovered = discover_system_font_pair()
    if discovered is not None:
        regular, bold = discovered
        pdf.add_font("InvoiceFont", "", str(regular))
        pdf.add_font("InvoiceFont", "B", str(bold))
        return "InvoiceFont", True

    return "Helvetica", False


def encode_text(text: str, unicode_enabled: bool) -> str:
    # Strip control characters that cannot be represented in PDF text streams.
    cleaned = "".join(" " if (ord(ch) < 32 or 127 <= ord(ch) < 160) else ch for ch in text)
    cleaned = cleaned.translate(
        {
            0x2010: ord("-"),
            0x2011: ord("-"),
            0x2012: ord("-"),
            0x2013: ord("-"),
            0x2014: ord("-"),
            0x2212: ord("-"),
        }
    )
    if unicode_enabled:
        return cleaned
    return cleaned.encode("latin-1", errors="replace").decode("latin-1")


def draw_key_value_section(
    pdf: FPDF,
    title: str,
    rows: list[tuple[str, str]],
    family: str,
    unicode_enabled: bool,
    *,
    font_size: int = 9,
    label_width: int = 55,
) -> None:
    if not rows:
        return

    pdf.set_fill_color(243, 245, 248)
    pdf.set_font(family, "B", 11)
    pdf.cell(0, 8, encode_text(title, unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font(family, "", font_size)
    with pdf.table(
        width=pdf.epw,
        col_widths=(label_width, pdf.epw - label_width),
        line_height=5,
        first_row_as_headings=False,
        text_align=("L", "L"),
    ) as table:
        for label, value in rows:
            row = table.row()
            row.cell(encode_text(label, unicode_enabled))
            row.cell(encode_text(value, unicode_enabled))
    pdf.ln(2)


def draw_three_column_section(
    pdf: FPDF,
    title: str,
    headers: tuple[str, str, str],
    rows: list[tuple[str, str, str]],
    family: str,
    unicode_enabled: bool,
    *,
    col_widths: tuple[int, int, int],
    font_size: int = 8,
) -> None:
    if not rows:
        return

    pdf.set_fill_color(243, 245, 248)
    pdf.set_font(family, "B", 11)
    pdf.cell(0, 8, encode_text(title, unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font(family, "", font_size)
    with pdf.table(
        width=pdf.epw,
        col_widths=col_widths,
        line_height=5,
        first_row_as_headings=True,
        text_align=("L", "L", "L"),
        headings_style=FontFace(emphasis="B"),
    ) as table:
        header = table.row()
        for label in headers:
            header.cell(encode_text(label, unicode_enabled))
        for first, second, third in rows:
            row = table.row()
            row.cell(encode_text(first or "-", unicode_enabled))
            row.cell(encode_text(second or "-", unicode_enabled))
            row.cell(encode_text(third or "-", unicode_enabled))
    pdf.ln(2)


def draw_party_block(pdf: FPDF, title: str, party: Party, family: str, unicode_enabled: bool) -> None:
    pdf.set_fill_color(243, 245, 248)
    pdf.set_font(family, "B", 11)
    pdf.cell(0, 8, encode_text(title, unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font(family, "", 10)
    pdf.cell(0, 6, encode_text(party.name or "-", unicode_enabled), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, encode_text(f"NIP: {party.nip or '-'}", unicode_enabled), new_x="LMARGIN", new_y="NEXT")
    for line in party.address_lines:
        pdf.cell(0, 6, encode_text(line, unicode_enabled), new_x="LMARGIN", new_y="NEXT")
    if party.extra_rows:
        pdf.set_font(family, "", 8)
        for label, value in party.extra_rows:
            pdf.multi_cell(0, 4.5, encode_text(f"{label}: {value}", unicode_enabled), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)


def create_qr_temp_png(data: str) -> Path:
    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'qrcode'. Install from requirements.txt.") from exc

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    temp_path = Path(temp_file.name)
    temp_file.close()
    image.save(str(temp_path))
    return temp_path


def render_invoice_pdf(
    invoice: InvoiceData,
    pdf_path: Path,
    *,
    regular_font: Optional[Path],
    bold_font: Optional[Path],
    hide_empty_fields: bool,
    ksef_id: Optional[str] = None,
) -> None:
    qr_temp_path: Optional[Path] = None
    normalized_ksef_id = (ksef_id or "").strip()

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.set_margins(10, 10, 10)
    pdf.add_page()

    family, unicode_enabled = configure_font(pdf, regular_font, bold_font)

    invoice_type_label = correction_invoice_type_label(invoice.invoice_type)
    pdf.set_font(family, "B", 17)
    pdf.cell(0, 10, encode_text(f"{invoice_type_label} - wizualizacja z XML KSeF", unicode_enabled), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 10)
    pdf.cell(
        0,
        6,
        encode_text(
            f"Numer: {invoice.invoice_number or '-'}   Data: {invoice.issue_date or '-'}   Typ: {invoice.invoice_type or '-'}",
            unicode_enabled,
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    if normalized_ksef_id:
        pdf.cell(
            0,
            6,
            encode_text(f"Identyfikator KSeF: {normalized_ksef_id}", unicode_enabled),
            new_x="LMARGIN",
            new_y="NEXT",
        )
    place = invoice.issue_place or "-"
    sale_date = invoice.sale_date or "-"
    period_text = (
        f"Okres: {invoice.period_from} - {invoice.period_to}"
        if invoice.period_from or invoice.period_to
        else "Okres: -"
    )
    pdf.cell(
        0,
        6,
        encode_text(
            f"Miejsce wystawienia: {place}   Data sprzedazy: {sale_date}   {period_text}",
            unicode_enabled,
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    if invoice.correction_rows:
        draw_key_value_section(
            pdf,
            "Dane faktury korygowanej",
            invoice.correction_rows,
            family,
            unicode_enabled,
            label_width=62,
        )

    draw_party_block(pdf, "Sprzedawca", invoice.seller, family, unicode_enabled)
    draw_party_block(pdf, "Nabywca", invoice.buyer, family, unicode_enabled)
    draw_key_value_section(
        pdf,
        "Podmioty dodatkowe",
        invoice.additional_party_rows,
        family,
        unicode_enabled,
    )

    if invoice.schema_note_rows:
        pdf.set_fill_color(243, 245, 248)
        pdf.set_font(family, "B", 11)
        pdf.cell(0, 8, encode_text("Dane KSeF i oznaczenia", unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_font(family, "", 8)
        with pdf.table(
            width=pdf.epw,
            col_widths=(48, 44, 48, 45),
            line_height=4.5,
            first_row_as_headings=False,
            text_align=("L", "L", "L", "L"),
        ) as table:
            rows = invoice.schema_note_rows
            for idx in range(0, len(rows), 2):
                row = table.row()
                left_label, left_value = rows[idx]
                row.cell(encode_text(left_label, unicode_enabled))
                row.cell(encode_text(left_value, unicode_enabled))
                if idx + 1 < len(rows):
                    right_label, right_value = rows[idx + 1]
                    row.cell(encode_text(right_label, unicode_enabled))
                    row.cell(encode_text(right_value, unicode_enabled))
                else:
                    row.cell("")
                    row.cell("")
        pdf.ln(2)

    draw_key_value_section(
        pdf,
        "Warunki transakcji",
        invoice.transaction_rows,
        family,
        unicode_enabled,
    )

    pdf.set_fill_color(243, 245, 248)
    pdf.set_font(family, "B", 11)
    pdf.cell(0, 8, encode_text("Pozycje faktury", unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font(family, "", 8)

    if not invoice.items:
        pdf.set_font(family, "", 9)
        pdf.multi_cell(
            0,
            6,
            encode_text(
                "Brak pozycji szczegolowych w XML. Faktury korygujace moga zawierac tylko dane korekty i podsumowanie.",
                unicode_enabled,
            ),
            border=1,
        )
    else:
        show_before_correction = any(item.before_correction for item in invoice.items)
        item_col_widths = (
            (6, 42, 7, 9, 16, 14, 9, 13, 15, 14)
            if show_before_correction
            else (6, 54, 8, 10, 17, 16, 10, 15, 16)
        )
        item_text_align = (
            ("C", "L", "C", "R", "R", "R", "R", "R", "R", "C")
            if show_before_correction
            else ("C", "L", "C", "R", "R", "R", "R", "R", "R")
        )
        item_labels = ["Lp", "Opis", "JM", "Ilosc", unit_price_heading(invoice.items), "Netto", "VAT%", "VAT", "Brutto"]
        if show_before_correction:
            item_labels.append("Stan przed")

        headings_style = FontFace(emphasis="B")
        with pdf.table(
            width=pdf.epw,
            col_widths=item_col_widths,
            line_height=5,
            first_row_as_headings=True,
            text_align=item_text_align,
            headings_style=headings_style,
        ) as table:
            header = table.row()
            for label in item_labels:
                header.cell(encode_text(label, unicode_enabled))

            for item in invoice.items:
                row = table.row()
                row.cell(encode_text(item.line_no or "-", unicode_enabled))
                row.cell(encode_text(item.description or "-", unicode_enabled))
                row.cell(encode_text(item.unit or "-", unicode_enabled))
                row.cell(encode_text(format_qty(item.quantity), unicode_enabled))
                row.cell(encode_text(format_amount(item.unit_price, invoice.currency), unicode_enabled))
                row.cell(encode_text(format_amount(item.net_amount, invoice.currency), unicode_enabled))
                row.cell(encode_text(item.vat_rate or "-", unicode_enabled))
                row.cell(encode_text(format_amount(item.vat_amount, invoice.currency), unicode_enabled))
                row.cell(encode_text(format_amount(item.gross_amount, invoice.currency), unicode_enabled))
                if show_before_correction:
                    row.cell(encode_text(item.before_correction, unicode_enabled))

    pdf.ln(2)
    item_extra_rows: list[tuple[str, str, str]] = []
    for item in invoice.items:
        for label, value in item.extra_rows:
            item_extra_rows.append((item.line_no, label, value))
    draw_three_column_section(
        pdf,
        "Dodatkowe dane pozycji",
        ("Wiersz", "Pole", "Wartosc"),
        item_extra_rows,
        family,
        unicode_enabled,
        col_widths=(18, 55, 112),
    )

    pdf.set_fill_color(243, 245, 248)
    pdf.set_font(family, "B", 11)
    pdf.cell(0, 8, encode_text("Podsumowanie", unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font(family, "", 10)
    pdf.cell(45, 7, encode_text("Razem netto", unicode_enabled), border=1)
    pdf.cell(0, 7, encode_text(format_amount(invoice.net_total, invoice.currency), unicode_enabled), border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(45, 7, encode_text("Razem VAT", unicode_enabled), border=1)
    pdf.cell(0, 7, encode_text(format_amount(invoice.vat_total, invoice.currency), unicode_enabled), border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "B", 10)
    pdf.cell(45, 7, encode_text("Razem brutto", unicode_enabled), border=1)
    pdf.cell(0, 7, encode_text(format_amount(invoice.gross_total, invoice.currency), unicode_enabled), border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 10)
    for label, value in invoice.summary_extra_rows:
        pdf.cell(45, 7, encode_text(label, unicode_enabled), border=1)
        pdf.cell(0, 7, encode_text(value, unicode_enabled), border=1, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    draw_key_value_section(
        pdf,
        "Platnosc",
        invoice.payment_rows,
        family,
        unicode_enabled,
        font_size=10,
        label_width=45,
    )

    draw_three_column_section(
        pdf,
        "Dodatkowe opisy z XML",
        ("Wiersz", "Klucz", "Wartosc"),
        invoice.additional_description_rows,
        family,
        unicode_enabled,
        col_widths=(18, 55, 112),
    )

    draw_key_value_section(
        pdf,
        "Stopka i rejestry",
        invoice.footer_rows,
        family,
        unicode_enabled,
        label_width=45,
    )

    if invoice.qr_url:
        if pdf.get_y() > 220:
            pdf.add_page()
        pdf.ln(2)
        pdf.set_fill_color(243, 245, 248)
        pdf.set_font(family, "B", 11)
        pdf.cell(0, 8, encode_text("Kod QR weryfikacji KSeF", unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_font(family, "", 9)
        pdf.multi_cell(0, 5, encode_text(invoice.qr_url, unicode_enabled))

        qr_temp_path = create_qr_temp_png(invoice.qr_url)
        qr_size = 34
        qr_x = pdf.l_margin
        qr_y = pdf.get_y() + 1
        pdf.image(str(qr_temp_path), x=qr_x, y=qr_y, w=qr_size, h=qr_size)
        pdf.set_xy(qr_x + qr_size + 4, qr_y)
        pdf.multi_cell(
            0,
            5,
            encode_text(
                "Zeskanuj QR, aby otworzyc publiczny podglad faktury w usludze weryfikacyjnej KSeF.",
                unicode_enabled,
            ),
        )
        pdf.set_y(max(pdf.get_y(), qr_y + qr_size + 2))

    pdf.add_page()
    pdf.set_fill_color(243, 245, 248)
    pdf.set_font(family, "B", 12)
    pdf.cell(0, 8, encode_text("Pola XML (sciezka -> wartosc)", unicode_enabled), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_font(family, "", 8)
    pdf.set_text_color(190, 30, 30)
    pdf.cell(
        0,
        5,
        encode_text(
            "Kolumna Wyst. pokazuje wystapienie n/N dla danej sciezki XML; czerwone wystapienia nie sa pokazane w glownej wizualizacji.",
            unicode_enabled,
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(family, "", 8)

    missing_field_style = FontFace(color=(190, 30, 30))
    with pdf.table(
        width=pdf.epw,
        col_widths=(82, 18, 85),
        line_height=4,
        first_row_as_headings=True,
        text_align=("L", "C", "L"),
        headings_style=FontFace(emphasis="B"),
    ) as table:
        header = table.row()
        header.cell(encode_text("Pole XML", unicode_enabled))
        header.cell(encode_text("Wyst.", unicode_enabled))
        header.cell(encode_text("Wartosc", unicode_enabled))

        path_totals = count_field_paths(invoice.all_fields)
        path_seen: Counter[str] = Counter()

        for key, value in invoice.all_fields:
            if hide_empty_fields and not value:
                continue
            path_seen[key] += 1
            occurrence_no = path_seen[key]
            total_occurrences = path_totals[key]
            occurrence_text = f"{occurrence_no}/{total_occurrences}"
            field_style = (
                None
                if occurrence_no <= invoice.visualized_field_counts.get(key, 0)
                else missing_field_style
            )
            row = table.row()
            row.cell(encode_text(key, unicode_enabled), style=field_style)
            row.cell(encode_text(occurrence_text, unicode_enabled), style=field_style)
            row.cell(encode_text(value if value else "", unicode_enabled), style=field_style)

    try:
        pdf.output(str(pdf_path))
    finally:
        if qr_temp_path and qr_temp_path.exists():
            qr_temp_path.unlink(missing_ok=True)


def collect_xml_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.glob("*.xml"))
    raise FileNotFoundError(f"Input path not found: {path}")


def resolve_output_path(xml_file: Path, input_path: Path, output_path: Optional[Path]) -> Path:
    if input_path.is_file():
        if output_path is None:
            return xml_file.with_suffix(".pdf")
        if output_path.suffix.lower() == ".pdf":
            return output_path
        output_path.mkdir(parents=True, exist_ok=True)
        return output_path / f"{xml_file.stem}.pdf"

    output_dir = output_path if output_path is not None else input_path
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{xml_file.stem}.pdf"


def main() -> int:
    args = parse_args()

    if (args.font_regular is None) != (args.font_bold is None):
        print("Use both --font-regular and --font-bold together.", file=sys.stderr)
        return 2

    try:
        xml_files = collect_xml_files(args.input)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not xml_files:
        print(f"No XML files found in {args.input}", file=sys.stderr)
        return 1

    if args.ksef_id and len(xml_files) != 1:
        print("Use --ksef-id only when rendering a single XML file.", file=sys.stderr)
        return 2

    rendered = 0
    for xml_file in xml_files:
        try:
            invoice = parse_invoice(xml_file)
            out_pdf = resolve_output_path(xml_file, args.input, args.output)
            render_invoice_pdf(
                invoice,
                out_pdf,
                regular_font=args.font_regular,
                bold_font=args.font_bold,
                hide_empty_fields=args.hide_empty_fields,
                ksef_id=args.ksef_id if len(xml_files) == 1 else None,
            )
            print(f"OK: {xml_file} -> {out_pdf}")
            rendered += 1
        except Exception as exc:
            print(f"ERROR: {xml_file}: {exc}", file=sys.stderr)

    return 0 if rendered == len(xml_files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
