"""
Google Contacts Manager — Vibe Edition
Streamlit + st-aggrid frontend for the Google People API.
"""

import csv
import io
import json
import os
import urllib.request
from datetime import datetime

import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode, JsCode

import auth
import contacts_api

# ─── Page config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Google Contacts Manager",
    page_icon="📇",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Session state ────────────────────────────────────────────────────────────

def init_state():
    defaults = {
        "authenticated": False,
        "service": None,
        "df": None,
        "df_original": None,
        "groups_map": {},       # {resourceName: displayName}
        "groups_map_inv": {},   # {displayName: resourceName}
        "active_filter": "Tümü",
        "search_query": "",
        "selected_rows": [],
        "show_delete_confirm": False,
        "delete_resource_names": [],
        "data_version": 0,       # load_data her çağrıldığında artar → grid reload tetiklenir
        "_grid_state": {},       # önceki filter/search/version → reload kararı için
        "grid_data": None,       # AgGrid'e geçilen son veri
        "pending_edits": {},     # {resource_name: {field: yeni_değer}} — VALUE_CHANGED'da dolar
        "force_grid_reload": False,  # bulk op sonrası grid'i yenile
        "visible_cols": ["Ad", "Soyad", "Cep Telefonu", "E-posta", "Şirket", "Etiketler", "Oluşturulma"],
        "detail_shown_for": None,  # en son dialog açılan rn — aynı satır için tekrar açılmaz
        "_post_save_reload": False,  # kaydet sonrası grid key + reload değişmeden refresh
        "_saved_selection_rns": [],  # kaydet sonrası geri yüklenecek seçimler
        "_saved_grid_data": None,    # kaydet öncesi grid_data yedeği — scroll/sort korunur
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v



# ─── Auth ─────────────────────────────────────────────────────────────────────

def handle_auth_page():
    st.markdown(
        "<h2 style='text-align:center;margin-top:3rem;'>📇 Google Contacts Manager</h2>",
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    cloud = auth.has_cloud_token()
    has_creds = os.path.exists("credentials.json")

    if not cloud and not has_creds:
        st.error(
            "**credentials.json bulunamadı.**\n\n"
            "GCP Console'dan bir OAuth Desktop App credential oluşturup "
            "bu klasöre koy."
        )
        return

    c1, c2, c3 = st.columns([1, 1, 1])
    with c2:
        if st.button("🔑 Google ile Giriş Yap", type="primary", use_container_width=True):
            with st.spinner("Bağlanıyor…"):
                creds = auth.get_credentials()
            if creds:
                st.session_state.service = contacts_api.build_service(creds)
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Giriş başarısız.")


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_data(show_spinner: bool = True):
    service = st.session_state.service
    ctx = st.spinner("Kişiler yükleniyor…") if show_spinner else _noop_ctx()
    with ctx:
        groups_map = contacts_api.fetch_groups(service)
        st.session_state.groups_map = groups_map
        st.session_state.groups_map_inv = {v: k for k, v in groups_map.items()}

        raw = contacts_api.fetch_all_contacts(service)
        df = contacts_api.contacts_to_df(raw, groups_map)
        st.session_state.df = df
        st.session_state.df_original = df.copy()
        st.session_state.show_delete_confirm = False
        st.session_state.pending_edits = {}
        st.session_state.grid_data = None
        st.session_state.data_version += 1


class _noop_ctx:
    def __enter__(self): return self
    def __exit__(self, *_): pass


# ─── Kişi Detay Dialog ────────────────────────────────────────────────────────

@st.dialog("📋 Kişi Detayı", width="large")
def contact_detail_dialog(resource_name: str):
    df_orig = st.session_state.df_original
    rows = df_orig[df_orig["_resource_name"] == resource_name]
    if rows.empty:
        st.warning("Kayıt bulunamadı.")
        return
    row = rows.iloc[0]

    def v(col): return str(row.get(col, "") or "").strip()

    full_name = f"{v('Ad')} {v('Soyad')}".strip()
    st.markdown(f"### {full_name or '(İsimsiz)'}")
    st.divider()

    # Telefonlar
    cep     = v("Cep Telefonu")
    tel2    = v("2. Telefon")
    primary = v("_primary_phone_col")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📱 Cep Telefonu**")
        if cep:
            st.markdown(f"⭐ **{cep}**" if primary == "Cep Telefonu" else cep)
        else:
            st.caption("—")
    with c2:
        st.markdown("**📞 2. Telefon**")
        if tel2:
            st.markdown(f"⭐ **{tel2}**" if primary == "2. Telefon" else tel2)
        else:
            st.caption("—")

    # Primary telefon değiştirici (ikisi de doluysa göster)
    if cep and tel2:
        st.divider()
        options = {"Cep Telefonu": cep, "2. Telefon": tel2}
        cur_idx = 0 if primary == "Cep Telefonu" else 1
        st.markdown("**⭐ Primary Numara**")
        new_primary_label = st.radio(
            "Primary Numara",
            list(options.keys()),
            index=cur_idx,
            format_func=lambda k: f"{k}: {options[k]}",
            horizontal=True,
            label_visibility="collapsed",
            key=f"primary_radio_{resource_name}",
        )
        if new_primary_label != primary:
            if st.button("⭐ Primary'i Güncelle", type="primary", use_container_width=True,
                         key=f"primary_save_{resource_name}"):
                target_val = cep if new_primary_label == "Cep Telefonu" else tel2
                phones_raw = json.loads(row.get("_phones_raw") or "[]")
                etag = str(row.get("_etag", ""))
                try:
                    with st.spinner("Güncelleniyor…"):
                        contacts_api.set_primary_phone(
                            st.session_state.service, resource_name, etag, phones_raw, target_val
                        )
                    load_data(show_spinner=False)
                    st.success(f"✅ Primary numara '{new_primary_label}' olarak güncellendi.")
                except Exception as e:
                    st.error(f"Hata: {e}")

    # E-postalar
    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**✉️ E-posta**")
        st.markdown(v("E-posta") or "—")
    with c4:
        st.markdown("**✉️ 2. E-posta**")
        st.markdown(v("2. E-posta") or "—")

    # Şirket / Ünvan
    c5, c6 = st.columns(2)
    with c5:
        st.markdown("**🏢 Şirket**")
        st.markdown(v("Şirket") or "—")
    with c6:
        st.markdown("**💼 Ünvan**")
        st.markdown(v("Ünvan") or "—")

    # Etiketler
    labels = v("Etiketler")
    st.markdown("**🏷️ Etiketler**")
    if labels:
        st.markdown("  ".join(f"`{lbl.strip()}`" for lbl in labels.split(",") if lbl.strip()))
    else:
        st.caption("—")

    # Adres
    adres = v("Adres")
    if adres:
        st.markdown("**📍 Adres**")
        st.markdown(adres)

    # Notlar
    notlar = v("Notlar")
    if notlar:
        st.markdown("**📝 Notlar**")
        st.info(notlar)


# ─── Yeni Kişi Dialog ─────────────────────────────────────────────────────────

@st.dialog("➕ Yeni Kişi Ekle", width="large")
def new_contact_dialog():
    service = st.session_state.service
    with st.form("new_contact_form_dlg", clear_on_submit=True):
        c1, c2 = st.columns(2)
        ad     = c1.text_input("Ad")
        soyad  = c2.text_input("Soyad")
        c3, c4 = st.columns(2)
        cep    = c3.text_input("Cep Telefonu")
        tel    = c4.text_input("2. Telefon")
        c5, c6 = st.columns(2)
        eposta  = c5.text_input("E-posta")
        eposta2 = c6.text_input("2. E-posta")
        c7, c8 = st.columns(2)
        sirket = c7.text_input("Şirket / Firma")
        unvan  = c8.text_input("Ünvan / Title")
        adres  = st.text_input("Adres")
        notlar = st.text_area("Notlar", height=70)
        submitted = st.form_submit_button("💾 Kaydet", use_container_width=True, type="primary")

    if submitted:
        if ad or soyad:
            with st.spinner("Ekleniyor…"):
                contacts_api.create_contact(
                    service,
                    {"Ad": ad, "Soyad": soyad,
                     "Cep Telefonu": cep, "2. Telefon": tel,
                     "E-posta": eposta, "2. E-posta": eposta2,
                     "Şirket": sirket, "Ünvan": unvan,
                     "Adres": adres, "Notlar": notlar},
                )
            st.success(f"✅ {ad} {soyad} eklendi!")
            load_data(show_spinner=False)
            st.rerun()
        else:
            st.error("En az bir ad veya soyad gir.")


# ─── Türkçe karakter düzeltme ─────────────────────────────────────────────────

_TR_CHARS = set("şğçöüıŞĞÇÖÜİ")

# Soyadlar için hardcoded fallback sözlüğü (anahtar: ascii-normalize edilmiş küçük harf)
_TR_SURNAMES: dict[str, str] = {
    "ozturk": "Öztürk", "ozdemir": "Özdemir", "ozkul": "Özkul", "ozay": "Özay",
    "ozyurt": "Özyurt", "ozyilmaz": "Özyılmaz", "ozmen": "Özmen", "ozer": "Özer",
    "yilmaz": "Yılmaz", "yildiz": "Yıldız", "yildirim": "Yıldırım",
    "simsek": "Şimşek", "sahin": "Şahin",
    "celik": "Çelik", "cetin": "Çetin", "cakmak": "Çakmak", "cinar": "Çınar",
    "coban": "Çoban", "coskun": "Coşkun",
    "kose": "Köse", "kocer": "Köçer", "koroglu": "Köroğlu", "kucuk": "Küçük",
    "gumus": "Gümüş", "gul": "Gül", "guler": "Güler", "gunduz": "Gündüz",
    "gunay": "Günay", "gurel": "Gürel",
    "dagci": "Dağcı", "karadag": "Karadağ", "karakus": "Karakuş",
    "sarac": "Saraç", "ates": "Ateş", "atas": "Ataş", "erdas": "Erdaş",
    "avci": "Avcı", "topcu": "Topçu",
    "ucar": "Uçar", "unlu": "Ünlü", "unsal": "Ünsal",
    "akturk": "Aktürk", "turk": "Türk", "turker": "Türker",
    "yuce": "Yüce", "tas": "Taş", "tasci": "Taşcı", "sari": "Sarı",
    "erdogan": "Erdoğan", "dogan": "Doğan", "aydin": "Aydın",
}

_TR_NAMES_CACHE_PATH = os.path.join(os.path.dirname(__file__), "data", "tr_names_cache.json")
_TR_NAMES_DICT: dict[str, str] | None = None   # oturum boyunca bellekte tutulur


def _ascii_key(s: str) -> str:
    """Türkçe harfleri ASCII'ye indir + küçük harf — sözlük araması için.
    Not: İ.lower() Python'da 'i\u0307' üretir, bu yüzden önce replace yapılır."""
    s = s.replace("İ", "i")          # büyük İ → küçük i (lower()'dan önce)
    return (s.lower()
            .replace("ö", "o").replace("ü", "u").replace("ş", "s")
            .replace("ğ", "g").replace("ç", "c").replace("ı", "i"))


def _get_tr_names() -> dict[str, str]:
    """Cache dosyasını + hardcoded soyadları birleştirerek döndür."""
    global _TR_NAMES_DICT
    if _TR_NAMES_DICT is not None:
        return _TR_NAMES_DICT
    merged = dict(_TR_SURNAMES)
    if os.path.exists(_TR_NAMES_CACHE_PATH):
        try:
            with open(_TR_NAMES_CACHE_PATH, encoding="utf-8") as f:
                merged.update(json.load(f))
        except Exception:
            pass
    _TR_NAMES_DICT = merged
    return merged


def _download_tr_names() -> int:
    """Gist'ten ~9.700 Türkçe ismi indir, Türkçe char içerenleri cache'e kaydet."""
    global _TR_NAMES_DICT
    url = "https://gist.githubusercontent.com/kvtoraman/f300ae077828c6940d96cd3b19181b3f/raw"
    with urllib.request.urlopen(url, timeout=15) as r:
        content = r.read().decode("utf-8")
    mapping: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(content)):
        name = row.get("name", "").strip()
        if name and any(c in _TR_CHARS for c in name) and " " not in name:
            key = _ascii_key(name)
            if key not in mapping:
                mapping[key] = name
    os.makedirs(os.path.dirname(_TR_NAMES_CACHE_PATH), exist_ok=True)
    with open(_TR_NAMES_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)
    _TR_NAMES_DICT = {**mapping, **_TR_SURNAMES}
    return len(mapping)


def _suggest_tr(phrase: str) -> str:
    """Her kelime için sözlükten Türkçe öneri döndür; bilinmeyeni olduğu gibi bırak."""
    if not phrase.strip():
        return phrase
    names = _get_tr_names()
    return " ".join(names.get(_ascii_key(w), w) for w in phrase.split())


@st.dialog("🇹🇷 Türkçe Karakter Önerileri", width="large")
def turkish_fix_dialog(resource_names: list):
    # İsim veritabanı yoksa otomatik indir
    if not os.path.exists(_TR_NAMES_CACHE_PATH):
        with st.spinner("İsim veritabanı indiriliyor (ilk açılış, ~2 sn)…"):
            try:
                n = _download_tr_names()
                st.toast(f"✅ {n} Türkçe isim yüklendi.", icon="🇹🇷")
            except Exception as e:
                st.warning(f"Veritabanı indirilemedi, sınırlı öneri kullanılıyor. ({e})")

    original_df = st.session_state.df_original
    pending     = st.session_state.pending_edits

    # Her kişi için: (rn, cur_ad, cur_soyad, sug_ad, sug_soyad, orig_ad, orig_soyad)
    to_fix = []
    for rn in resource_names:
        rows = original_df[original_df["_resource_name"] == rn]
        if rows.empty:
            continue
        row = rows.iloc[0]
        cur_ad    = pending.get(rn, {}).get("Ad",    _s(row.get("Ad",    "")))
        cur_soyad = pending.get(rn, {}).get("Soyad", _s(row.get("Soyad", "")))
        full = cur_ad + cur_soyad
        if full.strip() and not any(c in _TR_CHARS for c in full):
            sug_ad    = _suggest_tr(cur_ad)
            sug_soyad = _suggest_tr(cur_soyad)
            to_fix.append((rn, cur_ad, cur_soyad, sug_ad, sug_soyad,
                           _s(row.get("Ad", "")), _s(row.get("Soyad", ""))))

    if not to_fix:
        st.success("Seçili kişilerin tümünde zaten Türkçe karakter var.")
        return

    n_with_sug = sum(1 for _, a, b, sa, sb, *_ in to_fix if sa != a or sb != b)
    n_total    = len(to_fix)
    st.caption(
        f"**{n_total}** kişi incelendi — **{n_with_sug}** kişi için otomatik öneri bulundu, "
        f"geri kalanlar için manuel giriş yapabilirsiniz."
    )
    st.divider()

    edits = {}  # {rn: (final_ad, final_soyad, orig_ad, orig_soyad)}

    for rn, cur_ad, cur_soyad, sug_ad, sug_soyad, orig_ad, orig_soyad in to_fix:
        ad_has_sug    = sug_ad    != cur_ad
        soyad_has_sug = sug_soyad != cur_soyad

        with st.container(border=True):
            # Başlık: orijinal → öneri özeti
            if ad_has_sug or soyad_has_sug:
                full_sug = f"{sug_ad} {sug_soyad}".strip()
                st.markdown(
                    f"**{cur_ad} {cur_soyad}** &nbsp;"
                    f'<span style="color:#6366f1;font-size:0.8rem;">→ {full_sug}</span>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"**{cur_ad} {cur_soyad}** &nbsp;"
                    f'<span style="color:#94a3b8;font-size:0.75rem;">öneri bulunamadı</span>',
                    unsafe_allow_html=True,
                )

            c1, c2 = st.columns(2)

            # ── Ad ────────────────────────────────────────────────────────────
            with c1:
                if ad_has_sug:
                    accept = st.checkbox(
                        f'Ad: **{cur_ad}** → **{sug_ad}**',
                        value=True, key=f"tr_acc_ad_{rn}",
                    )
                    if accept:
                        final_ad = sug_ad
                    else:
                        final_ad = st.text_input(
                            "Ad (düzenle)", value=cur_ad,
                            key=f"tr_man_ad_{rn}", label_visibility="collapsed",
                        )
                else:
                    final_ad = st.text_input("Ad", value=cur_ad, key=f"tr_man_ad_{rn}")

            # ── Soyad ─────────────────────────────────────────────────────────
            with c2:
                if soyad_has_sug:
                    accept_s = st.checkbox(
                        f'Soyad: **{cur_soyad}** → **{sug_soyad}**',
                        value=True, key=f"tr_acc_sy_{rn}",
                    )
                    if accept_s:
                        final_soyad = sug_soyad
                    else:
                        final_soyad = st.text_input(
                            "Soyad (düzenle)", value=cur_soyad,
                            key=f"tr_man_sy_{rn}", label_visibility="collapsed",
                        )
                else:
                    final_soyad = st.text_input("Soyad", value=cur_soyad, key=f"tr_man_sy_{rn}")

            edits[rn] = (final_ad, final_soyad, orig_ad, orig_soyad)

    st.divider()
    if st.button("✅ Seçilenleri Uygula", type="primary", use_container_width=True):
        count = 0
        for rn, (new_ad, new_soyad, orig_ad, orig_soyad) in edits.items():
            if new_ad != orig_ad:
                _mark_bulk_edit(rn, "Ad", new_ad)
                count += 1
            if new_soyad != orig_soyad:
                _mark_bulk_edit(rn, "Soyad", new_soyad)
                count += 1
        if count:
            st.success(f"✅ {count} alan güncellendi. Kaydetmek için 💾 butonuna basın.")
        else:
            st.info("Değiştirilecek alan bulunamadı.")
        st.rerun()


# ─── Sidebar ──────────────────────────────────────────────────────────────────

def render_sidebar():
    df_full = st.session_state.df
    service = st.session_state.service

    with st.sidebar:
        st.markdown("#### 📇 Contacts Manager")
        st.caption(f"Toplam **{len(df_full)}** kişi")
        st.divider()

        # ── Akıllı Filtreler ─────────────────────────────────────────────────
        st.markdown("**🔍 Filtre**")
        filter_options = [
            "Tümü",
            "Telefonu olmayanlar",
            "E-postası olmayanlar",
            "Şirketi/Ünvanı olmayanlar",
            "Yinelenen isimler",
            "Yinelenen telefonlar",
            "Birden fazla etiketli",
        ]
        st.session_state.active_filter = st.selectbox(
            "Filtre", filter_options, label_visibility="collapsed",
            key="filter_select"
        )

        st.session_state.search_query = st.text_input(
            "Ara", placeholder="İsim, telefon, e-posta…",
            label_visibility="collapsed", key="search_input"
        )

        st.divider()

        # ── Sütunlar ─────────────────────────────────────────────────────────
        st.markdown("**📋 Sütunlar**")
        all_cols = ["Ad", "Soyad", "Cep Telefonu", "2. Telefon",
                    "E-posta", "2. E-posta", "Etiketler", "Şirket", "Ünvan", "Notlar", "Adres",
                    "Oluşturulma"]
        # Widget key'i olmadığında default olarak visible_cols kullan;
        # key varsa Streamlit kendi state'ini yönetir, default yok sayılır.
        if "col_selector" not in st.session_state:
            st.session_state["col_selector"] = list(st.session_state.visible_cols)
        selected_cols = st.multiselect(
            "Sütunlar", options=all_cols,
            label_visibility="collapsed", key="col_selector",
        )
        new_val = selected_cols if selected_cols else all_cols
        if new_val != st.session_state.visible_cols:
            st.session_state.visible_cols = new_val

        st.divider()

        # ── Yeni Etiket ──────────────────────────────────────────────────────
        st.markdown("**🏷️ Yeni Etiket**")
        with st.form("new_group_form", clear_on_submit=True):
            group_name = st.text_input("Etiket adı", placeholder="Örn: İş, Aile…",
                                       label_visibility="collapsed")
            grp_submitted = st.form_submit_button("Oluştur", use_container_width=True)

        if grp_submitted:
            if group_name.strip():
                with st.spinner("Etiket oluşturuluyor…"):
                    rn, name = contacts_api.create_group(service, group_name.strip())
                st.session_state.groups_map[rn] = name
                st.session_state.groups_map_inv[name] = rn
                st.success(f"✅ '{name}' oluşturuldu!")
            else:
                st.error("Etiket adı boş olamaz.")

        st.divider()

        # ── Export / Yenile / Çıkış ──────────────────────────────────────────
        df_export = df_full.drop(columns=["_resource_name", "_etag",
                                          "_phones_raw", "_emails_raw", "_addresses_raw"],
                                 errors="ignore")
        csv_bytes = df_export.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "📥 CSV İndir", data=csv_bytes,
            file_name=f"contacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv", use_container_width=True,
        )
        col_r, col_l = st.columns(2)
        with col_r:
            if st.button("🔄 Yenile", use_container_width=True):
                load_data()
                st.rerun()
        with col_l:
            if st.button("🚪 Çıkış", use_container_width=True):
                auth.revoke()
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
                st.rerun()


