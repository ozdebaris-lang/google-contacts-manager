"""
Google Contacts Manager — Vibe Edition
Streamlit + st-aggrid frontend for the Google People API.
"""

import json
import os
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
        "visible_cols": ["Ad", "Soyad", "Cep Telefonu", "E-posta", "Şirket", "Etiketler"],
        "detail_shown_for": None,  # en son dialog açılan rn — aynı satır için tekrar açılmaz
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v



# ─── Auth ─────────────────────────────────────────────────────────────────────

def handle_auth_page():
    st.title("📇 Google Contacts Manager")
    st.markdown("---")

    if not os.path.exists("credentials.json"):
        st.error(
            "**credentials.json bulunamadı.**\n\n"
            "GCP Console'dan bir OAuth Desktop App credential oluşturup "
            "bu klasöre koy. Detaylar için README.md dosyasına bak."
        )
        return

    st.info("Google hesabınla giriş yap. Tarayıcında bir onay ekranı açılacak.")

    if st.button("🔑 Google ile Giriş Yap", type="primary", use_container_width=False):
        with st.spinner("OAuth akışı başlatılıyor…"):
            creds = auth.get_credentials()
        if creds:
            st.session_state.service = contacts_api.build_service(creds)
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Giriş başarısız. credentials.json dosyasını kontrol et.")


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


# ─── Türkçe karakter düzeltme dialog ─────────────────────────────────────────

_TR_CHARS = set("şğçöüıŞĞÇÖÜİ")


@st.dialog("🇹🇷 Türkçe Karakter Düzeltme", width="large")
def turkish_fix_dialog(resource_names: list):
    """Türkçe karakter içermeyen isimleri listeler; kullanıcı düzeltir."""
    original_df = st.session_state.df_original
    pending     = st.session_state.pending_edits

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
            to_fix.append((rn, cur_ad, cur_soyad,
                           _s(row.get("Ad", "")), _s(row.get("Soyad", ""))))

    if not to_fix:
        st.success("Seçili kişilerin tümünde zaten Türkçe karakter var, düzeltme gerekmiyor.")
        return

    st.caption(
        f"Türkçe karakter **içermeyen** {len(to_fix)} kişi listelendi. "
        "Düzeltmek istediğiniz isimleri yazın, değiştirmek istemediklerinizi olduğu gibi bırakın."
    )
    st.divider()

    edits = {}
    for rn, cur_ad, cur_soyad, orig_ad, orig_soyad in to_fix:
        c1, c2 = st.columns(2)
        new_ad    = c1.text_input("Ad",    value=cur_ad,    key=f"trfix_ad_{rn}")
        new_soyad = c2.text_input("Soyad", value=cur_soyad, key=f"trfix_sy_{rn}")
        edits[rn] = (new_ad, new_soyad, orig_ad, orig_soyad)

    st.divider()
    if st.button("✅ Uygula", type="primary", use_container_width=True):
        count = 0
        for rn, (new_ad, new_soyad, orig_ad, orig_soyad) in edits.items():
            if new_ad != orig_ad:
                _mark_bulk_edit(rn, "Ad", new_ad)
                count += 1
            if new_soyad != orig_soyad:
                _mark_bulk_edit(rn, "Soyad", new_soyad)
                count += 1
        st.success(f"✅ {count} alan güncellendi. Kaydetmek için 💾 Değişiklikleri Kaydet butonuna basın.")
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
    # _resource_name + dahili kolonlar — gizle
    gb.configure_column("_resource_name", hide=True)
    for hidden in ("_etag", "_phones_raw", "_emails_raw", "_addresses_raw", "_primary_phone_col"):
        gb.configure_column(hidden, hide=True)

    # Oluşturulma: salt okunur
    gb.configure_column("Oluşturulma", editable=False)

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
        use_checkbox=True,
        header_checkbox=True,
        pre_selected_rows=st.session_state.get("selected_rows", []),
    )
    gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=50)
    gb.configure_grid_options(
        domLayout="normal",
        suppressRowClickSelection=False,
        rowHeight=30,
        headerHeight=40,
        rowStyle={"cursor": "pointer"},
    )
    return gb.build()


