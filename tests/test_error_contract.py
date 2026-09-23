"""Which routes answer a bad payload with 400, and which still answer 422.

The incident manager owes its callers a 400 naming one field. Every other
domain was built against FastAPI's 422 and its `detail` array, and the two UIs
branch on the difference: `resetPassword` in `uis/*/services/auth-service.ts`
reads any 400 as a spent reset token and replaces the form with "request a new
link". Remapping validation errors app-wide would therefore tell someone whose
new password was simply too short that their reset link had expired.

These tests pin both halves of that boundary.
"""


def test_incident_route_answers_validation_with_400_and_one_field(client):
    response = client.post("/api/incidents", json={})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == {"field": "title", "message": "This field is required."}


def test_users_route_still_answers_validation_with_422(client):
    """`POST /users` predates the incident manager and keeps FastAPI's default."""
    response = client.post("/users", json={"email": "not-an-email", "password": "x"})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list), "the pydantic array shape, unchanged"


def test_reset_password_keeps_422_for_a_short_password(client):
    """The case the app-wide remap broke.

    A 400 here means "this token is dead, ask for a new link". A password that
    is merely too short must not produce one, or the user is sent to restart a
    recovery that was working.
    """
    response = client.post(
        "/auth/reset-password",
        json={"token": "irrelevant-the-password-fails-first", "new_password": "short"},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_a_bad_query_parameter_on_an_incident_route_is_a_400(client):
    response = client.get("/api/incidents?status=nonsense")

    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "status"


def test_the_incident_prefix_is_read_from_the_router(client):
    """The scope follows the router, so moving the prefix moves the behaviour."""
    from services.incidents.router import router

    response = client.post(f"{router.prefix}", json={})
    assert response.status_code == 400