# ─── Grid ─────────────────────────────────────────────────────────────────────

DISPLAY_COLS = [
    "Ad", "Soyad", "Cep Telefonu", "2. Telefon",
    "E-posta", "2. E-posta", "Etiketler", "Şirket", "Ünvan", "Notlar", "Adres",
    "Oluşturulma",
]
COL_WIDTHS = {
    "Ad": 110, "Soyad": 110,
    "Cep Telefonu": 130, "2. Telefon": 130,
    "E-posta": 190, "2. E-posta": 190,
    "Etiketler": 150, "Şirket": 140, "Ünvan": 130,
    "Notlar": 200, "Adres": 220,
    "Oluşturulma": 160,
}


def build_grid_options(df: pd.DataFrame):
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(
        editable=True,
        resizable=True,
        sortable=True,
        filter=True,
        wrapText=False,
        autoHeight=False,
    )
    # _resource_name: response'da kalması için hide yerine invisible style kullan
    invisible_style = JsCode("function(p){return {'color':'transparent','userSelect':'none'}}")
    empty_renderer = JsCode("function(p){return '';}")
    gb.configure_column(
        "_resource_name", headerName="", width=1, minWidth=1, maxWidth=1,
        editable=False, sortable=False, filter=False, resizable=False,
        suppressMovable=True, cellStyle=invisible_style, cellRenderer=empty_renderer,
    )
    for hidden in ("_etag", "_phones_raw", "_emails_raw", "_addresses_raw", "_primary_phone_col"):
        gb.configure_column(hidden, hide=True)

    # Oluşturulma: salt okunur
    gb.configure_column("Oluşturulma", editable=False)

    # Primary telefon vurgusu
    primary_style = JsCode("""
function(p){
    if(p.data&&p.data._primary_phone_col===p.colDef.field&&p.value)
        return{'backgroundColor':'rgba(59,130,246,0.12)','fontWeight':'600','color':'#3b82f6','borderLeft':'3px solid rgba(59,130,246,0.5)'};
    return null;
}""")
    gb.configure_column("Cep Telefonu", cellStyle=primary_style)
    gb.configure_column("2. Telefon",   cellStyle=primary_style)

    # Ad: checkbox burada — ayrı sütun yok, layout temiz kalır
    gb.configure_column("Ad", checkboxSelection=True, headerCheckboxSelection=True,
                        headerCheckboxSelectionFilteredOnly=True, width=130)

    # Etiketler pill renderer
    pill_renderer = JsCode("""
(function(){
    function P(){}
    P.prototype.init=function(params){
        var palette=[['#3b82f6','rgba(59,130,246,0.12)'],['#10b981','rgba(16,185,129,0.12)'],['#f59e0b','rgba(245,158,11,0.12)'],['#8b5cf6','rgba(139,92,246,0.12)'],['#ef4444','rgba(239,68,68,0.12)']];
        this.eGui=document.createElement('div');
        this.eGui.style.cssText='display:flex;align-items:center;flex-wrap:wrap;gap:3px;height:100%;padding:2px 0;';
        if(!params.value)return;
        var self=this;
        params.value.split(',').map(function(s){return s.trim();}).filter(Boolean).forEach(function(lbl,i){
            var c=palette[i%palette.length],span=document.createElement('span');
            span.textContent=lbl;
            span.style.cssText='background:'+c[1]+';color:'+c[0]+';border:1px solid '+c[0]+'55;border-radius:10px;padding:1px 5px;font-size:0.6rem;font-weight:500;white-space:nowrap;';
            self.eGui.appendChild(span);
        });
    };
    P.prototype.getGui=function(){return this.eGui;};
    P.prototype.refresh=function(){return false;};
    return P;
})()""")
    gb.configure_column("Etiketler", cellRenderer=pill_renderer, wrapText=False)

    # Kullanıcının gizlediği display kolonlarını hide=True yap
    # (DataFrame'i kesmek yerine gridOptions'da gizlemek grid state'ini korur)
    visible_set = set(st.session_state.get("visible_cols", DISPLAY_COLS))
    for col in DISPLAY_COLS:
        if col not in visible_set and col in df.columns:
            gb.configure_column(col, hide=True)

    # Column widths
    for col, width in COL_WIDTHS.items():
        if col in df.columns:
            gb.configure_column(col, width=width)

    gb.configure_selection(
        selection_mode="multiple",
        use_checkbox=False,
        header_checkbox=False,
        pre_selected_rows=st.session_state.get("selected_rows", []),
    )
    gb.configure_pagination(enabled=False)
    gb.configure_grid_options(
        domLayout="normal",
        suppressRowClickSelection=False,
        rowHeight=30,
        headerHeight=40,
        rowStyle={"cursor": "pointer"},
    )
    return gb.build()