def render_grid(df_display: pd.DataFrame, reload: bool = True):
    """
    reload=True  → AgGrid veriyi sıfırdan yükler (filter değişimi, kayıt sonrası yenileme)
    reload=False → Grid mevcut state'ini korur; kullanıcı editlerini kaybetmez
    """
    response = AgGrid(
        df_display,
        height=600,
        key="main_grid",
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
    """Grid üstünde ince yatay çubuk — seçim yoksa hiçbir şey göstermez."""
    n = len(selected_rows)

    # Silme onayı aktifken, seçim kaybı olsa bile saklanan resource_names'i kullan
    if n == 0:
        if not (st.session_state.show_delete_confirm and st.session_state.get("delete_resource_names")):
            st.session_state.show_delete_confirm = False
            return

    service = st.session_state.service
    resource_names = [r["_resource_name"] for r in selected_rows if r.get("_resource_name")]

    # ── 1. Durum: Silme Onayı (Grid'in kaybolmaması için return kaldırıldı) ──
    if st.session_state.show_delete_confirm:
        # Seçim kaybı durumuna karşı kayıtlı listeyi kullan
        confirm_rns = st.session_state.get("delete_resource_names") or resource_names
        with st.container(border=True):
            st.warning(f"⚠️ **{len(confirm_rns)}** kişi kalıcı olarak silinecek. Bu işlem geri alınamaz!")
            dc1, dc2, dc3 = st.columns([4, 1, 1])
            if dc2.button("🗑️ Evet, Sil", key="confirm_delete", type="primary", use_container_width=True):
                try:
                    contacts_api.backup_csv(st.session_state.df)
                    with st.spinner("Siliniyor..."):
                        contacts_api.delete_contacts(service, confirm_rns)
                    st.session_state.df = st.session_state.df[~st.session_state.df["_resource_name"].isin(confirm_rns)]
                    st.session_state.selected_rows = []
                    st.session_state.show_delete_confirm = False
                    st.session_state.delete_resource_names = []
                    st.session_state.grid_data = None
                    st.session_state.data_version += 1
                    st.toast(f"✅ {len(confirm_rns)} kişi silindi.", icon="🗑️")
                    st.rerun()
                except Exception as e:
                    st.error(f"Hata oluştu: {e}")
            if dc3.button("Vazgeç", key="cancel_delete", use_container_width=True):
                st.session_state.show_delete_confirm = False
                st.session_state.delete_resource_names = []
                st.rerun()

    # ── 2. Durum: Normal Aksiyon Barı (Onay varken gizle ama grid'i öldürme) ──
    elif n > 0:
        st.markdown('<div class="action-bar-container">', unsafe_allow_html=True)
        
        # Sabit kolon yapısı: Seçim sayısından bağımsız olarak aynı slotlar kullanılır
        # [Bilgi/İsim, Detay, Aa Title, AA BÜYÜK, TR Düzelt, Etiket Seç, Uygula, Sil]
        cols = st.columns([2, 0.8, 0.8, 0.8, 0.9, 1.5, 0.8, 0.8])
        
        if n == 1:
            row = selected_rows[0]
            full_name = f"{row.get('Ad','')} {row.get('Soyad','')}".strip() or "İsimsiz"
            cols[0].markdown(f"**👤 {full_name}**")
            if cols[1].button("🔍 Detay", key="act_det", use_container_width=True):
                contact_detail_dialog(row["_resource_name"])
        else:
            cols[0].markdown(f"**⚡ {n} Seçili**")
        
        # Ortak Aksiyonlar (Her zaman aynı kolonlarda)
        if cols[2].button("Aa Title", key="bulk_title_btn", use_container_width=True):
            cnt = _apply_bulk_case(resource_names, "title")
            st.toast(f"✅ {cnt} kişi güncellendi.")

        if cols[3].button("AA BÜYÜK", key="bulk_upper_btn", use_container_width=True):
            cnt = _apply_bulk_case(resource_names, "upper")
            st.toast(f"✅ {cnt} kişi güncellendi.")

        if cols[4].button("🇹🇷 TR Düzelt", key="bulk_tr_btn", use_container_width=True):
            turkish_fix_dialog(resource_names)

        group_names = sorted(st.session_state.groups_map_inv.keys())
        sel_group = cols[5].selectbox("Etiket Seç", ["— Etiket Ata —"] + group_names, label_visibility="collapsed", key="bulk_label_sel")

        if cols[6].button("🏷️ Uygula", key="bulk_assign_btn", use_container_width=True):
            if sel_group != "— Etiket Ata —":
                grn = st.session_state.groups_map_inv.get(sel_group)
                contacts_api.assign_labels_to_contacts(service, resource_names, grn)
                st.success(f"Etiketlendi: {sel_group}")
                load_data(show_spinner=False)
                st.rerun()

        if cols[7].button("🗑️ Sil", key="bulk_delete_btn", type="secondary", use_container_width=True):
            st.session_state.show_delete_confirm = True
            st.session_state.delete_resource_names = resource_names
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_state()

    # Cloud deploy: secrets'ta token varsa otomatik giriş yap
    if not st.session_state.authenticated and auth.has_cloud_token():
        with st.spinner("Bağlanıyor…"):
            creds = auth.get_credentials()
        if creds:
            st.session_state.service = contacts_api.build_service(creds)
            st.session_state.authenticated = True

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
/* ── Streamlit üst bar (Deploy / Settings menüsü) gizle ── */
header[data-testid="stHeader"] { display: none !important; }
.block-container { padding-top: 0.6rem !important; }

/* Aksiyon Barı Konteynırı */
.action-bar-container {
    background-color: #f8fafc;
    padding: 10px;
    border-radius: 12px;
    border: 1px solid #e2e8f0;
    margin-bottom: 15px;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

/* Sil Butonu Özel - Kırmızı Vurgu */
button[key="bulk_delete_btn"] {
    background-color: #fee2e2 !important;
    color: #dc2626 !important;
    border: 1px solid #fecaca !important;
}

/* ════════════════════════════════════════════════════
   MODERN UI — butonlar, inputlar, geçişler
   ════════════════════════════════════════════════════ */

/* ── Tüm butonlar: yuvarlak köşe + hover animasyonu ── */
.stButton > button {
    border-radius: 8px !important;
    font-size: 0.72rem !important;
    font-weight: 500 !important;
    padding: 0.2rem 0.5rem !important;
    transition: transform 0.12s ease, box-shadow 0.12s ease, opacity 0.12s ease !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08) !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.13) !important;
}
.stButton > button:active {
    transform: translateY(0) !important;
    opacity: 0.85 !important;
}

