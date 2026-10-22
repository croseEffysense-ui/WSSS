"""Fenêtre Streamlit : diagnostic de connexion + journal d'exécution."""
from __future__ import annotations

from typing import Mapping, Optional

import streamlit as st

from .diagnostics import ICONS, report_text, run_diagnostics
from .logs import LEVELS, LOG_FILE, clear_entries, entries_text, get_entries


@st.dialog("🩺 Diagnostic & journal", width="large")
def diagnostic_dialog(secrets: Mapping, env: Mapping[str, str], secrets_error: Optional[str]) -> None:
    tab_diag, tab_log = st.tabs(["Diagnostic pas à pas", "Journal d'exécution"])

    with tab_diag:
        st.caption(
            "Teste chaque maillon de la chaîne (secrets → clé → réseau → jeton → accès au Sheet → onglets) "
            "et désigne le premier blocage. Aucun secret n'est affiché."
        )
        write_test = st.checkbox(
            "Inclure le test d'écriture", help="Crée puis supprime un onglet temporaire « _diagnostic_tmp » pour vérifier le rôle Éditeur."
        )
        if st.button("▶️ Lancer le diagnostic", type="primary"):
            with st.spinner("Diagnostic en cours (jusqu'à ~20 s)…"):
                st.session_state["diag_steps"] = run_diagnostics(secrets, env, secrets_error, write_test)

        steps = st.session_state.get("diag_steps")
        if steps:
            errors = [s for s in steps if s.status == "error"]
            warns = [s for s in steps if s.status == "warn"]
            if errors:
                st.error(f"❌ Premier blocage : **{errors[0].name}**")
            elif warns:
                st.warning(f"Connexion possible, {len(warns)} avertissement(s).")
            else:
                st.success("✅ Tout est en ordre.")
            for s in steps:
                with st.container(border=True):
                    st.markdown(f"{ICONS[s.status]} **{s.name}** · {s.duration_ms} ms")
                    if s.detail:
                        st.text(s.detail)
                    if s.hint:
                        st.info("💡 " + s.hint)
                    if s.trace:
                        with st.expander("Détail technique"):
                            st.code(s.trace, language="text")
            st.download_button(
                "📥 Télécharger le rapport (secrets masqués)",
                report_text(steps).encode("utf-8"),
                file_name="diagnostic_google_sheets.txt",
                mime="text/plain",
            )

    with tab_log:
        c1, c2, c3 = st.columns([2, 1, 1])
        level = c1.selectbox("Niveau minimum", LEVELS, index=0)
        if c2.button("🧹 Vider", width="stretch"):
            clear_entries()
        c3.button("🔄 Actualiser", width="stretch")
        entries = get_entries(level)
        st.caption(f"{len(entries)} ligne(s) · fichier : `{LOG_FILE}` · secrets masqués automatiquement")
        st.code("\n".join(e.line() for e in entries[-400:]) or "(journal vide)", language="text", height=420)
        st.download_button("📥 Télécharger le journal", entries_text(level).encode("utf-8"), file_name="journal.txt", mime="text/plain")