def render_grid(df_display: pd.DataFrame, reload: bool = True, grid_key: str = "main_grid"):
    """
    reload=True  → AgGrid veriyi sıfırdan yükler (filter değişimi, kayıt sonrası yenileme)
    reload=False → Grid mevcut state'ini korur; kullanıcı editlerini kaybetmez
    grid_key     → Sütun listesi değişince key değişir; AgGrid cache'i temizlenir
    """
    grid_options = build_grid_options(df_display)
    response = AgGrid(
        df_display,
        gridOptions=grid_options,
        update_mode=GridUpdateMode.VALUE_CHANGED | GridUpdateMode.SELECTION_CHANGED,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        fit_columns_on_grid_load=False,
        reload_data=reload,
        theme="streamlit",
        height=700,
        allow_unsafe_jscode=True,
        enable_enterprise_modules=False,
        key=grid_key,
    )

    # Normalise response (dict vs AgGridReturn object)
    try:
        raw_data = response["data"]
        raw_sel = response["selected_rows"]
    except (TypeError, KeyError):
        raw_data = response.data if response.data is not None else df_display
        raw_sel = response.selected_rows

    if isinstance(raw_data, pd.DataFrame):
        edited_df = raw_data.fillna("")
    elif raw_data is not None:
        edited_df = pd.DataFrame(raw_data).fillna("")
    else:
        edited_df = df_display.copy()

    # selected_rows may be a DataFrame, list, or None — always normalise to list
    if raw_sel is None:
        selected_rows = []
    elif isinstance(raw_sel, pd.DataFrame):
        selected_rows = raw_sel.to_dict("records")
    else:
        selected_rows = list(raw_sel) if raw_sel else []

    return edited_df, selected_rows