/* ── Input / selectbox ── */
.stTextInput > div > div > input,
.stSelectbox > div > div > div {
    border-radius: 8px !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
.stTextInput > div > div > input:focus {
    box-shadow: 0 0 0 2px rgba(99,102,241,0.25) !important;
}

/* ── Download butonu ── */
.stDownloadButton > button {
    border-radius: 8px !important;
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
section[data-testid="stSidebar"] { min-width:200px !important; max-width:220px !important; }
section[data-testid="stSidebar"] .block-container { padding:0.5rem 0.6rem !important; }

/* Tüm sidebar yazıları küçük punto */
section[data-testid="stSidebar"],
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] div,
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] .stMarkdown { font-size:0.72rem !important; }

section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3,
section[data-testid="stSidebar"] h4,
section[data-testid="stSidebar"] strong {
    font-size:0.72rem !important; margin:0.2rem 0 0.1rem !important; font-weight:700 !important;
}
section[data-testid="stSidebar"] hr { margin:0.3rem 0 !important; }
section[data-testid="stSidebar"] .stButton button {
    font-size:0.72rem !important; padding:0.15rem 0.4rem !important;
}
section[data-testid="stSidebar"] .stSelectbox > div,
section[data-testid="stSidebar"] .stTextInput > div { font-size:0.72rem !important; }

/* Multiselect: her etiket ayrı satır */
section[data-testid="stSidebar"] [data-testid="stMultiSelect"] { font-size:0.72rem !important; }
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
    badge_txt = (
        f' <span style="font-size:0.75rem;color:#b45309;font-weight:600;vertical-align:middle;">'
        f'⚡ {n_pending_hdr} bekliyor</span>'
        if n_pending_hdr else ""
    )
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

    hcol1, hcol2, hcol3 = st.columns([6, 1.4, 0.9])
    with hcol1:
        st.markdown(
            f'<span style="font-size:1.15rem;font-weight:700;line-height:2;">📇 Google Contacts Manager</span>'
            f'{badge_txt} &nbsp;<span style="font-size:0.8rem;font-weight:400;opacity:0.6;">{badge_count}</span>',
            unsafe_allow_html=True,
        )
    with hcol2:
        save_clicked = st.button("💾 Değişiklikleri Kaydet", type="primary", key="save_btn", use_container_width=True)
    with hcol3:
        if st.button("➕ Yeni Kişi", type="secondary", use_container_width=True):
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
    reload_grid = should_reload or force_grid_reload
    edited_df, grid_selection = render_grid(st.session_state.grid_data, reload=reload_grid)

    # Seçimi kaydet (ekstra rerun olmadan)
    st.session_state.selected_rows = grid_selection

    # Action bar'ı grid'den SONRA doldur — sort/scroll sıfırlanmaz
    with action_bar_slot:
        _render_action_bar(grid_selection)

    # VALUE_CHANGED / SELECTION_CHANGED rerun'larında editları pending_edits'e kaydet.
    if not should_reload and not force_grid_reload and edited_df is not None and not edited_df.empty:
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
            st.session_state.pending_edits = {}
            load_data(show_spinner=False)
            st.rerun()
        elif not errors:
            st.toast("Kaydedilecek değişiklik yok.", icon="ℹ️")


if __name__ == "__main__":
    main()
