"""
Google People API wrapper.

All public functions accept a `service` object built from
  googleapiclient.discovery.build('people', 'v1', credentials=creds)
"""

import json
import os
import time
from datetime import datetime

import pandas as pd
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def _execute(request, retries: int = 3, backoff: float = 2.0):
    """Execute an API request with exponential backoff on 5xx / 429 errors."""
    for attempt in range(retries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            if status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(backoff ** attempt)
                continue
            raise

PERSON_FIELDS = (
    "names,phoneNumbers,emailAddresses,memberships,"
    "organizations,biographies,addresses,metadata"
)


# ─── Service factory ────────────────────────────────────────────────────────

def build_service(creds):
    return build("people", "v1", credentials=creds)


# ─── Groups ─────────────────────────────────────────────────────────────────

def fetch_groups(service) -> dict:
    """Returns {resourceName: displayName} for user-created contact groups."""
    result = _execute(service.contactGroups().list())
    groups = result.get("contactGroups", [])
    return {
        g["resourceName"]: g["name"]
        for g in groups
        if g.get("groupType") == "USER_CONTACT_GROUP"
    }


def create_group(service, name: str) -> tuple[str, str]:
    """Creates a new contact group. Returns (resourceName, name)."""
    result = _execute(
        service.contactGroups().create(body={"contactGroup": {"name": name}})
    )
    return result["resourceName"], result["name"]


def assign_labels_to_contacts(service, resource_names: list[str], group_resource_name: str):
    """Adds all resource_names to the given group."""
    _execute(
        service.contactGroups().members().modify(
            resourceName=group_resource_name,
            body={"resourceNamesToAdd": resource_names},
        )
    )


# ─── Fetch ───────────────────────────────────────────────────────────────────

def fetch_all_contacts(service) -> list[dict]:
    """Fetches every contact, handling pagination automatically."""
    contacts = []
    page_token = None
    while True:
        resp = _execute(
            service.people()
            .connections()
            .list(
                resourceName="people/me",
                pageSize=1000,
                personFields=PERSON_FIELDS,
                pageToken=page_token,
            )
        )
        contacts.extend(resp.get("connections", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return contacts


def contacts_to_df(contacts: list[dict], groups_map: dict) -> pd.DataFrame:
    """Flattens the raw People API response into a display DataFrame."""
    rows = []
    for c in contacts:
        resource_name = c.get("resourceName", "")
        etag = c.get("etag", "")

        names = c.get("names") or [{}]
        given = names[0].get("givenName", "")
        family = names[0].get("familyName", "")

        phones = c.get("phoneNumbers") or []
        mobile_phone = next(
            (p.get("value", "") for p in phones
             if p.get("type", "").lower() in ("mobile", "cellular")), ""
        )
        # 2. Telefon = ilk MOBİL OLMAYAN telefon (_rebuild_phones ile tutarlı)
        second_phone = next(
            (p.get("value", "") for p in phones
             if p.get("type", "").lower() not in ("mobile", "cellular")), ""
        )

        # Hangi telefon primary? metadata.primary=True olanı bul; yoksa phones[0]
        primary_phone_val = next(
            (p.get("value", "") for p in phones
             if p.get("metadata", {}).get("primary", False)), ""
        ) or (phones[0].get("value", "") if phones else "")
        if primary_phone_val and primary_phone_val == mobile_phone:
            primary_phone_col = "Cep Telefonu"
        elif primary_phone_val and primary_phone_val == second_phone:
            primary_phone_col = "2. Telefon"
        elif primary_phone_val:
            # primary numara başka bir slotta (mobil ya da 2. değil) — cep varsa onu işaretle
            primary_phone_col = "Cep Telefonu" if mobile_phone else "2. Telefon" if second_phone else ""
        else:
            primary_phone_col = ""

        emails = c.get("emailAddresses") or []
        primary_email = emails[0].get("value", "") if emails else ""
        second_email = emails[1].get("value", "") if len(emails) > 1 else ""

        addresses = c.get("addresses") or []
        addr_raw = addresses[0] if addresses else {}
        address = addr_raw.get("formattedValue", "") or ", ".join(filter(None, [
            addr_raw.get("streetAddress", ""),
            addr_raw.get("city", ""),
            addr_raw.get("region", ""),
            addr_raw.get("country", ""),
        ]))

        memberships = c.get("memberships") or []
        label_names = []
        for m in memberships:
            grn = m.get("contactGroupMembership", {}).get("contactGroupResourceName", "")
            if grn in groups_map:
                label_names.append(groups_map[grn])
        labels = ", ".join(label_names)

        orgs = c.get("organizations") or [{}]
        company = orgs[0].get("name", "") if orgs else ""
        title = orgs[0].get("title", "") if orgs else ""

        bios = c.get("biographies") or [{}]
        notes = bios[0].get("value", "") if bios else ""

        # Oluşturulma zamanı — metadata.sources içindeki en eski updateTime
        sources = c.get("metadata", {}).get("sources", [])
        update_times = [s.get("updateTime", "") for s in sources if s.get("updateTime")]
        raw_time = min(update_times) if update_times else ""
        if raw_time:
            try:
                dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                last_updated = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                last_updated = raw_time[:16]
        else:
            last_updated = ""

        rows.append(
            {
                "_resource_name": resource_name,
                "_etag": etag,
                "_phones_raw": json.dumps(phones),
                "_emails_raw": json.dumps(emails),
                "_addresses_raw": json.dumps(addresses),
                "_primary_phone_col": primary_phone_col,
                "Oluşturulma": last_updated,
                "Ad": given,
                "Soyad": family,
                "Cep Telefonu": mobile_phone,
                "2. Telefon": second_phone,
                "E-posta": primary_email,
                "2. E-posta": second_email,
                "Etiketler": labels,
                "Şirket": company,
                "Ünvan": title,
                "Notlar": notes,
                "Adres": address,
            }
        )

    df = pd.DataFrame(rows)
    # Fill NaN with empty string so comparisons are stable
    return df.fillna("")


# ─── Create ──────────────────────────────────────────────────────────────────

def create_contact(service, row: dict) -> dict:
    """Creates a new contact. row is a plain dict with display-column keys."""
    def s(k): return str(row.get(k, "") or "").strip()

    person: dict = {
        "names": [{"givenName": s("Ad"), "familyName": s("Soyad")}]
    }

    # Phones
    phones = []
    if s("Cep Telefonu"):
        phones.append({"value": s("Cep Telefonu"), "type": "mobile"})
    if s("2. Telefon"):
        phones.append({"value": s("2. Telefon")})
    if phones:
        person["phoneNumbers"] = phones

    # Emails
    emails = []
    if s("E-posta"):
        emails.append({"value": s("E-posta")})
    if s("2. E-posta"):
        emails.append({"value": s("2. E-posta")})
    if emails:
        person["emailAddresses"] = emails

    if s("Şirket") or s("Ünvan"):
        person["organizations"] = [{"name": s("Şirket"), "title": s("Ünvan")}]
    if s("Notlar"):
        person["biographies"] = [{"value": s("Notlar"), "contentType": "TEXT_PLAIN"}]
    if s("Adres"):
        person["addresses"] = [{"formattedValue": s("Adres")}]

    return _execute(service.people().createContact(body=person))


# ─── Phone / Email / Address rebuild helpers ─────────────────────────────────

def _rebuild_phones(orig: list, new_mobile: str, new_second: str) -> list:
    """
    Orijinal phones dizisini koruyarak Cep Telefonu ve 2. Telefon slotlarını günceller.
    """
    phones = [dict(p) for p in orig]

    # --- Cep Telefonu (mobile) ---
    mob_idx = next(
        (i for i, p in enumerate(phones)
         if p.get("type", "").lower() in ("mobile", "cellular")), None
    )
    if new_mobile:
        if mob_idx is not None:
            phones[mob_idx]["value"] = new_mobile
        else:
            phones.insert(0, {"value": new_mobile, "type": "mobile"})
    else:
        if mob_idx is not None:
            phones.pop(mob_idx)

    # non-mobile listesini yenile
    non_mob = [i for i, p in enumerate(phones)
               if p.get("type", "").lower() not in ("mobile", "cellular")]

    # --- 2. Telefon (mobil olmayan ilk/ikinci) ---
    if new_second:
        if non_mob:
            phones[non_mob[0]]["value"] = new_second
        else:
            phones.append({"value": new_second})
    else:
        if non_mob:
            phones.pop(non_mob[0])

    return phones


def _rebuild_emails(orig: list, new_primary: str, new_second: str) -> list:
    """emails[0] ve emails[1] slotlarını günceller, geri kalanı korur."""
    emails = [dict(e) for e in orig]

    if new_primary:
        if emails:
            emails[0]["value"] = new_primary
        else:
            emails.append({"value": new_primary})
    else:
        if emails:
            emails.pop(0)

    if new_second:
        if len(emails) > 1:
            emails[1]["value"] = new_second
        else:
            emails.append({"value": new_second})
    else:
        if len(emails) > 1:
            emails.pop(1)

    return emails


# ─── Update ──────────────────────────────────────────────────────────────────

def update_contact(service, resource_name: str, etag: str, new_row: dict, orig_row: dict):
    """
    Sends a PATCH for only the fields that actually changed.
    Returns the API response, or None if nothing changed.
    """
    person: dict = {"etag": etag}
    update_fields = []

    def s(val):
        return str(val or "").strip()

    # Names
    if s(new_row.get("Ad")) != s(orig_row.get("Ad")) or s(new_row.get("Soyad")) != s(orig_row.get("Soyad")):
        person["names"] = [{"givenName": s(new_row.get("Ad")), "familyName": s(new_row.get("Soyad"))}]
        update_fields.append("names")

    # Phones — orijinal diziyi koruyarak sadece değişen slotları güncelle
    phone_fields = ["Cep Telefonu", "2. Telefon"]
    if any(s(new_row.get(f)) != s(orig_row.get(f)) for f in phone_fields):
        orig_phones = json.loads(orig_row.get("_phones_raw") or "[]")
        person["phoneNumbers"] = _rebuild_phones(
            orig_phones,
            s(new_row.get("Cep Telefonu")),
            s(new_row.get("2. Telefon")),
        )
        update_fields.append("phoneNumbers")

    # Emails — orijinal diziyi koruyarak güncelle
    email_fields = ["E-posta", "2. E-posta"]
    if any(s(new_row.get(f)) != s(orig_row.get(f)) for f in email_fields):
        orig_emails = json.loads(orig_row.get("_emails_raw") or "[]")
        person["emailAddresses"] = _rebuild_emails(
            orig_emails,
            s(new_row.get("E-posta")),
            s(new_row.get("2. E-posta")),
        )
        update_fields.append("emailAddresses")

    # Address
    if s(new_row.get("Adres")) != s(orig_row.get("Adres")):
        orig_addresses = json.loads(orig_row.get("_addresses_raw") or "[]")
        val = s(new_row.get("Adres"))
        if val:
            if orig_addresses:
                addr = dict(orig_addresses[0])
                addr["formattedValue"] = val
                person["addresses"] = [addr] + orig_addresses[1:]
            else:
                person["addresses"] = [{"formattedValue": val}]
        else:
            person["addresses"] = orig_addresses[1:] if len(orig_addresses) > 1 else []
        update_fields.append("addresses")

    # Organization
    if s(new_row.get("Şirket")) != s(orig_row.get("Şirket")) or s(new_row.get("Ünvan")) != s(orig_row.get("Ünvan")):
        sirket = s(new_row.get("Şirket"))
        unvan = s(new_row.get("Ünvan"))
        person["organizations"] = [{"name": sirket, "title": unvan}] if (sirket or unvan) else []
        update_fields.append("organizations")

    # Notes
    if s(new_row.get("Notlar")) != s(orig_row.get("Notlar")):
        val = s(new_row.get("Notlar"))
        person["biographies"] = [{"value": val, "contentType": "TEXT_PLAIN"}] if val else []
        update_fields.append("biographies")

    if not update_fields:
        return None

    result = _execute(
        service.people().updateContact(
            resourceName=resource_name,
            updatePersonFields=",".join(update_fields),
            body=person,
        )
    )
    # Kaydedilen alanları ve yeni etag'i döndür (tanı için)
    result["_updated_fields"] = update_fields
    return result


def set_primary_phone(service, resource_name: str, etag: str, phones_raw: list, target_value: str):
    """Hedef telefonu primary yapar.
    Google People API'de metadata.primary output-only; yazılabilir olan sourcePrimary kullanılır.
    """
    target_idx = next((i for i, p in enumerate(phones_raw) if p.get("value") == target_value), None)
    if target_idx is None:
        return None

    cleaned = []
    for i, p in enumerate(phones_raw):
        # Read-only alanları düşür
        phone = {k: v for k, v in p.items() if k not in ("metadata", "canonicalForm", "formattedType")}
        phone["metadata"] = {"sourcePrimary": i == target_idx}
        cleaned.append(phone)

    # Hedef başa taşı
    cleaned.insert(0, cleaned.pop(target_idx))

    return _execute(
        service.people().updateContact(
            resourceName=resource_name,
            updatePersonFields="phoneNumbers",
            body={"etag": etag, "phoneNumbers": cleaned},
        )
    )


def sync_contact_labels(
    service,
    resource_name: str,
    old_labels_str: str,
    new_labels_str: str,
    groups_map_inv: dict,
    service_ref=None,  # unused, kept for signature compat
):
    """
    Reconciles label membership.
    Auto-creates any new label names that don't yet exist in groups_map_inv.
    Mutates groups_map_inv in-place when new groups are created.
    Returns list of newly created (resourceName, name) tuples.
    """

    def parse(s):
        return {l.strip() for l in str(s or "").split(",") if l.strip()}

    old = parse(old_labels_str)
    new = parse(new_labels_str)

    if old == new:
        return []

    created = []

    # Auto-create unknown labels
    for label in new:
        if label not in groups_map_inv:
            rn, name = create_group(service, label)
            groups_map_inv[name] = rn
            created.append((rn, name))

    to_add = new - old
    to_remove = old - new

    for label in to_add:
        grn = groups_map_inv.get(label)
        if grn:
            _execute(
                service.contactGroups().members().modify(
                    resourceName=grn,
                    body={"resourceNamesToAdd": [resource_name]},
                )
            )

    for label in to_remove:
        grn = groups_map_inv.get(label)
        if grn:
            _execute(
                service.contactGroups().members().modify(
                    resourceName=grn,
                    body={"resourceNamesToRemove": [resource_name]},
                )
            )

    return created


# ─── Delete ──────────────────────────────────────────────────────────────────

def delete_contacts(service, resource_names: list[str]):
    """Deletes one or many contacts (uses batch API when > 1)."""
    if not resource_names:
        return
    try:
        if len(resource_names) == 1:
            _execute(service.people().deleteContact(resourceName=resource_names[0]))
        else:
            # People API batch delete max 500 contacts per request
            for i in range(0, len(resource_names), 500):
                batch = resource_names[i : i + 500]
                _execute(
                    service.people().batchDeleteContacts(body={"resourceNames": batch})
                )
    except HttpError as e:
        raise Exception(f"Silme işlemi başarısız: {e.reason}")


# ─── Backup ──────────────────────────────────────────────────────────────────

def backup_csv(df: pd.DataFrame) -> str:
    """Saves a timestamped CSV backup. Returns the file path."""
    backup_dir = "backups"
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"backup_{ts}.csv")
    export = df.drop(columns=["_resource_name", "_etag"], errors="ignore")
    export.to_csv(path, index=False, encoding="utf-8-sig")
    return path


# ─── Smart Filters ───────────────────────────────────────────────────────────

def apply_filter(df: pd.DataFrame, filter_name: str) -> pd.DataFrame:
    if filter_name == "Telefonu olmayanlar":
        no_cep = df["Cep Telefonu"].str.strip() == ""
        no_second = df["2. Telefon"].str.strip() == ""
        return df[no_cep & no_second].reset_index(drop=True)
    if filter_name == "E-postası olmayanlar":
        return df[df["E-posta"].str.strip() == ""].reset_index(drop=True)
    if filter_name == "Şirketi/Ünvanı olmayanlar":
        return df[(df["Şirket"].str.strip() == "") & (df["Ünvan"].str.strip() == "")].reset_index(drop=True)
    if filter_name == "Yinelenen isimler":
        full_name = (df["Ad"].str.strip() + " " + df["Soyad"].str.strip()).str.lower()
        dupes = full_name[full_name.duplicated(keep=False) & (full_name.str.strip() != "")]
        return df[df.index.isin(dupes.index)].reset_index(drop=True)
    if filter_name == "Yinelenen telefonlar":
        # Cep Telefonu'nu primary kaynak olarak kullan
        phones = df["Cep Telefonu"].str.strip()
        dupes = phones[phones.duplicated(keep=False) & (phones != "")]
        return df[df.index.isin(dupes.index)].reset_index(drop=True)
    return df  # "Tümü"