# ─── Turkish text helpers ────────────────────────────────────────────────────

def turkish_upper(s: str) -> str:
    """'i'→'İ', 'ı'→'I', diğerleri normal upper."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def turkish_title(s: str) -> str:
    """Her kelimenin ilk harfini Türkçe kurallarla büyütür, geri kalanı küçültür."""
    def _cap_word(w: str) -> str:
        if not w:
            return w
        first = w[0]
        rest  = w[1:]
        first_up = "İ" if first == "i" else ("I" if first == "ı" else first.upper())
        # Büyük I'yı küçültürken ı yapmalıyız; İ→i Python'da zaten doğru
        rest_low = rest.replace("I", "ı").lower()
        return first_up + rest_low
    return " ".join(_cap_word(word) for word in s.split(" "))


def _mark_bulk_edit(rn: str, col: str, new_val: str):
    """pending_edits'e yazar VE grid_data'yı günceller.
    Bu sayede _sync_pending_edits grid'den gelen eski veriyle değişikliği silmez."""
    st.session_state.pending_edits.setdefault(rn, {})[col] = new_val
    gd = st.session_state.grid_data
    if gd is not None and col in gd.columns and "_resource_name" in gd.columns:
        gd.loc[gd["_resource_name"] == rn, col] = new_val
    st.session_state.force_grid_reload = True


def _apply_email_lowercase(resource_names: list) -> int:
    """E-posta ve 2. E-posta alanlarındaki büyük harfleri küçültür."""
    original_df = st.session_state.df_original
    pending     = st.session_state.pending_edits
    count = 0
    for rn in resource_names:
        rows = original_df[original_df["_resource_name"] == rn]
        if rows.empty:
            continue
        row = rows.iloc[0]
        changed = False
        for col in ["E-posta", "2. E-posta"]:
            cur = pending.get(rn, {}).get(col, _s(row.get(col, "")))
            new_val = cur.lower()
            if new_val != cur:
                pending.setdefault(rn, {})[col] = new_val
                dfs_to_update = [st.session_state.df, st.session_state.df_original]
                if st.session_state.grid_data is not None:
                    dfs_to_update.append(st.session_state.grid_data)
                for df_ref in dfs_to_update:
                    mask = df_ref["_resource_name"] == rn
                    if mask.any():
                        df_ref.loc[mask, col] = new_val
                changed = True
        if changed:
            count += 1
    return count


