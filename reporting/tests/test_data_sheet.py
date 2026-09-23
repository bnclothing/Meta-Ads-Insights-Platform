from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from reporting.connectors.data_sheet import (
    DataSheetError,
    Lead,
    LeadDataset,
    load_lead_dataset,
    parse_click_rows,
    parse_dossier_rows,
    parse_landing_rows,
)


class DataSheetParserTests(TestCase):
    def test_landing_rows_inherit_markers_from_23_june_2025(self):
        before_marker = [1, "L-OLD", "Import", "Ancien", "0600", "Casa", "Produit", None, None, 2]
        before_marker.extend([None, "Observation", date(2025, 6, 22), "En cours"])
        first = [1, "L100", "Import", "Amina", "0611", "Casa", "Machine", None, None, 4]
        first.extend([None, "À rappeler", None, "En cours", None, "Traité"])
        second = [1, "L101", "Consultation", "Youssef", "0622", "Rabat", "Textile", None, None, 10]
        second.extend([None, "Information utile", None, "Nouveau lead"])

        leads = parse_landing_rows(
            [
                [" ", "Code client"],
                before_marker,
                ["LE 23/06/2025"],
                first,
                ["LE 24/06/2025"],
                second,
            ]
        )

        self.assertEqual([lead.code for lead in leads], ["L100", "L101"])
        self.assertEqual([lead.entered_on for lead in leads], [date(2025, 6, 23), date(2025, 6, 24)])
        self.assertEqual(leads[0].status, "Traité")
        self.assertEqual(leads[0].details, "Casa · À rappeler")

    def test_click_rows_inherit_each_date_divider(self):
        first = [1, "R0001", 5339, "Meta", "212 600", "Tracteur", 1, None, None, "Proforma"]
        second = [1, "R0002", "-", "Meta", "212 601", "Textile", 20, None, None, "À contacter"]

        leads = parse_click_rows(
            [
                [1, "Code client"],
                [date(2024, 5, 4)],
                first,
                ["La Date : 05/05/2024"],
                second,
            ]
        )

        self.assertEqual([lead.entered_on for lead in leads], [date(2024, 5, 4), date(2024, 5, 5)])
        self.assertEqual(leads[0].details, "Formulaire 5339 · Proforma")
        self.assertEqual(leads[1].details, "À contacter")

    def test_dossier_uses_status_one_as_entry_date_and_latest_timed_status(self):
        row = [None] * 34
        row[1:10] = ["A1-1", "Salma", "06 00 00 00 00", "Maroc", "B", "Machine", 3, "20 USD", "Chine"]
        row[14:18] = ["2026-08-04 00:38 UTC", "En attente", "2026-08-06 10:15 UTC", "Complété"]

        leads = parse_dossier_rows([[None, "Code Client"], row])

        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].entered_on, date(2026, 8, 4))
        self.assertEqual(leads[0].status, "Complété")
        self.assertEqual(leads[0].details, "Maroc · Chine")

    @override_settings(DATA_SHEET_LOCAL_XLSX="virtual-data.xlsx", GOOGLE_SHEETS_CACHE_SECONDS=0)
    @patch("reporting.connectors.data_sheet._load_local_rows")
    def test_consolidated_dataset_combines_the_three_normalized_sources(self, mocked_rows):
        dossier_row = [None] * 34
        dossier_row[1:8] = ["A1", "Salma", "0603", "Maroc", "B", "Machine", 3]
        dossier_row[14:16] = ["2026-08-04 00:38 UTC", "En attente"]
        rows_by_sheet = {
            "landing page": [["LE 23/06/2025"], [1, "L1", "Import", "Amina", "0601", "Casa", "Textile"]],
            "Clics": [[date(2024, 5, 4)], [1, "R1", 10, "Meta", "0602", "Produit"]],
            "Dossiers IA": [[None, "Code Client"], dossier_row],
        }
        mocked_rows.side_effect = lambda _path, sheet_name: rows_by_sheet[sheet_name]
        cache.clear()

        dataset = load_lead_dataset("all", refresh=True)

        self.assertEqual([lead.source for lead in dataset.leads], ["landing", "clicks", "dossiers"])
        self.assertEqual([lead.source_label for lead in dataset.leads], ["Landing page", "Clicks", "Dossier IA"])
        self.assertEqual(mocked_rows.call_count, 3)


class DataSheetViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("data-operator", password="A-strong-local-password-2026")

    def test_data_sheet_requires_login(self):
        response = self.client.get(reverse("data_sheet"))
        self.assertEqual(response.status_code, 302)

    @patch("reporting.views.load_lead_dataset")
    def test_data_sheet_filters_range_and_renders_navigation(self, mocked_load):
        mocked_load.return_value = LeadDataset(
            leads=(
                Lead("dossiers", 2, date(2026, 8, 3), "A1", "Amina", "0601", "B", "Machine", "2", "En cours", "Maroc"),
                Lead("dossiers", 3, date(2026, 8, 4), "A2", "Omar", "0602", "A1", "Textile", "5", "Complété", "Chine"),
                Lead("dossiers", 4, date(2026, 8, 8), "A3", "Salma", "0603", "B", "Plastique", "8", "En attente", "Maroc"),
            ),
            fetched_at=timezone.now(),
            mode="google",
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("data_sheet"),
            {"source": "dossiers", "start": "2026-08-03", "end": "2026-08-04"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["lead_count"], 2)
        self.assertEqual(response.context["kpis"][1]["value"], "2")
        self.assertContains(response, "DATA Sheet")
        self.assertContains(response, "Google Sheets en direct")
        self.assertContains(response, "Amina")
        self.assertContains(response, "Omar")
        self.assertNotContains(response, "Salma")
        mocked_load.assert_called_once_with("dossiers", refresh=False)

    @patch("reporting.views.load_lead_dataset")
    def test_consolidated_view_shows_all_sources_and_their_breakdown(self, mocked_load):
        mocked_load.return_value = LeadDataset(
            leads=(
                Lead("landing", 10, date(2026, 8, 4), "L1", "Amina", "0601", "Import", "Machine", "2", "En cours", "Casa"),
                Lead("clicks", 20, date(2026, 8, 4), "R1", "", "0602", "Meta", "Textile", "5", "", "Formulaire 3"),
                Lead("dossiers", 30, date(2026, 8, 4), "A1", "Omar", "0603", "B", "Plastique", "8", "En attente", "Maroc"),
            ),
            fetched_at=timezone.now(),
            mode="google",
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("data_sheet"),
            {"source": "all", "start": "2026-08-04", "end": "2026-08-04"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["lead_count"], 3)
        self.assertTrue(response.context["is_consolidated"])
        self.assertEqual([item["count"] for item in response.context["source_breakdown"]], [1, 1, 1])
        self.assertContains(response, "Vue consolidée")
        self.assertContains(response, "3 feuilles")
        self.assertContains(response, "<th>Feuille</th>", html=True)
        self.assertNotContains(response, "nav-child")
        mocked_load.assert_called_once_with("all", refresh=False)

    @patch("reporting.views.load_lead_dataset", side_effect=DataSheetError("Configuration Google requise."))
    def test_connection_error_is_presented_without_crashing(self, mocked_load):
        self.client.force_login(self.user)

        response = self.client.get(reverse("data_sheet"), {"source": "landing"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuration Google requise.")
        self.assertContains(response, "lecture seule")
