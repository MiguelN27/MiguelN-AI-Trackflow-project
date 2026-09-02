import csv
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_incidents import analyze_incidents, export_results


class AnalyzeIncidentsTests(unittest.TestCase):
    def test_supplied_fixture_matches_expected_metrics(self) -> None:
        project_root = Path(__file__).resolve().parents[2]

        result = analyze_incidents(project_root / "incidents-trackflow.csv")

        self.assertEqual(result.total_records, 100)
        self.assertEqual(result.valid_records, 97)
        self.assertEqual(len(result.invalid_records), 3)
        self.assertEqual(
            result.categories,
            {"DAMAGE": 7, "DELAYED_DELIVERY": 38, "LOST_PARCEL": 15, "RETURN_REQUEST": 17, "WRONG_ADDRESS": 20},
        )
        self.assertEqual(result.statuses, {"OPEN": 30, "CLOSED": 53, "DISCARDED": 14})
        self.assertEqual(result.closed_satisfaction_count, 52)
        self.assertAlmostEqual(result.closed_satisfaction_average, 3.06, places=2)

    def test_invalid_record_reports_all_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "incidents.csv"
            csv_path.write_text(
                "incident_id,date,country,customer_type,tracking_number,carrier,category,description,status,customer_email,satisfaction_score\n"
                "wrong,2024-99-99,Spain,B2C,short,,UNKNOWN,,DONE,bad,8\n",
                encoding="utf-8",
            )

            result = analyze_incidents(csv_path)

        self.assertEqual(result.total_records, 1)
        self.assertEqual(result.valid_records, 0)
        self.assertEqual(len(result.invalid_records), 1)
        self.assertEqual(len(result.invalid_records[0].reasons), 10)

    def test_export_contains_one_row_per_metric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "incidents.csv"
            output_path = directory / "results.csv"
            source_path.write_text(
                "incident_id,date,country,customer_type,tracking_number,carrier,category,description,status,customer_email,satisfaction_score\n"
                "TRF-000001,2024-01-01,ES,B2C,ABC1234567,MRW,DAMAGE,Damaged item,CLOSED,user@example.com,4\n",
                encoding="utf-8",
            )

            export_results(analyze_incidents(source_path), output_path)
            with output_path.open(newline="", encoding="utf-8") as exported_file:
                rows = list(csv.DictReader(exported_file))

        self.assertEqual(rows[0], {"metric": "records_processed", "dimension": "all", "value": "1"})
        self.assertEqual(len(rows), 12)


if __name__ == "__main__":
    unittest.main()