def _apply_bulk_case(resource_names: list, mode: str) -> int:
    """Seçili kişilerin Ad & Soyad alanlarına toplu harf dönüşümü uygular.
    mode: 'title' | 'upper'
    """
    original_df = st.session_state.df_original
    pending     = st.session_state.pending_edits
    fn = turkish_title if mode == "title" else turkish_upper
    count = 0
    for rn in resource_names:
        rows = original_df[original_df["_resource_name"] == rn]
        if rows.empty:
            continue
        row = rows.iloc[0]
        cur_ad    = pending.get(rn, {}).get("Ad",    _s(row.get("Ad",    "")))
        cur_soyad = pending.get(rn, {}).get("Soyad", _s(row.get("Soyad", "")))
        new_ad    = fn(cur_ad)
        new_soyad = fn(cur_soyad)
        changed = False
        if new_ad != _s(row.get("Ad", "")):
            _mark_bulk_edit(rn, "Ad", new_ad)
            changed = True
        if new_soyad != _s(row.get("Soyad", "")):
            _mark_bulk_edit(rn, "Soyad", new_soyad)
            changed = True
        if changed:
            count += 1
    return count


# ─── Edit tracking ────────────────────────────────────────────────────────────

EDITABLE_COLS = [
    "Ad", "Soyad", "Cep Telefonu", "2. Telefon",
    "E-posta", "2. E-posta", "Etiketler", "Şirket", "Ünvan", "Notlar", "Adres",
]


def _s(val) -> str:
    return str(val or "").strip()


def _sync_pending_edits(edited_df: pd.DataFrame):
    """VALUE_CHANGED rerun'ında çağrılır.
    edited_df ile df_original arasındaki farkları pending_edits'e yazar.
    Bu sayede save butonuna basınca AgGrid'den bağımsız olarak doğru veri kullanılır."""
    original_df = st.session_state.df_original
    pending = st.session_state.pending_edits

    for _, row in edited_df.iterrows():
        rn = _s(row.get("_resource_name", ""))
        if not rn:
            continue
        orig_rows = original_df[original_df["_resource_name"] == rn]
        if orig_rows.empty:
            continue
        orig_row = orig_rows.iloc[0]

        for col in EDITABLE_COLS:
            new_val = _s(row.get(col, ""))
            orig_val = _s(orig_row.get(col, ""))
            if new_val != orig_val:
                if rn not in pending:
                    pending[rn] = {}
                pending[rn][col] = new_val
            elif rn in pending and col in pending[rn]:
                # Kullanıcı değeri geri aldı
                del pending[rn][col]
                if not pending[rn]:
                    del pending[rn]


# ─── Save ─────────────────────────────────────────────────────────────────────

def save_changes():
    """pending_edits'teki değişiklikleri Google API'ye gönderir."""
    service = st.session_state.service
    original_df = st.session_state.df_original
    pending = st.session_state.pending_edits
    groups_map_inv = st.session_state.groups_map_inv

    if not pending:
        return 0, [], []

    saved = 0
    errors = []
    details = []  # [(isim, gönderilen_alanlar, yeni_etag)]

    for rn, changes in list(pending.items()):
        orig_rows = original_df[original_df["_resource_name"] == rn]
        if orig_rows.empty:
            continue
        orig_row = orig_rows.iloc[0]
        etag = _s(orig_row.get("_etag", ""))
        name_str = f"{orig_row.get('Ad', '')} {orig_row.get('Soyad', '')}".strip() or rn

        # Orijinal satırın üstüne değişiklikleri uygula
        new_row = orig_row.to_dict()
        new_row.update(changes)

        try:
            field_result = contacts_api.update_contact(
                service, rn, etag, new_row, orig_row.to_dict()
            )

            old_labels = _s(orig_row.get("Etiketler", ""))
            new_labels = _s(new_row.get("Etiketler", old_labels))
            new_created = contacts_api.sync_contact_labels(
                service, rn, old_labels, new_labels, groups_map_inv,
            )
            for grn, name in new_created:
                st.session_state.groups_map[grn] = name

            labels_changed = old_labels != new_labels
            if field_result is not None or labels_changed:
                saved += 1
                updated_fields = field_result.get("_updated_fields", []) if field_result else []
                if labels_changed:
                    updated_fields.append("labels")
                new_etag = field_result.get("etag", "?") if field_result else "labels-only"
                details.append((name_str, updated_fields, new_etag))

        except Exception as exc:
            errors.append(f"{name_str}: {exc}")

    return saved, errors, details


# ─── Üst Aksiyon Çubuğu ───────────────────────────────────────────────────────

