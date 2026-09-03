import io
import unittest
from pathlib import Path

from server import app


class IncidentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        app.extensions["incident_analysis"] = {}
        self.client = app.test_client()

    def upload(self, contents: bytes, filename: str = "incidents.csv"):
        return self.client.post(
            "/api/incidents/analyze",
            data={"file": (io.BytesIO(contents), filename)},
        )

    def test_analyze_fixture_returns_known_metrics_and_invalid_details(self) -> None:
        project_root = Path(__file__).resolve().parents[3]
        with (project_root / "incidents-trackflow.csv").open("rb") as fixture:
            response = self.client.post(
                "/api/incidents/analyze",
                data={"file": (fixture, "incidents.csv")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["export_url"], "/api/incidents/results/exports")
        self.assertEqual(payload["analysis"]["records_processed"], 100)
        self.assertEqual(payload["analysis"]["records_valid"], 97)
        self.assertEqual(payload["analysis"]["records_invalid"], 3)
        self.assertEqual(payload["analysis"]["closed_satisfaction_average"], 3.06)
        self.assertEqual(payload["analysis"]["invalid_records"][0]["row_number"], 4)
        self.assertEqual(
            payload["analysis"]["invalid_records"][0]["reasons"],
            ["tracking_number must be 10-20 uppercase letters or digits"],
        )

    def test_export_returns_last_successful_analysis_as_csv_attachment(self) -> None:
        valid_csv = (
            b"incident_id,date,country,customer_type,tracking_number,carrier,category,description,status,customer_email,satisfaction_score\n"
            b"TRF-000001,2024-01-01,ES,B2C,ABC1234567,MRW,DAMAGE,Damaged item,CLOSED,user@example.com,4\n"
        )
        self.assertEqual(self.upload(valid_csv).status_code, 200)

        response = self.client.get("/api/incidents/results/exports")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn("attachment; filename=incident-analysis-results.csv", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"metric,dimension,value\r\n"))
        self.assertIn(b"records_processed,all,1", response.data)

    def test_export_before_analysis_returns_not_found(self) -> None:
        response = self.client.get("/api/incidents/results/exports")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {"error": "No incident analysis is available for export."})

    def test_rejects_invalid_upload_requests(self) -> None:
        invalid_csv = b"incident_id\nTRF-000001\n"
        missing_file = self.client.post(
            "/api/incidents/analyze",
            data={},
            content_type="multipart/form-data",
        )
        self.assertEqual(missing_file.status_code, 400)
        self.assertEqual(
            missing_file.get_json(),
            {"error": "A CSV file is required in the 'file' field."},
        )

        cases = (
            ({"file": (io.BytesIO(b""), "incidents.csv")}, "Uploaded CSV file is empty."),
            ({"file": (io.BytesIO(invalid_csv), "incidents.txt")}, "Uploaded file must use the .csv extension."),
            ({"file": (io.BytesIO(invalid_csv), "incidents.csv")}, "Invalid CSV file: CSV file is missing required columns: carrier, category, country, customer_email, customer_type, date, description, satisfaction_score, status, tracking_number"),
        )

        for data, message in cases:
            with self.subTest(message=message):
                response = self.client.post("/api/incidents/analyze", data=data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"error": message})

    def test_rejects_non_multipart_and_malformed_csv(self) -> None:
        non_multipart = self.client.post("/api/incidents/analyze", data=b"not multipart")
        malformed = self.upload(
            b"incident_id,date,country,customer_type,tracking_number,carrier,category,description,status,customer_email,satisfaction_score\n"
            b"TRF-000001\n"
        )

        self.assertEqual(non_multipart.status_code, 400)
        self.assertEqual(non_multipart.get_json(), {"error": "Request must use multipart/form-data."})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.get_json(), {"error": "Invalid CSV file: a row is missing one or more fields."})

    def test_rejects_uploads_larger_than_ten_megabytes(self) -> None:
        response = self.upload(b"x" * (10 * 1024 * 1024 + 1))

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.get_json(),
            {"error": "File is too large. Maximum upload size is 10 MB."},
        )

    def test_failed_upload_does_not_replace_previous_export(self) -> None:
        valid_csv = (
            b"incident_id,date,country,customer_type,tracking_number,carrier,category,description,status,customer_email,satisfaction_score\n"
            b"TRF-000001,2024-01-01,ES,B2C,ABC1234567,MRW,DAMAGE,Damaged item,CLOSED,user@example.com,4\n"
        )
        self.assertEqual(self.upload(valid_csv).status_code, 200)
        self.assertEqual(self.upload(b"", "invalid.csv").status_code, 400)

        export = self.client.get("/api/incidents/results/exports")

        self.assertEqual(export.status_code, 200)
        self.assertIn(b"records_processed,all,1", export.data)


if __name__ == "__main__":
    unittest.main()