def _render_action_bar(selected_rows: list):
    """Grid üstünde araç çubuğu — seçim yoksa hiçbir şey göstermez."""
    n = len(selected_rows)

    if n == 0:
        if not (st.session_state.show_delete_confirm and st.session_state.get("delete_resource_names")):
            st.session_state.show_delete_confirm = False
            return

    service = st.session_state.service
    resource_names = [r["_resource_name"] for r in selected_rows if r.get("_resource_name")]

    # ── Silme Onayı ────────────────────────────────────────────────────────────
    if st.session_state.show_delete_confirm:
        confirm_rns = st.session_state.get("delete_resource_names") or resource_names
        st.markdown(
            f'<div class="delete-confirm-bar">'
            f'  <span class="dcb-icon">🗑️</span>'
            f'  <div class="dcb-body">'
            f'    <span class="dcb-title">Silme Onayı</span>'
            f'    <span class="dcb-desc"><b>{len(confirm_rns)}</b> kişi kalıcı olarak silinecek '
            f'— bu işlem geri alınamaz.</span>'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        _, btn_yes, btn_no = st.columns([6.5, 1.3, 0.9])
        with btn_yes:
            if st.button("🗑️ Onayla", key="confirm_delete", type="primary", use_container_width=True):
                try:
                    contacts_api.backup_csv(st.session_state.df)
                    with st.spinner("Siliniyor..."):
                        contacts_api.delete_contacts(service, confirm_rns)
                    st.session_state.df = st.session_state.df[
                        ~st.session_state.df["_resource_name"].isin(confirm_rns)
                    ]
                    st.session_state.selected_rows = []
                    st.session_state.show_delete_confirm = False
                    st.session_state.delete_resource_names = []
                    st.session_state.grid_data = None
                    st.session_state.data_version += 1
                    st.toast(f"✅ {len(confirm_rns)} kişi silindi.", icon="🗑️")
                    st.rerun()
                except Exception as e:
                    st.error(f"Hata oluştu: {e}")
        with btn_no:
            if st.button("Vazgeç", key="cancel_delete", use_container_width=True):
                st.session_state.show_delete_confirm = False
                st.session_state.delete_resource_names = []
                st.rerun()
        return

    # ── Normal Araç Çubuğu ─────────────────────────────────────────────────────
    if n == 0:
        return

    group_names = sorted(st.session_state.groups_map_inv.keys())

    st.markdown('<div class="tb-start"></div>', unsafe_allow_html=True)

    # Sabit sütun düzeni:
    # kimlik | detay | vsep | Aa | AA | 🇹🇷 | @↓ | vsep | label_sel | Ata | Kaldır | vsep | Sil
    (c_id, c_det, c_v1,
     c_aa, c_AA, c_tr, c_at,
     c_v2,
     c_lbl, c_ata, c_kldr,
     c_v3,
     c_sil) = st.columns([1.85, 0.62, 0.07, 0.58, 0.58, 0.58, 0.58, 0.07, 2.0, 0.65, 0.65, 0.07, 0.72])

    # ── Kimlik bölümü ─────────────────────────────────────────────────────────
    if n == 1:
        row = selected_rows[0]
        full_name = f"{row.get('Ad', '')} {row.get('Soyad', '')}".strip() or "İsimsiz"
        c_id.markdown(
            f'<div class="tb-name">👤 {full_name}</div>',
            unsafe_allow_html=True,
        )
        if c_det.button("Detay", key="act_det", use_container_width=True, help="Kişi detaylarını görüntüle"):
            contact_detail_dialog(row["_resource_name"])
    else:
        c_id.markdown(
            f'<div class="tb-badge">⚡ {n} seçili</div>',
            unsafe_allow_html=True,
        )

    # ── Dikey ayraçlar ────────────────────────────────────────────────────────
    c_v1.markdown('<div class="vsep"></div>', unsafe_allow_html=True)
    c_v2.markdown('<div class="vsep"></div>', unsafe_allow_html=True)
    c_v3.markdown('<div class="vsep"></div>', unsafe_allow_html=True)

    # ── Metin işlemleri ───────────────────────────────────────────────────────
    if c_aa.button("Aa", key="bulk_title_btn", use_container_width=True,
                   help="Title Case — Her kelimenin ilk harfini büyüt"):
        cnt = _apply_bulk_case(resource_names, "title")
        st.toast(f"✅ {cnt} kişi güncellendi.")

    if c_AA.button("AA", key="bulk_upper_btn", use_container_width=True,
                   help="BÜYÜK HARF — Tümünü büyük harfe çevir"):
        cnt = _apply_bulk_case(resource_names, "upper")
        st.toast(f"✅ {cnt} kişi güncellendi.")

    if c_tr.button("🇹🇷", key="bulk_tr_btn", use_container_width=True,
                   help="Türkçe karakter düzelt"):
        turkish_fix_dialog(resource_names)

    if c_at.button("@↓", key="bulk_email_lower_btn", use_container_width=True,
                   help="E-posta adreslerini küçük harfe çevir"):
        cnt = _apply_email_lowercase(resource_names)
        if cnt:
            st.toast(f"✅ {cnt} kişinin e-postası küçültüldü.")
            sel_rns = {r["_resource_name"] for r in st.session_state.get("selected_rows", []) if r.get("_resource_name")}
            if sel_rns and st.session_state.grid_data is not None:
                gd = st.session_state.grid_data
                st.session_state.selected_rows = gd[gd["_resource_name"].isin(sel_rns)].to_dict("records")
            st.session_state.force_grid_reload = True
            st.rerun()
        else:
            st.toast("E-postalarda büyük harf bulunamadı.")

    # ── Etiket işlemleri ──────────────────────────────────────────────────────
    sel_group = c_lbl.selectbox(
        "Etiket", ["— Etiket Seç —"] + group_names,
        label_visibility="collapsed", key="bulk_label_sel",
    )

    if c_ata.button("Ata", key="bulk_assign_btn", use_container_width=True,
                    help="Seçili etiketi kişilere ata"):
        if sel_group != "— Etiket Seç —":
            grn = st.session_state.groups_map_inv.get(sel_group)
            contacts_api.assign_labels_to_contacts(service, resource_names, grn)
            dfs_to_update = [st.session_state.df, st.session_state.df_original]
            if st.session_state.grid_data is not None:
                dfs_to_update.append(st.session_state.grid_data)
            for rn in resource_names:
                for df_ref in dfs_to_update:
                    mask = df_ref["_resource_name"] == rn
                    if mask.any():
                        cur = str(df_ref.loc[mask, "Etiketler"].iloc[0] or "").strip()
                        lbls = {l.strip() for l in cur.split(",") if l.strip()}
                        lbls.add(sel_group)
                        df_ref.loc[mask, "Etiketler"] = ", ".join(sorted(lbls))
            st.toast(f"✅ '{sel_group}' etiketi {len(resource_names)} kişiye atandı.")
            sel_rns = {r["_resource_name"] for r in st.session_state.get("selected_rows", []) if r.get("_resource_name")}
            if sel_rns and st.session_state.grid_data is not None:
                gd = st.session_state.grid_data
                st.session_state.selected_rows = gd[gd["_resource_name"].isin(sel_rns)].to_dict("records")
            st.session_state.force_grid_reload = True
            st.rerun()

    if c_kldr.button("Kaldır", key="bulk_remove_lbl_btn", use_container_width=True,
                     help="Seçili etiketi kişilerden kaldır"):
        if sel_group != "— Etiket Seç —":
            grn = st.session_state.groups_map_inv.get(sel_group)
            contacts_api.remove_label_from_contacts(service, resource_names, grn)
            dfs_to_update = [st.session_state.df, st.session_state.df_original]
            if st.session_state.grid_data is not None:
                dfs_to_update.append(st.session_state.grid_data)
            for rn in resource_names:
                for df_ref in dfs_to_update:
                    mask = df_ref["_resource_name"] == rn
                    if mask.any():
                        cur = str(df_ref.loc[mask, "Etiketler"].iloc[0] or "").strip()
                        lbls = {l.strip() for l in cur.split(",") if l.strip()} - {sel_group}
                        df_ref.loc[mask, "Etiketler"] = ", ".join(sorted(lbls))
            st.toast(f"✅ '{sel_group}' etiketi {len(resource_names)} kişiden kaldırıldı.")
            sel_rns = {r["_resource_name"] for r in st.session_state.get("selected_rows", []) if r.get("_resource_name")}
            if sel_rns and st.session_state.grid_data is not None:
                gd = st.session_state.grid_data
                st.session_state.selected_rows = gd[gd["_resource_name"].isin(sel_rns)].to_dict("records")
            st.session_state.force_grid_reload = True
            st.rerun()

    # ── Sil butonu ────────────────────────────────────────────────────────────
    with c_sil:
        st.markdown('<span class="danger-anchor"></span>', unsafe_allow_html=True)
        if st.button("🗑️ Sil", key="bulk_delete_btn", use_container_width=True):
            st.session_state.show_delete_confirm = True
            st.session_state.delete_resource_names = resource_names
            st.rerun()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_state()

    if not st.session_state.authenticated:
        handle_auth_page()
        return

    # First load
    if st.session_state.df is None:
        load_data()

    if st.session_state.df is None:
        st.error("Kişiler yüklenemedi.")
        return

    # ── CSS: kompakt sidebar + geniş içerik ─────────────────────────────────
    st.markdown("""
<style>
/* ── Streamlit üst bar tamamen gizle ── */
header[data-testid="stHeader"] { display: none !important; }
.block-container { padding-top: 0.5rem !important; }

/* ════════════════════════════════════════════════════
   ARAÇ ÇUBUĞU — grid başlık satırı görünümü
   tb-start işaretçisinin hemen sonraki kardeş .element-container'ı hedef alır
   ════════════════════════════════════════════════════ */

/* İşaretçi: görünmez, sadece CSS sibling targeting için */
.tb-start { display: none; }

/* Kolon satırını grid başlığı gibi göster */
.element-container:has(.tb-start) + .element-container {
    background: #f0f2f6 !important;
    border-top: 1px solid rgba(49,51,63,0.2) !important;
    border-left: 1px solid rgba(49,51,63,0.2) !important;
    border-right: 1px solid rgba(49,51,63,0.2) !important;
    border-bottom: 2px solid rgba(49,51,63,0.28) !important;
    border-radius: 6px 6px 0 0 !important;
    padding: 1px 6px 2px 6px !important;
    margin-bottom: -1px !important;
}

/* Toolbar içindeki tüm butonlar: grid başlık stilinde, çok kompakt */
.element-container:has(.tb-start) + .element-container .stButton > button {
    font-size: 0.6rem !important;
    padding: 1px 6px !important;
    height: 22px !important;
    min-height: 22px !important;
    border-radius: 4px !important;
    box-shadow: none !important;
    background: rgba(255,255,255,0.75) !important;
    color: #374151 !important;
    border: 1px solid rgba(49,51,63,0.12) !important;
    font-weight: 500 !important;
    transform: none !important;
    transition: background 0.1s ease, border-color 0.1s ease !important;
}
.element-container:has(.tb-start) + .element-container .stButton > button:hover {
    background: #ffffff !important;
    border-color: rgba(49,51,63,0.28) !important;
    transform: none !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1) !important;
}
.element-container:has(.tb-start) + .element-container .stButton > button:active {
    background: #e8eaf0 !important;
    transform: none !important;
}

/* Selectbox toolbar içinde kompakt */
.element-container:has(.tb-start) + .element-container .stSelectbox > div {
    margin-bottom: 0 !important;
}
.element-container:has(.tb-start) + .element-container [data-baseweb="select"] {
    font-size: 0.6rem !important;
    min-height: 22px !important;
}
.element-container:has(.tb-start) + .element-container [data-baseweb="select"] > div {
    min-height: 22px !important;
    padding: 0 6px !important;
    font-size: 0.6rem !important;
}

/* Kimlik etiketi */
.tb-name {
    font-size: 0.68rem;
    font-weight: 700;
    color: #1e293b;
    line-height: 2.0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* Çoklu seçim rozeti */
.tb-badge {
    display: inline-flex;
    align-items: center;
    background: #6366f1;
    color: white;
    border-radius: 10px;
    padding: 2px 9px;
    font-size: 0.6rem;
    font-weight: 600;
    margin-top: 4px;
    letter-spacing: 0.02em;
}

/* Dikey ayraç */
.vsep {
    border-left: 1px solid rgba(49,51,63,0.15);
    height: 20px;
    width: 1px;
    margin: 4px auto 0 auto;
}

/* ─── Sil butonu tehlike stili ─── */
[data-testid="column"]:has(.danger-anchor) .stButton > button {
    background-color: #fff1f1 !important;
    color: #dc2626 !important;
    border: 1px solid #fca5a5 !important;
    font-weight: 600 !important;
}
[data-testid="column"]:has(.danger-anchor) .stButton > button:hover {
    background-color: #dc2626 !important;
    color: #ffffff !important;
    border-color: #dc2626 !important;
    box-shadow: none !important;
}

/* ════════════════════════════════════════════════════
   SİLME ONAY BANDI — kırmızı tintli grid başlık satırı
   ════════════════════════════════════════════════════ */
.delete-confirm-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    background: #fff1f2;
    border-top: 1px solid #fecdd3;
    border-left: 1px solid #fecdd3;
    border-right: 1px solid #fecdd3;
    border-bottom: 2px solid #fca5a5;
    border-left-width: 3px;
    border-left-color: #ef4444;
    border-radius: 6px 6px 0 0;
    padding: 5px 10px;
    margin-bottom: -1px;
}
.dcb-icon { font-size: 0.95rem; flex-shrink: 0; }
.dcb-body { display: flex; flex-direction: column; gap: 0; }
.dcb-title {
    font-size: 0.62rem;
    font-weight: 700;
    color: #991b1b;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    line-height: 1.3;
}
.dcb-desc { font-size: 0.68rem; color: #b91c1c; line-height: 1.4; }

/* Silme onayı buton satırı da grid-like */
.element-container:has(.delete-confirm-bar) + .element-container {
    background: #fff5f5 !important;
    border-left: 1px solid #fecdd3 !important;
    border-right: 1px solid #fecdd3 !important;
    border-bottom: 1px solid #fecdd3 !important;
    padding: 2px 6px !important;
    margin-bottom: 0 !important;
}

/* ════════════════════════════════════════════════════
   MODERN UI — butonlar, inputlar, geçişler
   ════════════════════════════════════════════════════ */
.stButton > button {
    border-radius: 7px !important;
    font-size: 0.68rem !important;
    font-weight: 500 !important;
    padding: 0.2rem 0.5rem !important;
    transition: transform 0.12s ease, box-shadow 0.12s ease, opacity 0.12s ease !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07) !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.12) !important;
}
.stButton > button:active {
    transform: translateY(0) !important;
    opacity: 0.85 !important;
}

/* ── Input / selectbox ── */
.stTextInput > div > div > input,
.stSelectbox > div > div > div {
    border-radius: 7px !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
.stTextInput > div > div > input:focus {
    box-shadow: 0 0 0 2px rgba(99,102,241,0.22) !important;
}

/* ── Download butonu ── */
.stDownloadButton > button {
    border-radius: 7px !important;
    transition: transform 0.12s ease, box-shadow 0.12s ease !important;
}
.stDownloadButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 10px rgba(0,0,0,0.12) !important;
}

/* ── Divider ince ── */
hr { margin: 0.5rem 0 !important; opacity: 0.35 !important; }

/* ════════════════════════════════════════════════════
   SIDEBAR
   ════════════════════════════════════════════════════ */
section[data-testid="stSidebar"] { max-width:220px !important; }
section[data-testid="stSidebar"] .block-container { padding:0.5rem 0.6rem !important; }

section[data-testid="stSidebar"],
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] div,
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] .stMarkdown { font-size:0.68rem !important; }

section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3,
section[data-testid="stSidebar"] h4,
section[data-testid="stSidebar"] strong {
    font-size:0.68rem !important; margin:0.2rem 0 0.1rem !important; font-weight:700 !important;
}
section[data-testid="stSidebar"] hr { margin:0.3rem 0 !important; }
section[data-testid="stSidebar"] .stButton button {
    font-size:0.68rem !important; padding:0.15rem 0.4rem !important;
}
section[data-testid="stSidebar"] .stSelectbox > div,
section[data-testid="stSidebar"] .stTextInput > div { font-size:0.68rem !important; }

/* Multiselect: her etiket ayrı satır */
section[data-testid="stSidebar"] [data-testid="stMultiSelect"] { font-size:0.68rem !important; }
section[data-testid="stSidebar"] [data-testid="stMultiSelect"] [data-baseweb="select"] {
    flex-wrap: wrap !important;
}
section[data-testid="stSidebar"] [data-testid="stMultiSelect"] span[data-baseweb="tag"] {
    display: inline-flex !important;
    align-items: center !important;
    flex: 0 0 calc(100% - 4px) !important;
    max-width: calc(100% - 4px) !important;
    margin: 1px 0 !important;
    padding: 1px 4px !important;
    font-size: 0.68rem !important;
    line-height: 1.4 !important;
    border-radius: 3px !important;
    box-sizing: border-box !important;
    overflow: hidden !important;
}
</style>
""", unsafe_allow_html=True)

    render_sidebar()

    # ── Header satırı: başlık + kaydet + yeni kişi ──────────────────────────
    n_pending_hdr = len(st.session_state.pending_edits)
    # ── Apply filter + search ────────────────────────────────────────────────
    df_view = contacts_api.apply_filter(
        st.session_state.df, st.session_state.active_filter
    )

    query = st.session_state.search_query.strip().lower()
    if query:
        mask = df_view.apply(
            lambda row: row.drop(labels=["_resource_name", "_etag"], errors="ignore")
            .astype(str).str.lower().str.contains(query).any(),
            axis=1,
        )
        df_view = df_view[mask].reset_index(drop=True)

    filter_label = st.session_state.active_filter
    badge_count = f"**{filter_label}** — {len(df_view)} kayıt"
    if query:
        badge_count += f' | Arama: *"{query}"*'

    hcol1, hcol2, hcol3 = st.columns([6, 1.5, 0.85])
    with hcol1:
        pending_pill = (
            f'<span style="display:inline-flex;align-items:center;background:#fef3c7;color:#92400e;'
            f'border:1px solid #fcd34d;border-radius:20px;padding:1px 9px;font-size:0.65rem;'
            f'font-weight:700;margin-left:8px;vertical-align:middle;">⚡ {n_pending_hdr} bekliyor</span>'
            if n_pending_hdr else ""
        )
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:6px;padding:4px 0;">'
            f'  <span style="font-size:1.1rem;font-weight:800;color:#1e293b;letter-spacing:-0.01em;">'
            f'    📇 Google Contacts</span>'
            f'  {pending_pill}'
            f'  <span style="font-size:0.72rem;color:#94a3b8;margin-left:4px;">{badge_count}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with hcol2:
        save_clicked = st.button("💾 Değişiklikleri Kaydet", type="primary", key="save_btn", use_container_width=True)
    with hcol3:
        if st.button("➕ Yeni Kişi", use_container_width=True):
            new_contact_dialog()

    # Dahili kolonlar her zaman grid'e gider; display kolonlar build_grid_options'da
    # hide=True ile gizlenir — DataFrame'i kesmek yerine gridOptions kullan.
    # AgGrid Response'u için internal kolonları mutlaka ekle
    internal_cols = [c for c in [
        "_resource_name", "_etag",
        "_phones_raw", "_emails_raw", "_addresses_raw", "_primary_phone_col"
    ] if c in df_view.columns]
    vis = st.session_state.visible_cols
    display_cols = (
        [c for c in vis if c in df_view.columns] +
        [c for c in DISPLAY_COLS if c not in set(vis) and c in df_view.columns]
    )
    df_view = df_view[internal_cols + display_cols]

    # ── Reload kararı: filtre/arama/veri/görünür sütunlar değişince True ──
    grid_state_key = (
        st.session_state.active_filter,
        st.session_state.search_query,
        st.session_state.data_version,
        tuple(st.session_state.visible_cols),
    )
    prev_state = st.session_state._grid_state
    should_reload = (grid_state_key != prev_state)
    st.session_state._grid_state = grid_state_key

    # Bulk op (case/türkçe) sonrası grid_data zaten güncellendi; sadece grid'e reload sinyali ver.
    force_grid_reload = st.session_state.force_grid_reload
    if force_grid_reload:
        st.session_state.force_grid_reload = False

    if should_reload or st.session_state.grid_data is None:
        # Filtre/veri değişti: grid_data'yı sıfırdan yükle
        st.session_state.grid_data = df_view.copy()

    # ── Action bar için yer ayır (layout'ta grid'in üstünde görünür) ─────────
    action_bar_slot = st.container()

    # ── Boş durum mesajı ─────────────────────────────────────────────────────
    if df_view.empty:
        st.markdown("""
<div style="text-align:center;padding:3rem 1rem;opacity:0.55;">
  <div style="font-size:2.5rem;">🔍</div>
  <div style="font-size:1rem;font-weight:600;margin-top:0.4rem;">Sonuç bulunamadı</div>
  <div style="font-size:0.82rem;margin-top:0.2rem;">Filtre veya arama kriterini değiştirmeyi dene</div>
</div>""", unsafe_allow_html=True)
        return

    # ── Grid (tam genişlik) ──────────────────────────────────────────────────
    # Post-save: grid key + reload sabit kalır → scroll/sort/column-filter korunur
    post_save = st.session_state.get("_post_save_reload", False)
    if post_save:
        st.session_state["_post_save_reload"] = False
        # Yedeklenen grid_data'yı geri yükle (load_data None'a sıfırlamıştı)
        saved_grid = st.session_state.get("_saved_grid_data")
        if saved_grid is not None:
            st.session_state.grid_data = saved_grid
            st.session_state["_saved_grid_data"] = None
        # Seçimi resource_name üzerinden geri yükle
        saved_rns = set(st.session_state.get("_saved_selection_rns", []))
        if saved_rns and st.session_state.grid_data is not None:
            gd = st.session_state.grid_data
            restored = gd[gd["_resource_name"].isin(saved_rns)].to_dict("records")
            st.session_state.selected_rows = restored
        st.session_state["_saved_selection_rns"] = []

    # grid_data yeniden inşa: filtre/arama/veri değişince — ama post_save'de değil
    if (should_reload or st.session_state.grid_data is None) and not post_save:
        st.session_state.grid_data = df_view.copy()

    reload_grid = should_reload or force_grid_reload
    if should_reload and not post_save:
        st.session_state["_grid_key_v"] = st.session_state.get("_grid_key_v", 0) + 1
    grid_key = f"mg_{st.session_state.get('_grid_key_v', 0)}"

    # post_save'de reload=False → ag-grid iç state'ini (scroll, sort, filter) korur
    render_reload = reload_grid and not post_save
    edited_df, grid_selection = render_grid(st.session_state.grid_data, reload=render_reload, grid_key=grid_key)

    # Seçimi kaydet (ekstra rerun olmadan)
    st.session_state.selected_rows = grid_selection

    # Action bar'ı grid'den SONRA doldur — sort/scroll sıfırlanmaz
    with action_bar_slot:
        _render_action_bar(grid_selection)

    # VALUE_CHANGED / SELECTION_CHANGED rerun'larında editları pending_edits'e kaydet.
    if not render_reload and edited_df is not None and not edited_df.empty:
        _sync_pending_edits(edited_df)

    # ── Kaydet (buton başlık satırında; mantık grid sonrası çalışır) ─────────
    if save_clicked:
        if not force_grid_reload and edited_df is not None and not edited_df.empty:
            _sync_pending_edits(edited_df)
        with st.spinner("Kaydediliyor…"):
            saved, errors, details = save_changes()
        if errors:
            st.toast("⚠️ Bazı satırlar kaydedilemedi: " + " | ".join(errors), icon="⚠️")
        if saved > 0:
            st.toast(f"✅ {saved} kişi güncellendi.", icon="✅")
            st.session_state["_saved_selection_rns"] = [
                r["_resource_name"] for r in st.session_state.get("selected_rows", [])
                if r.get("_resource_name")
            ]
            # grid_data'yı yedekle: load_data None'a sıfırlar, biz eski halini koruruz
            st.session_state["_saved_grid_data"] = st.session_state.grid_data
            st.session_state["_post_save_reload"] = True
            st.session_state.pending_edits = {}
            load_data(show_spinner=False)
            st.rerun()
        elif not errors:
            st.toast("Kaydedilecek değişiklik yok.", icon="ℹ️")


if __name__ == "__main__":
    main()
